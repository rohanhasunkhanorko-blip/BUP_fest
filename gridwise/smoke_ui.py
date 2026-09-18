"""Smoke test: serve UI + verify chart loads + run a sample."""
import json
from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def main() -> None:
    # 1. UI root
    r = client.get("/")
    assert r.status_code == 200, r.text
    html = r.text
    assert "GridWise" in html and "chart" in html.lower(), "UI missing core content"
    print(f"GET  /                  -> {r.status_code}  ({len(html)} bytes)")

    # 2. Static asset
    r2 = client.get("/ui/index.html")
    assert r2.status_code == 200
    print(f"GET  /ui/index.html     -> {r2.status_code}")

    # 3. Health
    r3 = client.get("/health")
    print(f"GET  /health            -> {r3.status_code} {r3.json()}")

    # 4. Run sample 1
    samples = json.loads(Path("../files/BUP_CSE_FEST_2026_Preli_Public_Sample_Cases.json").read_text(encoding="utf-8"))
    r4 = client.post("/optimize-energy", json=samples["cases"][0]["input"])
    out = r4.json()
    print(f"POST /optimize-energy   -> {r4.status_code}")
    print(f"  scenario_id           = {out['scenario_id']}")
    print(f"  total_grid_kwh        = {out['total_grid_kwh']:.2f}")
    print(f"  total_cost_bdt        = {out['total_cost_bdt']:.2f}")
    print(f"  peak_grid_kwh         = {out['peak_grid_kwh']:.2f}")
    print(f"  plan_summary          = {out['plan_summary']}")
    print(f"  hourly_plan hours     = {[p['hour'] for p in out['hourly_plan']]}")

    print("\n✅ UI + backend pipeline verified.")


if __name__ == "__main__":
    main()
