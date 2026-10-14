
"""Deterministic disaster-state-aware agent capability control."""

from enum import Enum
from collections.abc import Callable
from typing import Any, TypeVar


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
    if not isinstance(agent_id, str) or not agent_id.strip():
        return {
            "allowed": False,
            "state": str(state),
            "capability": capability,
            "agent_id": agent_id,
            "reason": "INVALID_AGENT_ID",
        }

    try:
        normalized_state = DisasterState(state)
    except (ValueError, TypeError):
        return {
            "allowed": False,
            "state": str(state),
            "capability": capability,
            "agent_id": agent_id,
            "reason": "UNKNOWN_DISASTER_STATE",
        }

    allowed = is_permitted(normalized_state, capability)

    return {
        "allowed": allowed,
        "state": normalized_state.value,
        "capability": capability,
        "agent_id": agent_id,
        "reason": "CAPABILITY_PERMITTED" if allowed else "CAPABILITY_DENIED",
    }


from typing import Any, Callable


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


from collections.abc import Callable
from typing import TypeVar

T = TypeVar("T")


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
