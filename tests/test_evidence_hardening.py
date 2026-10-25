"""Regression tests: malformed, incomplete or mislabelled evidence fails closed.

Covers the evidence gate (capability_control), the workflow, the risk engine
and audit-log redaction. Every test starts from evidence that the real
service produced and that IS eligible, then breaks one thing, so a denial
can only come from that change. Offline.
"""

import copy
import json
from datetime import datetime, timezone

import pytest

import src.river_agent_workflow as workflow
from src.audit_log import REDACTED, AuditLog, read_audit_log, redact
from src.capability_control import (
    EVIDENCE_MALFORMED,
    EVIDENCE_NOT_ASSESSED,
    EVIDENCE_SIMULATED,
    EVIDENCE_UNRESOLVED_CONFLICT,
    REAL_WORLD_CAPABILITIES,
    assess_evidence_for_authorization,
    authorize_with_evidence,
    authorize_with_evidence_and_audit,
    execute_if_authorized,
)
from src.risk_assessment import CRITICAL, INDETERMINATE, assess_flood_risk, load_risk_config
from src.river_agent_workflow import run_river_workflow
from src.river_service import compute_evidence_id
from tests.test_river_service import NOW, clean_live_session, make_service

CONFIG = load_risk_config()


@pytest.fixture(scope="module")
def good_evidence(tmp_path_factory):
    """Verified, fresh, conflict-free live evidence from the real service."""
    tmp = tmp_path_factory.mktemp("good")
    evidence = make_service(tmp, clean_live_session()).get_evidence("live")
    assert assess_evidence_for_authorization(evidence)["eligible"] is True
    return evidence


def broken(evidence, path, value, delete=False, reseal=False):
    """Deep copy with evidence[path...] set to value (or deleted).

    reseal=True recomputes evidence_id, as if the service had produced this
    shape, so the test exercises the field check rather than the integrity check.
    """
    copy_ = copy.deepcopy(evidence)
    target = copy_
    for key in path[:-1]:
        target = target[key]
    if delete:
        target.pop(path[-1], None)
    else:
        target[path[-1]] = value
    if reseal:
        copy_["evidence_id"] = compute_evidence_id(copy_)
    return copy_


def audit(tmp_path, name="wf.jsonl"):
    return AuditLog(tmp_path / name, component="river_workflow")


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


# ---- the gate: wrong types --------------------------------------------------------------

WRONG_TYPES = [
    (("data_quality",), 5), (("data_quality",), "VERIFIED"), (("data_quality",), []),
    (("freshness",), "FRESH"), (("freshness",), 3),
    (("fallback",), "no"), (("fallback", "used"), "false"), (("fallback", "used"), 1),
    (("cross_source_check",), ["CONFLICT"]),
    (("cross_source_check", "conflicts_affecting_current_evidence"), "0"),
    (("cross_source_check", "conflicts_affecting_current_evidence"), -1),
    (("cross_source_check", "conflicts_affecting_current_evidence"), 0.0),
    (("cross_source_check", "conflicts_affecting_current_evidence"), False),
    (("data_quality", "recent_quality"), "ACCEPTABLE"),
    (("data_quality", "recent_quality", "status"), ["ACCEPTABLE"]),
    (("data_quality", "evidence_grade"), {"grade": "VERIFIED_LIVE"}),
    (("freshness", "status"), True),
    (("status",), 200), (("source_label",), ["LIVE_VERIFIED"]),
]


@pytest.mark.parametrize("path,value", WRONG_TYPES, ids=lambda x: str(x))
def test_wrong_types_fail_closed(good_evidence, path, value):
    evidence = broken(good_evidence, path, value)
    gate = assess_evidence_for_authorization(evidence)  # must not raise
    assert gate["eligible"] is False
    assert EVIDENCE_MALFORMED in gate["reasons"]
    assert any(".".join(path[:2]) in p or path[0] in p for p in gate["input_problems"])
    for cap in REAL_WORLD_CAPABILITIES:
        d = authorize_with_evidence("EMERGENCY", cap, "river_agent", gate, evidence=evidence)
        assert d["allowed"] is False and d["reason"] == "EVIDENCE_INSUFFICIENT"
    # Also when resealed: the field check denies on its own, not only the integrity check.
    resealed = broken(good_evidence, path, value, reseal=True)
    assert EVIDENCE_MALFORMED in assess_evidence_for_authorization(resealed)["reasons"]
    assert authorize_with_evidence("EMERGENCY", "prepare_advisory", "a",
                                   evidence=resealed)["allowed"] is False


