
import pytest

from src.capability_control import (
    DisasterState,
    is_permitted,
    permitted_capabilities,
)


def test_emergency_capability_is_not_permitted_in_recovery():
    capability = "request_human_emergency_authorization"

    assert is_permitted(DisasterState.EMERGENCY, capability)
    assert not is_permitted(DisasterState.RECOVERY, capability)


def test_recovery_revokes_evacuation_proposal_capability():
    capability = "propose_evacuation_plan"

    assert is_permitted(DisasterState.EMERGENCY, capability)
    assert not is_permitted(DisasterState.RECOVERY, capability)


def test_recovery_retains_read_and_reverification_capabilities():
    capabilities = permitted_capabilities(DisasterState.RECOVERY)

    assert "read_weather" in capabilities
    assert "read_river" in capabilities
    assert "request_evidence_reverification" in capabilities
    assert "prepare_recovery_assessment" in capabilities


def test_unknown_state_fails_closed():
    with pytest.raises(ValueError, match="Unknown disaster state"):
        permitted_capabilities("UNKNOWN")


def test_unlisted_capability_is_denied():
    assert not is_permitted(DisasterState.EMERGENCY, "execute_evacuation")


from src.capability_control import authorize_capability


def test_authorization_gate_allows_permitted_capability():
    result = authorize_capability(
        "ALERT", "request_evidence_reverification", "river_agent"
    )

    assert result["allowed"] is True
    assert result["reason"] == "CAPABILITY_PERMITTED"


def test_authorization_gate_denies_revoked_capability():
    result = authorize_capability(
        "RECOVERY", "request_human_emergency_authorization", "safety_agent"
    )

    assert result["allowed"] is False
    assert result["reason"] == "CAPABILITY_DENIED"


def test_authorization_gate_denies_unknown_state():
    result = authorize_capability(
        "UNKNOWN", "read_river", "river_agent"
    )

    assert result["allowed"] is False
    assert result["reason"] == "UNKNOWN_DISASTER_STATE"


def test_authorization_gate_denies_blank_agent_id():
    result = authorize_capability("NORMAL", "read_river", " ")

    assert result["allowed"] is False
    assert result["reason"] == "INVALID_AGENT_ID"


import json

from src.audit_log import AuditLog
from src.capability_control import authorize_and_audit


def test_authorize_and_audit_records_allowed_decision(tmp_path):
    log_path = tmp_path / "capability_audit.jsonl"
    audit = AuditLog(log_path, component="capability_control")

    result = authorize_and_audit(
        "ALERT",
        "request_evidence_reverification",
        "river_agent",
        audit,
        "test-run-1",
    )

    assert result["allowed"] is True
    assert result["audit_written"] is True

    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["run_id"] == "test-run-1"
    assert entry["event"] == "capability_authorization"
    assert entry["details"]["allowed"] is True
    assert entry["details"]["capability"] == "request_evidence_reverification"


def test_authorize_and_audit_records_denied_decision(tmp_path):
    log_path = tmp_path / "capability_audit.jsonl"
    audit = AuditLog(log_path, component="capability_control")

    result = authorize_and_audit(
        "RECOVERY",
        "request_human_emergency_authorization",
        "safety_agent",
        audit,
        "test-run-2",
    )

    assert result["allowed"] is False
    assert result["audit_written"] is True

    entry = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
    assert entry["details"]["allowed"] is False
    assert entry["details"]["reason"] == "CAPABILITY_DENIED"


from src.capability_control import execute_if_authorized


def test_denied_capability_never_executes_operation(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    calls = []

    decision, result = execute_if_authorized(
        "RECOVERY",
        "request_human_emergency_authorization",
        "safety_agent",
        audit,
        "run-denied",
        lambda: calls.append("executed"),
    )

    assert decision["allowed"] is False
    assert result is None
    assert calls == []


def test_allowed_capability_executes_after_audit(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    calls = []

    decision, result = execute_if_authorized(
        "ALERT",
        "request_evidence_reverification",
        "river_agent",
        audit,
        "run-allowed",
        lambda: calls.append("executed") or "done",
    )

    assert decision["allowed"] is True
    assert decision["audit_written"] is True
    assert result == "done"
    assert calls == ["executed"]
