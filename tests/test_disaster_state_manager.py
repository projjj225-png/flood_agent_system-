"""Tests for the Disaster State Manager and its workflow integration. Offline.

Evidence comes from the real RiverEvidenceService over the fabricated flood
cycle (data/simulated/flood_cycle/, TEST thresholds 3.0/4.0 m); weather is the
fabricated cycle weather parsed by the real WeatherAgent. River hour h means
2026-10-10T00:00Z + h; each evaluation is 30 min after the reading.
"""

import copy
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import src.river_agent_workflow as workflow
from src.audit_log import AuditLog, read_audit_log
from src.capability_control import REAL_WORLD_CAPABILITIES
from src.disaster_state_manager import (
    ACCEPTED,
    FAILED,
    INSUFFICIENT,
    REJECTED,
    UNCHANGED,
    _record_digest,
    audit_and_finalize,
    evaluate_state,
    load_state_config,
    load_state_record,
    new_state_record,
    run_state_sequence,
    save_state_record,
    validate_state_config,
    validate_state_record,
)
from src.risk_assessment import assess_flood_risk, load_risk_config, load_simulated_weather
from src.river_agent_workflow import TEST_THRESHOLDS_PATH, run_river_workflow
from src.river_monitoring_agent import PROJECT_DIR
from tests.nwdp_fixtures import IST, ok_session, synthetic_records
from tests.test_river_service import NOW, TEST_THRESHOLDS, clean_live_session, make_service

CFG = load_state_config()
RISK_CFG = load_risk_config()
RAW_CFG = json.loads((PROJECT_DIR / "config" / "disaster_state.json").read_text("utf-8"))
CYCLE = "data/simulated/flood_cycle"
START = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)


def cycle_service(tmp_path, river_until=None, **overrides):
    """Service over the cycle; ``river_until`` cuts the series after that river hour, so
    an evaluation made later finds no newer reading (the data really goes stale)."""
    path = f"{CYCLE}/river.csv"
    if river_until is not None:
        lines = (PROJECT_DIR / path).read_text("utf-8").splitlines()
        path = str(tmp_path / f"river_until_{river_until}.csv")
        Path(path).write_text("\n".join(lines[:river_until + 2]) + "\n", "utf-8")
    return make_service(tmp_path, simulated={"path": path, "station_id": "UPPER_ANICUT_TEST",
                                             "cross_check_paths": []},
                        thresholds_path=TEST_THRESHOLDS_PATH, **overrides)


def inputs(tmp_path, hour, delay_hours=0.0, cross_check_paths=None, river_until=None):
    """(evidence, weather, risk) for river hour ``hour``, assessed ``delay_hours`` late."""
    as_of = START + timedelta(hours=hour + delay_hours)
    evidence = cycle_service(tmp_path, river_until).get_evidence(
        "simulated", as_of=as_of, cross_check_paths=cross_check_paths)
    weather = load_simulated_weather(PROJECT_DIR / CYCLE / "weather.json", retrieved_at=as_of)
    risk = assess_flood_risk(copy.deepcopy(evidence), copy.deepcopy(weather), None, RISK_CFG,
                             clock=lambda: NOW)
    return evidence, weather, risk


def evaluate(tmp_path, record, hour, requested_state=None, config=CFG, **kw):
    evidence, weather, risk = inputs(tmp_path, hour, **kw)
    return evaluate_state(record, evidence, risk, config=config, risk_config=RISK_CFG,
                          now=NOW, weather=weather, requested_state=requested_state)


def walk(tmp_path, record, hours, **kw):
    """Evaluate successive hours, carrying the record forward; return all results."""
    results = []
    for hour in hours:
        result = evaluate(tmp_path, record, hour, **kw)
        results.append(result)
        record = result["record"]
    return results


def sim_record(state):
    return new_state_record(state, "simulated")


def with_history(record, entries):
    """A record with hand-written history entries (condition, observed hour)."""
    record = copy.deepcopy(record)
    for condition, hour in entries:
        record["history"].append({"observed_at": (START + timedelta(hours=hour - 0.5))
                                  .isoformat(), "condition": condition,
                                  "evidence_id": f"e{hour}"})
    record["record_id"] = _record_digest(record)
    return record


def audit(tmp_path, name="wf.jsonl"):
    return AuditLog(tmp_path / name, component="river_workflow")


# ---- 1-2. escalation ----------------------------------------------------------------------

def test_normal_to_alert(tmp_path):
    result = evaluate(tmp_path, sim_record("NORMAL"), 34)  # 24 h rain >= 115.6 mm: HIGH
    assert result["outcome"] == ACCEPTED
    assert (result["current_state"], result["resulting_state"]) == ("NORMAL", "ALERT")
    assert result["condition"] == "ALERT" and result["transition_rule"]["kind"] == "escalation"
    assert result["record"]["state"] == "ALERT" and result["record"]["entered_at"]
    assert result["supporting_evidence_ids"] == [result["evidence_id"]]
    assert result["persistence"]["required_observations"] == 1


def test_alert_to_emergency_needs_a_second_observation(tmp_path):
    first, second = walk(tmp_path, sim_record("ALERT"), [46, 48])
    assert first["condition"] == "EMERGENCY" and first["outcome"] == UNCHANGED
    assert first["proposed_state"] == "EMERGENCY"
    assert first["persistence"]["observations"] == 1 and not first["persistence"]["satisfied"]
    assert second["outcome"] == ACCEPTED and second["resulting_state"] == "EMERGENCY"
    assert len(second["supporting_evidence_ids"]) == 2


