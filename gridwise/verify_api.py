"""FastAPI TestClient smoke test: /health and /optimize-energy via the in-process ASGI app."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

ROOT = Path(__file__).resolve().parent.parent
SAMPLE_FILE = ROOT / "files" / "BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json"


def main() -> None:
    client = TestClient(app)
    h = client.get("/health")
    print("GET /health ->", h.status_code, h.json())

    data = json.loads(SAMPLE_FILE.read_text(encoding="utf-8"))
    sample = data["cases"][0]["input"]
    r = client.post("/optimize-energy", json=sample)
    out = r.json()
    expected = data["cases"][0]["expected_output"]
    print("POST /optimize-energy ->", r.status_code)
    print(f"  scenario_id           = {out['scenario_id']}")
    print(f"  total_grid_kwh        = {out['total_grid_kwh']} (expected {expected['total_grid_kwh']})")
    print(f"  total_cost_bdt        = {out['total_cost_bdt']} (expected {expected['total_cost_bdt']})")
    print(f"  peak_grid_kwh         = {out['peak_grid_kwh']} (expected {expected['peak_grid_kwh']})")
    print(f"  plan_summary          = {out['plan_summary']}")
    print(f"  interpretation count  = {len(out['directive_interpretation'])}")
    print(f"  hourly_plan hours     = {[p['hour'] for p in out['hourly_plan']]}")


if __name__ == "__main__":
    main()
