"""Automatic disaster-state evaluation from the three agents' evidence. Offline.

Every test goes through the same path the system uses: run_river_workflow ->
evaluate_state (or evaluate_state directly for rule-level checks). River and
weather come from the fabricated flood cycle (data/simulated/flood_cycle/, TEST
thresholds); traffic from the fabricated TrafficAgent scenarios
(data/simulated/traffic/), parsed by the real TrafficAgent. Nothing here assigns
a state: records start in NORMAL (or carry a hand-written history) and change
only through the state manager.

River hours on the cycle (TEST thresholds: warning 3.0 m, danger 4.0 m, recovery level
2.8 m = warning - 0.2 m): h34-h44 HIGH risk but below warning (ELEVATED), h46-h50 ALERT
(at/above warning), h52-h60 EMERGENCY (at/above danger), h62-h68 ALERT, h70-h72 ELEVATED
(below warning, not yet below the recovery level), h74 onward NORMAL.
"""

import json
from datetime import timedelta

import pytest

from src.audit_log import AuditLog, read_audit_log
from src.capability_control import REAL_WORLD_CAPABILITIES
from src.dashboard_service import DashboardService
from src.disaster_state_manager import (
    ACCEPTED,
    INSUFFICIENT,
    REJECTED,
    UNCHANGED,
    assess_traffic,
    load_state_record,
    new_state_record,
    run_state_sequence,
    save_state_record,
    validate_state_config,
)
from src.river_agent_workflow import run_river_workflow
from tests.test_dashboard_api import LIVE_WEATHER
from tests.test_disaster_state_manager import (
    CFG,
    RAW_CFG,
    START,
    audit,
    cycle_service,
    evaluate,
    inputs,
    sim_record,
    traffic_at,
    with_history,
)
from tests.test_river_service import NOW, clean_live_session, make_service


def step(tmp_path, record, hour, traffic="normal_flow", log=None):
    """One workflow run at river hour ``hour`` with the three agents' evidence."""
    as_of = START + timedelta(hours=hour)
    if isinstance(traffic, str):
        traffic = traffic_at(traffic, as_of)
    return run_river_workflow(cycle_service(tmp_path), log or audit(tmp_path), mode="simulated",
                              disaster_state=record["state"], as_of=as_of,
                              weather=inputs(tmp_path, hour)[1], state_record=record,
                              traffic=traffic)


def walk(tmp_path, record, hours, traffic="normal_flow", log=None):
    """Successive runs, carrying forward only the record the state manager returned."""
    results = []
    for hour in hours:
        result = step(tmp_path, record, hour, traffic, log)
        results.append(result)
        record = result["state_evaluation"]["record"]
    return results


def allowed(result):
    return {d["capability"] for d in result["capability_decisions"] if d["allowed"]}


# ---- 1-4. the four automatic transitions, with capability changes ------------------------------

def test_normal_to_alert_automatically(tmp_path):
    result = step(tmp_path, with_history(sim_record("NORMAL"), [("ALERT", 46)]), 48)
    state = result["state_evaluation"]
    assert state["outcome"] == ACCEPTED and state["resulting_state"] == "ALERT"
    assert state["previous_state"] == "NORMAL" and state["transition_at"]
    changes = result["capability_changes"]
    assert result["capability_state"] == "ALERT" and changes["changed"] is True
    assert changes["allowlist_added"] == ["propose_evacuation_plan",
                                          "request_evidence_reverification"]
    assert changes["allowlist_revoked"] == []
    assert "request_evidence_reverification" in allowed(result)  # internal: evidence-eligible


