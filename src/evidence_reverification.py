
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


# --------------------------------------------------------------------------
# Actual re-check of conflicting readings (pure, deterministic)
# --------------------------------------------------------------------------

OUTCOME_RESOLVED = "RESOLVED"
OUTCOME_PERSISTS = "CONFLICT_PERSISTS"
OUTCOME_FAILED = "FAILED"

CHECK_CORROBORATES_PRIMARY = "CORROBORATES_PRIMARY"
CHECK_CORROBORATES_OTHER = "CORROBORATES_OTHER_SOURCE"
CHECK_AMBIGUOUS = "AMBIGUOUS"
CHECK_NO_INDEPENDENT_READING = "NO_INDEPENDENT_READING"
CHECK_PRIMARY_MISSING = "PRIMARY_MISSING_ON_RECHECK"
CHECK_PRIMARY_CHANGED = "PRIMARY_CHANGED_ON_RECHECK"


def reverify_conflicts(
    conflicts: list[dict[str, Any]],
    fresh_primary: dict[str, float],
    independent: dict[str, dict[str, float]],
    tolerance_m: float = 0.005,
    independent_errors: list[str] | None = None,
) -> dict[str, Any]:
    """Re-check each conflicting reading against fresh and independent data.

    conflicts       cross_source_check["conflict_details"] entries
                    ({observed_at, primary_m, other_m, other_source, ...})
    fresh_primary   {observed_at ISO: level} from re-obtaining the primary source
    independent     {source name: {observed_at ISO: level}} from sources that are
                    neither the primary nor the conflicting source

    A conflict counts as resolved only if (1) the primary source still reports
    the same value on re-check and (2) an independent source agrees with the
    primary value (within tolerance) and none agrees with the conflicting
    value. Nothing is averaged, chosen or corrected: this only decides whether
    the restriction caused by the conflict may be lifted.
    """
    independent_errors = list(independent_errors or [])
    result: dict[str, Any] = {
        "outcome": OUTCOME_FAILED,
        "conflicts_checked": len(conflicts),
        "conflicts_remaining": len(conflicts),
        "tolerance_m": tolerance_m,
        "independent_sources": sorted(independent),
        "independent_errors": independent_errors,
        "per_conflict": [],
        "reason": None,
    }
    if not conflicts:
        result.update(outcome=OUTCOME_RESOLVED, conflicts_remaining=0,
                      reason="No conflicting readings to re-check.")
        return result
    if not independent:
        result["reason"] = ("No independent source could be read"
                            + (f": {'; '.join(independent_errors)}" if independent_errors
                               else " (none configured)")
                            + ". The conflict cannot be re-checked.")
        return result

    remaining = 0
    for conflict in conflicts:
        moment = conflict["observed_at"]
        primary_value = conflict["primary_m"]
        other_value = conflict["other_m"]
        entry = {"observed_at": moment, "primary_m": primary_value, "other_m": other_value,
                 "other_source": conflict.get("other_source"), "fresh_primary_m": None,
                 "independent_values": {}, "status": None}
        fresh = fresh_primary.get(moment)
        entry["fresh_primary_m"] = fresh
        values = {name: readings[moment] for name, readings in independent.items()
                  if moment in readings}
        entry["independent_values"] = values
        if fresh is None:
            entry["status"] = CHECK_PRIMARY_MISSING
        elif abs(fresh - primary_value) > tolerance_m:
            entry["status"] = CHECK_PRIMARY_CHANGED
        elif not values:
            entry["status"] = CHECK_NO_INDEPENDENT_READING
        else:
            backs_primary = any(abs(v - primary_value) <= tolerance_m for v in values.values())
            backs_other = any(abs(v - other_value) <= tolerance_m for v in values.values())
            if backs_primary and not backs_other:
                entry["status"] = CHECK_CORROBORATES_PRIMARY
            elif backs_other and not backs_primary:
                entry["status"] = CHECK_CORROBORATES_OTHER
            else:
                entry["status"] = CHECK_AMBIGUOUS
        if entry["status"] != CHECK_CORROBORATES_PRIMARY:
            remaining += 1
        result["per_conflict"].append(entry)

    result["conflicts_remaining"] = remaining
    if remaining == 0:
        result["outcome"] = OUTCOME_RESOLVED
        result["reason"] = (f"All {len(conflicts)} conflicting reading(s) were confirmed by "
                            "re-checking the primary source and by an independent source.")
    else:
        statuses = sorted({e["status"] for e in result["per_conflict"]
                           if e["status"] != CHECK_CORROBORATES_PRIMARY})
        result["outcome"] = OUTCOME_PERSISTS
        result["reason"] = (f"{remaining} of {len(conflicts)} conflicting reading(s) could not "
                            f"be confirmed ({', '.join(statuses)}).")
    return result