def test_emergency_from_normal_goes_through_alert(tmp_path):
    a, b = walk(tmp_path, sim_record("NORMAL"), [46, 48])
    assert (a["resulting_state"], b["resulting_state"]) == ("ALERT", "EMERGENCY")


# ---- 3. escalation is never delayed by de-escalation persistence ------------------------------

def test_worsening_in_recovery_escalates_immediately(tmp_path):
    # RECOVERY, three NORMAL observations into a 4-observation de-escalation run.
    record = with_history(sim_record("RECOVERY"), [("NORMAL", 20), ("NORMAL", 24),
                                                   ("NORMAL", 28)])
    result = evaluate(tmp_path, record, 34)  # ALERT condition
    assert result["outcome"] == ACCEPTED and result["resulting_state"] == "ALERT"


def test_emergency_condition_in_recovery_escalates_to_alert_at_once(tmp_path):
    record = with_history(sim_record("RECOVERY"), [("NORMAL", 20), ("NORMAL", 30)])
    first = evaluate(tmp_path, record, 46)  # EMERGENCY condition, first observation
    assert first["outcome"] == ACCEPTED and first["resulting_state"] == "ALERT"
    second = evaluate(tmp_path, first["record"], 48)
    assert second["resulting_state"] == "EMERGENCY"  # confirmation carries across


# ---- 4-5. de-escalation needs verified, sustained improvement -----------------------------------

def test_emergency_to_recovery_requires_improvement(tmp_path):
    results = walk(tmp_path, sim_record("EMERGENCY"), [62, 64, 66, 68, 70, 72])
    assert [r["condition"] for r in results[:4]] == ["ALERT"] * 4  # still above warning
    assert all(r["outcome"] == UNCHANGED for r in results[:5])
    assert results[4]["proposed_state"] == "RECOVERY"
    assert results[5]["outcome"] == ACCEPTED and results[5]["resulting_state"] == "RECOVERY"


def test_gap_in_evidence_restarts_the_recovery_run(tmp_path):
    first = evaluate(tmp_path, sim_record("EMERGENCY"), 70)
    stale = evaluate(tmp_path, first["record"], 71, delay_hours=4,
                     river_until=71)  # no reading after h71: 4.5 h old, STALE
    assert stale["outcome"] == INSUFFICIENT and "EVIDENCE_STALE" in stale["reasons"]
    after = evaluate(tmp_path, stale["record"], 72)
    assert after["outcome"] == UNCHANGED and after["persistence"]["observations"] == 1


def test_recovery_to_normal_requires_sustained_improvement(tmp_path):
    results = walk(tmp_path, sim_record("RECOVERY"), [74, 76, 78, 80, 82, 84, 86])
    assert all(r["condition"] == "NORMAL" for r in results)
    assert all(r["outcome"] == UNCHANGED for r in results[:-1])  # under 12 h
    assert results[-1]["outcome"] == ACCEPTED and results[-1]["resulting_state"] == "NORMAL"
    assert results[-1]["persistence"]["span_hours"] >= 12


def test_alert_falls_back_to_normal_only_after_persistence(tmp_path):
    results = walk(tmp_path, sim_record("ALERT"), [74, 76, 78, 80])
    assert [r["outcome"] for r in results] == [UNCHANGED, UNCHANGED, UNCHANGED, ACCEPTED]
    assert results[-1]["resulting_state"] == "NORMAL"


# ---- 6-8. stale, conflicting, malformed or missing evidence --------------------------------------

def test_stale_evidence_leaves_state_unchanged(tmp_path):
    # The last reading is EMERGENCY-level, but 5.5 h old by the time it is assessed.
    result = evaluate(tmp_path, sim_record("ALERT"), 46, delay_hours=5, river_until=46)
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"
    assert "EVIDENCE_STALE" in result["reasons"]
    assert result["record"]["history"][-1]["condition"] == "INSUFFICIENT"


def test_conflicting_sources_leave_state_unchanged(tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("timestamp,station_id,river_level_m,source_type\n"
                     "2026-10-11T22:00:00Z,UPPER_ANICUT_TEST,2.50,simulated\n", "utf-8")
    result = evaluate(tmp_path, sim_record("ALERT"), 46, cross_check_paths=[str(other)])
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"
    assert "EVIDENCE_UNRESOLVED_CONFLICT" in result["reasons"]


@pytest.mark.parametrize("evidence", [None, "evidence", [], {}])
def test_missing_or_malformed_evidence_cannot_escalate(tmp_path, evidence):
    _, weather, risk = inputs(tmp_path, 46)
    result = evaluate_state(sim_record("ALERT"), evidence, risk, config=CFG,
                            risk_config=RISK_CFG, now=NOW, weather=weather)
    assert result["outcome"] in (INSUFFICIENT, REJECTED)
    assert result["resulting_state"] == "ALERT"


@pytest.mark.parametrize("edit", [
    lambda e: e.update(river_level_m=9.9),                      # integrity fails
    lambda e: e.update(data_quality=5),                         # malformed section
    lambda e: e["freshness"].update(status="FRESH", age_hours=0),
])
def test_edited_evidence_cannot_escalate(tmp_path, edit):
    evidence, weather, risk = inputs(tmp_path, 48)
    edit(evidence)
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    result = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG,
                            now=NOW, weather=weather)
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"


@pytest.mark.parametrize("record", [None, "NORMAL", {"state": "NORMAL"},
                                    {**new_state_record("NORMAL", "simulated"), "state": "PANIC"}])