def test_alert_to_emergency_automatically(tmp_path):
    record = with_history(sim_record("ALERT"), [("EMERGENCY", 52)])
    result = step(tmp_path, record, 54)
    state = result["state_evaluation"]
    assert state["outcome"] == ACCEPTED and state["resulting_state"] == "EMERGENCY"
    assert result["capability_changes"]["allowlist_added"] == [
        "request_human_emergency_authorization"]
    # Emergency restrictions: simulated evidence never authorizes a real-world action, and
    # nothing is ever issued automatically.
    for d in result["capability_decisions"]:
        if d["capability"] in REAL_WORLD_CAPABILITIES:
            assert d["allowed"] is False and "EVIDENCE_SIMULATED" in d["evidence_reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"
    assert state["authorizes_actions"] is False and state["executes_actions"] is False


def test_emergency_to_recovery_revokes_emergency_permissions(tmp_path):
    results = walk(tmp_path, sim_record("EMERGENCY"), [74, 76], traffic="flood_road_closure")
    first, second = (r["state_evaluation"] for r in results)
    assert first["outcome"] == UNCHANGED and first["condition"] in ("IMPROVED", "NORMAL")
    assert second["outcome"] == ACCEPTED and second["resulting_state"] == "RECOVERY"
    changes = results[1]["capability_changes"]
    assert changes["allowlist_revoked"] == ["propose_evacuation_plan",
                                            "request_human_emergency_authorization"]
    assert changes["allowlist_added"] == ["prepare_recovery_assessment"]
    revoked = {d["capability"]: d for d in results[1]["capability_decisions"]}
    for cap in ("propose_evacuation_plan", "request_human_emergency_authorization"):
        assert revoked[cap]["allowed"] is False and revoked[cap]["reason"] == "CAPABILITY_DENIED"
        assert revoked[cap]["state"] == "RECOVERY"
    # The improvement alone is enough: a closed road does not hold EMERGENCY -> RECOVERY.
    assert second["traffic"]["status"] == "ROUTE_BLOCKED" and second["traffic_hold"] is None


def test_recovery_to_normal_needs_sustained_normal_and_usable_routes(tmp_path):
    results = walk(tmp_path, sim_record("RECOVERY"), range(74, 87, 2))
    outcomes = [r["state_evaluation"]["outcome"] for r in results]
    assert outcomes[:-1] == [UNCHANGED] * 6 and outcomes[-1] == ACCEPTED
    last = results[-1]
    assert last["state_evaluation"]["resulting_state"] == "NORMAL"
    assert last["capability_changes"]["allowlist_revoked"] == [
        "prepare_recovery_assessment", "request_evidence_reverification"]
    assert any("evacuation route(s) usable" in r for r in last["state_evaluation"]["reasons"])
    assert last["state_evaluation"]["persistence"]["traffic_status"] == "ROUTES_USABLE"


def test_full_cycle_runner_uses_the_state_engine_and_never_assigns_a_state(tmp_path):
    steps = run_state_sequence("flood-cycle", cycle_service(tmp_path), audit(tmp_path))
    for before, after in zip(steps, steps[1:]):
        assert after["state_before"] == before["state_after"]   # only the engine's record
    for s in steps:
        if s["state_before"] != s["state_after"]:
            assert s["outcome"] == ACCEPTED
        assert s["is_simulated"] is True and s["real_world_authorization"] == "RESTRICTED"
        assert set(s["evidence_classes"].values()) <= {"SIMULATED", "CONFLICTING",
                                                       "INSUFFICIENT", "UNAVAILABLE"}
    path = [(s["state_before"], s["state_after"]) for s in steps
            if s["state_before"] != s["state_after"]]
    assert path == [("NORMAL", "ALERT"), ("ALERT", "EMERGENCY"), ("EMERGENCY", "RECOVERY"),
                    ("RECOVERY", "NORMAL")]


# ---- 5. invalid transitions --------------------------------------------------------------------

def test_invalid_transition_is_rejected(tmp_path):
    result = evaluate(tmp_path, sim_record("NORMAL"), 48, requested_state="EMERGENCY")
    assert result["outcome"] == REJECTED and "UNSUPPORTED_TRANSITION" in result["reasons"]
    assert result["resulting_state"] == "NORMAL"
    result = evaluate(tmp_path, sim_record("EMERGENCY"), 74, requested_state="NORMAL")
    assert result["outcome"] == REJECTED and result["resulting_state"] == "EMERGENCY"


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["rules"]["NORMAL"][0].update(requires_traffic="ROUTES_USABLE"),
     "only allowed on de-escalation"),
    (lambda c: c["rules"]["RECOVERY"][-1].update(requires_traffic="ANY"), "requires_traffic"),
    (lambda c: c["traffic"].update(usable_route_conditions=["FREE_FLOW", "BLOCKED"]),
     "never usable"),
    (lambda c: c["traffic"].update(max_age_hours=0), "max_age_hours"),
])
def test_traffic_configuration_cannot_weaken_the_rules(mutate, message):
    config = json.loads(json.dumps(RAW_CFG))
    mutate(config)
    with pytest.raises(ValueError, match=message):
        validate_state_config(config)


