"""LLM interpreter for operator notes.

The Problem Statement requires a language-capable generative model to be part
of the operator-note interpretation path. This module wraps three optional
backends (OpenAI, Groq, Ollama) behind one function and always returns a
list[dict] of raw interpretation entries. A `mock` provider is available for
local testing and offline judging so the service never depends on network
reachability for the demo. All structured output is then handed to
``app.guardrails`` for deterministic validation before it touches the
optimizer.
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any, List, Optional

import httpx

logger = logging.getLogger(__name__)


SYSTEM_PROMPT = """You convert short campus-operator notes into a structured JSON array.

Strict rules:
- Return ONLY a JSON object with a single key "interpretations" mapping to a JSON array.
- The array MUST contain exactly N entries, where N = the number of operator notes you receive, in the same order.
- Each entry MUST have these keys: note_index (int), applies (bool), directive_type (string), structured_adjustment (object or null), explanation (string).
- directive_type MUST be one of exactly these literal strings: solar_reduction, minimum_battery_reserve, no_charge_window, no_discharge_window, max_grid_window, no_op.
- For directive_type=no_op: applies MUST be false and structured_adjustment MUST be null.
- For every other directive_type: applies MUST be true.
- structured_adjustment shapes:
  * solar_reduction: {"hours": [int,...], "factor": number between 0 and 1 inclusive}
  * minimum_battery_reserve: {"hours": [int,...], "minimum_energy_kwh": number >= 0}
  * no_charge_window: {"hours": [int,...]}
  * no_discharge_window: {"hours": [int,...]}
  * max_grid_window: {"hours": [int,...], "max_grid_kwh": number >= 0}
- hours arrays: integers in 0..23, unique, ascending.
- Time windows in the notes are START-INCLUSIVE, END-EXCLUSIVE. "1 PM to 3 PM" maps to hours [13, 14], NOT [13, 14, 15].
- If a note expresses a percentage of capacity (e.g., "50% of the battery"), convert it to kWh using the battery capacity provided in context.
- If a note does not affect today's 24-hour energy schedule (menu change, deadline, sports office, weather chat, etc.), use directive_type=no_op.
- Do NOT invent unsupported directive types. Do NOT modify base demand, tariff, or battery limits.
- Do NOT output commentary outside the JSON.

Output JSON now, nothing else.
"""


FEW_SHOT_EXAMPLES = """
Example A — relevant solar reduction + distractor:
Input notes (2):
0: "Solar output will drop to about 20% from 1 PM to 3 PM."
1: "The cafeteria menu changes tomorrow."
Battery capacity_kwh = 200.
Output:
{"interpretations": [
  {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "Solar availability is reduced to 20% during the stated window."},
  {"note_index": 1, "applies": false, "directive_type": "no_op",
   "structured_adjustment": null,
   "explanation": "Menu change does not affect today's energy schedule."}
]}

Example B — no-charge window:
Input notes (1):
0: "Do not charge the battery between 2 PM and 4 PM."
Output:
{"interpretations": [
  {"note_index": 0, "applies": true, "directive_type": "no_charge_window",
   "structured_adjustment": {"hours": [14, 15]},
   "explanation": "Battery charging is unavailable for hours 14 and 15."}
]}

Example C — percentage reserve:
Input notes (1):
0: "Keep at least 50% of the battery capacity stored from 6 PM until 9 PM for emergency operations."
Battery capacity_kwh = 200.
Output:
{"interpretations": [
  {"note_index": 0, "applies": true, "directive_type": "minimum_battery_reserve",
   "structured_adjustment": {"hours": [18, 19, 20], "minimum_energy_kwh": 100},
   "explanation": "Half of 200 kWh = 100 kWh must remain during the window."}
]}

