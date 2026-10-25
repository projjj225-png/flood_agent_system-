"""Authorization boundary: callers cannot grant themselves evidence eligibility.

The evidence gate computes eligibility from the evidence envelope and the
reverification block itself. A caller-supplied assessment, a forged
"RESOLVED" reverification or edited evidence can only lead to a denial.
Offline; evidence comes from the real service.
"""

import copy

import pytest

import src.river_agent_workflow as workflow
from src.audit_log import AuditLog, read_audit_log
from src.capability_control import (
    EVIDENCE_ASSESSMENT_MISMATCH,
    EVIDENCE_ASSESSMENT_REJECTED,
    EVIDENCE_INTEGRITY_FAILED,
    EVIDENCE_MALFORMED,
    EVIDENCE_NOT_ASSESSED,
    EVIDENCE_REVERIFICATION_INCONSISTENT,
    EVIDENCE_SIMULATED,
    EVIDENCE_STALE,
    EVIDENCE_STALE_AT_DECISION,
    EVIDENCE_UNRESOLVED_CONFLICT,
    INTERNAL_CAPABILITIES,
    READ_ONLY_CAPABILITIES,
    REAL_WORLD_CAPABILITIES,
    assess_evidence_for_authorization,
    authorize_with_evidence,
    authorize_with_evidence_and_audit,
)
from src.river_agent_workflow import run_river_workflow
from tests.nwdp_fixtures import ok_session
from tests.test_river_service import NOW, clean_live_session, make_service
from tests.test_river_workflow import write_source

FORGED = {"eligible": True, "reasons": []}
STATE = "EMERGENCY"  # every real-world capability is on this allowlist


def deny_reasons(evidence=None, assessment=None, reverification=None,
                 capability="request_human_emergency_authorization", now=NOW):
    """Decide at ``now`` (default: the test clock, i.e. right after the evidence was
    produced), so the time-of-use freshness check is not what denies."""
    d = authorize_with_evidence(STATE, capability, "agent", assessment, evidence=evidence,
                                reverification=reverification, now=now)
    assert d["allowed"] is False and d["human_approval_required"] is False
    assert d["reason"] == "EVIDENCE_INSUFFICIENT"
    return set(d["evidence_reasons"])