# ---- 6-8. insufficient, stale and conflicting evidence -----------------------------------------

def test_missing_river_evidence_is_insufficient_never_low_risk(tmp_path):
    from src.disaster_state_manager import evaluate_state
    from src.risk_assessment import load_risk_config
    result = evaluate_state(sim_record("ALERT"), None, None, config=CFG,
                            risk_config=load_risk_config(), now=NOW,
                            traffic=traffic_at("normal_flow"))
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"
    assert result["agent_evidence"]["river"]["evidence_class"] == "INSUFFICIENT"


@pytest.mark.parametrize("traffic,status", [
    (None, "UNAVAILABLE"),
    ("degraded_data", "ROUTES_UNCERTAIN"),
    ("evacuation_congestion", "ROUTES_UNCERTAIN"),
])
def test_missing_or_uncertain_traffic_never_counts_as_usable_routes(tmp_path, traffic, status):
    results = walk(tmp_path, sim_record("RECOVERY"), range(74, 87, 2), traffic=traffic)
    last = results[-1]["state_evaluation"]
    assert last["outcome"] == UNCHANGED and last["resulting_state"] == "RECOVERY"
    assert last["traffic"]["status"] == status
    assert last["traffic_hold"] == "TRAFFIC_EVIDENCE_INSUFFICIENT"
    assert last["proposed_state"] == "NORMAL"


def test_stale_traffic_holds_recovery(tmp_path):
    record = sim_record("RECOVERY")
    for hour in range(74, 87, 2):
        as_of = START + timedelta(hours=hour)
        old = traffic_at("normal_flow", as_of - timedelta(hours=2))   # limit 1 h
        result = step(tmp_path, record, hour, traffic=old)
        record = result["state_evaluation"]["record"]
    state = result["state_evaluation"]
    assert state["traffic"]["status"] == "STALE" and state["resulting_state"] == "RECOVERY"
    assert "TRAFFIC_STALE" in state["reasons"]


def test_stale_river_evidence_is_insufficient(tmp_path):
    result = evaluate(tmp_path, sim_record("ALERT"), 74, delay_hours=9, river_until=74)
    assert result["outcome"] == INSUFFICIENT and result["resulting_state"] == "ALERT"


def test_blocked_route_while_the_river_is_normal_is_conflicting_evidence(tmp_path):
    results = walk(tmp_path, sim_record("RECOVERY"), range(74, 87, 2),
                   traffic="flood_road_closure")
    last = results[-1]["state_evaluation"]
    assert last["resulting_state"] == "RECOVERY"
    assert last["traffic_hold"] == "CONFLICTING_EVIDENCE_ROUTE_BLOCKED"
    assert last["agent_evidence"]["traffic"]["evidence_class"] == "CONFLICTING"
    assert last["traffic"]["blocked_routes"] == ["EVAC_PRIMARY_NH81"]


def test_simulated_traffic_cannot_change_a_real_state():
    view = assess_traffic(traffic_at("normal_flow"), "real", NOW, CFG)
    assert view["status"] == "SCOPE_MISMATCH"
    assert "SIMULATED_TRAFFIC_FOR_REAL_STATE" in view["reasons"]


def test_traffic_never_escalates(tmp_path):
    # Steady NORMAL-condition river with every route closed: still NORMAL.
    result = step(tmp_path, sim_record("NORMAL"), 80, traffic="shared_nh81_closed")
    state = result["state_evaluation"]
    assert state["condition"] == "NORMAL" and state["outcome"] == UNCHANGED
    assert state["resulting_state"] == "NORMAL" and result["capability_changes"]["changed"] is False


