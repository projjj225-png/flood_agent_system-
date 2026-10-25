"""Deterministic disaster-state-aware agent capability control.

Two independent gates, both fail-closed:

1. State gate (``authorize_capability``): a capability must be on the
   explicit allowlist of the current disaster state.
2. Evidence gate (``authorize_with_evidence``): capabilities that can affect
   the real world additionally need evidence that is verified live, fresh,
   of acceptable quality, not simulated, not a fallback, and free of
   unresolved conflicts. Even then the decision only says that the action
   may be put to a HUMAN for approval; this module never executes or
   self-authorizes a real-world action.

Authorization boundary: the evidence gate decides eligibility ITSELF from the
evidence envelope (``evidence=``) and the reverification block
(``reverification=``), via ``assess_evidence_for_authorization``, which also
recomputes the envelope's ``evidence_id`` to detect tampering. A
caller-supplied ``evidence_assessment`` is accepted for compatibility but can
only add denials; it can never grant eligibility.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any, TypeVar

from src.evidence_reverification import CHECK_CORROBORATES_PRIMARY

T = TypeVar("T")


class DisasterState(str, Enum):
    NORMAL = "NORMAL"
    ALERT = "ALERT"
    EMERGENCY = "EMERGENCY"
    RECOVERY = "RECOVERY"


# Explicit allowlists: capabilities not listed are denied.
STATE_CAPABILITIES = {
    DisasterState.NORMAL: frozenset({
        "read_weather",
        "read_river",
        "read_traffic",
        "prepare_advisory",
    }),
    DisasterState.ALERT: frozenset({
        "read_weather",
        "read_river",
        "read_traffic",
        "prepare_advisory",
        "request_evidence_reverification",
        "propose_evacuation_plan",
    }),
    DisasterState.EMERGENCY: frozenset({
        "read_weather",
        "read_river",
        "read_traffic",
        "prepare_advisory",
        "request_evidence_reverification",
        "propose_evacuation_plan",
        "request_human_emergency_authorization",
    }),
    DisasterState.RECOVERY: frozenset({
        "read_weather",
        "read_river",
        "read_traffic",
        "prepare_advisory",
        "request_evidence_reverification",
        "prepare_recovery_assessment",
    }),
}

# Every capability must be in exactly one class (checked by tests).
READ_ONLY_CAPABILITIES = frozenset({"read_weather", "read_river", "read_traffic"})
# Internal work whose output stays inside the system and carries evidence labels.
INTERNAL_CAPABILITIES = frozenset({"request_evidence_reverification",
                                   "prepare_recovery_assessment"})
# Anything that could reach the public or trigger real-world action.
REAL_WORLD_CAPABILITIES = frozenset({"prepare_advisory", "propose_evacuation_plan",
                                     "request_human_emergency_authorization"})
# The only classes execute_if_authorized may ever run.
EXECUTABLE_CAPABILITIES = READ_ONLY_CAPABILITIES | INTERNAL_CAPABILITIES

# Evidence-gate reason codes.
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
EVIDENCE_SIMULATED = "EVIDENCE_SIMULATED"
EVIDENCE_NOT_VERIFIED_LIVE = "EVIDENCE_NOT_VERIFIED_LIVE"
EVIDENCE_STALE = "EVIDENCE_STALE"
EVIDENCE_QUALITY_NOT_ACCEPTABLE = "EVIDENCE_QUALITY_NOT_ACCEPTABLE"
EVIDENCE_HISTORICAL_FALLBACK = "EVIDENCE_HISTORICAL_FALLBACK"
EVIDENCE_UNRESOLVED_CONFLICT = "EVIDENCE_UNRESOLVED_CONFLICT"
EVIDENCE_NOT_ASSESSED = "EVIDENCE_NOT_ASSESSED"
EVIDENCE_MALFORMED = "EVIDENCE_MALFORMED"
EVIDENCE_INTEGRITY_FAILED = "EVIDENCE_INTEGRITY_FAILED"
EVIDENCE_ASSESSMENT_REJECTED = "EVIDENCE_ASSESSMENT_REJECTED"
EVIDENCE_ASSESSMENT_MISMATCH = "EVIDENCE_ASSESSMENT_MISMATCH"
EVIDENCE_REVERIFICATION_INCONSISTENT = "EVIDENCE_REVERIFICATION_INCONSISTENT"
EVIDENCE_STALE_AT_DECISION = "EVIDENCE_STALE_AT_DECISION"
# Readings timestamped further in the future than this are not accepted.
MAX_CLOCK_SKEW = timedelta(minutes=5)

# Evidence sections that, when present, must be JSON objects. Sections the gate
# does not read are still checked: a corrupted envelope never grants anything.
_EVIDENCE_SECTIONS = ("data_quality", "freshness", "fallback", "cross_source_check",
                      "decision_support", "trend", "threshold_evaluation", "live_fetch")
SIMULATED_SOURCE_LABEL = "SIMULATED_TEST_DATA"


def permitted_capabilities(state: DisasterState | str) -> frozenset[str]:
    """Return the explicit allowlist for a recognized disaster state."""
    try:
        normalized_state = DisasterState(state)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"Unknown disaster state: {state!r}") from exc

    return STATE_CAPABILITIES[normalized_state]


def is_permitted(state: DisasterState | str, capability: str) -> bool:
    """Fail closed: only explicitly listed capabilities are permitted."""
    return capability in permitted_capabilities(state)


def authorize_capability(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
) -> dict[str, object]:
    """Return a deterministic authorization decision; never execute actions."""
    base = {"allowed": False, "state": str(getattr(state, "value", state)),
            "capability": capability, "agent_id": agent_id}
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {**base, "reason": "INVALID_AGENT_ID"}
    if not isinstance(capability, str) or not capability.strip():
        return {**base, "reason": "INVALID_CAPABILITY"}

    try:
        normalized_state = DisasterState(state)
    except (ValueError, TypeError):
        return {**base, "reason": "UNKNOWN_DISASTER_STATE"}

    allowed = is_permitted(normalized_state, capability)

    return {
        "allowed": allowed,
        "state": normalized_state.value,
        "capability": capability,
        "agent_id": agent_id,
        "reason": "CAPABILITY_PERMITTED" if allowed else "CAPABILITY_DENIED",
    }


def _safe_log(audit_log: Any, run_id: str, event: str, **details: Any) -> bool:
    """Write an audit event; anything other than an explicit True counts as not written."""
    try:
        return audit_log.log(run_id, event, **details) is True
    except Exception:  # a broken logger must not crash authorization; it denies instead
        return False


def authorize_and_audit(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    audit_log: Any,
    run_id: str,
) -> dict[str, object]:
    """Authorize a capability and record the decision in the existing audit log."""
    return _audited(authorize_capability(state, capability, agent_id), audit_log, run_id)


def _audited(decision: dict[str, Any], audit_log: Any, run_id: str) -> dict[str, object]:
    audit_written = _safe_log(
        audit_log,
        run_id,
        "capability_authorization",
        disaster_state=decision["state"],
        capability=decision["capability"],
        agent_id=decision["agent_id"],
        allowed=decision["allowed"],
        reason=decision["reason"],
    )

    return {
        **decision,
        "audit_written": audit_written,
    }


def execute_if_authorized(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    audit_log: Any,
    run_id: str,
    operation: Callable[[], T],
) -> tuple[dict[str, object], T | None]:
    """Execute an operation only when authorized and the decision is audited.

    Only read-only and internal capabilities can ever be executed here. A
    real-world capability is refused (EXECUTION_NOT_PERMITTED) in every state:
    at most it may be put to a human, via authorize_with_evidence.
    """
    decision = authorize_capability(state, capability, agent_id)
    if decision["allowed"] and capability not in EXECUTABLE_CAPABILITIES:
        decision = {**decision, "allowed": False, "reason": "EXECUTION_NOT_PERMITTED"}
    decision = _audited(decision, audit_log, run_id)

    if not decision["allowed"] or not decision["audit_written"]:
        return decision, None

    return decision, operation()


# --------------------------------------------------------------------------
# Evidence gate
# --------------------------------------------------------------------------

def assess_evidence_for_authorization(
    evidence: dict[str, Any] | None,
    reverification: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Decide whether river evidence may support real-world capabilities.

    ``evidence`` is a RiverEvidenceService result. ``reverification`` is the
    workflow's reverification block; a current-evidence conflict is cleared
    only by an outcome of RESOLVED. Each failed condition adds a reason code;
    the evidence is eligible only when there are none.

    Malformed input fails closed: a section of the wrong type is treated as
    absent (so every check that needs it fails), is reported in
    ``input_problems`` (field paths and type names only, never values) and
    adds EVIDENCE_MALFORMED. This function never raises.
    """
    if not isinstance(evidence, dict) or "data_quality" not in evidence:
        return {"eligible": False, "reasons": [EVIDENCE_NOT_ASSESSED],
                "evidence_id": None, "source_label": None, "input_problems": []}
    try:
        return _assess_evidence(evidence, reverification)
    except Exception as exc:  # defensive: an unexpected shape must never permit anything
        return {"eligible": False, "reasons": [EVIDENCE_NOT_ASSESSED, EVIDENCE_MALFORMED],
                "evidence_id": None, "source_label": None,
                "input_problems": [f"evidence could not be assessed ({type(exc).__name__})"]}