@pytest.mark.parametrize("path", [
    ("freshness",), ("fallback",), ("source_label",), ("status",),
    ("data_quality", "recent_quality"), ("data_quality", "is_verified_live"),
    ("freshness", "status"), ("data_quality", "recent_quality", "status"),
], ids=lambda x: ".".join(x))
def test_missing_fields_fail_closed(good_evidence, path):
    gate = assess_evidence_for_authorization(broken(good_evidence, path, None, delete=True))
    assert gate["eligible"] is False or path in (("fallback",), ("status",))
    if path not in (("fallback",), ("status",)):
        assert gate["reasons"]


def test_missing_optional_sections_keep_their_meaning(good_evidence):
    """No fallback block means no fallback; no status means not DATA_UNAVAILABLE.
    Neither adds a permission: every other check still has to pass."""
    for path in (("fallback",), ("status",)):
        gate = assess_evidence_for_authorization(broken(good_evidence, path, None, delete=True,
                                                        reseal=True))
        assert gate["eligible"] is True and gate["input_problems"] == []


@pytest.mark.parametrize("path", [
    ("data_quality",), ("freshness",), ("data_quality", "recent_quality"),
    ("data_quality", "is_verified_live"), ("freshness", "status"), ("source_label",),
    ("data_quality", "recent_quality", "status"),
], ids=lambda x: ".".join(x))
def test_null_values_fail_closed(good_evidence, path):
    gate = assess_evidence_for_authorization(broken(good_evidence, path, None))
    assert gate["eligible"] is False


def test_missing_data_quality_is_not_assessed(good_evidence):
    gate = assess_evidence_for_authorization(broken(good_evidence, ("data_quality",), None,
                                                    delete=True))
    assert gate["reasons"] == [EVIDENCE_NOT_ASSESSED]


@pytest.mark.parametrize("evidence", [None, "evidence", 7, [], {}, {"data_quality": {}}])
def test_non_evidence_is_never_eligible(evidence):
    assert assess_evidence_for_authorization(evidence)["eligible"] is False


def test_malformed_conflict_count_is_an_unresolved_conflict(good_evidence):
    evidence = broken(good_evidence, ("cross_source_check",
                                      "conflicts_affecting_current_evidence"), "2")
    gate = assess_evidence_for_authorization(evidence, {"outcome": "RESOLVED"})
    # Even a RESOLVED reverification cannot clear a count that cannot be read.
    assert EVIDENCE_UNRESOLVED_CONFLICT in gate["reasons"]
    assert gate["conflicts_affecting_current_evidence"] is None


def test_conflict_status_without_count_is_unresolved(good_evidence):
    evidence = broken(good_evidence, ("cross_source_check",), {"status": "CONFLICT"})
    gate = assess_evidence_for_authorization(evidence)
    assert {EVIDENCE_UNRESOLVED_CONFLICT, EVIDENCE_MALFORMED} <= set(gate["reasons"])


@pytest.mark.parametrize("reverification", ["RESOLVED", ["RESOLVED"], 1])
def test_malformed_reverification_cannot_clear_a_conflict(good_evidence, reverification):
    evidence = broken(good_evidence, ("cross_source_check",
                                      "conflicts_affecting_current_evidence"), 1)
    gate = assess_evidence_for_authorization(evidence, reverification)
    assert {EVIDENCE_UNRESOLVED_CONFLICT, EVIDENCE_MALFORMED} <= set(gate["reasons"])


def test_problem_reports_carry_no_values(good_evidence):
    secret = "https://user:hunter2@example.org/?token=abc123"
    gate = assess_evidence_for_authorization(broken(good_evidence, ("freshness",), secret))
    text = json.dumps(gate)
    assert "hunter2" not in text and "abc123" not in text
    assert gate["input_problems"] == ["freshness: expected object, got str"]
    assert gate["integrity_problem"].startswith("evidence_id: does not match")


# ---- the gate: simulated evidence is never treated as real ---------------------------------

@pytest.mark.parametrize("path,value", [
    (("data_quality", "evidence_grade"), "SIMULATED"),
    (("data_quality", "is_simulated"), True),
    (("data_quality", "is_simulated"), "yes"),
    (("data_quality", "source_types_present"), ["live", "simulated"]),
    (("source_type",), "simulated"),
    (("source_label",), "SIMULATED_TEST_DATA"),
], ids=lambda x: str(x))
def test_any_simulation_marker_denies_real_world(good_evidence, path, value):
    gate = assess_evidence_for_authorization(broken(good_evidence, path, value))
    assert EVIDENCE_SIMULATED in gate["reasons"] and gate["eligible"] is False