# ---- 9. hysteresis and persistence --------------------------------------------------------------

def test_alert_needs_sustained_normal_conditions_to_clear(tmp_path):
    # Below warning but not below the recovery level is not enough to leave ALERT: hysteresis.
    result = evaluate(tmp_path, sim_record("ALERT"), 70)
    assert result["condition"] == "ELEVATED" and result["outcome"] == UNCHANGED
    # NORMAL condition must hold for 3 observations over 6 h.
    results = walk(tmp_path, sim_record("ALERT"), [74, 76, 78, 80])
    outcomes = [r["state_evaluation"]["outcome"] for r in results]
    assert outcomes == [UNCHANGED, UNCHANGED, UNCHANGED, ACCEPTED]
    assert results[-1]["state_evaluation"]["resulting_state"] == "NORMAL"


def test_emergency_needs_two_observations(tmp_path):
    results = walk(tmp_path, sim_record("ALERT"), [52, 54])
    assert [r["state_evaluation"]["outcome"] for r in results] == [UNCHANGED, ACCEPTED]


def test_a_reading_gap_restarts_the_recovery_run(tmp_path):
    # 74, 76, then nothing until 82 (> 3 h gap): the run restarts at 82.
    results = walk(tmp_path, sim_record("RECOVERY"), [74, 76, 82, 84, 86])
    last = results[-1]["state_evaluation"]
    assert last["outcome"] == UNCHANGED and last["resulting_state"] == "RECOVERY"
    assert last["persistence"]["run_broken_by_gap_hours"] == 6.0


# ---- 10-12. capabilities and audit trail --------------------------------------------------------

def test_capabilities_are_reevaluated_after_every_accepted_transition(tmp_path):
    steps = run_state_sequence("flood-cycle", cycle_service(tmp_path), audit(tmp_path))
    transitions = [s for s in steps if s["state_before"] != s["state_after"]]
    assert [s["capability_state"] for s in transitions] == ["ALERT", "EMERGENCY", "RECOVERY",
                                                            "NORMAL"]
    assert [(s["capabilities_granted"], s["capabilities_revoked"]) for s in transitions] == [
        (["propose_evacuation_plan", "request_evidence_reverification"], []),
        (["request_human_emergency_authorization"], []),
        (["prepare_recovery_assessment"],
         ["propose_evacuation_plan", "request_human_emergency_authorization"]),
        ([], ["prepare_recovery_assessment", "request_evidence_reverification"]),
    ]
    unchanged = [s for s in steps if s["state_before"] == s["state_after"]]
    assert all(s["capability_state"] == s["state_after"] for s in unchanged)


def test_audit_trail_records_the_transition_and_the_reevaluation(tmp_path):
    log = audit(tmp_path)
    result = step(tmp_path, with_history(sim_record("NORMAL"), [("ALERT", 46)]), 48, log=log)
    entries = read_audit_log(log.path)
    events = [e["event"] for e in entries]
    accepted = next(e["details"] for e in entries
                    if e["event"] == "disaster_state_transition_accepted")
    assert accepted["current_state"] == "NORMAL" and accepted["resulting_state"] == "ALERT"
    assert accepted["previous_state"] == "NORMAL"
    assert accepted["evidence_classes"] == {"river": "SIMULATED", "weather": "SIMULATED",
                                            "traffic": "SIMULATED"}
    assert accepted["authorizes_actions"] is False
    assert accepted["state_decision_id"] == result["state_decision_id"]
    i = events.index("disaster_state_transition_accepted")
    j = events.index("capabilities_reevaluated_after_transition")
    assert i < j < events.index("river_workflow_completed")
    # Every re-made decision is audited under the new state.
    after = [e for e in entries[j:] if e["details"].get("disaster_state") == "ALERT"
             and e["event"].startswith("capability_")]
    assert len(after) >= len(result["capability_decisions"])


class FailsOnEvent(AuditLog):
    def __init__(self, path, event):
        super().__init__(path, component="river_workflow")
        self.failing_event = event

    def log(self, run_id, event, **details):
        if event == self.failing_event:
            self.write_errors.append(f"{event}: simulated write failure")
            return False
        return super().log(run_id, event, **details)


