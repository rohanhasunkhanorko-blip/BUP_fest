"""FastAPI entrypoint for the GridWise preliminary service.

Endpoints (public):
  GET  /health             -> {"status": "ok"}
  POST /optimize-energy    -> run the full LLM + guardrails + LP pipeline

Auth:
  POST /auth/signup        -> create account + return API key
  POST /auth/login         -> return fresh API key for existing account
  POST /auth/keys          -> mint a new API key (requires X-API-Key)
  GET  /auth/me            -> who am I? (requires X-API-Key)

Persistence (requires X-API-Key):
  GET    /scenarios        -> list caller's scenarios
  POST   /scenarios        -> create a named scenario
  GET    /scenarios/{id}   -> read a scenario
  PUT    /scenarios/{id}   -> replace a scenario
  PATCH  /scenarios/{id}   -> rename or repayload a scenario
  DELETE /scenarios/{id}   -> remove a scenario
  GET    /runs             -> recent runs for the caller
  POST   /runs             -> manually persist a run
  GET    /runs/{id}        -> read a single run

Error policy:
  * 400 for client-side schema errors (Pydantic validation, missing fields).
  * 422 for infeasible optimization schedules (the optimizer could not satisfy
    all constraints simultaneously).
  * 500 for unexpected server failures with secrets scrubbed from the response.
  * 503 when the database is unreachable and the endpoint requires it.
  * Per-request timeout: 30 seconds (enforced via asyncio.wait_for).

Backward compatibility:
  /optimize-energy works without any auth when the DB is offline. With the DB
  online, anonymous requests still work but their runs are not attributed.
"""
from __future__ import annotations

import asyncio
import logging
import os
from contextlib import asynccontextmanager
from typing import Any, Dict, List

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from . import db
from .auth import get_optional_user
from .guardrails import validate_and_normalize
from .llm import interpret_notes
from .optimizer import Infeasible, solve
from .routes_auth import router as auth_router
from .routes_runs import _record_run
from .routes_runs import router as runs_router
from .routes_scenarios import router as scenarios_router
from .schemas import (
    DirectiveInterpretation,
    HourlyPlan,
    ResponseOut,
    ScenarioIn,
)
from .summarizer import build_plan_summary

load_dotenv()  # loads gridwise/.env (or any .env on PYTHONPATH) at startup
logger = logging.getLogger("gridwise")

REQUEST_BUDGET_SECONDS = 30


def _scrub_secret(text: str) -> str:
    if not text:
        return ""
    for key in (
        "OPENAI_API_KEY",
        "GROQ_API_KEY",
        "OLLAMA_API_KEY",
        "sk-",
        "Bearer ",
    ):
        text = text.replace(key, "[redacted]")
    return text


@asynccontextmanager
async def lifespan(app: FastAPI):
    provider = os.getenv("LLM_PROVIDER", "mock")
    logger.info("GridWise starting with LLM provider=%s", provider)
    await db.connect()
    try:
        yield
    finally:
        await db.disconnect()
    logger.info("GridWise shutting down")


app = FastAPI(
    title="GridWise LLM API",
    version="1.0.0",
    description="BUP CSE Fest 2026 preliminary submission.",
    lifespan=lifespan,
)


@app.exception_handler(RequestValidationError)
async def _validation_exception(_request: Request, exc: RequestValidationError):
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


@app.exception_handler(ValidationError)
async def _pydantic_validation_exception(_request: Request, exc: ValidationError):
    return JSONResponse(status_code=400, content={"detail": exc.errors()})


@app.get("/health")
async def health() -> Dict[str, str]:
    return {"status": "ok"}


# ---- Static UI --------------------------------------------------------------
import pathlib  # noqa: E402  (local import keeps the top of the file clean)

_STATIC_DIR = pathlib.Path(__file__).parent / "static"


@app.get("/", include_in_schema=False)
async def root_ui() -> FileResponse:
    return FileResponse(_STATIC_DIR / "index.html")


app.mount("/ui", StaticFiles(directory=_STATIC_DIR, html=True), name="ui")

app.include_router(auth_router)
app.include_router(scenarios_router)
app.include_router(runs_router)


@app.post("/optimize-energy", response_model=ResponseOut)
async def optimize_energy(payload: ScenarioIn, user=Depends(get_optional_user)) -> ResponseOut:
    owner_id = user["user_id"] if user else None
    request_dict = payload.model_dump(mode="json")
    try:
        response = await asyncio.wait_for(_run_pipeline(payload), timeout=REQUEST_BUDGET_SECONDS)
    except asyncio.TimeoutError:
        await _record_run(
            owner_id=owner_id,
            scenario_id=payload.scenario_id,
            request_payload=request_dict,
            response_payload={"plan_summary": "Request exceeded 30s budget"},
            status_str="failed",
            error_message="Request exceeded 30s budget",
        )
        raise HTTPException(status_code=504, detail="Request exceeded 30s budget")
    except Infeasible as exc:
        await _record_run(
            owner_id=owner_id,
            scenario_id=payload.scenario_id,
            request_payload=request_dict,
            response_payload={"plan_summary": f"Infeasible schedule: {exc}"},
            status_str="failed",
            error_message=f"Infeasible: {exc}",
        )
        raise HTTPException(status_code=422, detail=f"Infeasible schedule: {exc}")
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001 - last-resort server error guard
        logger.exception("Unexpected failure in optimize-energy")
        await _record_run(
            owner_id=owner_id,
            scenario_id=payload.scenario_id,
            request_payload=request_dict,
            response_payload={"plan_summary": "Server error"},
            status_str="failed",
            error_message=_scrub_secret(repr(exc)),
        )
        raise HTTPException(status_code=500, detail=_scrub_secret(repr(exc)))

    await _record_run(
        owner_id=owner_id,
        scenario_id=payload.scenario_id,
        request_payload=request_dict,
        response_payload=response.model_dump(mode="json"),
        status_str="success",
    )
    return response


async def _run_pipeline(payload: ScenarioIn) -> ResponseOut:
    # 1. LLM interpretation (untrusted input from here on).
    raw = await interpret_notes(payload.operator_notes, payload.battery.capacity_kwh)
    validated = validate_and_normalize(raw, n_notes=len(payload.operator_notes), battery=payload.battery)

    interpretations: List[DirectiveInterpretation] = []
    for entry in validated:
        interpretations.append(DirectiveInterpretation.model_validate(entry))

    # 2. Translate validated directives into per-hour constraints.
    from .directives import apply_directives
    applied = apply_directives(
        [i.model_dump() for i in interpretations],
        hours=payload.hours,
        battery=payload.battery,
    )

    # 3. Solve the LP.
    result = solve(payload.hours, payload.battery, applied)

    # 4. Build the response model. The optimizer already returned the right shape.
    plan = [HourlyPlan.model_validate(row) for row in result.hourly_plan]
    summary = build_plan_summary(interpretations)

    return ResponseOut(
        scenario_id=payload.scenario_id,
        directive_interpretation=interpretations,
        hourly_plan=plan,
        total_grid_kwh=result.total_grid_kwh,
        total_cost_bdt=result.total_cost_bdt,
        peak_grid_kwh=result.peak_grid_kwh,
        plan_summary=summary,
    )
