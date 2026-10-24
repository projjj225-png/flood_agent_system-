
import pytest
from src.audit_log import AuditLog
from src.evidence_reverification import record_reverification_request

from src.evidence_reverification import assess_conflict


def test_current_conflict_requires_reverification():
    result = assess_conflict(
        {"conflicts_affecting_current_evidence": 2},
        current_evidence_usable=True,
    )

    assert result["status"] == "REVERIFICATION_REQUIRED"
    assert result["authorization_restricted"] is True
    assert result["current_evidence_usable"] is False


def test_historical_conflict_does_not_trigger_current_reverification():
    result = assess_conflict(
        {"conflicts_affecting_current_evidence": 0, "conflicts": 3},
        current_evidence_usable=True,
    )

    assert result["status"] == "NOT_REQUIRED"
    assert result["authorization_restricted"] is False


def test_unusable_evidence_stays_restricted_without_conflict():
    result = assess_conflict(
        {"conflicts_affecting_current_evidence": 0},
        current_evidence_usable=False,
    )

    assert result["status"] == "EVIDENCE_UNUSABLE"
    assert result["authorization_restricted"] is True


@pytest.mark.parametrize("count", [-1, True, "2", None])
def test_invalid_conflict_count_is_rejected(count):
    with pytest.raises(ValueError):
        assess_conflict(
            {"conflicts_affecting_current_evidence": count},
            current_evidence_usable=True,
        )


def test_reverification_request_is_audited_as_pending(tmp_path):
    audit = AuditLog(tmp_path / "audit.jsonl")
    assessment = assess_conflict(
        {"conflicts_affecting_current_evidence": 1},
        current_evidence_usable=False,
    )

    result = record_reverification_request(
        assessment, audit, "run-1", "river_agent", "nwdp_cwc"
    )

    assert result["status"] == "REQUESTED"
    assert result["outcome"] == "PENDING"
    assert result["authorization_restricted"] is True
    assert result["audit_written"] is True


def test_no_conflict_does_not_create_reverification_request(tmp_path):
    audit_path = tmp_path / "audit.jsonl"
    audit = AuditLog(audit_path)
    assessment = assess_conflict(
        {"conflicts_affecting_current_evidence": 0},
        current_evidence_usable=True,
    )

    result = record_reverification_request(
        assessment, audit, "run-2", "river_agent", "nwdp_cwc"
    )

    assert result["status"] == "NOT_REQUESTED"
    assert not audit_path.exists()