def test_unaudited_reevaluation_undoes_the_transition_and_its_permissions(tmp_path):
    log = FailsOnEvent(tmp_path / "f.jsonl", "capabilities_reevaluated_after_transition")
    result = step(tmp_path, with_history(sim_record("NORMAL"), [("ALERT", 46)]), 48, log=log)
    state = result["state_evaluation"]
    assert state["outcome"] == REJECTED and state["resulting_state"] == "NORMAL"
    assert "AUDIT_TRAIL_INCOMPLETE" in state["reasons"]
    assert result["capability_state"] == "NORMAL"
    assert result["capability_changes"]["changed"] is False
    assert all(d["state"] == "NORMAL" for d in result["capability_decisions"])


# ---- real mode: automatic persistence -----------------------------------------------------------

def real_dashboard(tmp_path, state_file, **kw):
    return DashboardService(make_service(tmp_path, clean_live_session()), state_file=state_file,
                            audit_path=tmp_path / "dash.jsonl", weather_fetcher=LIVE_WEATHER,
                            cache_seconds=0, **kw)


def test_real_mode_saves_and_carries_the_record_forward(tmp_path):
    path = tmp_path / "real_state.json"
    dash = real_dashboard(tmp_path, path)
    first, _ = dash.get("real")
    assert first["state"]["automatic"]["record_saved"]["saved"] is True
    saved = load_state_record(path)
    assert saved["scope"] == "real" and len(saved["history"]) == 1
    second, _ = dash.get("real")
    assert second["state"]["state_basis"] == "PERSISTED_RECORD"
    # The fixture's real station has no thresholds (risk INDETERMINATE), so each run adds a
    # gap marker; the state stays NORMAL and the history stays bounded (history_limit).
    record = load_state_record(path)
    assert record["state"] == "NORMAL" and len(record["history"]) == 2
    assert {e["condition"] for e in record["history"]} == {"INSUFFICIENT"}


def test_real_mode_never_overwrites_an_invalid_record(tmp_path):
    path = tmp_path / "real_state.json"
    path.write_text(json.dumps({**new_state_record("ALERT", "real"), "state": "EMERGENCY"}),
                    encoding="utf-8")
    before = path.read_bytes()
    doc, _ = real_dashboard(tmp_path, path).get("real")
    assert doc["state"]["automatic"]["record_saved"]["saved"] is False
    assert "invalid" in doc["state"]["automatic"]["record_saved"]["reason"]
    assert path.read_bytes() == before


def test_real_mode_without_traffic_reports_it_unavailable(tmp_path):
    doc, _ = real_dashboard(tmp_path, tmp_path / "s.json").get("real")
    traffic = doc["state"]["automatic"]["agent_evidence"]["traffic"]
    assert traffic["evidence_class"] == "UNAVAILABLE" and "TRAFFIC_NOT_PROVIDED" in traffic["reasons"]


def test_real_record_in_recovery_cannot_leave_with_simulated_traffic(tmp_path):
    path = tmp_path / "real_state.json"
    save_state_record(new_state_record("RECOVERY", "real"), path)
    dash = real_dashboard(tmp_path, path, traffic_fetcher=lambda: traffic_at("normal_flow"))
    doc, _ = dash.get("real")
    auto = doc["state"]["automatic"]
    assert auto["accepted_state"] == "RECOVERY"
    assert auto["traffic"]["status"] == "SCOPE_MISMATCH"


def test_the_state_cannot_be_chosen_through_the_dashboard(tmp_path):
    dash = real_dashboard(tmp_path, tmp_path / "s.json")
    for mode, scenario in (("real", None), ("simulated", "simulated-high-risk"),
                           ("simulated_live", "stable")):
        with pytest.raises(ValueError, match="evaluated automatically"):
            dash.get(mode, scenario, "EMERGENCY")
    assert dash.scenarios()["state_selection"] == "AUTOMATIC"
    assert "start_states" not in dash.scenarios()
