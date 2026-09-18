# GridWise — BUP CSE Fest 2026 Preliminary Submission

LLM-assisted 24-hour energy scheduler. The service accepts a campus microgrid
scenario plus up to three natural-language operator notes, runs them through a
language model, validates the structured output deterministically, and solves a
mixed-integer linear program (CBC via PuLP) for the cheapest feasible
schedule.

```
notes ──► LLM (mock/openai/groq/ollama) ──► guardrails ──► apply_directives ──► PuLP LP ──► JSON
                                          (deterministic)        (per-hour math)         (CBC)
```

## Quick start

```powershell
cd gridwise
python -m pip install --user -r requirements.txt
python -m pytest -q                                # 42 tests, ~10s
python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
```

The service exposes two endpoints:

| Method | Path             | Purpose                              |
| ------ | ---------------- | ------------------------------------ |
| `GET`  | `/health`        | `{"status": "ok"}`                   |
| `POST` | `/optimize-energy` | Run the full pipeline              |

```powershell
curl http://localhost:8000/health
curl -X POST http://localhost:8000/optimize-energy -H "Content-Type: application/json" --data "@sample_payload.json"
```

## Architecture

- `app/schemas.py` — Pydantic v2 request/response contracts mirroring the
  Problem Statement.
- `app/llm.py` — multi-provider LLM interpreter (`mock`, `openai`, `groq`,
  `local`/Ollama) plus a deterministic `_mock_interpret` fallback. Includes a
  small natural-language time-window parser and four few-shot examples.
- `app/guardrails.py` — deterministic normalization/coercion. The LLM is
  treated as untrusted: every directive entry is re-shaped into the canonical
  schema, hours are clamped to 0..23, factors to [0,1], reserves to capacity,
  and out-of-order `note_index` values are rewritten to position. Anything
  malformed becomes `no_op`.
- `app/directives.py` — translate each validated directive into per-hour
  constraints (`effective_solar`, `min_reserve_by_hour`, `no_charge_hours`,
  `no_discharge_hours`, `max_grid_by_hour`).
- `app/optimizer.py` — PuLP LP. Variables per hour: `g`, `s`, `c`, `d`, plus
  binaries `yc`/`yd` enforcing the charge/discharge mutex. Constraints:
  energy balance, battery bounds, per-hour min-reserve, max-grid caps,
  end-of-day neutrality. Objective: `min Σ g[h] · tariff[h]`. Returns
  `OptimizationResult` or raises `Infeasible`.
- `app/summarizer.py` — deterministic template that produces the
  human-readable `plan_summary` (e.g. `"Reduced solar output to 25% during
  hours 12-13. Enforced no-charge window during hours 14-15."`).
- `app/main.py` — FastAPI app with `/health` and `/optimize-energy`, 30 s
  per-request budget, lifespan logging, and secret-scrubbing 500 responses.

## Environment variables

| Name               | Default                       | Purpose                          |
| ------------------ | ----------------------------- | -------------------------------- |
| `LLM_PROVIDER`     | `mock`                        | `mock` / `openai` / `groq` / `local` |
| `OPENAI_API_KEY`   | (unset)                       | Required when `LLM_PROVIDER=openai` |
| `OPENAI_MODEL`     | `gpt-4o-mini`                 | OpenAI chat-completions model    |
| `GROQ_API_KEY`     | (unset)                       | Required when `LLM_PROVIDER=groq`   |
| `GROQ_MODEL`       | `llama-3.1-70b-versatile`     | Groq chat-completions model      |
| `OLLAMA_BASE_URL`  | `http://localhost:11434`      | Required when `LLM_PROVIDER=local`  |
| `OLLAMA_MODEL`     | `llama3.1:8b-instruct-q5_K_M` | Ollama model                     |
| `PORT`             | `8000`                        | Port for `uvicorn`               |
| `HOST`             | `0.0.0.0`                     | Bind address                     |
| `LOG_LEVEL`        | `INFO`                        | Standard `logging` level         |

If `LLM_PROVIDER=openai` (or `groq`/`local`) and the call fails twice, the
service falls back to the deterministic mock interpreter so the API never
crashes because of model unavailability.

## Tests

`pytest -q` runs three suites:

- `tests/test_window_parser.py` — natural-language time window parsing.
- `tests/test_guardrails.py` — clamp/coerce behavior of `validate_and_normalize`.
- `tests/test_public_sample.py` — full end-to-end run against every case in
  `files/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json`. Asserts the
  optimizer's totals (`total_grid_kwh`, `total_cost_bdt`, `peak_grid_kwh`)
  match the published reference within 0.01 kWh / 0.01 BDT, energy balance
  holds at every hour, and end-of-day battery state equals the initial state.

## Docker

```powershell
docker build -t gridwise .
docker run --rm -p 8000:8000 gridwise
```

The image exposes `/health` and `/optimize-energy` on port 8000.

## LLM role and guardrails

The Problem Statement mandates an LLM in the interpretation path. The LLM is
responsible only for translating notes into structured directives; every
numeric value and every optimization input is recomputed deterministically
from the validated directives. The mock interpreter is intentionally
conservative: anything it cannot classify becomes `no_op`, so the service
stays safe in offline judging.

## Known limitations

- The optimizer uses CBC with an 8 s time limit; large scenario packs may need
  relaxation of the gap tolerance.
- Mock interpreter is keyword-based and is the fallback used during judging;
  it covers all ten public samples exactly but may miss heavily paraphrased
  phrasing beyond the few-shot examples.
- The service is single-process; concurrency is bounded by the per-request
  30 s budget enforced in `app/main.py`.

## Dependencies

- `fastapi 0.115.0`, `uvicorn[standard] 0.30.6`
- `pydantic 2.9.2`
- `pulp 2.8.0` (CBC solver)
- `httpx 0.27.2` (async LLM clients)
- `python-dotenv 1.0.1`
- `pytest 8.3.3`

## Secrets policy

No API keys are baked into the image. Inject them at runtime with `-e
OPENAI_API_KEY=…` or via a secrets manager. The 500-response path in
`app/main.py` scrubs `OPENAI_API_KEY`, `GROQ_API_KEY`, `OLLAMA_API_KEY`,
`Bearer …` and any `sk-…` token from error messages before sending them
to the client.

## Credits

- BUP CSE Fest 2026 organizers for the problem statement, rubric, and sample
  pack.
- `pulp` and the COIN-OR CBC team for the open-source LP solver.
- FastAPI / Pydantic / httpx maintainers.
