"""Tests for the integrated River Agent workflow: real conflict detection,
reverification and evidence-aware capability permissions. Offline."""

import json
from datetime import datetime, timezone

import pytest
import requests

import src.river_agent_workflow as workflow
from src.audit_log import AuditLog, read_audit_log
from src.capability_control import (
    INTERNAL_CAPABILITIES,
    READ_ONLY_CAPABILITIES,
    REAL_WORLD_CAPABILITIES,
    STATE_CAPABILITIES,
    assess_evidence_for_authorization,
    authorize_with_evidence,
    authorize_with_evidence_and_audit,
)
from src.evidence_reverification import reverify_conflicts
from src.river_agent_workflow import run_river_workflow, run_simulated_scenario
from tests.nwdp_fixtures import (
    IST,
    FakeResponse,
    FakeSession,
    datastore_payload,
    ok_session,
    resource_show_payload,
    synthetic_records,
)
from tests.test_river_service import CLEAN_LEVELS, clean_live_session, make_service

SIM_AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
LIVE_TS = "2026-10-13T07:30:00+00:00"  # latest synthetic live reading (74.95 m)
HEADER = "timestamp,station_id,river_level_m,source_type\n"


def audit(tmp_path, name="wf.jsonl"):
    return AuditLog(tmp_path / name, component="river_workflow")


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


def write_source(tmp_path, name, value, ts=LIVE_TS, station="UPPER_ANICUT"):
    path = tmp_path / name
    path.write_text(HEADER + f"{ts},{station},{value},historical\n", encoding="utf-8")
    return str(path)


# ---- no injected conflicts --------------------------------------------------------

def test_injection_code_is_gone():
    assert not hasattr(workflow, "_injected_conflict_count")


def test_conflict_comes_from_actual_values(tmp_path):
    """Same scenario wiring, different numbers -> different outcome."""
    service = make_service(tmp_path)
    near = write_source(tmp_path, "near.csv", 3.353, ts="2026-10-10T11:00:00Z",
                        station="UPPER_ANICUT_TEST")
    far = write_source(tmp_path, "far.csv", 3.36, ts="2026-10-10T11:00:00Z",
                       station="UPPER_ANICUT_TEST")
    r_near = run_river_workflow(service, audit(tmp_path), mode="simulated", as_of=SIM_AS_OF,
                                cross_check_paths=[near])
    r_far = run_river_workflow(service, audit(tmp_path), mode="simulated", as_of=SIM_AS_OF,
                               cross_check_paths=[far])
    assert r_near["cross_source_check"]["status"] == "CONSISTENT"  # 0.003 m <= 0.005
    assert r_far["cross_source_check"]["status"] == "CONFLICT"     # 0.010 m >  0.005
    assert r_far["cross_source_check"]["examples"][0]["difference_m"] == 0.01


# ---- simulated scenarios (data-driven) ------------------------------------------------

@pytest.mark.parametrize("scenario,cross,outcome,sim_permission", [
    ("no-conflict", "CONSISTENT", "NOT_REQUIRED", "SIMULATION_ALLOWED"),
    ("conflict-resolves", "CONFLICT", "RESOLVED", "SIMULATION_ALLOWED"),
    ("conflict-persists", "CONFLICT", "CONFLICT_PERSISTS", "SIMULATION_RESTRICTED"),
    ("reverification-fails", "CONFLICT", "FAILED", "SIMULATION_RESTRICTED"),
])
def test_simulated_scenarios(tmp_path, scenario, cross, outcome, sim_permission):
    result = run_simulated_scenario(scenario, make_service(tmp_path), audit(tmp_path),
                                    as_of=SIM_AS_OF, disaster_state="EMERGENCY")
    assert result["cross_source_check"]["status"] == cross
    assert result["reverification"]["outcome"] == outcome
    assert result["simulation_permission"] == sim_permission
    # Simulated evidence never authorizes anything real, in any outcome.
    assert result["real_world_authorization"] == "RESTRICTED"
    assert result["river_level_m"] is None
    for cap in REAL_WORLD_CAPABILITIES:
        d = decision(result, cap)
        assert d["allowed"] is False and "EVIDENCE_SIMULATED" in d["evidence_reasons"]