def test_malformed_record_fails_closed(tmp_path, record):
    evidence, weather, risk = inputs(tmp_path, 34)
    result = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG, now=NOW,
                            weather=weather)
    assert result["outcome"] == FAILED
    assert result["resulting_state"] is None and result["record_changed"] is False


def test_edited_record_is_detected():
    record = sim_record("NORMAL")
    record["state"] = "EMERGENCY"  # edited without recomputing record_id
    assert "record.record_id: does not match the record content" in validate_state_record(record)


def test_older_evidence_cannot_rewrite_history(tmp_path):
    later = evaluate(tmp_path, sim_record("ALERT"), 48)
    older = evaluate(tmp_path, later["record"], 46)
    assert older["outcome"] == INSUFFICIENT
    assert older["reasons"] == ["EVIDENCE_OLDER_THAN_HISTORY"]


def test_naive_evaluation_time_fails(tmp_path):
    evidence, weather, risk = inputs(tmp_path, 34)
    result = evaluate_state(sim_record("NORMAL"), evidence, risk, config=CFG,
                            risk_config=RISK_CFG, now=datetime(2026, 10, 13), weather=weather)
    assert result["outcome"] == FAILED and result["resulting_state"] == "NORMAL"


# ---- 9. simulated evidence never declares a real emergency -----------------------------------------

def test_simulated_evidence_cannot_change_a_real_state(tmp_path):
    record = new_state_record("ALERT", "real")
    for hour in (46, 48):
        result = evaluate(tmp_path, record, hour)
        assert result["outcome"] == REJECTED
        assert result["reasons"] == ["SIMULATED_EVIDENCE_FOR_REAL_STATE"]
        assert result["resulting_state"] == "ALERT" and result["record_changed"] is False


def test_simulated_states_are_labelled(tmp_path):
    result = evaluate(tmp_path, sim_record("NORMAL"), 34)
    assert result["is_simulated"] is True and result["scope"] == "simulated"
    assert result["state_applies_to"] == "SIMULATION_ONLY"


def test_historical_evidence_cannot_change_a_real_state(tmp_path):
    evidence = make_service(tmp_path).get_evidence(
        "historical", as_of=datetime(2025, 12, 30, 6, 0, tzinfo=IST))
    risk = assess_flood_risk(copy.deepcopy(evidence), None, None, RISK_CFG, clock=lambda: NOW)
    result = evaluate_state(new_state_record("NORMAL", "real"), evidence, risk, config=CFG,
                            risk_config=RISK_CFG, now=NOW)
    assert result["outcome"] == REJECTED and result["reasons"] == ["EVIDENCE_SCOPE_MISMATCH"]


def _live_service(tmp_path, levels):
    thresholds = tmp_path / "thresholds.json"
    thresholds.write_text(json.dumps(TEST_THRESHOLDS), encoding="utf-8")
    records = synthetic_records(datetime(2026, 10, 13, 13, 0, tzinfo=IST), levels)
    return make_service(tmp_path, ok_session(records=records), thresholds_path=str(thresholds))


def test_real_scope_transition_needs_verified_fresh_live_evidence(tmp_path):
    service = _live_service(tmp_path, ["75.0", "75.3", "75.6", "75.9", "76.2", "76.5"])
    evidence = service.get_evidence("live")
    risk = assess_flood_risk(copy.deepcopy(evidence), None, None, RISK_CFG, clock=lambda: NOW)
    record = new_state_record("NORMAL", "real")
    ok = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG, now=NOW)
    assert ok["outcome"] == ACCEPTED and ok["resulting_state"] == "ALERT"  # TEST thresholds
    assert ok["state_applies_to"] == "CURRENT_CONDITIONS"
    late = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG,
                          now=NOW + timedelta(hours=4))
    assert late["outcome"] == INSUFFICIENT and "EVIDENCE_STALE_AT_DECISION" in late["reasons"]


def test_real_stale_nwdp_data_never_changes_state(tmp_path):
    evidence = make_service(tmp_path, ok_session()).get_evidence("live")
    risk = assess_flood_risk(copy.deepcopy(evidence), None, None, RISK_CFG, clock=lambda: NOW)
    result = evaluate_state(new_state_record("ALERT", "real"), evidence, risk, config=CFG,
                            risk_config=RISK_CFG, now=NOW)
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"


# ---- 10. unsupported transitions are rejected --------------------------------------------------------

@pytest.mark.parametrize("state,hour,requested,code", [
    ("NORMAL", 34, "EMERGENCY", "UNSUPPORTED_TRANSITION"),
    ("EMERGENCY", 74, "NORMAL", "UNSUPPORTED_TRANSITION"),
    ("EMERGENCY", 74, "ALERT", "UNSUPPORTED_TRANSITION"),
    ("NORMAL", 74, "ALERT", "REQUESTED_TRANSITION_NOT_SUPPORTED"),   # evidence is normal
    ("ALERT", 46, "EMERGENCY", "REQUESTED_TRANSITION_NOT_SUPPORTED"),  # not yet confirmed
    ("NORMAL", 34, "PANIC", "UNKNOWN_REQUESTED_STATE"),
])
def test_requested_transitions_are_checked(tmp_path, state, hour, requested, code):
    result = evaluate(tmp_path, sim_record(state), hour, requested_state=requested)
    assert result["outcome"] == REJECTED and code in result["reasons"]
    assert result["resulting_state"] == state


def test_requested_transition_backed_by_evidence_is_accepted(tmp_path):
    result = evaluate(tmp_path, sim_record("NORMAL"), 34, requested_state="ALERT")
    assert result["outcome"] == ACCEPTED


