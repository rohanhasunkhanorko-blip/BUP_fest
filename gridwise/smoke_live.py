"""Live HTTP test against the running uvicorn at http://127.0.0.1:8000."""
import json
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:8000"


def main() -> None:
    # 1. /health
    with urllib.request.urlopen(BASE + "/health", timeout=5) as r:
        print(f"GET  /health            -> {r.status} {r.read().decode()}")

    # 2. / (UI)
    with urllib.request.urlopen(BASE + "/", timeout=5) as r:
        body = r.read().decode()
        has_gridwise = "GridWise" in body
        print(f"GET  /                  -> {r.status}  {len(body)} bytes, GridWise present: {has_gridwise}")

    # 3. /optimize-energy (sample 1)
    data = json.loads(Path("../files/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(encoding="utf-8"))
    req = urllib.request.Request(
        BASE + "/optimize-energy",
        data=json.dumps(data["cases"][0]["input"]).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        out = json.loads(r.read())
    print(f"POST /optimize-energy   -> {r.status}")
    print(f"  total_grid_kwh        = {out['total_grid_kwh']:.2f}")
    print(f"  total_cost_bdt        = {out['total_cost_bdt']:.2f}")
    print(f"  peak_grid_kwh         = {out['peak_grid_kwh']:.2f}")
    print(f"  plan_summary          = {out['plan_summary']}")
    print(f"  hourly_plan hours     = {[p['hour'] for p in out['hourly_plan']]}")

    # 4. All 10 samples
    print("\nAll 10 samples:")
    n_ok = 0
    for i, case in enumerate(data["cases"], 1):
        req = urllib.request.Request(
            BASE + "/optimize-energy",
            data=json.dumps(case["input"]).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            o = json.loads(r.read())
        exp = case["expected_output"]
        ok = (
            abs(o["total_grid_kwh"] - exp["total_grid_kwh"]) < 0.01
            and abs(o["total_cost_bdt"] - exp["total_cost_bdt"]) < 0.01
            and abs(o["peak_grid_kwh"] - exp["peak_grid_kwh"]) < 0.01
        )
        n_ok += int(ok)
        print(f"  sample {i:>2}: {'OK ' if ok else 'FAIL'}  grid={o['total_grid_kwh']:>8.2f}  cost={o['total_cost_bdt']:>9.2f}  peak={o['peak_grid_kwh']:>6.2f}")
    print(f"\n{n_ok}/10 samples match published totals.")


if __name__ == "__main__":
    main()