def test_simulated_evidence_relabelled_live_is_still_simulated(tmp_path):
    sim = make_service(tmp_path).get_evidence(
        "simulated", as_of=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    forged = copy.deepcopy(sim)
    forged["source_label"] = "LIVE_VERIFIED"
    forged["data_quality"]["is_verified_live"] = True
    gate = assess_evidence_for_authorization(forged)
    assert EVIDENCE_SIMULATED in gate["reasons"]
    risk = assess_flood_risk(forged, None, None, CONFIG)
    assert risk["assessment_mode"] == "simulated" and risk["risk_level"] == INDETERMINATE
    assert "EVIDENCE_SIMULATION_LABEL_MISMATCH" in \
        risk["evidence_quality"]["river_blocking_reasons"]
    assert "current_level_m" not in risk["supporting_evidence"]["river"]
    assert "SIMULATION_LABEL_MISMATCH" in risk["human_review_reasons"]


def test_live_evidence_with_simulated_grade_is_not_reported_as_real(good_evidence):
    forged = broken(good_evidence, ("data_quality", "evidence_grade"), "SIMULATED")
    risk = assess_flood_risk(forged, None, None, CONFIG)
    assert risk["assessment_mode"] == "simulated"
    assert risk["risk_level"] == INDETERMINATE
    assert "simulated_level_m" in risk["supporting_evidence"]["river"]


# ---- authorize_with_evidence: only a well-formed, clean assessment counts ---------------------

@pytest.mark.parametrize("assessment", [
    {"eligible": "yes", "reasons": []},
    {"eligible": 1, "reasons": []},
    {"eligible": True},                          # no reason list
    {"eligible": True, "reasons": ["EVIDENCE_STALE"]},
    {"eligible": True, "reasons": "none"},
    "eligible", ["eligible"], 1,
])
def test_forged_or_malformed_assessment_is_denied(assessment):
    d = authorize_with_evidence("EMERGENCY", "propose_evacuation_plan", "river_agent", assessment)
    assert d["allowed"] is False and d["reason"] == "EVIDENCE_INSUFFICIENT"
    assert d["human_approval_required"] is False


def test_non_dict_assessment_is_reported_malformed():
    d = authorize_with_evidence("EMERGENCY", "prepare_advisory", "river_agent", "eligible")
    assert EVIDENCE_MALFORMED in d["evidence_reasons"]


def test_clean_assessment_still_needs_a_human(good_evidence):
    gate = assess_evidence_for_authorization(good_evidence)
    d = authorize_with_evidence("EMERGENCY", "request_human_emergency_authorization", "a", gate,
                                evidence=good_evidence, now=NOW)
    assert d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL" and d["human_approval_required"]
    # The assessment alone, without the evidence it describes, is not enough.
    alone = authorize_with_evidence("EMERGENCY", "request_human_emergency_authorization", "a",
                                    gate)
    assert alone["allowed"] is False and EVIDENCE_NOT_ASSESSED in alone["evidence_reasons"]


class RaisingLog:
    def log(self, *args, **kwargs):
        raise RuntimeError("disk on fire")


class TruthyLog:
    def log(self, *args, **kwargs):
        return "ok"  # not an explicit True


@pytest.mark.parametrize("log", [RaisingLog(), TruthyLog()])
def test_unreliable_audit_logger_revokes_permission(good_evidence, log):
    gate = assess_evidence_for_authorization(good_evidence)
    d = authorize_with_evidence_and_audit("EMERGENCY", "read_river", "a", gate, log, "r")
    assert d["allowed"] is False and d["reason"] == "AUDIT_WRITE_FAILED"
    ran = []
    decision_, outcome = execute_if_authorized("ALERT", "request_evidence_reverification", "a",
                                               log, "r", lambda: ran.append(1))
    assert outcome is None and ran == [] and decision_["audit_written"] is False


# ---- workflow: malformed evidence from the service --------------------------------------------

def patched_service(tmp_path, evidence_or_exc):
    service = make_service(tmp_path, clean_live_session())

    def fake(*args, **kwargs):
        if isinstance(evidence_or_exc, Exception):
            raise evidence_or_exc
        return copy.deepcopy(evidence_or_exc)
    service.get_evidence = fake
    return service


@pytest.mark.parametrize("make", [
    lambda e: "not evidence",
    lambda e: [e],
    lambda e: broken(e, ("data_quality",), 5),
    lambda e: broken(e, ("decision_support",), "usable"),
    lambda e: broken(e, ("cross_source_check", "conflicts_affecting_current_evidence"), "2"),
    lambda e: broken(e, ("freshness",), None),
    lambda e: RuntimeError("upstream exploded"),
], ids=["string", "list", "dq-int", "ds-string", "count-string", "freshness-null", "raises"])
def test_workflow_fails_closed_on_malformed_evidence(tmp_path, good_evidence, make):
    log = audit(tmp_path)
    result = run_river_workflow(patched_service(tmp_path, make(good_evidence)), log,
                                mode="live", disaster_state="EMERGENCY")
    assert result["real_world_authorization"] == "RESTRICTED"
    assert result["river_level_m"] is None and result["current_evidence_usable"] is False
    for cap in REAL_WORLD_CAPABILITIES:
        assert decision(result, cap)["allowed"] is False
    assert result["risk_assessment"]["risk_level"] == INDETERMINATE
    assert result["risk_assessment"]["requires_human_review"] is True
    events = [e["event"] for e in read_audit_log(log.path)]
    assert events[-1] == "river_workflow_completed"


def test_workflow_records_evidence_and_conflict_failures(tmp_path, good_evidence):
    log = audit(tmp_path)
    run_river_workflow(patched_service(tmp_path, RuntimeError("upstream exploded")), log,
                       mode="live", disaster_state="EMERGENCY")
    bad = broken(good_evidence, ("cross_source_check", "conflicts_affecting_current_evidence"),
                 "2")
    result = run_river_workflow(patched_service(tmp_path, bad), log, mode="live",
                                disaster_state="EMERGENCY")
    entries = read_audit_log(log.path)
    failed = next(e for e in entries if e["event"] == "river_evidence_failed")
    assert failed["details"]["error"] == "RuntimeError: upstream exploded"
    assert any(e["event"] == "conflict_assessment_failed" for e in entries)
    assert result["conflict_assessment"]["status"] == "EVIDENCE_MALFORMED"
    assert result["evidence_input_problems"]
    assert result["reverification"]["outcome"] == "NOT_REQUIRED"  # nothing re-checked on junk


# ---- risk assessment cannot modify decisions or authorize ----------------------------------------

def strip(result):
    return [{k: v for k, v in d.items()} for d in result["capability_decisions"]]


def test_risk_engine_cannot_mutate_inputs_or_decisions(tmp_path, monkeypatch):
    baseline = run_river_workflow(make_service(tmp_path, clean_live_session()),
                                  audit(tmp_path, "a.jsonl"), mode="live", disaster_state="ALERT")
    real_engine = workflow.assess_flood_risk

    def tampering(evidence, weather, reverification, config, clock=None):
        result = real_engine(evidence, weather, reverification, config, clock=clock)
        evidence["data_quality"]["is_verified_live"] = False   # try to change the evidence
        evidence["freshness"]["status"] = "STALE"
        reverification["outcome"] = "RESOLVED"                  # and the reverification
        return {**result, "risk_level": CRITICAL}
    monkeypatch.setattr(workflow, "assess_flood_risk", tampering)
    result = run_river_workflow(make_service(tmp_path, clean_live_session()),
                                audit(tmp_path, "b.jsonl"), mode="live", disaster_state="ALERT")
    assert result["risk_assessment"]["risk_level"] == CRITICAL
    assert strip(result) == strip(baseline)
    assert result["evidence_assessment"] == baseline["evidence_assessment"]
    assert result["reverification"] == baseline["reverification"]
    assert result["real_world_authorization"] == baseline["real_world_authorization"] \
        == "HUMAN_AUTHORIZATION_REQUIRED"  # still only ever pending human approval


@pytest.mark.parametrize("tamper", [
    lambda r: {**r, "authorizes_actions": True},
    lambda r: {**r, "assigns_disaster_state": True},
    lambda r: {**r, "risk_level": "SAFE"},
    lambda r: "LOW",
    lambda r: {k: v for k, v in r.items() if k != "assessment_id"},
], ids=["authorizes", "sets-state", "bad-level", "not-dict", "missing-id"])
def test_engine_output_claiming_authority_is_rejected(tmp_path, monkeypatch, tamper):
    real_engine = workflow.assess_flood_risk
    monkeypatch.setattr(workflow, "assess_flood_risk",
                        lambda *a, **k: tamper(real_engine(*a, **k)))
    log = audit(tmp_path)
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), log, mode="live",
                                disaster_state="EMERGENCY")
    risk = result["risk_assessment"]
    assert risk["risk_level"] == INDETERMINATE and risk["authorizes_actions"] is False
    assert "R0_ENGINE_ERROR" in risk["rules_fired"]
    assert result["disaster_state"] == "EMERGENCY"
    events = [e["event"] for e in read_audit_log(log.path)]
    assert "risk_assessment_failed" in events and "risk_assessment_completed" in events


