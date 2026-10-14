"""Simulated River Agent workflow backed by the existing RiverEvidenceService.

Run from the project root:
    python -m src.river_agent_workflow --scenario conflict-resolves --mode simulated

Important: conflict scenarios inject a controlled simulation event after the RiverEvidenceService
has retrieved and assessed its configured simulated dataset. They do not represent live data.
"""
from __future__ import annotations

import argparse
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from src.audit_log import AuditLog
from src.evidence_reverification import assess_conflict, record_reverification_request
from src.river_service import RiverEvidenceService


SCENARIOS = ("no-conflict", "conflict-resolves", "conflict-persists", "reverification-fails")


def _injected_conflict_count(scenario: str, attempt: int) -> int:
    """Controlled fault injection used to exercise reverification behavior."""
    if scenario == "no-conflict":
        return 0
    if scenario == "conflict-resolves":
        return 1 if attempt == 1 else 0
    if scenario in ("conflict-persists", "reverification-fails"):
        return 1
    raise ValueError(f"Unknown scenario: {scenario}")


def run_simulated_scenario(
    scenario: str,
    service: RiverEvidenceService,
    audit_log: AuditLog,
    run_id: str | None = None,
    as_of: datetime | None = None,
) -> dict[str, Any]:
    """Use the real service's simulated evidence, then exercise conflict handling."""
    if scenario not in SCENARIOS:
        raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")

    run_id = run_id or uuid.uuid4().hex
    audit_log.log(run_id, "river_workflow_started", scenario=scenario, mode="simulated")

    # This is the key integration: use the project's configured simulated dataset and
    # the actual RiverEvidenceService validation/quality/freshness pipeline.
    evidence = service.get_evidence(mode="simulated", as_of=as_of)
    quality = evidence.get("data_quality", {})
    freshness = evidence.get("freshness", {})
    ds = evidence.get("decision_support", {})

    primary_conflicts = _injected_conflict_count(scenario, attempt=1)
    simulated_current_usable = (
        freshness.get("status") == "FRESH"
        and quality.get("recent_quality", {}).get("status") == "ACCEPTABLE"
        and primary_conflicts == 0
        and evidence.get("status") != "UNAVAILABLE"
    )
    initial = assess_conflict(
        {"conflicts_affecting_current_evidence": primary_conflicts},
        current_evidence_usable=simulated_current_usable,
    )

    reverification: dict[str, Any] = {
        "status": "NOT_REQUIRED",
        "outcome": "NOT_REQUIRED",
        "attempts": 0,
        "authorization_restricted": initial["authorization_restricted"],
    }

    if initial["status"] == "REVERIFICATION_REQUIRED":
        request = record_reverification_request(
            initial, audit_log, run_id, "river_agent", "simulated_independent_source"
        )
        reverification.update(
            status=request["status"],
            outcome=request["outcome"],
            attempts=0,
            authorization_restricted=True,
        )
        if request["status"] == "REQUESTED":
            # Count the retrieval attempt before performing it, including failures.
            reverification["attempts"] = 1
            try:
                if scenario == "reverification-fails":
                    raise RuntimeError("Simulated independent source unavailable")

                second_conflicts = _injected_conflict_count(scenario, attempt=2)
                resolved = second_conflicts == 0
                reverification["outcome"] = "RESOLVED" if resolved else "CONFLICT_PERSISTS"
                reverification["status"] = "COMPLETED"
                reverification["authorization_restricted"] = not resolved
                audit_log.log(
                    run_id,
                    "evidence_reverification_completed",
                    source_to_recheck="simulated_independent_source",
                    outcome=reverification["outcome"],
                    conflicts_remaining=second_conflicts,
                    authorization_restricted=not resolved,
                )
            except Exception as exc:
                reverification["status"] = "FAILED"
                reverification["outcome"] = "FAILED"
                reverification["error"] = str(exc)
                reverification["authorization_restricted"] = True
                audit_log.log(
                    run_id,
                    "evidence_reverification_failed",
                    source_to_recheck="simulated_independent_source",
                    error=str(exc),
                    authorization_restricted=True,
                )

    unresolved = reverification["outcome"] in (
        "CONFLICT_PERSISTS", "FAILED", "PENDING", "UNKNOWN"
    )
    # In simulation, assess quality/freshness without pretending the simulated source is
    # verified live. Keep the service's live-use decision separately visible in the output.
    service_current_evidence_usable = bool(ds.get("current_evidence_usable"))
    simulation_baseline_usable = (
        freshness.get("status") == "FRESH"
        and quality.get("recent_quality", {}).get("status") == "ACCEPTABLE"
        and evidence.get("status") != "UNAVAILABLE"
    )
    simulation_evidence_eligible = (
        simulation_baseline_usable
        and not unresolved
        and (primary_conflicts == 0 or reverification["outcome"] == "RESOLVED")
    )
    if reverification["outcome"] == "RESOLVED":
        conflicts_remaining = 0
    else:
        conflicts_remaining = _injected_conflict_count(scenario, attempt=1)

    # Strict semantic boundary:
    # simulated evidence may be eligible for a simulation, but never counts as
    # verified live/current evidence and never grants real-world authorization.
    real_world_authorization = "RESTRICTED"
    simulation_permission = (
        "SIMULATION_ALLOWED" if simulation_evidence_eligible else "SIMULATION_RESTRICTED"
    )

    result = {
        "run_id": run_id,
        "mode": "SIMULATED",
        "scenario": scenario,
        "station": evidence.get("station_name") or evidence.get("station_id"),
        "source_label": evidence.get("source_label"),
        "service_evidence_id": evidence.get("evidence_id"),
        "river_level_m": None,
        "simulated_river_level_m": (
            ds.get("last_known_level_m") if simulation_evidence_eligible else None
        ),
        "observed_at": ds.get("last_known_observed_at"),
        "evidence_freshness": freshness.get("status", "UNKNOWN"),
        "evidence_quality": quality.get("recent_quality", {}).get("status", "UNKNOWN"),
        "source_verified": quality.get("is_verified_live", False),
        "service_current_evidence_usable": service_current_evidence_usable,
        "simulation_baseline_usable": simulation_baseline_usable,
        "simulation_evidence_eligible": simulation_evidence_eligible,
        "simulation_permission": simulation_permission,
        "initial_conflicts": primary_conflicts,
        "reverification": reverification,
        "conflicts_remaining": conflicts_remaining,
        "current_evidence_usable": service_current_evidence_usable,
        "current_river_condition": (
            "ASSESSED_SIMULATED" if simulation_evidence_eligible else "UNKNOWN"
        ),
        "real_world_authorization": real_world_authorization,
        "authorization": real_world_authorization,
        "reason": (
            "Simulated evidence passed simulation checks and no simulated conflict remains. "
            "This does not authorize real-world action."
            if simulation_evidence_eligible
            else "Simulation evidence is unusable or a simulated conflict remains unresolved."
        ),
        "service_reasons_not_usable": ds.get("reasons_not_usable", []),
        "service_audit": evidence.get("audit", {}),
        "workflow_audit_log_path": str(audit_log.path) if audit_log.path else None,
    }
    audit_log.log(
        run_id,
        "river_workflow_completed",
        scenario=scenario,
        service_evidence_id=evidence.get("evidence_id"),
        current_evidence_usable=service_current_evidence_usable,
        simulation_evidence_eligible=simulation_evidence_eligible,
        real_world_authorization=real_world_authorization,
        reverification_outcome=reverification["outcome"],
    )
    result["workflow_audit_events_written"] = audit_log.events_written
    result["workflow_audit_write_errors"] = list(audit_log.write_errors)
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="Run River Agent with simulated evidence.")
    parser.add_argument("--scenario", choices=SCENARIOS, default="conflict-resolves")
    parser.add_argument("--config", default=None, help="Optional river_agent.json path")
    parser.add_argument("--audit", default="outputs/simulated_river_workflow_audit.jsonl")
    parser.add_argument("--as-of", default="2026-10-10T12:00:00+00:00",
                        help="Timezone-aware replay time for the simulated dataset")
    args = parser.parse_args()

    audit_path = Path(args.audit)
    audit_path.parent.mkdir(parents=True, exist_ok=True)
    audit = AuditLog(audit_path)
    service = (
        RiverEvidenceService.from_file(args.config)
        if args.config
        else RiverEvidenceService.from_file()
    )
    try:
        as_of = datetime.fromisoformat(args.as_of)
    except ValueError as exc:
        parser.error(f"--as-of must be a timezone-aware ISO 8601 timestamp: {exc}")
    if as_of.tzinfo is None or as_of.utcoffset() is None:
        parser.error("--as-of must include a timezone offset, e.g. +00:00")
    result = run_simulated_scenario(args.scenario, service, audit, as_of=as_of)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    return 0 if not result["workflow_audit_write_errors"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
