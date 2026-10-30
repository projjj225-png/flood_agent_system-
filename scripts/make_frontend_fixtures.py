"""Regenerate the frontend test fixtures from the REAL backend pipeline (offline).

    python scripts/make_frontend_fixtures.py

Writes frontend/src/__tests__/fixtures/{simulation_*.json, scenario_*.json}. Every
document is fabricated, labelled simulated data produced by src/simulation_engine.py
or the existing risk scenarios; nothing is hand-edited.
"""

import json
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from src.dashboard_service import DashboardService  # noqa: E402
from src.river_service import RiverEvidenceService  # noqa: E402
from src.simulation_engine import CYCLE, DemoSimulation, load_demo_config  # noqa: E402

OUT = ROOT / "frontend" / "src" / "__tests__" / "fixtures"
START = datetime(2026, 10, 30, 6, 30, tzinfo=timezone.utc)


def write(name: str, doc: dict) -> None:
    (OUT / name).write_text(json.dumps(doc, indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    print("wrote", name)


def main() -> None:
    service = RiverEvidenceService.from_file()
    with tempfile.TemporaryDirectory() as tmp:
        config = load_demo_config()
        config.update(csv_path=str(Path(tmp) / "demo.csv"),
                      river_audit_path=str(Path(tmp) / "river_audit.jsonl"))
        sim = DemoSimulation(service, config, wall_clock=lambda: START, seed="fixture")
        sim.reset(CYCLE, log=False)
        write("simulation_start.json", sim.snapshot())
        while sim.record["state"] != "EMERGENCY" or sim.tick_count < 13:
            sim.tick()
        write("simulation_emergency.json", sim.snapshot())
        while len(sim.transitions) < 3:
            sim.tick()
        write("simulation_recovery.json", sim.snapshot())
        dash = DashboardService(service, state_file=None, audit_path=Path(tmp) / "a.jsonl")
        for name in ("simulated-high-risk", "stale-river", "conflicting-evidence",
                     "missing-weather"):
            doc, _ = dash.get("simulated", name)
            write(f"scenario_{name.replace('-', '_')}.json", doc)


if __name__ == "__main__":
    main()