# ---- 11. the risk assessment cannot steer the state manager -------------------------------------------

def test_forged_risk_assessment_is_not_trusted(tmp_path):
    evidence, weather, risk = inputs(tmp_path, 74)  # LOW / NORMAL in reality
    forged = {**risk, "risk_level": "CRITICAL"}
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 70)])
    result = evaluate_state(record, evidence, forged, config=CFG, risk_config=RISK_CFG,
                            now=NOW, weather=weather)
    assert result["outcome"] == INSUFFICIENT and "RISK_ASSESSMENT_MISMATCH" in result["reasons"]
    assert result["resulting_state"] == "ALERT"


def test_inputs_are_not_shared_with_the_result(tmp_path):
    evidence, weather, risk = inputs(tmp_path, 34)
    record = sim_record("NORMAL")
    result = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG, now=NOW,
                            weather=weather)
    snapshot = json.dumps(result, sort_keys=True, default=str)
    risk["risk_level"] = "LOW"
    evidence["observed_at"] = None
    record["state"] = "EMERGENCY"
    assert json.dumps(result, sort_keys=True, default=str) == snapshot
    assert record["history"] == []  # the caller's record was not modified either


def test_workflow_risk_engine_cannot_force_a_state(tmp_path, monkeypatch):
    real_engine = workflow.assess_flood_risk

    def critical(*args, **kwargs):
        return {**real_engine(*args, **kwargs), "risk_level": "CRITICAL"}
    monkeypatch.setattr(workflow, "assess_flood_risk", critical)
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 70)])
    result = run_river_workflow(cycle_service(tmp_path), audit(tmp_path), mode="simulated",
                                disaster_state="ALERT", as_of=START + timedelta(hours=74),
                                weather=inputs(tmp_path, 74)[1], state_record=record)
    state = result["state_evaluation"]
    assert state["outcome"] == INSUFFICIENT and "RISK_ASSESSMENT_MISMATCH" in state["reasons"]
    assert state["resulting_state"] == "ALERT"


# ---- 12. state changes never bypass capability authorization -------------------------------------------

def _run(tmp_path, state, record, hour, log=None):
    return run_river_workflow(cycle_service(tmp_path), log or audit(tmp_path),
                              mode="simulated", disaster_state=state,
                              as_of=START + timedelta(hours=hour),
                              weather=inputs(tmp_path, hour)[1], state_record=record)


def test_emergency_classification_is_not_a_permission(tmp_path):
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    result = _run(tmp_path, "ALERT", record, 48)
    assert result["state_evaluation"]["resulting_state"] == "EMERGENCY"
    # This run's decisions used the caller's state (ALERT): the state gate still applies.
    d = {x["capability"]: x for x in result["capability_decisions"]}
    assert d["request_human_emergency_authorization"]["reason"] == "CAPABILITY_DENIED"
    assert result["state_evaluation"]["authorizes_actions"] is False
    assert result["state_evaluation"]["executes_actions"] is False
    # Next run in EMERGENCY: the evidence gate still denies every real-world capability.
    nxt = _run(tmp_path, "EMERGENCY", result["state_evaluation"]["record"], 50)
    for cap in REAL_WORLD_CAPABILITIES:
        x = next(c for c in nxt["capability_decisions"] if c["capability"] == cap)
        assert x["allowed"] is False and "EVIDENCE_SIMULATED" in x["evidence_reasons"]
    assert nxt["real_world_authorization"] == "RESTRICTED"


def test_state_evaluation_does_not_change_decisions(tmp_path):
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    with_state = _run(tmp_path, "ALERT", record, 48)
    without = _run(tmp_path, "ALERT", None, 48)
    strip = lambda r: [{k: v for k, v in d.items() if k != "audit_written"}
                       for d in r["capability_decisions"]]
    assert strip(with_state) == strip(without)
    assert with_state["state_evaluation"]["outcome"] == ACCEPTED
    assert without["state_evaluation"]["outcome"] == UNCHANGED  # no history: 1 of 2


def test_record_state_must_match_the_callers_state(tmp_path):
    result = _run(tmp_path, "ALERT", sim_record("NORMAL"), 48)
    state = result["state_evaluation"]
    assert state["outcome"] == FAILED and "STATE_RECORD_MISMATCH" in state["reasons"][0]
    assert state["resulting_state"] == "NORMAL" and state["record_changed"] is False


# ---- 13. audit failures ---------------------------------------------------------------------------------

class FailsOnEvent(AuditLog):
    def __init__(self, path, event):
        super().__init__(path, component="river_workflow")
        self.failing_event = event

    def log(self, run_id, event, **details):
        if event == self.failing_event:
            self.write_errors.append(f"{event}: simulated write failure")
            return False
        return super().log(run_id, event, **details)


def test_unaudited_transition_is_not_applied(tmp_path):
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    result = _run(tmp_path, "ALERT", record,
                  48, log=FailsOnEvent(tmp_path / "a.jsonl",
                                       "disaster_state_transition_accepted"))
    state = result["state_evaluation"]
    assert state["outcome"] == REJECTED and "AUDIT_WRITE_FAILED" in state["reasons"]
    assert state["resulting_state"] == "ALERT" and state["record"] == record


def test_late_audit_failure_reverts_the_transition(tmp_path):
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    result = _run(tmp_path, "ALERT", record, 48,
                  log=FailsOnEvent(tmp_path / "a.jsonl", "river_workflow_completed"))
    state = result["state_evaluation"]
    assert state["outcome"] == REJECTED and "AUDIT_TRAIL_INCOMPLETE" in state["reasons"]
    assert state["record"] == record and state["resulting_state"] == "ALERT"


