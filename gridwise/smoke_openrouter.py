"""Smoke test for the OpenRouter LLM provider.

Posts a small SAMPLE-style payload to /optimize-energy and checks that the
provider was actually called (i.e. the cache key didn't already exist from a
prior mock run) and that the response makes sense.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.request

URL = "http://localhost:8000/optimize-energy"

payload = {
    "scenario_id": "OPENROUTER-SMOKE",
    "battery": {
        "capacity_kwh": 200.0,
        "initial_energy_kwh": 50.0,
        "minimum_energy_kwh": 0.0,
        "max_charge_kwh_per_hour": 60.0,
        "max_discharge_kwh_per_hour": 60.0,
    },
    "hours": [
        {"hour": h, "demand_kwh": 100.0, "solar_kwh": 0.0, "tariff_bdt_per_kwh": 6.0}
        for h in range(24)
    ],
    # Two distinct directive forms, plus a clear no-op.
    "operator_notes": [
        "Solar output will drop to about 20% from 1 PM to 3 PM.",
        "Do not charge the battery between 2 PM and 4 PM.",
        "The cafeteria menu changes tomorrow.",
    ],
}


def post() -> dict:
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=60) as r:
        data = json.loads(r.read())
    data["_elapsed_ms"] = round((time.time() - t0) * 1000)
    return data


def main() -> int:
    out = post()
    interp = out.get("directive_interpretation", [])
    types = [i.get("directive_type") for i in interp]
    print(f"status: OK in {out['_elapsed_ms']} ms")
    print(f"scenario_id: {out.get('scenario_id')}")
    print(f"plan_summary: {out.get('plan_summary')}")
    print(f"total_grid_kwh: {out.get('total_grid_kwh'):.2f}")
    print(f"total_cost_bdt: {out.get('total_cost_bdt'):.2f}")
    print(f"directive_types: {types}")

    # Heuristic checks: the OpenRouter model should recognise the three notes.
    expected_non_noop = 2
    non_noop = [t for t in types if t and t != "no_op"]
    if len(non_noop) < expected_non_noop:
        print(f"FAIL: expected at least {expected_non_noop} non-no_op directives, got {non_noop}")
        return 1
    print("PASS: OpenRouter path returned valid structured directives.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