def _section(container: dict[str, Any], key: str, path: str,
             problems: list[str]) -> dict[str, Any]:
    """container[key] if it is an object; {} (and a recorded problem) otherwise."""
    value = container.get(key)
    if isinstance(value, dict):
        return value
    if value is not None:
        problems.append(f"{path}: expected object, got {type(value).__name__}")
    return {}


def _text(container: dict[str, Any], key: str, path: str,
          problems: list[str]) -> str | None:
    value = container.get(key)
    if value is None or isinstance(value, str):
        return value
    problems.append(f"{path}: expected string, got {type(value).__name__}")
    return None


def _assess_evidence(evidence: dict[str, Any],
                     reverification: Any) -> dict[str, Any]:
    problems: list[str] = []
    sections = {key: _section(evidence, key, key, problems) for key in _EVIDENCE_SECTIONS}
    quality, freshness = sections["data_quality"], sections["freshness"]
    fallback, cross = sections["fallback"], sections["cross_source_check"]
    recent = _section(quality, "recent_quality", "data_quality.recent_quality", problems)
    status = _text(evidence, "status", "status", problems)
    label = _text(evidence, "source_label", "source_label", problems)
    grade = _text(quality, "evidence_grade", "data_quality.evidence_grade", problems)
    fresh_status = _text(freshness, "status", "freshness.status", problems)
    recent_status = _text(recent, "status", "data_quality.recent_quality.status", problems)
    if reverification is not None and not isinstance(reverification, dict):
        problems.append(f"reverification: expected object, got {type(reverification).__name__}")
        reverification = None
    outcome = (reverification or {}).get("outcome")

    reasons: list[str] = []
    if status == "DATA_UNAVAILABLE":
        reasons.append(EVIDENCE_UNAVAILABLE)
    # Any simulation marker anywhere makes the evidence simulated.
    types_present = quality.get("source_types_present")
    if (label == SIMULATED_SOURCE_LABEL or grade == "SIMULATED"
            or quality.get("is_simulated") not in (False, None)  # True or malformed
            or evidence.get("source_type") == "simulated"
            or isinstance(types_present, list) and "simulated" in types_present):
        reasons.append(EVIDENCE_SIMULATED)
    if quality.get("is_verified_live") is not True or label != "LIVE_VERIFIED":
        reasons.append(EVIDENCE_NOT_VERIFIED_LIVE)
    if fresh_status != "FRESH":
        reasons.append(EVIDENCE_STALE)
    if recent_status != "ACCEPTABLE":
        reasons.append(EVIDENCE_QUALITY_NOT_ACCEPTABLE)
    used = fallback.get("used", False)
    if not isinstance(used, bool):
        problems.append(f"fallback.used: expected boolean, got {type(used).__name__}")
    if used is not False:  # a malformed flag counts as a fallback
        reasons.append(EVIDENCE_HISTORICAL_FALLBACK)
    conflicts = cross.get("conflicts_affecting_current_evidence", 0)
    if conflicts is None:
        conflicts = 0
    if isinstance(conflicts, bool) or not isinstance(conflicts, int) or conflicts < 0:
        problems.append("cross_source_check.conflicts_affecting_current_evidence: expected "
                        f"non-negative integer, got {type(conflicts).__name__}")
        conflicts = None  # unknown: treated as an unresolved conflict
    elif conflicts == 0 and cross.get("status") == "CONFLICT" \
            and cross.get("conflicts_affecting_current_evidence") is None:
        conflicts = None  # status says conflict but the count is missing
        problems.append("cross_source_check: status CONFLICT without a conflict count")
    # A RESOLVED reverification clears a known conflict count, never an unknown one,
    # and only if the claim checks out against this evidence's own conflict details.
    reverification_problem = None
    if conflicts:
        reverification_problem = _reverification_problem(cross, conflicts, reverification)
    if conflicts is None or (conflicts > 0 and reverification_problem):
        reasons.append(EVIDENCE_UNRESOLVED_CONFLICT)
        if outcome == "RESOLVED" and reverification_problem:
            reasons.append(EVIDENCE_REVERIFICATION_INCONSISTENT)
    if problems:
        reasons.append(EVIDENCE_MALFORMED)
    # Kept apart from input_problems: the structure may be fine but the content changed.
    integrity = _integrity_problem(evidence)
    if integrity:
        reasons.append(EVIDENCE_INTEGRITY_FAILED)
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "evidence_id": evidence.get("evidence_id") if isinstance(
            evidence.get("evidence_id"), str) else None,
        "source_label": label,
        "evidence_grade": grade,
        "freshness": fresh_status,
        "recent_quality": recent_status,
        "conflicts_affecting_current_evidence": conflicts,
        "reverification_outcome": outcome if isinstance(outcome, str) else None,
        "input_problems": problems,
        "integrity_problem": integrity,
        "reverification_problem": reverification_problem,
    }


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _reverification_problem(cross: dict[str, Any], conflicts: int,
                            reverification: Any) -> str | None:
    """None if ``reverification`` verifiably resolves every current conflict in
    ``cross``; otherwise why not. The outcome label alone is never trusted: each
    current conflict must have a matching per-conflict entry whose values show the
    primary unchanged on re-check and an independent source backing the primary
    but not the other value (the rule in evidence_reverification.reverify_conflicts).
    """
    if not isinstance(reverification, dict) or reverification.get("outcome") != "RESOLVED":
        return "no RESOLVED reverification"
    if reverification.get("status") != "COMPLETED" or reverification.get(
            "conflicts_remaining") != 0:
        return "reverification is not a completed run with no conflicts remaining"
    if not reverification.get("independent_sources"):
        return "reverification names no independent source"
    tolerance = _number(cross.get("tolerance_m"))
    details = cross.get("conflict_details")
    if tolerance is None or not isinstance(details, list):
        return "evidence lacks the conflict details needed to verify the reverification"
    current = [d for d in details if isinstance(d, dict) and d.get("affects_current_evidence")
               is True]
    if len(current) != conflicts:
        return "evidence conflict details do not cover every current conflict"
    entries = reverification.get("per_conflict")
    if not isinstance(entries, list):
        return "reverification has no per-conflict results"
    by_time = {e.get("observed_at"): e for e in entries if isinstance(e, dict)}
    for detail in current:
        entry = by_time.get(detail.get("observed_at"))
        if entry is None or entry.get("status") != CHECK_CORROBORATES_PRIMARY:
            return f"conflict at {detail.get('observed_at')} is not shown as resolved"
        primary, other = _number(detail.get("primary_m")), _number(detail.get("other_m"))
        fresh = _number(entry.get("fresh_primary_m"))
        values = entry.get("independent_values")
        if primary is None or other is None or fresh is None or not isinstance(values, dict) \
                or _number(entry.get("primary_m")) != primary \
                or _number(entry.get("other_m")) != other:
            return f"reverification values for {detail.get('observed_at')} do not match"
        independent = [v for v in map(_number, values.values()) if v is not None]
        backs_primary = any(abs(v - primary) <= tolerance for v in independent)
        backs_other = any(abs(v - other) <= tolerance for v in independent)
        if abs(fresh - primary) > tolerance or not backs_primary or backs_other:
            return f"reverification values for {detail.get('observed_at')} do not resolve it"
    return None