def test_raising_logger_is_handled():
    class Raising:
        def log(self, *a, **k):
            raise RuntimeError("disk on fire")
    result = {"outcome": ACCEPTED, "current_state": "ALERT", "resulting_state": "EMERGENCY",
              "reasons": [], "record": {}}
    out = audit_and_finalize(result, sim_record("ALERT"), Raising(), "run")
    assert out["outcome"] == REJECTED and out["resulting_state"] == "ALERT"


def test_every_outcome_is_audited(tmp_path):
    log = audit(tmp_path)
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    _run(tmp_path, "ALERT", record, 48, log=log)                      # accepted
    _run(tmp_path, "ALERT", sim_record("ALERT"), 46, log=log)         # unchanged
    _run(tmp_path, "ALERT", new_state_record("ALERT", "real"), 46, log=log)  # rejected
    run_river_workflow(cycle_service(tmp_path, river_until=46), log, mode="simulated",
                       disaster_state="ALERT", as_of=START + timedelta(hours=51),
                       weather=inputs(tmp_path, 51)[1])               # insufficient (stale)
    _run(tmp_path, "NORMAL", sim_record("ALERT"), 46, log=log)        # failed (mismatch)
    events = {e["event"] for e in read_audit_log(log.path)}
    assert {"disaster_state_transition_accepted", "disaster_state_unchanged",
            "disaster_state_transition_rejected", "disaster_state_insufficient_evidence",
            "disaster_state_evaluation_failed"} <= events
    entry = next(e for e in read_audit_log(log.path)
                 if e["event"] == "disaster_state_transition_accepted")["details"]
    assert entry["current_state"] == "ALERT" and entry["resulting_state"] == "EMERGENCY"
    assert entry["authorizes_actions"] is False and len(entry["supporting_evidence_ids"]) == 2


# ---- 14. no oscillation ------------------------------------------------------------------------------------

def test_reevaluating_the_same_observation_never_confirms(tmp_path):
    record = sim_record("ALERT")
    for _ in range(5):
        result = evaluate(tmp_path, record, 46)
        record = result["record"]
    assert result["outcome"] == UNCHANGED and result["persistence"]["observations"] == 1
    assert len(record["history"]) == 1


def test_mixed_conditions_hold_the_state(tmp_path):
    record = with_history(sim_record("ALERT"), [("NORMAL", 20), ("ELEVATED", 22),
                                                ("NORMAL", 24), ("ELEVATED", 26)])
    result = evaluate(tmp_path, record, 74)  # NORMAL again: run of 1
    assert result["outcome"] == UNCHANGED and result["persistence"]["observations"] == 1


def test_full_flood_cycle_has_exactly_four_transitions(tmp_path):
    steps = run_state_sequence("flood-cycle", cycle_service(tmp_path), audit(tmp_path))
    changes = [(s["hour"], s["state_before"], s["state_after"]) for s in steps
               if s["state_before"] != s["state_after"]]
    assert [(a, b) for _, a, b in changes] == [("NORMAL", "ALERT"), ("ALERT", "EMERGENCY"),
                                               ("EMERGENCY", "RECOVERY"), ("RECOVERY", "NORMAL")]
    assert [h for h, _, _ in changes] == [34, 48, 72, 86]


# ---- 15. configuration errors fail safely ----------------------------------------------------------------

@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["allowed_transitions"].update(NORMAL=["ALERT", "EMERGENCY"]), "NORMAL ->"),
    (lambda c: c["allowed_transitions"].update(EMERGENCY=["NORMAL"]), "through RECOVERY"),
    (lambda c: c["allowed_transitions"].update(ALERT=["ALERT"]), "other known states"),
    (lambda c: c["allowed_transitions"].pop("RECOVERY"), "exactly"),
    (lambda c: c["rules"]["ALERT"][0].update(min_observations=1), "at least 2"),
    (lambda c: c["rules"]["ALERT"][1].update(gap_breaks_run=False), "unbroken"),
    (lambda c: c["rules"]["NORMAL"][0].update(to="RECOVERY"), "not an allowed"),
    (lambda c: c["rules"]["ALERT"].reverse(), "before de-escalation"),
    (lambda c: c["rules"]["ALERT"][0].update(when=["PANIC"]), "when"),
    (lambda c: c["rules"]["ALERT"][1].update(min_hours=-1), "min_hours"),
    (lambda c: c["conditions"]["EMERGENCY"].update(river_factor_min_level=None), "rainfall"),
    (lambda c: c["conditions"]["ALERT"].update(min_risk_level="SEVERE"), "min_risk_level"),
    (lambda c: c.update(history_limit=1), "history_limit"),
])
def test_invalid_config_is_rejected(mutate, message):
    raw = copy.deepcopy(RAW_CFG)
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        validate_state_config(raw)


def test_workflow_with_bad_config_keeps_state_and_decisions(tmp_path, monkeypatch):
    def broken():
        raise ValueError("State config: broken")
    monkeypatch.setattr(workflow, "load_state_config", broken)
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 46)])
    result = _run(tmp_path, "ALERT", record, 48)
    state = result["state_evaluation"]
    assert state["outcome"] == FAILED and state["resulting_state"] == "ALERT"
    assert state["record_changed"] is False
    assert result["capability_decisions"]  # authorization still ran normally


# ---- 16. existing scenarios and the CLI --------------------------------------------------------------------

