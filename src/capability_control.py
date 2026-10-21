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
"""

from __future__ import annotations

from collections.abc import Callable
from enum import Enum
from typing import Any, TypeVar

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
        # Re-checking conflicting evidence is a read-only safety action; without
        # it a conflict detected in NORMAL could never be cleared.
        "request_evidence_reverification",
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

# Evidence-gate reason codes.
EVIDENCE_UNAVAILABLE = "EVIDENCE_UNAVAILABLE"
EVIDENCE_SIMULATED = "EVIDENCE_SIMULATED"
EVIDENCE_NOT_VERIFIED_LIVE = "EVIDENCE_NOT_VERIFIED_LIVE"
EVIDENCE_STALE = "EVIDENCE_STALE"
EVIDENCE_QUALITY_NOT_ACCEPTABLE = "EVIDENCE_QUALITY_NOT_ACCEPTABLE"
EVIDENCE_HISTORICAL_FALLBACK = "EVIDENCE_HISTORICAL_FALLBACK"
EVIDENCE_INCOMPLETE = "EVIDENCE_INCOMPLETE"
EVIDENCE_UNRESOLVED_CONFLICT = "EVIDENCE_UNRESOLVED_CONFLICT"
EVIDENCE_NOT_ASSESSED = "EVIDENCE_NOT_ASSESSED"


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


def authorize_and_audit(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    audit_log: Any,
    run_id: str,
) -> dict[str, object]:
    """Authorize a capability and record the decision in the existing audit log."""
    decision = authorize_capability(state, capability, agent_id)

    audit_written = audit_log.log(
        run_id,
        "capability_authorization",
        disaster_state=decision["state"],
        capability=capability,
        agent_id=agent_id,
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
    """Execute an operation only when authorized and the decision is audited."""
    decision = authorize_and_audit(
        state, capability, agent_id, audit_log, run_id
    )

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
    """
    if not isinstance(evidence, dict) or "data_quality" not in evidence:
        return {"eligible": False, "reasons": [EVIDENCE_NOT_ASSESSED],
                "evidence_id": None, "source_label": None}
    quality = evidence.get("data_quality") or {}
    reasons: list[str] = []
    if evidence.get("status") == "DATA_UNAVAILABLE":
        reasons.append(EVIDENCE_UNAVAILABLE)
    if (evidence.get("source_label") == "SIMULATED_TEST_DATA"
            or quality.get("evidence_grade") == "SIMULATED" or quality.get("is_simulated")):
        reasons.append(EVIDENCE_SIMULATED)
    if quality.get("is_verified_live") is not True or evidence.get("source_label") != "LIVE_VERIFIED":
        reasons.append(EVIDENCE_NOT_VERIFIED_LIVE)
    if (evidence.get("freshness") or {}).get("status") != "FRESH":
        reasons.append(EVIDENCE_STALE)
    if (quality.get("recent_quality") or {}).get("status") != "ACCEPTABLE":
        reasons.append(EVIDENCE_QUALITY_NOT_ACCEPTABLE)
    if ((quality.get("recent_quality") or {}).get("completeness") or {}).get("status") \
            == "INCOMPLETE":
        reasons.append(EVIDENCE_INCOMPLETE)
    if (evidence.get("fallback") or {}).get("used"):
        reasons.append(EVIDENCE_HISTORICAL_FALLBACK)
    conflicts = (evidence.get("cross_source_check") or {}).get(
        "conflicts_affecting_current_evidence", 0) or 0
    if conflicts and (reverification or {}).get("outcome") != "RESOLVED":
        reasons.append(EVIDENCE_UNRESOLVED_CONFLICT)
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "evidence_id": evidence.get("evidence_id"),
        "source_label": evidence.get("source_label"),
        "evidence_grade": quality.get("evidence_grade"),
        "freshness": (evidence.get("freshness") or {}).get("status"),
        "recent_quality": (quality.get("recent_quality") or {}).get("status"),
        "conflicts_affecting_current_evidence": conflicts,
        "reverification_outcome": (reverification or {}).get("outcome"),
    }


def authorize_with_evidence(
    state: DisasterState | str,
    capability: str,
    agent_id: str,
    evidence_assessment: dict[str, Any] | None,
) -> dict[str, object]:
    """State gate, then evidence gate for real-world capabilities. Never executes."""
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
    assessment = evidence_assessment or {"eligible": False, "reasons": [EVIDENCE_NOT_ASSESSED]}
    if not assessment.get("eligible"):
        decision.update(allowed=False, reason="EVIDENCE_INSUFFICIENT",
                        evidence_reasons=list(assessment.get("reasons") or
                                              [EVIDENCE_NOT_ASSESSED]))
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
) -> dict[str, object]:
    """Evidence-aware authorization, audited. Unaudited permissions are revoked."""
    decision = authorize_with_evidence(state, capability, agent_id, evidence_assessment)
    written = audit_log.log(
        run_id, "capability_authorization",
        disaster_state=decision["state"], capability=capability, agent_id=agent_id,
        allowed=decision["allowed"], reason=decision["reason"],
        capability_class=decision["capability_class"],
        evidence_reasons=decision["evidence_reasons"],
        human_approval_required=decision["human_approval_required"],
        evidence_id=(evidence_assessment or {}).get("evidence_id"),
    )
    if not written and decision["allowed"]:
        decision.update(allowed=False, reason="AUDIT_WRITE_FAILED",
                        human_approval_required=False)
    return {**decision, "audit_written": written}