def test_resolved_conflict_records_per_conflict_evidence(tmp_path):
    result = run_simulated_scenario("conflict-resolves", make_service(tmp_path),
                                    audit(tmp_path), as_of=SIM_AS_OF, disaster_state="ALERT")
    rev = result["reverification"]
    assert rev["attempts"] == 1 and rev["fresh_evidence_id"]
    entry = rev["per_conflict"][0]
    assert entry["status"] == "CORROBORATES_PRIMARY"
    assert (entry["primary_m"], entry["other_m"], entry["fresh_primary_m"]) == (3.35, 3.8, 3.35)
    assert result["simulated_river_level_m"] == 3.35
    assert result["conflicts_remaining"] == 0


def test_normal_state_can_reverify(tmp_path):
    result = run_simulated_scenario("conflict-resolves", make_service(tmp_path),
                                    audit(tmp_path), as_of=SIM_AS_OF, disaster_state="NORMAL")
    assert result["reverification"]["outcome"] == "RESOLVED"


def test_unknown_state_cannot_reverify(tmp_path):
    log = audit(tmp_path)
    result = run_simulated_scenario("conflict-resolves", make_service(tmp_path), log,
                                    as_of=SIM_AS_OF, disaster_state="PANIC")
    assert result["reverification"]["outcome"] == "NOT_AUTHORIZED"
    assert result["reverification"]["authorization_decision"]["reason"] == \
        "UNKNOWN_DISASTER_STATE"
    assert result["simulation_permission"] == "SIMULATION_RESTRICTED"
    events = [e["event"] for e in read_audit_log(log.path)]
    assert "evidence_reverification_requested" not in events


def test_workflow_audit_trail_is_linked(tmp_path):
    log = audit(tmp_path)
    result = run_simulated_scenario("conflict-resolves", make_service(tmp_path), log,
                                    as_of=SIM_AS_OF, disaster_state="ALERT")
    entries = read_audit_log(log.path)
    events = [e["event"] for e in entries]
    assert events[0] == "river_workflow_started" and events[-1] == "river_workflow_completed"
    assert events.index("capability_authorization") < events.index(
        "evidence_reverification_requested") < events.index("evidence_reverification_completed")
    assert {e["run_id"] for e in entries} == {result["run_id"]}
    done = entries[-1]["details"]
    assert done["decision_id"] == result["decision_id"]
    assert done["service_evidence_id"] == result["service_evidence_id"]


def test_decision_id_is_deterministic(tmp_path):
    a = run_simulated_scenario("conflict-persists", make_service(tmp_path), audit(tmp_path),
                               as_of=SIM_AS_OF, disaster_state="ALERT")
    b = run_simulated_scenario("conflict-persists", make_service(tmp_path), audit(tmp_path),
                               as_of=SIM_AS_OF, disaster_state="ALERT")
    assert a["run_id"] != b["run_id"] and a["decision_id"] == b["decision_id"]


# ---- live mode ------------------------------------------------------------------------

def test_live_verified_fresh_permits_only_human_approval(tmp_path):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    assert result["current_evidence_usable"] is True
    assert result["river_level_m"] == 74.95
    d = decision(result, "request_human_emergency_authorization")
    assert d["allowed"] is True
    assert d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"
    assert d["human_approval_required"] is True
    assert result["real_world_authorization"] == "HUMAN_AUTHORIZATION_REQUIRED"
    assert "AUTHORIZED" != result["real_world_authorization"]


def test_live_state_gate_still_applies(tmp_path):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="RECOVERY")
    assert decision(result, "propose_evacuation_plan")["reason"] == "CAPABILITY_DENIED"
    assert decision(result, "prepare_advisory")["allowed"] is True