@pytest.mark.parametrize("scenario,outcome", [
    ("no-conflict", "NOT_REQUIRED"), ("conflict-resolves", "RESOLVED"),
    ("conflict-persists", "CONFLICT_PERSISTS"), ("reverification-fails", "FAILED")])
def test_existing_scenarios_unchanged(tmp_path, scenario, outcome):
    result = workflow.run_simulated_scenario(
        scenario, make_service(tmp_path), audit(tmp_path), disaster_state="ALERT",
        as_of=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    assert result["reverification"]["outcome"] == outcome
    assert result["real_world_authorization"] == "RESTRICTED"
    assert result["state_evaluation"]["resulting_state"] == "ALERT"  # no thresholds/weather
    assert result["state_evaluation"]["outcome"] in (UNCHANGED, INSUFFICIENT)


def _cli_config(tmp_path):
    from tests.test_river_service import BASE_CONFIG
    config = json.loads(json.dumps(BASE_CONFIG))
    config["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                         "audit_log": str(tmp_path / "service_audit.jsonl"),
                         "evidence": str(tmp_path / "evidence.json")}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def test_cli_state_file_round_trip(tmp_path, capsys):
    state_file = tmp_path / "state.json"
    args = ["--mode", "simulated", "--risk-scenario", "simulated-high-risk",
            "--config", _cli_config(tmp_path), "--audit", str(tmp_path / "a.jsonl"),
            "--state-file", str(state_file)]
    assert workflow.main(args + ["--state", "NORMAL"]) == 0
    first = json.loads(capsys.readouterr().out)["state_evaluation"]
    assert first["outcome"] == ACCEPTED and load_state_record(state_file)["state"] == "ALERT"
    assert workflow.main(args) == 0  # state now read from the file
    second = json.loads(capsys.readouterr().out)["state_evaluation"]
    # Same observation again: no confirmation, so no EMERGENCY.
    assert second["current_state"] == "ALERT" and second["outcome"] == UNCHANGED
    assert workflow.main(args + ["--state", "NORMAL"]) == 2  # contradicts the file


def test_cli_refuses_a_tampered_state_file(tmp_path, capsys):
    state_file = tmp_path / "state.json"
    record = sim_record("NORMAL")
    record["state"] = "EMERGENCY"
    state_file.write_text(json.dumps(record), encoding="utf-8")
    assert workflow.main(["--mode", "simulated", "--risk-scenario", "simulated-high-risk",
                          "--config", _cli_config(tmp_path), "--audit",
                          str(tmp_path / "a.jsonl"), "--state-file", str(state_file)]) == 2


def test_save_refuses_invalid_records_and_is_atomic(tmp_path):
    path = tmp_path / "s.json"
    save_state_record(sim_record("ALERT"), path)
    bad = sim_record("ALERT")
    bad["state"] = "PANIC"
    with pytest.raises(ValueError):
        save_state_record(bad, path)
    assert load_state_record(path)["state"] == "ALERT"
    assert list(tmp_path.glob("*.tmp")) == []


def test_history_is_bounded(tmp_path):
    raw = copy.deepcopy(RAW_CFG)
    raw["history_limit"] = 10
    cfg = validate_state_config(raw)
    record = sim_record("NORMAL")
    for hour in range(74, 100, 2):
        record = evaluate(tmp_path, record, hour, config=cfg)["record"]
    assert len(record["history"]) == 10


# ==== Sampling adequacy: spacing, gaps, duplicates, ordering, restart ====================
# Persistence counts adequately sampled, distinct observations. Readings closer than
# min_spacing_hours count once; a de-escalation run ends at a gap > max_gap_hours.

from src.disaster_state_manager import run_statistics  # noqa: E402

T0 = datetime(2026, 10, 13, 0, 0, tzinfo=timezone.utc)
DE_ESCALATE = CFG["rules"]["RECOVERY"][-1]   # RECOVERY -> NORMAL: 4 obs, 12 h, gap <= 3 h
CONFIRM = CFG["rules"]["ALERT"][0]           # ALERT -> EMERGENCY: 2 obs, no gap limit


def entries(condition, hours):
    return [{"observed_at": (T0 + timedelta(hours=h)).isoformat(), "condition": condition,
             "evidence_id": f"e{h}"} for h in hours]


def satisfied(stats, rule):
    return stats["observations"] >= rule["min_observations"] \
        and stats["span_hours"] >= rule["min_hours"]


def test_rules_carry_sampling_limits():
    for rules in CFG["rules"].values():
        for rule in rules:
            assert rule["min_spacing_hours"] == 0.5
            assert (rule["max_gap_hours"] is None) == (rule["kind"] == "escalation")


# ---- duplicate timestamps ---------------------------------------------------------------

def test_single_emergency_reading_cannot_confirm_itself(tmp_path):
    """Regression: ALERT -> EMERGENCY used to be accepted on ONE reading evaluated twice
    with an INSUFFICIENT evaluation in between (the duplicate was appended, not replaced)."""
    evidence, weather, risk = inputs(tmp_path, 46)
    first = evaluate_state(sim_record("ALERT"), evidence, risk, config=CFG,
                           risk_config=RISK_CFG, now=NOW, weather=weather)
    gap = evaluate_state(first["record"], None, None, config=CFG, risk_config=RISK_CFG,
                         now=NOW)
    again = evaluate_state(gap["record"], evidence, risk, config=CFG, risk_config=RISK_CFG,
                           now=NOW, weather=weather)
    assert gap["outcome"] == INSUFFICIENT
    assert again["outcome"] == UNCHANGED and again["resulting_state"] == "ALERT"
    assert again["persistence"]["observations"] == 1
    valid = [e for e in again["record"]["history"] if e["condition"] != "INSUFFICIENT"]
    assert [e["observed_at"] for e in valid] == [first["observed_at"]]  # replaced, not added


def test_duplicate_timestamps_in_history_count_once():
    history = entries("EMERGENCY", [1]) + [{"observed_at": None, "condition": "INSUFFICIENT"}] \
        + entries("EMERGENCY", [1])
    stats = run_statistics(history, CONFIRM)
    assert stats["observations"] == 1 and not satisfied(stats, CONFIRM)
    assert stats["readings_skipped_as_duplicate_or_too_close"] == 1


# ---- irregular sampling -----------------------------------------------------------------

def test_bunched_readings_count_once():
    stats = run_statistics(entries("NORMAL", [0, 0.1, 0.2, 0.3, 0.4, 12.4]), DE_ESCALATE)
    # 12 h of silence breaks the run: only the newest reading is left.
    assert stats["observations"] == 1 and stats["run_broken_by_gap_hours"] == 12.0
    bunched = run_statistics(entries("NORMAL", [0, 0.1, 0.2, 0.3]), DE_ESCALATE)
    assert bunched["observations"] == 1  # all within 0.5 h of the newest
    assert bunched["readings_skipped_as_duplicate_or_too_close"] == 3


def test_irregular_but_adequate_sampling_still_counts():
    stats = run_statistics(entries("NORMAL", [0, 1.5, 4, 6.5, 9, 12]), DE_ESCALATE)
    assert stats["observations"] == 6 and stats["span_hours"] == 12
    assert stats["largest_gap_hours"] == 3.0 and satisfied(stats, DE_ESCALATE)  # 9 -> 12 h


def test_spacing_threshold_is_inclusive():
    stats = run_statistics(entries("NORMAL", [0, 0.5, 1.0]), DE_ESCALATE)
    assert stats["observations"] == 3


def test_emergency_confirmation_needs_separately_spaced_readings():
    close = run_statistics(entries("EMERGENCY", [0, 0.25]), CONFIRM)
    assert close["observations"] == 1 and not satisfied(close, CONFIRM)
    spaced = run_statistics(entries("EMERGENCY", [0, 1]), CONFIRM)
    assert satisfied(spaced, CONFIRM)


# ---- large gaps ---------------------------------------------------------------------------

def test_the_documented_false_recovery_no_longer_passes():
    """Three readings within 30 min, 12 h of silence, one reading: used to satisfy the
    12 h sustained-recovery rule; now the gap ends the run."""
    stats = run_statistics(entries("NORMAL", [0, 0.25, 0.5, 12.5]), DE_ESCALATE)
    assert not satisfied(stats, DE_ESCALATE) and stats["run_broken_by_gap_hours"] == 12.0


def test_delayed_feed_cannot_complete_recovery(tmp_path):
    # Hourly NORMAL readings h74-77, then nothing until h86: 12 h span, 5 readings,
    # but a 9 h hole. Before the gap limit this was accepted as RECOVERY -> NORMAL.
    results = walk(tmp_path, sim_record("RECOVERY"), [74, 75, 76, 77, 86])
    last = results[-1]
    assert last["outcome"] == UNCHANGED and last["resulting_state"] == "RECOVERY"
    assert last["persistence"]["run_broken_by_gap_hours"] == 9.0
    assert last["persistence"]["observations"] == 1


def test_gap_exactly_at_the_limit_is_allowed():
    stats = run_statistics(entries("NORMAL", [0, 3, 6, 9, 12]), DE_ESCALATE)
    assert satisfied(stats, DE_ESCALATE) and stats["run_broken_by_gap_hours"] is None
    over = run_statistics(entries("NORMAL", [0, 3, 6, 9.01, 12]), DE_ESCALATE)
    assert not satisfied(over, DE_ESCALATE)


def test_gaps_never_hold_back_escalation(tmp_path):
    # EMERGENCY readings 10 h apart still confirm: escalation has no gap limit.
    stats = run_statistics(entries("EMERGENCY", [0, 10]), CONFIRM)
    assert satisfied(stats, CONFIRM)
    # And a single ALERT-level reading after a long silence escalates at once.
    record = with_history(sim_record("RECOVERY"), [("NORMAL", 10)])
    result = evaluate(tmp_path, record, 34)
    assert result["outcome"] == ACCEPTED and result["resulting_state"] == "ALERT"


# ---- out-of-order readings --------------------------------------------------------------

def test_out_of_order_reading_is_ignored_and_the_run_continues(tmp_path):
    a = evaluate(tmp_path, sim_record("RECOVERY"), 76)
    old = evaluate(tmp_path, a["record"], 74)
    assert old["outcome"] == INSUFFICIENT and old["reasons"] == ["EVIDENCE_OLDER_THAN_HISTORY"]
    assert old["record"]["history"] == a["record"]["history"]  # nothing recorded
    b = evaluate(tmp_path, old["record"], 78)
    assert b["persistence"]["observations"] == 2  # h76, h78 - never h74


def test_out_of_order_after_a_gap_entry_is_still_rejected(tmp_path):
    a = evaluate(tmp_path, sim_record("RECOVERY"), 76)
    gap = evaluate_state(a["record"], None, None, config=CFG, risk_config=RISK_CFG, now=NOW)
    old = evaluate(tmp_path, gap["record"], 74)
    assert old["outcome"] == INSUFFICIENT and old["reasons"] == ["EVIDENCE_OLDER_THAN_HISTORY"]


# ---- repeated evaluation of the same evidence -----------------------------------------------

def test_repeated_evaluation_is_idempotent(tmp_path):
    record = sim_record("RECOVERY")
    results = []
    for _ in range(4):
        results.append(evaluate(tmp_path, record, 74))
        record = results[-1]["record"]
    assert len({r["state_decision_id"] for r in results}) == 1
    assert len(record["history"]) == 1 and results[-1]["persistence"]["observations"] == 1


# ---- restart from a saved state record --------------------------------------------------------

def test_restart_from_saved_record_matches_an_uninterrupted_run(tmp_path):
    hours = [74, 76, 78, 80, 82, 84, 86]
    straight = walk(tmp_path, sim_record("RECOVERY"), hours)
    first = walk(tmp_path, sim_record("RECOVERY"), hours[:3])
    path = tmp_path / "state" / "record.json"
    save_state_record(first[-1]["record"], path)
    resumed = walk(tmp_path, load_state_record(path), hours[3:])
    assert [r["state_decision_id"] for r in first + resumed] == \
        [r["state_decision_id"] for r in straight]
    assert resumed[-1]["record"]["record_id"] == straight[-1]["record"]["record_id"]
    assert resumed[-1]["outcome"] == ACCEPTED and resumed[-1]["resulting_state"] == "NORMAL"


def test_restart_after_a_long_outage_restarts_recovery(tmp_path):
    first = walk(tmp_path, sim_record("RECOVERY"), [74, 76, 78])
    path = tmp_path / "record.json"
    save_state_record(first[-1]["record"], path)
    resumed = walk(tmp_path, load_state_record(path), [84, 86, 88, 90])  # 6 h hole first
    assert all(r["outcome"] == UNCHANGED for r in resumed)
    assert resumed[0]["persistence"]["run_broken_by_gap_hours"] == 6.0


# ---- invalid state records ----------------------------------------------------------------------

def _resealed(record):
    record["record_id"] = _record_digest(record)
    return record


@pytest.mark.parametrize("make,problem", [
    (lambda r: {**r, "history": "none"}, "record.history: expected list"),
    (lambda r: _resealed({**r, "history": [{"condition": "NORMAL"}]}), "observed_at"),
    (lambda r: _resealed({**r, "history": [{"observed_at": "x", "condition": "NORMAL"}]}),
     "observed_at"),
    (lambda r: _resealed({**r, "history": [{"observed_at": T0.isoformat(),
                                            "condition": "CALM"}]}), "malformed entry"),
    (lambda r: _resealed({**r, "history": ["NORMAL"]}), "malformed entry"),
    (lambda r: _resealed({**r, "scope": "live"}), "record.scope"),
    (lambda r: _resealed({**r, "state": "PANIC"}), "record.state"),
    (lambda r: {**r, "state": "EMERGENCY"}, "record.record_id"),
    (lambda r: {k: v for k, v in r.items() if k != "record_id"}, "record.record_id"),
], ids=["history-type", "no-time", "bad-time", "bad-condition", "entry-type", "scope",
        "state", "edited", "no-id"])
def test_invalid_records_fail_closed(tmp_path, make, problem):
    record = make(sim_record("ALERT"))
    assert any(problem in p for p in validate_state_record(record))
    evidence, weather, risk = inputs(tmp_path, 48)
    result = evaluate_state(record, evidence, risk, config=CFG, risk_config=RISK_CFG, now=NOW,
                            weather=weather)
    assert result["outcome"] == FAILED and result["record_changed"] is False
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(record), encoding="utf-8")
    with pytest.raises(ValueError):
        load_state_record(path)


