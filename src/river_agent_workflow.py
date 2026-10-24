"""River Agent workflow: evidence -> conflict check -> reverification -> permissions.

Run from the project root, for example:
    python -m src.river_agent_workflow --mode simulated --scenario conflict-resolves --state ALERT
    python -m src.river_agent_workflow --mode auto --state EMERGENCY

Pipeline (every step deterministic and audited):
1. Evidence: RiverEvidenceService.get_evidence() (live / auto / historical /
   simulated). Conflicts come from the service's ACTUAL comparison of
   readings across sources (cross_source_check); nothing is injected.
2. Conflict policy: evidence_reverification.assess_conflict().
3. Reverification, only if a conflict affects current evidence AND the
   disaster state permits "request_evidence_reverification" (executed via
   capability_control.execute_if_authorized, so it is audited and fails
   closed). It re-obtains the primary source (a new live fetch, or a re-read
   of the file), confirms the conflicting readings are unchanged, and checks
   each one against independent sources (reverify_conflicts).
4. Permissions: capability_control.authorize_with_evidence for each
   requested capability. Real-world capabilities need verified-live, fresh,
   acceptable, non-simulated, conflict-free evidence and are then only
   PERMITTED_PENDING_HUMAN_APPROVAL. Nothing is ever executed here.

Simulated scenarios are DATA files in data/simulated/ (fabricated, labelled
simulated): they choose which simulated sources are compared, not the result.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import sys
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from src.audit_log import AuditLog
from src.capability_control import (
    REAL_WORLD_CAPABILITIES,
    DisasterState,
    assess_evidence_for_authorization,
    authorize_with_evidence_and_audit,
    execute_if_authorized,
)
from src.evidence_reverification import (
    OUTCOME_FAILED,
    OUTCOME_PERSISTS,
    OUTCOME_RESOLVED,
    assess_conflict,
    record_reverification_request,
    reverify_conflicts,
)
from src.river_monitoring_agent import load_observations, parse_timestamp
from src.river_service import (
    LABEL_SIMULATED,
    MODE_AUTO,
    MODE_HISTORICAL,
    MODE_LIVE,
    MODE_SIMULATED,
    MODES,
    RiverEvidenceService,
)

SIM_DIR = "data/simulated"
# Scenario -> which simulated sources are compared and used for re-checking.
SCENARIO_SOURCES = {
    "no-conflict": {
        "cross_check_paths": [f"{SIM_DIR}/secondary_agrees.csv"],
        "independent_paths": [f"{SIM_DIR}/independent_supports_primary.csv"],
    },
    "conflict-resolves": {
        "cross_check_paths": [f"{SIM_DIR}/secondary_conflicts.csv"],
        "independent_paths": [f"{SIM_DIR}/independent_supports_primary.csv"],
    },
    "conflict-persists": {
        "cross_check_paths": [f"{SIM_DIR}/secondary_conflicts.csv"],
        "independent_paths": [f"{SIM_DIR}/independent_supports_secondary.csv"],
    },
    "reverification-fails": {
        "cross_check_paths": [f"{SIM_DIR}/secondary_conflicts.csv"],
        "independent_paths": [f"{SIM_DIR}/missing_independent_source.csv"],
    },
}
SCENARIOS = tuple(SCENARIO_SOURCES)
DEFAULT_CAPABILITIES = ("read_river", "request_evidence_reverification", "prepare_advisory",
                        "propose_evacuation_plan", "request_human_emergency_authorization")
DEFAULT_AUDIT_PATH = "outputs/audit/river_workflow_audit.jsonl"
REVERIFY_CAPABILITY = "request_evidence_reverification"


def read_station_readings(path: Path, station_id: str,
                          reference_time: datetime) -> tuple[dict[str, float] | None, str | None]:
    """{observed_at ISO (UTC): level} for one station up to reference_time, or (None, error)."""
    if path is None or not Path(path).is_file():
        return None, f"source not found: {path}"
    loaded = load_observations(path)
    if loaded["fatal"]:
        return None, f"{path}: " + "; ".join(e["message"] for e in loaded["errors"])
    return ({o["observed_at"].isoformat(): o["river_level_m"] for o in loaded["observations"]
             if o["station_id"] == station_id and o["observed_at"] <= reference_time}, None)


def _primary_path(evidence: dict[str, Any]) -> Path | None:
    live = evidence.get("live_fetch") or {}
    if live.get("snapshot_path"):
        return Path(live["snapshot_path"])
    path = (evidence.get("input_file") or {}).get("path")
    return Path(path) if path else None


def _reverify(service: RiverEvidenceService, audit_log: AuditLog, run_id: str,
              agent_id: str, assessment: dict[str, Any], evidence: dict[str, Any], mode: str,
              as_of: datetime | None, cross_check_paths: list[str] | None,
              independent_paths: list[str], tolerance_m: float) -> dict[str, Any]:
    """Re-obtain the primary source and re-check every current conflict."""
    block: dict[str, Any] = {"status": "FAILED", "outcome": OUTCOME_FAILED, "attempts": 0,
                             "authorization_restricted": True}
    request = record_reverification_request(
        assessment, audit_log, run_id, agent_id,
        source_to_recheck=str(_primary_path(evidence) or evidence.get("source_label")))
    block["request"] = request
    if request["status"] != "REQUESTED":
        block.update(status=request["status"], outcome="UNKNOWN",
                     reason="Reverification request could not be audited.")
        return block
    block["attempts"] = 1
    conflicts = [c for c in (evidence.get("cross_source_check") or {}).get("conflict_details", [])
                 if c.get("affects_current_evidence")]
    try:
        fresh = service.get_evidence(mode=mode, as_of=as_of, cross_check_paths=cross_check_paths)
        block["fresh_evidence_id"] = fresh.get("evidence_id")
        if fresh.get("status") == "DATA_UNAVAILABLE":
            raise RuntimeError("primary source unavailable on re-check")
        reference = parse_timestamp(fresh["freshness"]["reference_time"])
        station = fresh.get("station_id") or evidence.get("station_id")
        fresh_primary, error = read_station_readings(_primary_path(fresh), station, reference)
        if error:
            raise RuntimeError(f"primary source unreadable on re-check: {error}")
        # Independent means: not the primary file and not a source in the conflict.
        excluded = {str(Path(p).resolve()) for p in
                    [_primary_path(fresh), _primary_path(evidence)]
                    + [c["other_source"] for c in conflicts] if p}
        independent, errors = {}, []
        for raw in independent_paths:
            path = service.resolve_path(raw)
            if path is not None and str(path.resolve()) in excluded:
                errors.append(f"{raw}: not independent (it is the primary or conflicting source)")
                continue
            readings, error = read_station_readings(path, station, reference)
            if error:
                errors.append(error)
            else:
                independent[Path(raw).as_posix()] = readings
        result = reverify_conflicts(conflicts, fresh_primary, independent, tolerance_m, errors)
    except Exception as exc:  # any failure keeps the restriction in place
        block.update(status="FAILED", outcome=OUTCOME_FAILED, error=str(exc),
                     reason=f"Reverification failed: {exc}")
        audit_log.log(run_id, "evidence_reverification_failed", error=str(exc),
                      authorization_restricted=True)
        return block
    block.update(status="COMPLETED" if result["outcome"] != OUTCOME_FAILED else "FAILED",
                 outcome=result["outcome"], reason=result["reason"],
                 conflicts_checked=result["conflicts_checked"],
                 conflicts_remaining=result["conflicts_remaining"],
                 independent_sources=result["independent_sources"],
                 independent_errors=result["independent_errors"],
                 per_conflict=result["per_conflict"],
                 authorization_restricted=result["outcome"] != OUTCOME_RESOLVED)
    audit_log.log(run_id, "evidence_reverification_completed" if block["status"] == "COMPLETED"
                  else "evidence_reverification_failed",
                  outcome=result["outcome"], conflicts_checked=result["conflicts_checked"],
                  conflicts_remaining=result["conflicts_remaining"],
                  per_conflict=[{k: e[k] for k in ("observed_at", "status")}
                                for e in result["per_conflict"]],
                  independent_sources=result["independent_sources"],
                  independent_errors=result["independent_errors"],
                  fresh_evidence_id=block.get("fresh_evidence_id"),
                  authorization_restricted=block["authorization_restricted"])
    return block


def run_river_workflow(
    service: RiverEvidenceService,
    audit_log: AuditLog,
    mode: str = MODE_AUTO,
    disaster_state: str = DisasterState.NORMAL.value,
    capabilities: tuple[str, ...] | list[str] = DEFAULT_CAPABILITIES,
    as_of: datetime | None = None,
    cross_check_paths: list[str] | None = None,
    independent_paths: list[str] | None = None,
    agent_id: str = "river_agent",
    run_id: str | None = None,
    scenario: str | None = None,
) -> dict[str, Any]:
    """Run the full river workflow once. Never raises for evidence problems."""
    run_id = run_id or uuid.uuid4().hex
    rev_cfg = service.config.get("reverification") or {}
    if independent_paths is None:
        independent_paths = list(rev_cfg.get("simulated_independent_paths" if mode ==
                                             MODE_SIMULATED else "independent_paths", []))
    tolerance = float(rev_cfg.get("tolerance_m", 0.005))
    audit_log.log(run_id, "river_workflow_started", mode=mode, scenario=scenario,
                  disaster_state=str(disaster_state), capabilities=list(capabilities),
                  cross_check_paths=cross_check_paths, independent_paths=independent_paths)

    evidence: dict[str, Any] | None = None
    evidence_error = None
    try:
        evidence = service.get_evidence(mode=mode, as_of=as_of,
                                        cross_check_paths=cross_check_paths)
    except (ValueError, OSError) as exc:
        evidence_error = str(exc)
        audit_log.log(run_id, "river_evidence_failed", error=evidence_error)

    reverification: dict[str, Any] = {"status": "NOT_REQUIRED", "outcome": "NOT_REQUIRED",
                                      "attempts": 0, "authorization_restricted": True}
    conflict_assessment = None
    if evidence is not None:
        ds = evidence.get("decision_support") or {}
        cross = evidence.get("cross_source_check") or {}
        conflict_assessment = assess_conflict(cross, bool(ds.get("current_evidence_usable")))
        reverification["authorization_restricted"] = conflict_assessment[
            "authorization_restricted"]
        if conflict_assessment["status"] == "REVERIFICATION_REQUIRED":
            decision, outcome = execute_if_authorized(
                disaster_state, REVERIFY_CAPABILITY, agent_id, audit_log, run_id,
                lambda: _reverify(service, audit_log, run_id, agent_id, conflict_assessment,
                                  evidence, mode, as_of, cross_check_paths,
                                  independent_paths, tolerance))
            if outcome is None:
                reverification = {
                    "status": "NOT_AUTHORIZED", "outcome": "NOT_AUTHORIZED", "attempts": 0,
                    "authorization_restricted": True,
                    "reason": (f"{REVERIFY_CAPABILITY} not allowed in state "
                               f"{decision['state']} ({decision['reason']}"
                               + ("" if decision.get("audit_written", True)
                                  else ", decision could not be audited") + ")."),
                }
            else:
                reverification = outcome
            reverification["authorization_decision"] = {
                k: decision[k] for k in ("allowed", "state", "reason", "audit_written")}

    evidence_assessment = assess_evidence_for_authorization(evidence, reverification)
    if evidence_error:
        evidence_assessment["error"] = evidence_error
    decisions = [authorize_with_evidence_and_audit(disaster_state, cap, agent_id,
                                                   evidence_assessment, audit_log, run_id)
                 for cap in capabilities]
    real_world_allowed = [d["capability"] for d in decisions
                          if d["allowed"] and d["capability"] in REAL_WORLD_CAPABILITIES]
    # The system never self-authorizes: at best an action may go to a human.
    real_world_authorization = ("HUMAN_AUTHORIZATION_REQUIRED" if real_world_allowed
                                else "RESTRICTED")

    is_simulated = bool(evidence) and evidence.get("source_label") == LABEL_SIMULATED
    freshness = ((evidence or {}).get("freshness") or {}).get("status", "UNKNOWN")
    recent = (((evidence or {}).get("data_quality") or {}).get("recent_quality") or {}).get(
        "status", "UNKNOWN")
    cross = (evidence or {}).get("cross_source_check") or {}
    initial_conflicts = cross.get("conflicts_affecting_current_evidence", 0) or 0
    conflict_cleared = initial_conflicts == 0 or reverification["outcome"] == OUTCOME_RESOLVED
    simulation_baseline_usable = (bool(evidence) and freshness == "FRESH"
                                  and recent == "ACCEPTABLE"
                                  and evidence.get("status") != "DATA_UNAVAILABLE")
    simulation_evidence_eligible = is_simulated and simulation_baseline_usable and conflict_cleared
    ds = (evidence or {}).get("decision_support") or {}

    result: dict[str, Any] = {
        "run_id": run_id,
        "mode": mode.upper(),
        "scenario": scenario,
        "disaster_state": str(getattr(disaster_state, "value", disaster_state)),
        "station": (evidence or {}).get("station_id"),
        "source_label": (evidence or {}).get("source_label"),
        "service_evidence_id": (evidence or {}).get("evidence_id"),
        "evidence_error": evidence_error,
        "observed_at": ds.get("last_known_observed_at"),
        # A level is handed out as current only when the evidence is eligible.
        "river_level_m": ds.get("last_known_level_m") if evidence_assessment["eligible"] else None,
        "simulated_river_level_m": (ds.get("last_known_level_m")
                                    if simulation_evidence_eligible else None),
        "trend_direction": ds.get("trend_direction"),
        "rate_of_change_m_per_hour": ds.get("rate_of_change_m_per_hour"),
        "threshold_status": (evidence or {}).get("threshold_status", "UNKNOWN"),
        "threshold_evaluation": (evidence or {}).get("threshold_evaluation"),
        "last_known_level_m": ds.get("last_known_level_m"),
        "evidence_freshness": freshness,
        "evidence_quality": recent,
        "source_verified": bool(((evidence or {}).get("data_quality") or {}).get(
            "is_verified_live")),
        "cross_source_check": {k: cross.get(k) for k in (
            "status", "compared_with", "overlapping_timestamps", "conflicts",
            "conflicts_affecting_current_evidence", "examples")},
        "conflict_assessment": conflict_assessment,
        "initial_conflicts": initial_conflicts,
        "reverification": reverification,
        "conflicts_remaining": (0 if conflict_cleared
                                else reverification.get("conflicts_remaining",
                                                        initial_conflicts)),
        "evidence_assessment": evidence_assessment,
        "capability_decisions": decisions,
        "service_current_evidence_usable": bool(ds.get("current_evidence_usable")),
        "current_evidence_usable": evidence_assessment["eligible"],
        "simulation_baseline_usable": simulation_baseline_usable if is_simulated else None,
        "simulation_evidence_eligible": simulation_evidence_eligible,
        "simulation_permission": (None if not is_simulated else
                                  "SIMULATION_ALLOWED" if simulation_evidence_eligible
                                  else "SIMULATION_RESTRICTED"),
        "current_river_condition": ("ASSESSED_LIVE" if evidence_assessment["eligible"]
                                    else "ASSESSED_SIMULATED" if simulation_evidence_eligible
                                    else "UNKNOWN"),
        "real_world_authorization": real_world_authorization,
        "authorization": real_world_authorization,
        "real_world_capabilities_pending_human_approval": real_world_allowed,
        "service_reasons_not_usable": ds.get("reasons_not_usable", []),
        "service_audit": (evidence or {}).get("audit", {}),
        "workflow_audit_log_path": str(audit_log.path) if audit_log.path else None,
    }
    result["reason"] = (
        "Verified, fresh, conflict-free live evidence: real-world capabilities may be put to a "
        "human for approval." if real_world_allowed else
        "Simulated evidence passed simulation checks; it never authorizes real-world action."
        if simulation_evidence_eligible else
        "Real-world capabilities restricted: " + ", ".join(evidence_assessment["reasons"]) + ".")
    stable = {k: v for k, v in result.items()
              if k not in ("run_id", "service_audit", "workflow_audit_log_path")}
    stable["capability_decisions"] = [{k: v for k, v in d.items() if k != "audit_written"}
                                      for d in decisions]
    result["decision_id"] = hashlib.sha256(json.dumps(
        stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    audit_log.log(run_id, "river_workflow_completed", scenario=scenario, mode=mode,
                  decision_id=result["decision_id"],
                  service_evidence_id=result["service_evidence_id"],
                  evidence_reasons=evidence_assessment["reasons"],
                  reverification_outcome=reverification["outcome"],
                  real_world_authorization=real_world_authorization,
                  capabilities_allowed=[d["capability"] for d in decisions if d["allowed"]],
                  capabilities_denied=[d["capability"] for d in decisions if not d["allowed"]])
    result["workflow_audit_events_written"] = audit_log.events_written
    result["workflow_audit_write_errors"] = list(audit_log.write_errors)
    if audit_log.write_errors:
        # An incomplete audit trail never leaves a real-world permission standing.
        result["real_world_authorization"] = result["authorization"] = "RESTRICTED"
        result["real_world_capabilities_pending_human_approval"] = []
    return result


def run_simulated_scenario(
    scenario: str,
    service: RiverEvidenceService,
    audit_log: AuditLog,
    run_id: str | None = None,
    as_of: datetime | None = None,
    disaster_state: str = DisasterState.NORMAL.value,
    capabilities: tuple[str, ...] | list[str] = DEFAULT_CAPABILITIES,
) -> dict[str, Any]:
    """Simulated workflow whose scenario selects the simulated SOURCE FILES compared."""
    if scenario not in SCENARIO_SOURCES:
        raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
    sources = SCENARIO_SOURCES[scenario]
    return run_river_workflow(service, audit_log, mode=MODE_SIMULATED,
                              disaster_state=disaster_state, capabilities=capabilities,
                              as_of=as_of, cross_check_paths=sources["cross_check_paths"],
                              independent_paths=sources["independent_paths"],
                              run_id=run_id, scenario=scenario)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the River Agent workflow once.")
    parser.add_argument("--mode", choices=MODES, default=MODE_SIMULATED)
    parser.add_argument("--scenario", choices=SCENARIOS, default=None,
                        help="Simulated mode only: which simulated sources to compare "
                             "(default conflict-resolves).")
    parser.add_argument("--state", default=DisasterState.NORMAL.value,
                        help="Disaster state: NORMAL, ALERT, EMERGENCY or RECOVERY.")
    parser.add_argument("--capability", action="append", default=None,
                        help="Capability to request (repeatable). Default: a standard set.")
    parser.add_argument("--config", default=None, help="Optional river_agent.json path")
    parser.add_argument("--audit", default=DEFAULT_AUDIT_PATH)
    parser.add_argument("--as-of", default=None,
                        help="Replay time for simulated/historical modes "
                             "(simulated default 2026-10-10T12:00:00+00:00).")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.scenario and args.mode != MODE_SIMULATED:
        parser.error("--scenario is only valid with --mode simulated")
    as_of = None
    as_of_text = args.as_of or ("2026-10-10T12:00:00+00:00" if args.mode == MODE_SIMULATED
                                else None)
    if as_of_text:
        if args.mode in (MODE_LIVE, MODE_AUTO):
            parser.error("--as-of cannot be used with live/auto modes")
        as_of = parse_timestamp(as_of_text)
        if as_of is None:
            parser.error("--as-of must be a timezone-aware ISO 8601 time")
    capabilities = tuple(args.capability) if args.capability else DEFAULT_CAPABILITIES
    try:
        service = (RiverEvidenceService.from_file(args.config) if args.config
                   else RiverEvidenceService.from_file())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    audit = AuditLog(service.resolve_path(args.audit), component="river_workflow")
    if args.mode == MODE_SIMULATED:
        result = run_simulated_scenario(args.scenario or "conflict-resolves", service, audit,
                                        as_of=as_of, disaster_state=args.state,
                                        capabilities=capabilities)
    else:
        result = run_river_workflow(service, audit, mode=args.mode,
                                    disaster_state=args.state, capabilities=capabilities,
                                    as_of=as_of)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if result["workflow_audit_write_errors"]:
        return 2
    return 1 if result["evidence_error"] or result["source_label"] in (None, "LIVE_UNAVAILABLE") \
        else 0


if __name__ == "__main__":
    raise SystemExit(main())
