"""Verify the payload that the UI would actually send, per scenario."""
import json
import re
import urllib.request
from pathlib import Path

INDEX_HTML = Path("app/static/index.html").read_text(encoding="utf-8")
BASE = "http://127.0.0.1:8000"


def extract_block(name: str) -> str:
    """Extract a JS object literal starting at 'name: {'."""
    idx = INDEX_HTML.find(f'"{name}":')
    if idx < 0:
        return ""
    brace = INDEX_HTML.find("{", idx)
    depth = 0
    end = brace
    for i in range(brace, len(INDEX_HTML)):
        c = INDEX_HTML[i]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                end = i + 1
                break
    return INDEX_HTML[brace:end]


# Re-implement buildPayload logic in Python and call the API.
SCENARIOS = {
    "SAMPLE-01": ("SAMPLE-01", 220, 110, 40, 50, 50,
                  "6,6,5,5,5,6,8,10,12,14,16,16,15,14,13,14,18,22,28,30,26,18,10,7",
                  "90,85,80,80,85,95,110,130,150,165,175,180,185,180,170,165,170,185,205,215,205,175,135,105",
                  "0,0,0,0,0,0,5,20,50,90,130,160,180,170,140,90,45,10,0,0,0,0,0,0",
                  "Facilities will wash the rooftop solar panels from noon until 2 PM. During cleaning, usable solar should be treated as roughly 25% of the forecast.\nThe sports office moved next month's registration deadline."),
    "SAMPLE-04": ("DEMO-04", 500, 200, 50, 100, 100,
                  "6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,11.5,11.5,11.5,11.5,6.5,6.5",
                  "90,80,75,70,65,80,110,140,180,210,240,250,235,210,200,205,230,260,300,290,270,240,180,120",
                  "0,0,0,0,0,0,10,30,70,120,160,180,170,140,90,40,10,0,0,0,0,0,0,0",
                  "Battery must not discharge from 6 PM until 8 PM."),
    "SAMPLE-06": ("DEMO-06", 500, 200, 50, 100, 100,
                  "6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,6.5,11.5,11.5,11.5,11.5,6.5,6.5",
                  "90,80,75,70,65,80,110,140,180,210,240,250,235,210,200,205,230,260,300,290,270,240,180,120",
                  "0,0,0,0,0,0,10,30,70,120,160,180,170,140,90,40,10,0,0,0,0,0,0,0",
                  "Panel inspection will leave about half of the forecast solar output.\nThe charging circuit will be unavailable from 9 PM until 11 PM."),
    "SAMPLE-07": ("SAMPLE-07", 250, 150, 40, 60, 60,
                  "6,6,5,5,5,6,8,10,12,14,15,16,16,15,14,15,18,23,29,32,30,21,11,7",
                  "100,95,90,90,95,105,120,135,150,165,175,185,190,185,175,170,180,195,210,225,215,185,145,115",
                  "0,0,0,0,0,0,5,20,50,90,135,170,190,180,145,95,45,10,0,0,0,0,0,0",
                  "Keep at least 90 kWh in the battery from 6 PM until 10 PM for emergency services.\nThe evening transformer limit is 180 kWh of grid import from 7 PM until 9 PM."),
}


def build_payload(sid, cap, init, mn, mc, md, tariff, demand, solar, notes):
    def nums(s, n=24):
        out = [float(x.strip()) for x in s.split(",")]
        assert len(out) == n, f"expected {n}, got {len(out)}"
        return out
    t = nums(tariff); d = nums(demand); s = nums(solar)
    hours = [{"hour": h, "demand_kwh": d[h], "solar_kwh": s[h], "tariff_bdt_per_kwh": t[h]} for h in range(24)]
    return {
        "scenario_id": sid,
        "battery": {
            "capacity_kwh": cap,
            "initial_energy_kwh": init,
            "minimum_energy_kwh": mn,
            "max_charge_kwh_per_hour": mc,
            "max_discharge_kwh_per_hour": md,
        },
        "hours": hours,
        "operator_notes": [n for n in notes.split("\n") if n.strip()],
    }


def post(payload):
    req = urllib.request.Request(
        BASE + "/optimize-energy",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


print(f"{'scenario':>12}  {'http':>5}  {'grid':>8}  {'cost':>10}  {'peak':>6}  plan_summary")
print("-" * 110)
for name, args in SCENARIOS.items():
    payload = build_payload(*args)
    status, out = post(payload)
    if status == 200:
        s = out.get("plan_summary", "")
        print(f"{name:>12}  {status:>5}  {out['total_grid_kwh']:>8.2f}  {out['total_cost_bdt']:>10.2f}  {out['peak_grid_kwh']:>6.2f}  {s}")
    else:
        print(f"{name:>12}  {status:>5}  ERROR: {json.dumps(out)[:200]}")