def _integrity_problem(evidence: dict[str, Any]) -> str | None:
    """Recompute the service's evidence_id; a mismatch means the envelope changed after
    it was produced. This detects tampering; it does not authenticate the producer."""
    from src.river_service import compute_evidence_id  # lazy: keeps this module light

    claimed = evidence.get("evidence_id")
    if not isinstance(claimed, str) or not claimed:
        return "evidence_id: missing, so integrity cannot be verified"
    if compute_evidence_id(evidence) != claimed:
        return "evidence_id: does not match the evidence content (modified after production)"
    return None


def _supplied_assessment_denials(supplied: Any, computed: dict[str, Any]) -> list[str]:
    """Reasons a caller-supplied assessment adds. It can only ever deny."""
    if supplied is None:
        return []
    if not isinstance(supplied, dict):
        return [EVIDENCE_ASSESSMENT_REJECTED, EVIDENCE_MALFORMED]
    denials = []
    reasons = supplied.get("reasons")
    if supplied.get("eligible") is not True or reasons != []:
        denials.append(EVIDENCE_ASSESSMENT_REJECTED)
        if isinstance(reasons, list):
            denials += [r for r in reasons if isinstance(r, str)]
    if supplied.get("evidence_id") is not None and \
            supplied.get("evidence_id") != computed.get("evidence_id"):
        denials.append(EVIDENCE_ASSESSMENT_MISMATCH)
    return denials