# ---- risk engine: stale / missing / conflicting / malformed are INDETERMINATE ---------------------

@pytest.mark.parametrize("path,value,reason", [
    (("freshness", "status"), "STALE", "EVIDENCE_STALE"),
    (("status",), "DATA_UNAVAILABLE", "EVIDENCE_UNAVAILABLE"),
    (("cross_source_check", "conflicts_affecting_current_evidence"), 1,
     "EVIDENCE_UNRESOLVED_CONFLICT"),
    (("data_quality", "recent_quality", "status"), "SUSPECT", "EVIDENCE_QUALITY_NOT_ACCEPTABLE"),
    (("fallback", "used"), True, "EVIDENCE_HISTORICAL_FALLBACK"),
    (("data_quality",), 5, "EVIDENCE_MALFORMED"),
    (("trend",), "RISING", "EVIDENCE_MALFORMED"),
    (("threshold_evaluation",), ["AT_OR_ABOVE_DANGER"], "EVIDENCE_MALFORMED"),
    (("cross_source_check", "conflicts_affecting_current_evidence"), "1", "EVIDENCE_MALFORMED"),
])
def test_unusable_river_evidence_is_indeterminate(good_evidence, path, value, reason):
    risk = assess_flood_risk(broken(good_evidence, path, value), None, None, CONFIG)
    assert risk["risk_level"] == INDETERMINATE
    assert reason in risk["evidence_quality"]["river_blocking_reasons"]
    assert risk["evidence_quality"]["confidence"] == "NONE"
    assert risk["requires_human_review"] is True
    assert "current_level_m" not in risk["supporting_evidence"]["river"]