Example D — paraphrased solar reduction:
Input notes (1):
0: "PV production will drop to about 20% between 13:00 and 15:00."
Output:
{"interpretations": [
  {"note_index": 0, "applies": true, "directive_type": "solar_reduction",
   "structured_adjustment": {"hours": [13, 14], "factor": 0.2},
   "explanation": "Solar is reduced to 20% for the stated window."}
]}
"""


# ---------------------------------------------------------------------------
# Mock interpreter — keyword based, used for offline judging and tests.
# ---------------------------------------------------------------------------

# Each pattern is (regex, mode) where mode is a tuple describing how the two
# tokens map to 24-hour values. Modes:
#   ("h24",)          -> both groups are 24-hour ints
#   ("ampm","am","pm")  -> group1 am, group2 pm
#   ("ampm","pm","pm")  -> both pm
#   ("ampm","am","am")  -> both am
#   ("token","noon","pm")  -> start is noon (12), end is X pm
#   ("token","am","noon")  -> start is X am, end is noon (12)
#   ("token","midnight","am")  -> start is midnight (0), end is X am
_HMS = r"(?:to|until|till|and|or|-|–)"

_TIME_PATTERNS = [
    (re.compile(rf"(\d{{1,2}})\s*(?:am|a\.m\.)\s*{_HMS}\s*(\d{{1,2}})\s*(?:pm|p\.m\.)", re.I), ("ampm", "am", "pm")),
    (re.compile(rf"(\d{{1,2}})\s*(?:pm|p\.m\.)\s*{_HMS}\s*(\d{{1,2}})\s*(?:pm|p\.m\.)", re.I), ("ampm", "pm", "pm")),
    (re.compile(rf"(\d{{1,2}})\s*(?:am|a\.m\.)\s*{_HMS}\s*(\d{{1,2}})\s*(?:am|a\.m\.)", re.I), ("ampm", "am", "am")),
    (re.compile(rf"\b(\d{{1,2}}):00\s*{_HMS}\s*(\d{{1,2}}):00\b"), ("h24",)),
    (re.compile(rf"\bnoon\b\s*{_HMS}\s*(\d{{1,2}})\s*(?:pm|p\.m\.)", re.I), ("token", "noon", "pm")),
    (re.compile(rf"(\d{{1,2}})\s*(?:am|a\.m\.)\s*{_HMS}\s*\bnoon\b", re.I), ("token", "am", "noon")),
    (re.compile(rf"\bmidnight\b\s*{_HMS}\s*(\d{{1,2}})\s*(?:am|a\.m\.)", re.I), ("token", "midnight", "am")),
]


def _resolve(token: str, marker: str) -> int:
    if marker == "noon":
        return 12
    if marker == "midnight":
        return 0
    n = int(token)
    if marker == "am":
        return 0 if n == 12 else n
    if marker == "pm":
        return 12 if n == 12 else n + 12
    return n


def _parse_window_hours(text: str) -> Optional[List[int]]:
    """Parse a whole-hour window from natural language. Returns an ascending
    list of unique hour ints in 0..23, or None when no window can be extracted.
    Supports 12-hour with am/pm, 24-hour with :00, and noon/midnight anchors.
    The window is start-inclusive, end-exclusive.
    """
    text_l = text.lower()
    for pat, mode in _TIME_PATTERNS:
        m = pat.search(text_l)
        if not m:
            continue
        kind = mode[0]
        if kind == "h24":
            start = int(m.group(1))
            end = int(m.group(2))
        elif kind == "ampm":
            start = _resolve(m.group(1), mode[1])
            end = _resolve(m.group(2), mode[2])
        elif kind == "token":
            # Single numeric group; the other marker is fixed.
            if mode[1] == "noon":
                start = 12
                end = _resolve(m.group(1), mode[2])
            elif mode[2] == "noon":
                start = _resolve(m.group(1), mode[1])
                end = 12
            else:
                start = 0
                end = _resolve(m.group(1), mode[2])
        else:
            continue
        if start == end:
            return None
        start %= 24
        end %= 24
        if start < end:
            hours = list(range(start, end))
        else:
            hours = list(range(start, 24)) + list(range(0, end))
        return [h for h in hours if 0 <= h <= 23]
    return None


_SOLAR_PHRASE_REMAINING = re.compile(r"(?:about|approx(?:imately)?|around|treat(?:ed)? as|to|usable|expected|forecast)\s*(?:the\s*)?(?:forecast|usable|expected|availability|output|production)?\s*(\d{1,3})\s*%", re.I)
_SOLAR_PHRASE_REDUCTION = re.compile(r"(\d{1,3})\s*%\s*(?:reduc|drop|loss|cut|shutdown)", re.I)
_SOLAR_PHRASE_AVAILABLE = re.compile(r"(\d{1,3})\s*%\s*(?:of\s*the\s*)?(?:forecast|usable|expected|availability|output|production)", re.I)


def _extract_solar_factor(nl: str) -> Optional[float]:
    """Return the usable solar fraction (0..1) implied by the note.

    Handles three phrasings:
      * "80% reduction" / "80% drop" / "80% loss"   -> factor = 1 - pct/100
      * "treated as 25% of the forecast" / "usable solar to 20%" -> factor = pct/100
      * "leave about half / a third / a quarter" -> 0.5 / 0.33 / 0.25
      * "one-fifth", "one-tenth", "half", "third", "quarter"
    """
    m = _SOLAR_PHRASE_REMAINING.search(nl) or _SOLAR_PHRASE_AVAILABLE.search(nl)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        return max(0.0, min(1.0, pct / 100.0))
    m = _SOLAR_PHRASE_REDUCTION.search(nl)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        return max(0.0, min(1.0, 1.0 - pct / 100.0))
    if "one-fifth" in nl or "fifth" in nl:
        return 0.2
    if "one-tenth" in nl:
        return 0.1
    if "one-quarter" in nl or "quarter" in nl:
        return 0.25
    if "one-third" in nl or "third" in nl:
        return 0.3333
    if "half" in nl:
        return 0.5
    # "drop to 20%" / "fall to 30%" — pick up bare "to X%" or "of X%"
    m = re.search(r"\bto\s+(\d{1,3})\s*%", nl)
    if m:
        pct = max(0.0, min(100.0, float(m.group(1))))
        return max(0.0, min(1.0, pct / 100.0))
    return None


def _mock_interpret(notes: List[str], battery_capacity_kwh: Any) -> List[dict]:
    """Deterministic, dependency-free interpreter used when no API key is set
    or when ``LLM_PROVIDER=mock``. It is intentionally conservative: any note
    it cannot classify confidently is returned as ``no_op``.

    ``battery_capacity_kwh`` may be a float (the canonical form) or a Pydantic
    ``Battery`` model (defensive: the caller may pass the whole object).
    """
    # Defensive coercion: accept Battery model or float.
    if hasattr(battery_capacity_kwh, "capacity_kwh"):
        capacity = float(battery_capacity_kwh.capacity_kwh)  # type: ignore[attr-defined]
    else:
        capacity = float(battery_capacity_kwh)
    out: List[dict] = []
    for idx, note in enumerate(notes):
        n = note.strip()
        nl = n.lower()
        window = _parse_window_hours(n)
        # Solar reduction ----------------------------------------------------
        # Trigger when the note is about solar/PV AND mentions a reduction-like
        # verb OR gives a usable fraction/percentage.
        is_solar = any(k in nl for k in ("solar", "pv", "panel", "rooftop"))
        solar_verbs = ("reduc", "drop", "loss", "lower", "wash", "clean", "shutdown", "cut", "leave")
        if is_solar and any(k in nl for k in solar_verbs):
            factor = _extract_solar_factor(nl)
            if factor is not None and window:
                out.append(
                    {
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "solar_reduction",
                        "structured_adjustment": {"hours": window, "factor": factor},
                        "explanation": "Mock interpreter classified as solar_reduction.",
                    }
                )
                continue
        # No-discharge window ------------------------------------------------
        # Check discharge FIRST so that ambiguity like "must not discharge"
        # (which also contains the word "charge" via "charger") prefers the
        # discharge interpretation.
        if re.search(r"\bdischarge", nl) and any(k in nl for k in ("not ", "no ", "don't", "dont", "cannot", "can't", "protection", "test", "must not", "disabled")):
            if window:
                out.append(
                    {
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "no_discharge_window",
                        "structured_adjustment": {"hours": window},
                        "explanation": "Mock interpreter classified as no_discharge_window.",
                    }
                )
                continue
        # No-charge window ----------------------------------------------------
        if re.search(r"\bcharg(e|ing|er)\b", nl) and any(k in nl for k in ("not ", "no ", "don't", "dont", "cannot", "can't", "isolated", "unavailable", "maintenance", "offline", "disabled")):
            if window:
                out.append(
                    {
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "no_charge_window",
                        "structured_adjustment": {"hours": window},
                        "explanation": "Mock interpreter classified as no_charge_window.",
                    }
                )
                continue
        # Max grid window ----------------------------------------------------
        if ("grid" in nl or "feeder" in nl or "import" in nl or "intake" in nl) and any(
            k in nl for k in (
                "cap",
                "limit",
                "exceed",
                "must not",
                "no more than",
                "maximum",
                "max ",
                "at or below",
                "stay at or below",
                "must stay",
                "constrained",
            )
        ):
            cap = None
            m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", nl)
            if m:
                cap = float(m.group(1))
            if window is not None and cap is not None:
                out.append(
                    {
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "max_grid_window",
                        "structured_adjustment": {"hours": window, "max_grid_kwh": cap},
                        "explanation": "Mock interpreter classified as max_grid_window.",
                    }
                )
                continue
        # Minimum reserve ----------------------------------------------------
        if any(k in nl for k in ("reserve", "keep", "maintain", "at least", "minimum", "stored", "available", "emergency")) and (
            "battery" in nl or "kwh" in nl or "%" in nl or "capacity" in nl
        ):
            if window is None:
                window = []
            min_kwh: Optional[float] = None
            m = re.search(r"(\d+(?:\.\d+)?)\s*kwh", nl)
            if m:
                min_kwh = float(m.group(1))
            else:
                m = re.search(r"(\d{1,3})\s*%", nl)
                if m:
                    pct = max(0.0, min(100.0, float(m.group(1))))
                    min_kwh = round(capacity * pct / 100.0, 4)
                elif "half" in nl:
                    min_kwh = round(capacity * 0.5, 4)
            if min_kwh is not None:
                out.append(
                    {
                        "note_index": idx,
                        "applies": True,
                        "directive_type": "minimum_battery_reserve",
                        "structured_adjustment": {"hours": window, "minimum_energy_kwh": min_kwh},
                        "explanation": "Mock interpreter classified as minimum_battery_reserve.",
                    }
                )
                continue
        # Fallback ------------------------------------------------------------
        out.append(
            {
                "note_index": idx,
                "applies": False,
                "directive_type": "no_op",
                "structured_adjustment": None,
                "explanation": "Mock interpreter marked note as no_op.",
            }
        )
    return out


# ---------------------------------------------------------------------------
# Provider implementations
# ---------------------------------------------------------------------------


def _build_user_prompt(notes: List[str], battery_capacity_kwh: float) -> str:
    numbered = "\n".join(f"{i}: {n}" for i, n in enumerate(notes))
    return (
        f"Battery capacity_kwh = {battery_capacity_kwh}.\n"
        f"Operator notes ({len(notes)}):\n{numbered}\n\n"
        "Return ONLY the JSON object."
    )


async def _openai_interpret(notes: List[str], battery_capacity_kwh: float) -> List[dict]:
    api_key = os.environ.get("OPENAI_API_KEY", "")
    model = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES},
            {"role": "user", "content": _build_user_prompt(notes, battery_capacity_kwh)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post("https://api.openai.com/v1/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_provider_output(content, len(notes))


async def _groq_interpret(notes: List[str], battery_capacity_kwh: float) -> List[dict]:
    api_key = os.environ.get("GROQ_API_KEY", "")
    model = os.environ.get("GROQ_MODEL", "llama-3.1-70b-versatile")
    if not api_key:
        raise RuntimeError("GROQ_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES},
            {"role": "user", "content": _build_user_prompt(notes, battery_capacity_kwh)},
        ],
        "temperature": 0.0,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=20.0) as client:
        r = await client.post("https://api.groq.com/openai/v1/chat/completions", json=payload, headers=headers)
        r.raise_for_status()
        data = r.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_provider_output(content, len(notes))


async def _ollama_interpret(notes: List[str], battery_capacity_kwh: float) -> List[dict]:
    base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/")
    model = os.environ.get("OLLAMA_MODEL", "llama3.1:8b-instruct-q5_K_M")
    payload = {
        "model": model,
        "prompt": SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES + "\n\n" + _build_user_prompt(notes, battery_capacity_kwh),
        "stream": False,
        "format": "json",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(f"{base}/api/generate", json=payload)
        r.raise_for_status()
        data = r.json()
    content = data.get("response", "")
    return _parse_provider_output(content, len(notes))


async def _openrouter_interpret(notes: List[str], battery_capacity_kwh: float) -> List[dict]:
    """OpenRouter is OpenAI-API-compatible at https://openrouter.ai/api/v1.

    Note: json_object response_format is only enforced by some OpenRouter
    models, so we don't rely on it — the SYSTEM_PROMPT + few-shot examples
    are enough to make the model return a clean JSON object we can parse.
    """
    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    model = os.environ.get("OPENROUTER_MODEL", "meta-llama/llama-3.1-70b-instruct")
    if not api_key:
        raise RuntimeError("OPENROUTER_API_KEY is not set")
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\n" + FEW_SHOT_EXAMPLES},
            {"role": "user", "content": _build_user_prompt(notes, battery_capacity_kwh)},
        ],
        "temperature": 0.0,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter recommends identifying the app via these two headers.
        "HTTP-Referer": "https://gridwise.local",
        "X-Title": "GridWise",
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        r = await client.post(
            "https://openrouter.ai/api/v1/chat/completions",
            json=payload,
            headers=headers,
        )
        r.raise_for_status()
        data = r.json()
    content = data["choices"][0]["message"]["content"]
    return _parse_provider_output(content, len(notes))


# ---------------------------------------------------------------------------
# Parsing + retry
# ---------------------------------------------------------------------------


def _parse_provider_output(content: str, n_notes: int) -> List[dict]:
    """Parse a provider's JSON output into a flat list of interpretation
    dicts of length ``n_notes``. Raises ``ValueError`` when the output is
    unusable so the caller can fall back.
    """
    if not content:
        raise ValueError("empty LLM output")
    try:
        obj = json.loads(content)
    except json.JSONDecodeError as exc:
        raise ValueError(f"LLM output is not valid JSON: {exc}") from exc
    if isinstance(obj, list):
        items = obj
    elif isinstance(obj, dict):
        if "interpretations" in obj and isinstance(obj["interpretations"], list):
            items = obj["interpretations"]
        else:
            for v in obj.values():
                if isinstance(v, list):
                    items = v
                    break
            else:
                raise ValueError("LLM JSON did not contain an array")
    else:
        raise ValueError("LLM JSON was not an array or object")
    if len(items) != n_notes:
        raise ValueError(f"LLM returned {len(items)} entries, expected {n_notes}")
    return items


async def interpret_notes(notes: List[str], battery_capacity_kwh: Any) -> List[dict]:
    """Interpret operator notes via the configured LLM provider.

    Falls back to the deterministic mock interpreter on any provider error so
    that the service never crashes because of model unavailability. The mock
    output is still passed through deterministic guardrails.
    """
    # Cache layer: read-through. Cache failures must not affect correctness.
    from . import cache as _cache

    cached = await _cache.get(notes, battery_capacity_kwh)
    if cached is not None:
        return cached

    provider = (os.environ.get("LLM_PROVIDER") or "mock").lower()
    impls = {
        "openai": _openai_interpret,
        "groq": _groq_interpret,
        "local": _ollama_interpret,
        "ollama": _ollama_interpret,
        "openrouter": _openrouter_interpret,
    }
    impl = impls.get(provider)
    if impl is None:
        logger.info("Using mock LLM interpreter (provider=%s)", provider)
        result = _mock_interpret(notes, battery_capacity_kwh)
    else:
        last_err: Optional[Exception] = None
        result = None
        for attempt in range(2):
            try:
                result = await impl(notes, battery_capacity_kwh)
                break
            except Exception as exc:  # pragma: no cover - network error path
                last_err = exc
                logger.warning("LLM provider %s attempt %d failed: %s", provider, attempt + 1, exc)
        if result is None:
            logger.error("LLM provider %s failed twice; falling back to mock. Last error: %s", provider, last_err)
            result = _mock_interpret(notes, battery_capacity_kwh)

    await _cache.put(notes, battery_capacity_kwh, result)
    return result
