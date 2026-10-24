"""End-to-end flood orchestrator: evidence -> risk -> state -> revocation -> permissions.

One cycle:
1. Load the current disaster state (real or simulated state file).
2. Combined Weather + River evidence (flood_evidence_workflow), evaluated
   under the current state (so reverification follows the state's allowlist).
3. Risk (risk_assessment.assess_flood_risk). Real data use the combined
   assessment as the evidence gate; simulated data use the simulation gate
   and are labelled SIMULATED throughout.
4. State transition (disaster_state_manager), persisted; capabilities revoked
   or granted by the transition are audited.
5. Capability decisions under the NEW state with the combined assessment.
   Simulated evidence never permits a real-world capability.

    python -m src.flood_orchestrator --river-mode auto
    python -m src.flood_orchestrator --simulate-event          # fabricated flood event replay
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog
from src.capability_control import (
    REAL_WORLD_CAPABILITIES,
    STATE_CAPABILITIES,
    authorize_with_evidence_and_audit,
    permitted_capabilities,
)
from src.disaster_state_manager import StateStore
from src.flood_evidence_workflow import load_settings, run_flood_evidence_workflow
from src.river_monitoring_agent import PROJECT_DIR, load_json_file, parse_timestamp
from src.risk_assessment import DEFAULT_RISK_SETTINGS, assess_flood_risk
from src.river_service import MODE_AUTO, MODE_LIVE, MODE_SIMULATED, MODES, RiverEvidenceService

REAL_STATE_FILE = "outputs/state/disaster_state.json"
SIM_STATE_FILE = "outputs/state/simulated_disaster_state.json"
EVENT_FILE = "data/simulated/flood_event_upper_anicut_test.csv"
EVENT_THRESHOLDS = "config/river_thresholds.example.json"
EVENT_TIMELINE = "outputs/simulated_flood_event_timeline.json"
ORCHESTRATOR_AUDIT = "outputs/audit/flood_orchestrator_audit.jsonl"
# Every capability any state can grant, so decisions show what each state allows.
ALL_CAPABILITIES = tuple(sorted(set().union(*STATE_CAPABILITIES.values())))


def run_cycle(service: RiverEvidenceService, audit_log: AuditLog, store: StateStore,
              river_mode: str = MODE_AUTO, as_of: datetime | None = None,
              scenario: str | None = None, weather_file: str | Path | None = None,
              use_weather: bool = True, weather_agent: Any = None,
              capabilities: tuple[str, ...] | list[str] = ALL_CAPABILITIES,
              settings: dict[str, Any] | None = None,
              risk_settings: dict[str, Any] | None = None,
              clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    simulated = river_mode == MODE_SIMULATED
    if simulated != store.simulated:
        raise ValueError("Simulated runs must use the simulated state file and real runs the "
                         "real one.")
    clock = clock or (lambda: datetime.now(timezone.utc))
    run_id = uuid.uuid4().hex
    state_before = store.load()["state"]
    audit_log.log(run_id, "orchestrator_cycle_started", river_mode=river_mode,
                  state=state_before, simulated=simulated,
                  as_of=as_of.isoformat() if as_of else None)

    combined = run_flood_evidence_workflow(
        service, audit_log, river_mode=river_mode, disaster_state=state_before,
        capabilities=(), as_of=as_of, scenario=scenario, weather_file=weather_file,
        weather_agent=weather_agent, settings=settings, clock=clock, run_id=run_id,
        use_weather=use_weather)
    river, weather = combined["river"], combined["weather"]
    assessment = combined["combined_assessment"]
    if simulated:
        usable, basis = bool(river.get("simulation_evidence_eligible")), "SIMULATED"
        reasons = [] if usable else ["SIMULATION_EVIDENCE_NOT_ELIGIBLE"]
    else:
        usable, basis = bool(assessment["eligible"]), river.get("source_label") or "UNKNOWN"
        reasons = assessment["reasons"]
    risk = assess_flood_risk(river, weather if use_weather else None, usable, basis, reasons,
                             risk_settings)
    reference = parse_timestamp(combined["reference_time"])
    transition = store.apply(risk, reference, evidence_ref=combined["combined_id"])
    audit_log.log(run_id, "risk_assessed", risk_level=risk["risk_level"],
                  factors=risk["factors"], reasons_unknown=risk["reasons_unknown"],
                  indicative=risk["indicative"], evidence_basis=basis)
    audit_log.log(run_id, "state_transition", **{k: transition[k] for k in (
        "from", "to", "changed", "reason", "pending", "risk_level")}, simulated=simulated)
    if transition["revoked"] or transition["granted"]:
        audit_log.log(run_id, "capabilities_changed", revoked=transition["revoked"],
                      granted=transition["granted"], state_from=transition["from"],
                      state_to=transition["to"])

    decisions = [authorize_with_evidence_and_audit(transition["to"], cap,
                                                   "flood_orchestrator", assessment,
                                                   audit_log, run_id)
                 for cap in capabilities]
    pending = [d["capability"] for d in decisions
               if d["allowed"] and d["capability"] in REAL_WORLD_CAPABILITIES]
    authorization = ("HUMAN_AUTHORIZATION_REQUIRED" if pending and not simulated
                     and not audit_log.write_errors else "RESTRICTED")
    result = {
        "run_id": run_id,
        "mode": "SIMULATED" if simulated else river_mode.upper(),
        "reference_time": combined["reference_time"],
        "state_before": state_before,
        "state_after": transition["to"],
        "transition": transition,
        "risk": risk,
        "river": {k: river.get(k) for k in (
            "source_label", "observed_at", "last_known_level_m", "river_level_m",
            "simulated_river_level_m", "river_trend_direction", "rate_of_change_m_per_hour",
            "threshold_status", "freshness", "recent_quality", "reverification_outcome")},
        "weather": {k: weather.get(k) for k in ("source_label", "status", "age_hours",
                                                "rainfall_mm", "reasons")},
        "cross_agent_checks": combined["cross_agent_checks"],
        "combined_assessment": assessment,
        "capability_decisions": decisions,
        "real_world_authorization": authorization,
        "real_world_capabilities_pending_human_approval": pending if authorization !=
        "RESTRICTED" else [],
        "combined_id": combined["combined_id"],
        "audit": {"log_path": str(audit_log.path) if audit_log.path else None,
                  "write_errors": list(audit_log.write_errors)},
    }
    audit_log.log(run_id, "orchestrator_cycle_completed", state_after=transition["to"],
                  risk_level=risk["risk_level"], real_world_authorization=authorization)
    return result


def event_service(base_config: dict[str, Any], base_dir: Path, outputs: dict[str, str] | None,
                  event_file: str = EVENT_FILE,
                  thresholds: str = EVENT_THRESHOLDS) -> RiverEvidenceService:
    """Service configured for the fabricated flood event (simulated station only)."""
    config = copy.deepcopy(base_config)
    config["simulated"] = {**config.get("simulated", {}), "path": event_file,
                           "station_id": "UPPER_ANICUT_TEST", "cross_check_paths": []}
    config["thresholds_path"] = thresholds
    if outputs:
        config["outputs"] = outputs
    return RiverEvidenceService(config, base_dir=base_dir)


def simulate_event(service: RiverEvidenceService, audit_log: AuditLog, store: StateStore,
                   start: datetime, hours: int, step_hours: float = 1.0,
                   offset_minutes: int = 30) -> list[dict[str, Any]]:
    """Replay the event hour by hour; returns a compact timeline."""
    if store.path.is_file():
        store.path.unlink()  # every replay starts from NORMAL
    timeline = []
    for k in range(hours):
        as_of = start + timedelta(hours=k * step_hours, minutes=offset_minutes)
        cycle = run_cycle(service, audit_log, store, river_mode=MODE_SIMULATED, as_of=as_of,
                          use_weather=False, capabilities=ALL_CAPABILITIES,
                          clock=lambda a=as_of: a)
        timeline.append({
            "as_of": cycle["reference_time"],
            "level_m": cycle["river"]["simulated_river_level_m"],
            "trend": cycle["river"]["river_trend_direction"],
            "rate_m_per_hour": cycle["river"]["rate_of_change_m_per_hour"],
            "threshold_status": cycle["river"]["threshold_status"],
            "risk": cycle["risk"]["risk_level"],
            "risk_factors": [f["factor"] for f in cycle["risk"]["factors"]],
            "state": cycle["state_after"],
            "transition": cycle["transition"]["reason"] if cycle["transition"]["changed"]
            else None,
            "revoked": cycle["transition"]["revoked"],
            "granted": cycle["transition"]["granted"],
            "state_capabilities": sorted(permitted_capabilities(cycle["state_after"])),
            "capabilities_allowed": [d["capability"] for d in cycle["capability_decisions"]
                                     if d["allowed"]],
            "real_world_authorization": cycle["real_world_authorization"],
        })
    return timeline


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Flood orchestrator (one cycle, or a "
                                                 "simulated flood event replay).")
    parser.add_argument("--river-mode", choices=MODES, default=MODE_AUTO)
    parser.add_argument("--scenario", default=None)
    parser.add_argument("--as-of", default=None)
    parser.add_argument("--weather-file", default=None)
    parser.add_argument("--no-weather", action="store_true")
    parser.add_argument("--simulate-event", action="store_true",
                        help="Replay data/simulated/flood_event_upper_anicut_test.csv hour by hour.")
    parser.add_argument("--river-config", default=None)
    parser.add_argument("--state-file", default=None)
    parser.add_argument("--audit", default=ORCHESTRATOR_AUDIT)
    parser.add_argument("--output", default=None, help="Write the result JSON here.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        base = (RiverEvidenceService.from_file(args.river_config) if args.river_config
                else RiverEvidenceService.from_file())
        settings = load_settings()
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    audit = AuditLog(base.resolve_path(args.audit), component="flood_orchestrator")

    if args.simulate_event:
        service = event_service(base.config, base.base_dir, None)
        store = StateStore(base.resolve_path(args.state_file or SIM_STATE_FILE), simulated=True)
        timeline = simulate_event(service, audit, store,
                                  datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc), hours=84)
        test_thresholds = load_json_file(base.resolve_path(EVENT_THRESHOLDS),
                                         "Thresholds").get("UPPER_ANICUT_TEST", {})
        document = {"label": "SIMULATED flood event replay (fabricated data, TEST thresholds, "
                             "not official)",
                    "thresholds": {k: test_thresholds.get(k) for k in (
                        "warning_level_m", "danger_level_m", "reference")},
                    "timeline": timeline}
        path = base.resolve_path(args.output or EVENT_TIMELINE)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
        for row in timeline:
            if row["transition"] or row is timeline[0] or row is timeline[-1]:
                print(f"{row['as_of'][:16]}  level {row['level_m']} m  {row['trend'] or '-':9s} "
                      f"risk {row['risk']:8s} state {row['state']:9s} "
                      f"{row['transition'] or ''}"
                      + (f"  revoked: {', '.join(row['revoked'])}" if row["revoked"] else ""))
        print(f"Saved timeline to {path}", file=sys.stderr)
        return 0

    simulated = args.river_mode == MODE_SIMULATED
    store = StateStore(base.resolve_path(args.state_file or (SIM_STATE_FILE if simulated
                                                             else REAL_STATE_FILE)),
                       simulated=simulated)
    as_of = parse_timestamp(args.as_of) if args.as_of else (
        datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc) if simulated else None)
    if as_of is not None and args.river_mode in (MODE_LIVE, MODE_AUTO):
        parser.error("--as-of cannot be used with live/auto modes")
    result = run_cycle(base, audit, store, river_mode=args.river_mode, as_of=as_of,
                       scenario=args.scenario, weather_file=args.weather_file,
                       use_weather=not args.no_weather, settings=settings)
    text = json.dumps(result, indent=2, ensure_ascii=False, default=str)
    print(text)
    if args.output:
        Path(args.output).write_text(text + "\n", encoding="utf-8")
    return 2 if result["audit"]["write_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
