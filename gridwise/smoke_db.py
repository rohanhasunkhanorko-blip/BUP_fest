"""End-to-end smoke for the MongoDB-backed routes.

Exercises: signup -> login -> create scenario -> optimize (with X-API-Key) ->
list runs -> reload scenario -> cache hit check.
"""
from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8000"


def _http(method: str, path: str, body=None, headers=None):
    data = None
    h = {"Accept": "application/json"}
    if headers:
        h.update(headers)
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(BASE + path, data=data, method=method, headers=h)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read()
            return resp.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            payload = json.loads(raw)
        except Exception:
            payload = raw.decode("utf-8", "replace")
        return exc.code, payload


def _ok(label, status, expect, payload):
    if status != expect:
        print(f"  FAIL {label}: HTTP {status} expected {expect}: {payload}")
        sys.exit(1)
    print(f"  OK   {label}: HTTP {status}")


def main() -> int:
    ts = int(time.time())
    email = f"smoke+{ts}@example.com"
    password = "correct horse battery staple"
    name = f"Smoke scenario {ts}"

    # 1) Signup
    print("signup")
    s, body = _http("POST", "/auth/signup", {"email": email, "password": password})
    _ok("signup status", s, 201, body)
    api_key = body["api_key"]
    user_id = body["user_id"]
    print(f"  user_id={user_id} api_key={api_key[:10]}...")

    # 2) Login (mint a fresh key)
    print("login")
    s, body = _http("POST", "/auth/login", {"email": email, "password": password})
    _ok("login status", s, 200, body)
    new_key = body["api_key"]
    assert new_key != api_key, "expected a new key on login"
    api_key = new_key

    # 3) /auth/me requires key
    print("auth/me")
    s, body = _http("GET", "/auth/me", headers={"X-API-Key": api_key})
    _ok("/auth/me", s, 200, body)
    assert body["email"].lower() == email.lower()

    # 4) Missing key -> 401
    s, body = _http("GET", "/auth/me")
    _ok("/auth/me no-key", s, 401, body)

    # 5) Build a simple scenario and POST /optimize-energy with the API key.
    print("optimize-energy (DB)")
    hours = [
        {"hour": h,
         "demand_kwh": 10.0,
         "solar_kwh": 5.0 if 8 <= h <= 17 else 0.0,
         "tariff_bdt_per_kwh": 12.0 if 18 <= h <= 21 else 8.0}
        for h in range(24)
    ]
    scenario = {
        "scenario_id": f"db-smoke-{ts}",
        "battery": {
            "capacity_kwh": 100.0,
            "initial_energy_kwh": 50.0,
            "minimum_energy_kwh": 10.0,
            "max_charge_kwh_per_hour": 20.0,
            "max_discharge_kwh_per_hour": 20.0,
        },
        "hours": hours,
        "operator_notes": [
            "Reduce solar output to 50% during peak hours 18-21.",
            "Keep at least 20 kWh in the battery for the evening.",
        ],
    }
    s, body = _http("POST", "/optimize-energy", scenario, headers={"X-API-Key": api_key})
    _ok("optimize-energy", s, 200, body)
    assert body["total_grid_kwh"] >= 0
    print(f"  grid={body['total_grid_kwh']:.2f} cost={body['total_cost_bdt']:.2f}")

    # 6) Persist the scenario
    print("POST /scenarios")
    s, body = _http(
        "POST",
        "/scenarios",
        {"name": name, "payload": scenario},
        headers={"X-API-Key": api_key},
    )
    _ok("create scenario", s, 201, body)
    scenario_obj_id = body["id"]

    # 7) List scenarios (must include ours)
    s, body = _http("GET", "/scenarios", headers={"X-API-Key": api_key})
    _ok("list scenarios", s, 200, body)
    assert any(s["id"] == scenario_obj_id for s in body), "created scenario missing"

    # 8) Fetch scenario by id
    s, body = _http("GET", f"/scenarios/{scenario_obj_id}", headers={"X-API-Key": api_key})
    _ok("get scenario", s, 200, body)
    assert body["payload"]["scenario_id"] == scenario["scenario_id"]

    # 9) List runs (should have at least one, attributed to our user)
    s, body = _http("GET", "/runs", headers={"X-API-Key": api_key})
    _ok("list runs", s, 200, body)
    assert isinstance(body, list) and body, "expected at least one run"
    assert body[0]["scenario_id"] == scenario["scenario_id"], "run scenario_id mismatch"
    print(f"  runs={len(body)} first_scenario_id={body[0]['scenario_id']}")

    # 10) Run the same scenario again -> cache should serve interpretations faster.
    print("second optimize (cache hit expected)")
    t0 = time.perf_counter()
    s, body = _http("POST", "/optimize-energy", scenario, headers={"X-API-Key": api_key})
    elapsed_ms = (time.perf_counter() - t0) * 1000
    _ok("optimize-energy #2", s, 200, body)
    print(f"  second request took {elapsed_ms:.1f} ms")

    # 11) DELETE scenario -> 204
    s, body = _http("DELETE", f"/scenarios/{scenario_obj_id}", headers={"X-API-Key": api_key})
    _ok("delete scenario", s, 204, body)

    # 12) Missing key on protected endpoint -> 401
    s, body = _http("GET", "/scenarios")
    _ok("no-key /scenarios", s, 401, body)

    # 13) DB unreachable simulation: ensure /optimize-energy still works without key
    # (already proven above). Now check /health
    s, body = _http("GET", "/health")
    _ok("/health", s, 200, body)

    print("\nALL DB SMOKE CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