@pytest.fixture(scope="module")
def evidence_set(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("boundary")
    good = make_service(tmp, clean_live_session()).get_evidence("live")
    stale = make_service(tmp, ok_session()).get_evidence("live")  # real NWDP data: months old
    from datetime import datetime, timezone
    simulated = make_service(tmp).get_evidence(
        "simulated", as_of=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    other = write_source(tmp, "other.csv", 76.10)
    independent = write_source(tmp, "independent.csv", 74.95)
    conflict = make_service(tmp, clean_live_session()).get_evidence(
        "live", cross_check_paths=[other])
    # A genuine RESOLVED block, produced by the real workflow for the same data.
    run = run_river_workflow(make_service(tmp, clean_live_session()),
                             AuditLog(tmp / "wf.jsonl"), mode="live", disaster_state=STATE,
                             cross_check_paths=[other], independent_paths=[independent])
    assert run["reverification"]["outcome"] == "RESOLVED"
    return {"good": good, "stale": stale, "simulated": simulated, "conflict": conflict,
            "resolved": run["reverification"]}


# ---- forged assessments --------------------------------------------------------------------

@pytest.mark.parametrize("capability", sorted(REAL_WORLD_CAPABILITIES))
def test_forged_assessment_without_evidence_is_denied(capability):
    assert deny_reasons(assessment=FORGED, capability=capability) == {EVIDENCE_NOT_ASSESSED}


def test_copied_genuine_assessment_without_evidence_is_denied(evidence_set):
    genuine = assess_evidence_for_authorization(evidence_set["good"])
    assert genuine["eligible"] is True
    assert EVIDENCE_NOT_ASSESSED in deny_reasons(assessment=genuine)


def test_forged_assessment_cannot_override_stale_evidence(evidence_set):
    reasons = deny_reasons(evidence_set["stale"], FORGED)
    assert EVIDENCE_STALE in reasons


def test_genuine_assessment_of_other_evidence_cannot_be_reused(evidence_set):
    """An assessment for good evidence, presented with stale evidence."""
    genuine = assess_evidence_for_authorization(evidence_set["good"])
    reasons = deny_reasons(evidence_set["stale"], genuine)
    assert {EVIDENCE_STALE, EVIDENCE_ASSESSMENT_MISMATCH} <= reasons


def test_forged_assessment_cannot_override_simulated_evidence(evidence_set):
    assert EVIDENCE_SIMULATED in deny_reasons(evidence_set["simulated"], FORGED)


def test_forged_assessment_cannot_override_unresolved_conflict(evidence_set):
    assert EVIDENCE_UNRESOLVED_CONFLICT in deny_reasons(evidence_set["conflict"], FORGED)


def test_forged_assessment_does_not_bypass_the_state_gate():
    d = authorize_with_evidence("NORMAL", "propose_evacuation_plan", "agent", FORGED)
    assert d["allowed"] is False and d["reason"] == "CAPABILITY_DENIED"


# ---- malformed assessments ------------------------------------------------------------------

@pytest.mark.parametrize("assessment", ["eligible", ["eligible"], 1, True])
def test_malformed_assessment_is_rejected_even_with_valid_evidence(evidence_set, assessment):
    reasons = deny_reasons(evidence_set["good"], assessment)
    assert {EVIDENCE_ASSESSMENT_REJECTED, EVIDENCE_MALFORMED} <= reasons


@pytest.mark.parametrize("assessment", [
    {"eligible": False, "reasons": ["EVIDENCE_STALE"]},
    {"eligible": "yes", "reasons": []},
    {"eligible": True, "reasons": ["CALLER_SAYS_NO"]},
    {"eligible": True},
])
def test_supplied_assessment_can_only_deny(evidence_set, assessment):
    reasons = deny_reasons(evidence_set["good"], assessment)
    assert EVIDENCE_ASSESSMENT_REJECTED in reasons


def test_assessment_for_a_different_evidence_id_is_rejected(evidence_set):
    reasons = deny_reasons(evidence_set["good"], {**FORGED, "evidence_id": "0" * 64})
    assert reasons == {EVIDENCE_ASSESSMENT_MISMATCH}


# ---- forged reverification -------------------------------------------------------------------

def test_bare_resolved_label_does_not_clear_a_conflict(evidence_set):
    reasons = deny_reasons(evidence_set["conflict"],
                           reverification={"outcome": "RESOLVED", "status": "COMPLETED"})
    assert {EVIDENCE_UNRESOLVED_CONFLICT, EVIDENCE_REVERIFICATION_INCONSISTENT} <= reasons


@pytest.mark.parametrize("tamper", [
    lambda r: r.update(status="FAILED"),
    lambda r: r.update(conflicts_remaining=1),
    lambda r: r.update(independent_sources=[]),
    lambda r: r.update(per_conflict=[]),
    lambda r: r["per_conflict"][0].update(status="AMBIGUOUS"),
    lambda r: r["per_conflict"][0].update(other_m=99.0),            # describes other readings
    lambda r: r["per_conflict"][0].update(fresh_primary_m=75.5),     # primary changed
    lambda r: r["per_conflict"][0].update(independent_values={"x": 76.10}),  # backs other
    lambda r: r["per_conflict"][0].update(independent_values={}),
    lambda r: r["per_conflict"][0].update(observed_at="2020-01-01T00:00:00+00:00"),
], ids=["status", "remaining", "no-sources", "no-entries", "entry-status", "other-value",
        "primary-changed", "backs-other", "no-values", "wrong-time"])
def test_inconsistent_resolved_claim_does_not_clear(evidence_set, tamper):
    forged = copy.deepcopy(evidence_set["resolved"])
    tamper(forged)
    reasons = deny_reasons(evidence_set["conflict"], reverification=forged)
    assert EVIDENCE_UNRESOLVED_CONFLICT in reasons


def test_genuine_reverification_clears_the_conflict(evidence_set):
    d = authorize_with_evidence(STATE, "request_human_emergency_authorization", "agent",
                                evidence=evidence_set["conflict"],
                                reverification=evidence_set["resolved"], now=NOW)
    assert d["allowed"] is True and d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"


# ---- evidence integrity -------------------------------------------------------------------------

@pytest.mark.parametrize("edit", [
    lambda e: e["freshness"].update(status="FRESH", age_hours=0.1),
    lambda e: e.update(observed_at="2026-10-13T07:59:00+00:00"),
    lambda e: e.update(river_level_m=1.0),
    lambda e: e.pop("evidence_id"),
    lambda e: e.update(evidence_id=None),
], ids=["freshness", "observed-at", "level", "no-id", "null-id"])
def test_edited_evidence_fails_integrity(evidence_set, edit):
    edited = copy.deepcopy(evidence_set["good"])
    edit(edited)
    assert EVIDENCE_INTEGRITY_FAILED in deny_reasons(edited)


def test_stale_evidence_relabelled_fresh_is_caught(evidence_set):
    """The integrity check is what stops this: every field check would now pass."""
    edited = copy.deepcopy(evidence_set["stale"])
    edited["freshness"]["status"] = "FRESH"
    edited["data_quality"]["evidence_grade"] = "VERIFIED_LIVE"
    edited["data_quality"]["is_verified_live"] = True
    edited["data_quality"]["recent_quality"]["status"] = "ACCEPTABLE"
    edited["source_label"] = "LIVE_VERIFIED"
    # Two independent layers catch it: the edit, and the months-old observed_at.
    assert deny_reasons(edited) == {EVIDENCE_INTEGRITY_FAILED, EVIDENCE_STALE_AT_DECISION}


@pytest.mark.parametrize("evidence", ["evidence", [], 0, {}])
def test_non_evidence_is_not_assessed(evidence):
    assert EVIDENCE_NOT_ASSESSED in deny_reasons(evidence, FORGED)


# ---- valid evidence and compatibility ---------------------------------------------------------

def test_valid_evidence_is_permitted_only_pending_human_approval(evidence_set):
    genuine = assess_evidence_for_authorization(evidence_set["good"])
    for assessment in (None, genuine):  # with or without the legacy argument
        for cap in REAL_WORLD_CAPABILITIES:
            d = authorize_with_evidence(STATE, cap, "agent", assessment,
                                        evidence=evidence_set["good"], now=NOW)
            assert d["allowed"] is True
            assert d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"
            assert d["human_approval_required"] is True
            assert d["eligibility_source"] == "COMPUTED_BY_GATE"
            assert d["evidence_id"] == evidence_set["good"]["evidence_id"]


def test_legacy_positional_call_still_works_for_non_real_world():
    for cap in sorted(READ_ONLY_CAPABILITIES | INTERNAL_CAPABILITIES):
        d = authorize_with_evidence(STATE if cap != "prepare_recovery_assessment"
                                    else "RECOVERY", cap, "agent", None)
        assert d["allowed"] is True, cap


def test_audit_records_computed_eligibility_not_the_callers_claim(tmp_path, evidence_set):
    log = AuditLog(tmp_path / "a.jsonl")
    forged = {**FORGED, "evidence_id": "f" * 64}
    d = authorize_with_evidence_and_audit(STATE, "propose_evacuation_plan", "agent", forged,
                                          log, "run", evidence=evidence_set["stale"])
    assert d["allowed"] is False
    details = read_audit_log(log.path)[-1]["details"]
    assert details["eligibility_source"] == "COMPUTED_BY_GATE"
    assert details["caller_assessment_supplied"] is True
    assert details["evidence_id"] == evidence_set["stale"]["evidence_id"]  # not the forged id
    assert EVIDENCE_ASSESSMENT_MISMATCH in details["evidence_reasons"]


# ---- workflow --------------------------------------------------------------------------------------

def test_workflow_cannot_be_talked_into_eligibility(tmp_path, monkeypatch):
    """Even if the workflow's own assessment were replaced by a forged one, the gate
    recomputes eligibility from the (stale) evidence and denies."""
    monkeypatch.setattr(workflow, "assess_evidence_for_authorization",
                        lambda *a, **k: dict(FORGED))
    result = run_river_workflow(make_service(tmp_path, ok_session()),
                                AuditLog(tmp_path / "wf.jsonl"), mode="live",
                                disaster_state=STATE)
    for cap in REAL_WORLD_CAPABILITIES:
        d = next(x for x in result["capability_decisions"] if x["capability"] == cap)
        assert d["allowed"] is False and EVIDENCE_STALE in d["evidence_reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_legitimate_workflow_paths_unchanged(tmp_path):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()),
                                AuditLog(tmp_path / "wf.jsonl"), mode="live",
                                disaster_state=STATE)
    d = next(x for x in result["capability_decisions"]
             if x["capability"] == "request_human_emergency_authorization")
    assert d["allowed"] is True and d["eligibility_source"] == "COMPUTED_BY_GATE"
    assert result["real_world_authorization"] == "HUMAN_AUTHORIZATION_REQUIRED"
    events = read_audit_log(tmp_path / "wf.jsonl")
    auth = [e for e in events if e["event"] == "capability_authorization"
            and e["details"]["capability_class"] == "REAL_WORLD"]
    assert auth and all(e["details"]["eligibility_source"] == "COMPUTED_BY_GATE" for e in auth)


def test_simulated_resolved_scenario_still_only_simulated(tmp_path):
    result = workflow.run_simulated_scenario(
        "conflict-resolves", make_service(tmp_path), AuditLog(tmp_path / "wf.jsonl"),
        disaster_state="ALERT", as_of=__import__("datetime").datetime(
            2026, 10, 10, 12, 0, tzinfo=__import__("datetime").timezone.utc))
    assert result["reverification"]["outcome"] == "RESOLVED"
    assert result["simulation_permission"] == "SIMULATION_ALLOWED"
    assert result["evidence_assessment"]["reverification_problem"] is None
    assert result["real_world_authorization"] == "RESTRICTED"


# ---- time-of-use freshness ------------------------------------------------------------------
# freshness.status is computed when the evidence is produced. Evidence that is held,
# cached or replayed must be re-checked when the decision is made.

def test_evidence_that_aged_after_production_is_denied(evidence_set):
    from datetime import timedelta
    good = evidence_set["good"]  # observed 07:30Z, max_age 3 h, produced at NOW (08:00Z)
    assert deny_reasons(good, now=NOW + timedelta(hours=3)) == {EVIDENCE_STALE_AT_DECISION}
    d = authorize_with_evidence(STATE, "propose_evacuation_plan", "agent", evidence=good,
                                now=NOW + timedelta(hours=2, minutes=29))
    assert d["allowed"] is True  # still inside the 3 h window


def test_default_decision_time_is_the_real_clock(evidence_set):
    """Without ``now`` the gate uses the current time; the fixture's reading is days old."""
    d = authorize_with_evidence(STATE, "propose_evacuation_plan", "agent",
                                evidence=evidence_set["good"])
    assert d["allowed"] is False and EVIDENCE_STALE_AT_DECISION in d["evidence_reasons"]


def test_reading_from_the_future_is_denied(evidence_set):
    from datetime import timedelta
    assert EVIDENCE_STALE_AT_DECISION in deny_reasons(evidence_set["good"],
                                                      now=NOW - timedelta(hours=1))


@pytest.mark.parametrize("edit", [
    lambda e: e["freshness"].pop("max_age_hours"),
    lambda e: e.update(observed_at="not a time"),
    lambda e: e.update(observed_at="2026-10-13T07:30:00"),  # naive
], ids=["no-max-age", "bad-time", "naive-time"])
def test_unverifiable_age_is_denied_even_if_resealed(evidence_set, edit):
    from src.river_service import compute_evidence_id
    edited = copy.deepcopy(evidence_set["good"])
    edit(edited)
    edited["evidence_id"] = compute_evidence_id(edited)
    assert EVIDENCE_STALE_AT_DECISION in deny_reasons(edited)


def test_audited_entry_point_also_checks_time_of_use(tmp_path, evidence_set):
    from datetime import timedelta
    log = AuditLog(tmp_path / "a.jsonl")
    d = authorize_with_evidence_and_audit(STATE, "propose_evacuation_plan", "agent", None, log,
                                          "run", evidence=evidence_set["good"],
                                          now=NOW + timedelta(days=1))
    assert d["allowed"] is False
    assert EVIDENCE_STALE_AT_DECISION in read_audit_log(log.path)[-1]["details"][
        "evidence_reasons"]


# ---- real-world actions are never executed ---------------------------------------------------

@pytest.mark.parametrize("capability", sorted(REAL_WORLD_CAPABILITIES))
def test_execute_if_authorized_never_runs_real_world_capabilities(tmp_path, capability):
    from src.capability_control import execute_if_authorized
    log = AuditLog(tmp_path / "a.jsonl")
    ran = []
    decision, outcome = execute_if_authorized(STATE, capability, "agent", log, "run",
                                              lambda: ran.append(capability))
    assert ran == [] and outcome is None
    assert decision["allowed"] is False and decision["reason"] == "EXECUTION_NOT_PERMITTED"
    entry = read_audit_log(log.path)[-1]["details"]
    assert entry["allowed"] is False and entry["reason"] == "EXECUTION_NOT_PERMITTED"


def test_execute_if_authorized_keeps_state_denial_reason(tmp_path):
    from src.capability_control import execute_if_authorized
    decision, _ = execute_if_authorized("RECOVERY", "propose_evacuation_plan", "agent",
                                        AuditLog(tmp_path / "a.jsonl"), "run", lambda: 1)
    assert decision["reason"] == "CAPABILITY_DENIED"  # state gate still answers first


# ---- audit failure late in the workflow ---------------------------------------------------------

class FailsOnEvent(AuditLog):
    def __init__(self, path, event):
        super().__init__(path, component="river_workflow")
        self.failing_event = event

    def log(self, run_id, event, **details):
        if event == self.failing_event:
            self.write_errors.append(f"{event}: simulated write failure")
            return False
        return super().log(run_id, event, **details)


@pytest.mark.parametrize("event", ["risk_assessment_completed", "river_workflow_completed"])
def test_late_audit_failure_revokes_every_real_world_decision(tmp_path, event):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()),
                                FailsOnEvent(tmp_path / "wf.jsonl", event), mode="live",
                                disaster_state=STATE)
    assert result["real_world_authorization"] == "RESTRICTED"
    for d in result["capability_decisions"]:
        if d["capability_class"] == "REAL_WORLD":
            assert d["allowed"] is False and d["reason"] == "AUDIT_TRAIL_INCOMPLETE"
            assert d["human_approval_required"] is False