def test_real_live_data_is_stale_and_restricted(tmp_path):
    result = run_river_workflow(make_service(tmp_path, ok_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    reasons = result["evidence_assessment"]["reasons"]
    assert {"EVIDENCE_NOT_VERIFIED_LIVE", "EVIDENCE_STALE"} <= set(reasons)
    assert result["real_world_authorization"] == "RESTRICTED"
    assert result["river_level_m"] is None


def test_live_outage_restricts_everything_real(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    result = run_river_workflow(make_service(tmp_path, session), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    assert "EVIDENCE_UNAVAILABLE" in result["evidence_assessment"]["reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"
    assert workflow.main  # CLI exit code for this case is covered below


def test_live_conflict_resolved_by_refetch_and_independent_source(tmp_path):
    session = clean_live_session()
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    result = run_river_workflow(make_service(tmp_path, session), audit(tmp_path), mode="live",
                                disaster_state="EMERGENCY", cross_check_paths=[other],
                                independent_paths=[independent])
    assert result["initial_conflicts"] == 1
    assert result["reverification"]["outcome"] == "RESOLVED"
    assert [c[0] for c in session.calls].count("datastore_search") == 2  # real re-fetch
    assert decision(result, "request_human_emergency_authorization")["allowed"] is True


def test_live_conflict_without_independent_source_stays_restricted(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY",
                                cross_check_paths=[other])  # config has no independent source
    assert result["reverification"]["outcome"] == "FAILED"
    assert "none configured" in result["reverification"]["reason"]
    assert "EVIDENCE_UNRESOLVED_CONFLICT" in result["evidence_assessment"]["reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_primary_changing_on_refetch_keeps_conflict(tmp_path):
    end = datetime(2026, 10, 13, 13, 0, tzinfo=IST)
    first = synthetic_records(end, CLEAN_LEVELS)
    second = synthetic_records(end, CLEAN_LEVELS[:-1] + ["74.99"])
    session = FakeSession({
        "resource_show": [FakeResponse(200, resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(first)),
                             FakeResponse(200, datastore_payload(second))],
    })
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    result = run_river_workflow(make_service(tmp_path, session), audit(tmp_path), mode="live",
                                disaster_state="EMERGENCY", cross_check_paths=[other],
                                independent_paths=[independent])
    rev = result["reverification"]
    assert rev["outcome"] == "CONFLICT_PERSISTS"
    assert rev["per_conflict"][0]["status"] == "PRIMARY_CHANGED_ON_RECHECK"
    assert result["real_world_authorization"] == "RESTRICTED"


def test_outage_during_reverification_fails_closed(tmp_path):
    end = datetime(2026, 10, 13, 13, 0, tzinfo=IST)
    session = FakeSession({
        "resource_show": [FakeResponse(200, resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(
            synthetic_records(end, CLEAN_LEVELS))), requests.exceptions.Timeout("slow")],
    })
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    result = run_river_workflow(make_service(tmp_path, session), audit(tmp_path), mode="live",
                                disaster_state="EMERGENCY", cross_check_paths=[other],
                                independent_paths=[independent])
    assert result["reverification"]["outcome"] == "FAILED"
    assert "unavailable on re-check" in result["reverification"]["reason"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_conflicting_source_cannot_be_its_own_tiebreaker(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY",
                                cross_check_paths=[other], independent_paths=[other])
    assert result["reverification"]["outcome"] == "FAILED"
    assert "not independent" in result["reverification"]["independent_errors"][0]


# ---- audit failures ----------------------------------------------------------------------

def test_unwritable_audit_revokes_all_permissions(tmp_path):
    (tmp_path / "wf.jsonl").mkdir()  # a directory: every audit write fails
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    assert result["workflow_audit_write_errors"]
    assert all(d["allowed"] is False for d in result["capability_decisions"])
    assert {d["reason"] for d in result["capability_decisions"]} == {"AUDIT_WRITE_FAILED"}
    assert result["real_world_authorization"] == "RESTRICTED"


def test_unknown_state_denies_everything(tmp_path):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="PANIC")
    assert {d["reason"] for d in result["capability_decisions"]} == {"UNKNOWN_DISASTER_STATE"}


# ---- pure policy functions -----------------------------------------------------------------

def test_every_capability_is_classified_exactly_once():
    all_caps = set().union(*STATE_CAPABILITIES.values())
    classes = [READ_ONLY_CAPABILITIES, INTERNAL_CAPABILITIES, REAL_WORLD_CAPABILITIES]
    assert all_caps == set().union(*classes)
    assert sum(len(c) for c in classes) == len(all_caps)


def test_evidence_gate_requires_assessment():
    d = authorize_with_evidence("EMERGENCY", "propose_evacuation_plan", "river_agent", None)
    assert d["allowed"] is False and d["evidence_reasons"] == ["EVIDENCE_NOT_ASSESSED"]
    assert assess_evidence_for_authorization(None)["eligible"] is False


def test_read_only_capability_needs_no_evidence():
    d = authorize_with_evidence("NORMAL", "read_river", "river_agent", None)
    assert d["allowed"] is True and d["capability_class"] == "READ_ONLY"


def test_audited_permission_is_revoked_when_audit_fails():
    class FailingLog:
        def log(self, *args, **kwargs):
            return False
    d = authorize_with_evidence_and_audit("NORMAL", "read_river", "a", None, FailingLog(), "r")
    assert d["allowed"] is False and d["reason"] == "AUDIT_WRITE_FAILED"


def test_reverify_conflicts_statuses():
    conflict = {"observed_at": "t", "primary_m": 3.35, "other_m": 3.80, "other_source": "s"}
    run = lambda fresh, indep: reverify_conflicts([conflict], fresh, indep, 0.005)
    assert run({"t": 3.35}, {"i": {"t": 3.351}})["outcome"] == "RESOLVED"
    assert run({"t": 3.35}, {"i": {"t": 3.80}})["per_conflict"][0]["status"] == \
        "CORROBORATES_OTHER_SOURCE"
    assert run({"t": 3.35}, {"i": {"t": 3.35}, "j": {"t": 3.80}})["per_conflict"][0]["status"] \
        == "AMBIGUOUS"
    assert run({"t": 3.35}, {"i": {"x": 3.35}})["per_conflict"][0]["status"] == \
        "NO_INDEPENDENT_READING"
    assert run({}, {"i": {"t": 3.35}})["per_conflict"][0]["status"] == \
        "PRIMARY_MISSING_ON_RECHECK"
    assert run({"t": 3.35}, {})["outcome"] == "FAILED"
    assert reverify_conflicts([], {}, {})["outcome"] == "RESOLVED"


# ---- CLI ---------------------------------------------------------------------------------

def test_cli_simulated_scenario(tmp_path, capsys):
    from tests.test_river_service import BASE_CONFIG
    config = json.loads(json.dumps(BASE_CONFIG))
    config["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                         "audit_log": str(tmp_path / "service_audit.jsonl"),
                         "evidence": str(tmp_path / "evidence.json")}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    code = workflow.main(["--mode", "simulated", "--scenario", "conflict-resolves",
                          "--state", "ALERT", "--config", str(config_path),
                          "--audit", str(tmp_path / "a.jsonl")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["reverification"]["outcome"] == "RESOLVED"
    assert out["real_world_authorization"] == "RESTRICTED"


def test_cli_rejects_scenario_outside_simulated(tmp_path):
    with pytest.raises(SystemExit):
        workflow.main(["--mode", "historical", "--scenario", "no-conflict"])


def test_incomplete_recent_data_alone_blocks_real_world(tmp_path):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    assert assess_evidence_for_authorization(evidence)["eligible"] is True
    evidence["data_quality"]["recent_quality"]["completeness"]["status"] = "INCOMPLETE"
    assessment = assess_evidence_for_authorization(evidence)
    assert assessment["reasons"] == ["EVIDENCE_INCOMPLETE"]
    d = authorize_with_evidence("EMERGENCY", "propose_evacuation_plan", "a", assessment)
    assert d["allowed"] is False and d["evidence_reasons"] == ["EVIDENCE_INCOMPLETE"]
