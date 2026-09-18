"""In-process HTTP smoke test using FastAPI TestClient."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)
SAMPLES = Path("../files/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json")


def main() -> None:
    r = client.get("/health")
    print(f"GET  /health               -> {r.status_code} {r.json()}")
    assert r.status_code == 200, r.text

    data = json.loads(SAMPLES.read_text(encoding="utf-8"))
    print(f"POST /optimize-energy      -> {len(data['cases'])} sample(s)\n")

    print(f"{'sample':>8}  {'grid_kwh':>10}  {'cost_bdt':>10}  {'peak_kwh':>10}  "
          f"{'exp_grid':>10}  {'exp_cost':>10}  {'exp_peak':>10}  result")
    print("-" * 90)
    n_ok = 0
    for i, case in enumerate(data["cases"], 1):
        r = client.post("/optimize-energy", json=case["input"])
        assert r.status_code == 200, f"sample {i}: {r.status_code} {r.text}"
        out = r.json()
        exp = case["expected_output"]
        ok = (
            abs(out["total_grid_kwh"] - exp["total_grid_kwh"]) < 0.01
            and abs(out["total_cost_bdt"] - exp["total_cost_bdt"]) < 0.01
            and abs(out["peak_grid_kwh"] - exp["peak_grid_kwh"]) < 0.01
        )
        n_ok += int(ok)
        print(
            f"{i:>8}  {out['total_grid_kwh']:>10.2f}  {out['total_cost_bdt']:>10.2f}  "
            f"{out['peak_grid_kwh']:>10.2f}  "
            f"{exp['total_grid_kwh']:>10.2f}  {exp['total_cost_bdt']:>10.2f}  {exp['peak_grid_kwh']:>10.2f}  "
            f"{'OK' if ok else 'FAIL'}"
        )

    print("\nResponse shape (sample 1):")
    s1 = client.post("/optimize-energy", json=data["cases"][0]["input"]).json()
    print(f"  scenario_id              = {s1['scenario_id']}")
    print(f"  hourly_plan length       = {len(s1['hourly_plan'])}")
    print(f"  hours                    = {[p['hour'] for p in s1['hourly_plan']]}")
    print(f"  directive_interp count   = {len(s1['directive_interpretation'])}")
    print(f"  applied_notes count      = {len(s1.get('applied_notes', []))}")
    print(f"  plan_summary preview     = {s1['plan_summary'][:140]}...")
    print(f"\nFirst 3 hourly_plan entries:")
    for p in s1["hourly_plan"][:3]:
        print(f"  {p}")

    print(f"\n{n_ok}/{len(data['cases'])} samples match published totals exactly.")


if __name__ == "__main__":
    main()