def _stale_at_decision(evidence: Any, now: datetime) -> bool:
    """True unless the latest reading is still within max_age_hours at decision time.

    freshness.status was computed when the evidence was produced; evidence held
    or replayed later must be re-checked. Uses only fields covered by the
    evidence_id hash (observed_at, freshness.max_age_hours), never generated_at.
    """
    if not isinstance(evidence, dict) or not isinstance(evidence.get("freshness"), dict):
        return True
    max_age = _number(evidence["freshness"].get("max_age_hours"))
    try:
        observed = datetime.fromisoformat(evidence.get("observed_at"))
    except (TypeError, ValueError):
        return True
    if max_age is None or observed.tzinfo is None or now.tzinfo is None:
        return True
    age = now - observed
    return age > timedelta(hours=max_age) or age < -MAX_CLOCK_SKEW


def evidence_stale_at(evidence: Any, now: datetime) -> bool:
    """Public form of the gate's decision-time freshness rule (same check, same fields),
    for components such as the disaster state manager that must not duplicate it."""
    return _stale_at_decision(evidence, now)


def authorize_with_evidence(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    evidence_assessment: dict[str, Any] | None = None,
    *,
    evidence: dict[str, Any] | None = None,
    reverification: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """State gate, then evidence gate for real-world capabilities. Never executes.

    Eligibility for a real-world capability is computed here from ``evidence``
    and ``reverification``. Without evidence the answer is EVIDENCE_NOT_ASSESSED,
    whatever ``evidence_assessment`` says; that legacy argument can only add
    denials (an ineligible assessment, its reasons, or one for other evidence).
    Freshness is re-checked at decision time ``now`` (default: the current UTC
    time), so evidence that was fresh when produced but has aged is denied.
    """
    decision: dict[str, Any] = {**authorize_capability(state, capability, agent_id),
                                "capability_class": None, "evidence_reasons": [],
                                "human_approval_required": False}
    if not decision["allowed"]:
        return decision
    if capability in READ_ONLY_CAPABILITIES:
        decision["capability_class"] = "READ_ONLY"
        return decision
    if capability in INTERNAL_CAPABILITIES:
        decision["capability_class"] = "INTERNAL"
        return decision
    if capability not in REAL_WORLD_CAPABILITIES:
        decision.update(allowed=False, reason="CAPABILITY_UNCLASSIFIED")
        return decision
    decision["capability_class"] = "REAL_WORLD"
    computed = assess_evidence_for_authorization(evidence, reverification)
    decision["evidence_id"] = computed.get("evidence_id")
    decision["eligibility_source"] = "COMPUTED_BY_GATE"
    reasons = list(computed["reasons"])
    decided_at = now if isinstance(now, datetime) else datetime.now(timezone.utc)
    if EVIDENCE_NOT_ASSESSED not in reasons and _stale_at_decision(evidence, decided_at):
        reasons.append(EVIDENCE_STALE_AT_DECISION)
    for reason in _supplied_assessment_denials(evidence_assessment, computed):
        if reason not in reasons:
            reasons.append(reason)
    if computed["eligible"] is not True or reasons:
        decision.update(allowed=False, reason="EVIDENCE_INSUFFICIENT",
                        evidence_reasons=reasons or [EVIDENCE_NOT_ASSESSED])
        return decision
    decision.update(reason="PERMITTED_PENDING_HUMAN_APPROVAL", human_approval_required=True)
    return decision


def authorize_with_evidence_and_audit(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    evidence_assessment: dict[str, Any] | None,
    audit_log: Any,
    run_id: str,
    *,
    evidence: dict[str, Any] | None = None,
    reverification: dict[str, Any] | None = None,
    now: datetime | None = None,
) -> dict[str, object]:
    """Evidence-aware authorization, audited. Unaudited permissions are revoked."""
    decision = authorize_with_evidence(state, capability, agent_id, evidence_assessment,
                                       evidence=evidence, reverification=reverification,
                                       now=now)
    written = _safe_log(
        audit_log, run_id, "capability_authorization",
        disaster_state=decision["state"], capability=capability, agent_id=agent_id,
        allowed=decision["allowed"], reason=decision["reason"],
        capability_class=decision["capability_class"],
        evidence_reasons=decision["evidence_reasons"],
        human_approval_required=decision["human_approval_required"],
        evidence_id=decision.get("evidence_id"),
        eligibility_source=decision.get("eligibility_source"),
        caller_assessment_supplied=evidence_assessment is not None,
    )
    if not written and decision["allowed"]:
        decision.update(allowed=False, reason="AUDIT_WRITE_FAILED",
                        human_approval_required=False)
    return {**decision, "audit_written": written}