def test_malformed_evidence_is_explained_and_flagged(good_evidence):
    risk = assess_flood_risk(broken(good_evidence, ("trend",), "RISING"), None, None, CONFIG)
    assert "RIVER_EVIDENCE_MALFORMED" in {u["code"] for u in risk["uncertainties"]}
    assert "INVALID_INPUT" in risk["human_review_reasons"]


# ---- audit log: failures recorded, secrets never written ------------------------------------------

def test_redact_secrets():
    data = {"api_key": "k1", "Authorization": "Bearer abc", "password": "p", "db_token": "t",
            "real_world_authorization": "RESTRICTED", "authorization_restricted": True,
            "url": "https://u:pw@host/api?resource_id=r1&token=t2&key=k3",
            "error": "401 for Bearer eyJhbGciOi.x.y", "nested": [{"secret": "s"}]}
    out = redact(data)
    assert out["api_key"] == out["Authorization"] == out["password"] == REDACTED
    assert out["db_token"] == out["nested"][0]["secret"] == REDACTED
    assert out["real_world_authorization"] == "RESTRICTED"
    assert out["authorization_restricted"] is True
    assert out["url"] == f"https://{REDACTED}@host/api?resource_id=r1&token={REDACTED}" \
                         f"&key={REDACTED}"
    assert "eyJhbGciOi" not in out["error"]
    assert data["api_key"] == "k1"  # input untouched
    assert len(redact("x" * 5000)) < 2100


def test_audit_file_never_contains_secrets(tmp_path):
    log = AuditLog(tmp_path / "a.jsonl")
    assert log.log("r", "live_fetch_failed", message="GET https://h/x?api_key=SEKRIT failed",
                   attempts=[{"url": "https://h", "params": {"token": "SEKRIT2"}}])
    text = (tmp_path / "a.jsonl").read_text(encoding="utf-8")
    assert "SEKRIT" not in text and REDACTED in text


def test_unserializable_detail_is_recorded_not_raised(tmp_path):
    class Hostile:
        def __str__(self):
            raise RuntimeError("cannot render")
    log = AuditLog(tmp_path / "a.jsonl")
    assert log.log("r", "evt?token=abc", value=Hostile()) is False
    assert log.write_errors and "RuntimeError" in log.write_errors[0]
    assert "abc" not in log.write_errors[0]


def test_audit_failure_in_workflow_restricts_and_is_reported(tmp_path):
    (tmp_path / "wf.jsonl").mkdir()
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    assert result["workflow_audit_write_errors"]
    assert result["real_world_authorization"] == "RESTRICTED"
    assert result["risk_assessment"]["audit_written"] is False
    assert "AUDIT_WRITE_FAILED" in result["risk_assessment"]["human_review_reasons"]
