
"""Deterministic policy for conflict-triggered evidence reverification."""

from typing import Any


def assess_conflict(
    cross_source_check: dict[str, Any],
    current_evidence_usable: bool,
) -> dict[str, Any]:
    """Decide whether conflicting evidence requires reverification."""
    conflicts = cross_source_check.get(
        "conflicts_affecting_current_evidence", 0
    )

    if not isinstance(conflicts, int) or isinstance(conflicts, bool) or conflicts < 0:
        raise ValueError("conflicts_affecting_current_evidence must be a non-negative integer")

    if conflicts > 0:
        return {
            "status": "REVERIFICATION_REQUIRED",
            "trigger": "CURRENT_EVIDENCE_CONFLICT",
            "authorization_restricted": True,
            "current_evidence_usable": False,
            "reason": "Conflicting sources affect current evidence.",
        }

    if not current_evidence_usable:
        return {
            "status": "EVIDENCE_UNUSABLE",
            "trigger": None,
            "authorization_restricted": True,
            "current_evidence_usable": False,
            "reason": "Evidence is unusable; no current-source conflict was identified.",
        }

    return {
        "status": "NOT_REQUIRED",
        "trigger": None,
        "authorization_restricted": False,
        "current_evidence_usable": True,
        "reason": "No current-source conflict was identified and evidence is usable.",
    }


def record_reverification_request(
    assessment: dict[str, Any],
    audit_log: Any,
    run_id: str,
    requested_by: str,
    source_to_recheck: str,
) -> dict[str, Any]:
    """Record a reverification request without claiming it succeeded."""
    if assessment.get("status") != "REVERIFICATION_REQUIRED":
        return {
            "status": "NOT_REQUESTED",
            "reason": "Reverification is not required by the assessment.",
            "audit_written": False,
        }

    if not requested_by.strip() or not source_to_recheck.strip():
        raise ValueError("requested_by and source_to_recheck must not be blank")

    written = audit_log.log(
        run_id,
        "evidence_reverification_requested",
        requested_by=requested_by,
        source_to_recheck=source_to_recheck,
        trigger=assessment.get("trigger"),
        outcome="PENDING",
    )

    return {
        "status": "REQUESTED" if written else "REQUEST_AUDIT_FAILED",
        "reason": assessment.get("reason"),
        "source_to_recheck": source_to_recheck,
        "outcome": "PENDING" if written else "UNKNOWN",
        "audit_written": written,
        "authorization_restricted": True,
    }