def test_unreadable_record_file_is_refused(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text("{not json", encoding="utf-8")
    with pytest.raises(ValueError):
        load_state_record(path)


# ---- sampling configuration is validated ------------------------------------------------------------

@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["rules"]["ALERT"][1].update(max_gap_hours=None), "needs max_gap_hours"),
    (lambda c: c["rules"]["ALERT"][1].update(max_gap_hours=0.25), ">= min_spacing_hours"),
    (lambda c: c["rules"]["ALERT"][1].update(min_spacing_hours=0), "> 0"),
    (lambda c: c["rules"]["ALERT"][1].pop("min_spacing_hours"), "min_spacing_hours"),
    (lambda c: c["rules"]["ALERT"][0].update(max_gap_hours="3"), "max_gap_hours"),
])
def test_invalid_sampling_config_is_rejected(mutate, message):
    raw = copy.deepcopy(RAW_CFG)
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        validate_state_config(raw)


# ---- scope, audit and authority, re-checked with the sampling rules ----------------------------------

@pytest.mark.parametrize("state,hours", [
    ("EMERGENCY", [72, 74]),
    ("RECOVERY", [74, 76, 78, 80, 82, 84, 86]),
])
def test_simulated_improvement_cannot_de_escalate_a_real_record(tmp_path, state, hours):
    record = new_state_record(state, "real")
    for result in walk(tmp_path, record, hours):
        assert result["outcome"] == REJECTED and result["resulting_state"] == state
        assert result["reasons"] == ["SIMULATED_EVIDENCE_FOR_REAL_STATE"]
        assert result["record"] == record


def test_no_outcome_ever_authorizes_or_executes(tmp_path):
    steps = []
    record = sim_record("NORMAL")
    for hour in (34, 46, 48, 70, 72):
        result = evaluate(tmp_path, record, hour)
        steps.append(result)
        record = result["record"]
    assert {r["outcome"] for r in steps} >= {ACCEPTED, UNCHANGED}
    assert all(r["authorizes_actions"] is False and r["executes_actions"] is False
               for r in steps)
