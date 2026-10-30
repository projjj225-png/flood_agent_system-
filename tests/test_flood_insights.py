"""Flood Risk Score, Evidence Trust Score, insights and the continuous demonstration
simulation (src/risk_score.py, src/trust_score.py, src/insights.py,
src/simulation_engine.py). Offline: only fabricated, labelled scenario data is used.
"""

import copy
import hashlib
import json
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest

from src.audit_log import AuditLog
from src.disaster_state_manager import load_state_config
from src.insights import build_insights
from src.risk_assessment import load_risk_config
from src.risk_score import (
    compute_risk_score,
    load_scoring_config,
    rainfall_index,
    river_level_index,
    validate_scoring_config,
)
from src.river_agent_workflow import run_risk_scenario
from src.river_api import create_server
from src.river_monitoring_agent import PROJECT_DIR
from src.river_service import RiverEvidenceService
from src.simulation_engine import (
    CYCLE,
    DemoSimulation,
    load_demo_config,
    next_level,
    validate_demo_config,
)
from src.trust_score import compute_trust_score

SCORING = load_scoring_config()
RISK_CFG = load_risk_config()
STATE_CFG = load_state_config()
REAL_STATE_FILE = PROJECT_DIR / "outputs" / "state" / "real_state.json"
REAL_RIVER_AUDIT = PROJECT_DIR / "outputs" / "audit" / "river_audit.jsonl"
START = datetime(2026, 10, 30, 6, 30, tzinfo=timezone.utc)


@pytest.fixture(scope="module")
def service():
    return RiverEvidenceService.from_file()


@pytest.fixture(scope="module")
def scenario(service, tmp_path_factory):
    """Run a fabricated risk scenario once and cache (result, insights)."""
    audit = AuditLog(tmp_path_factory.mktemp("audit") / "scenario_audit.jsonl")
    cache = {}

    def run(name, state="NORMAL"):
        if (name, state) not in cache:
            result = run_risk_scenario(name, service, audit, disaster_state=state,
                                       include_evidence=True)
            cache[(name, state)] = (result, build_insights(result, SCORING, RISK_CFG, STATE_CFG))
        return cache[(name, state)]
    return run


def cap(insights, name):
    return next(c for c in insights["capabilities"] if c["capability"] == name)


# ---- 1-4: risk score ---------------------------------------------------------------------

def test_low_risk_with_reliable_evidence(scenario):
    _, ins = scenario("low-rain-valid-river")
    score, trust = ins["risk_score"], ins["trust"]
    assert score["available"] and score["category"] == "LOW" and 0 <= score["score"] <= 24
    assert trust["band"] == "HIGH" and trust["score"] >= 80
    assert trust["is_simulated"] and "NOT verified" in trust["simulated_note"]
    assert ins["evidence_summary"]["accepted"] == 3
    assert {line["label"] for line in ins["risk_explanation"]["lines"]} >= {
        "River level", "River trend", "Rainfall (past 24 h)", "Evidence trust"}
    assert "River level" in ins["risk_explanation"]["lowering"]


def test_high_risk_with_reliable_evidence(scenario):
    _, ins = scenario("simulated-high-risk")
    score = ins["risk_score"]
    assert score["category"] == "CRITICAL" and 75 <= score["score"] <= 100
    assert score["dominant_factor"] == "river_level"
    assert ins["trust"]["band"] == "HIGH"
    assert "River level" in ins["risk_explanation"]["raising"]
    assert ins["risk_explanation"]["combined"] == "Critical flood risk"
    assert score["summary"].startswith("Critical flood risk")


def test_high_risk_with_low_trust(scenario):
    """Risk and trust are independent: a usable river reading can show high danger while
    missing corroborating evidence makes the evidence as a whole weakly trusted."""
    result, _ = scenario("simulated-high-risk")
    degraded = copy.deepcopy(result)
    degraded["traffic_evidence"] = None
    degraded["state_evaluation"]["traffic"] = None
    layer = degraded["evidence_layer"]
    layer["records"] = [r for r in layer["records"] if r.get("kind") != "WEATHER_MODEL"]
    ins = build_insights(degraded, SCORING, RISK_CFG, STATE_CFG)
    assert ins["risk_score"]["category"] == "CRITICAL" and ins["risk_score"]["score"] >= 75
    assert ins["trust"]["band"] in ("LOW", "VERY_LOW") and ins["trust"]["score"] < 60
    assert ins["trust"]["completeness"] == 50
    assert ins["agents"]["traffic"]["status"] == "MISSING"


@pytest.mark.parametrize("name", ["missing-weather", "stale-river", "conflicting-evidence"])
def test_insufficient_evidence_is_indeterminate_never_low(scenario, name):
    _, ins = scenario(name)
    score = ins["risk_score"]
    assert score["category"] == "INDETERMINATE"
    assert score["score"] is None and score["available"] is False and score["band"] is None
    assert score["missing"] and score["summary"].startswith("No defensible flood-risk score")
    assert ins["risk_explanation"]["combined"].startswith("Risk indeterminate")
    assert "risk INDETERMINATE" in ins["headline"]


def test_missing_weather_shows_agent_missing(scenario):
    _, ins = scenario("missing-weather")
    assert ins["agents"]["weather"]["status"] == "MISSING"
    assert ins["evidence_summary"]["missing"] == 1
    assert any("Weather" in d["reason"] for d in ins["trust"]["deductions"])


def test_risk_band_follows_engine_category_exactly(scenario):
    for name in ("low-rain-valid-river", "heavy-forecast-stable-river",
                 "rising-river-increasing-rain", "simulated-high-risk",
                 "recovery-after-high-water"):
        result, ins = scenario(name)
        lo, hi = SCORING["risk_bands"][result["risk_assessment"]["risk_level"]]
        assert lo <= ins["risk_score"]["score"] <= hi, name


def test_indicator_indices_match_engine_levels():
    assert river_level_index(74.0, 75.0, 75.6) < 25
    assert 50 <= river_level_index(75.2, 75.0, 75.6) < 75
    assert river_level_index(75.7, 75.0, 75.6) >= 75
    bands = RISK_CFG["rainfall"]["observed_24h_mm"]
    assert rainfall_index(10, bands) < 25 <= rainfall_index(70, bands) < 50 <= \
        rainfall_index(120, bands)


def test_risk_score_is_deterministic(scenario):
    result, ins = scenario("rising-river-increasing-rain")
    again = compute_risk_score(result["risk_assessment"],
                               result["evidence"]["threshold_evaluation"],
                               result["state_evaluation"]["traffic"], RISK_CFG, SCORING)
    assert again["score"] == ins["risk_score"]["score"]


def test_scoring_config_rejects_bad_bands():
    bad = json.loads(json.dumps(SCORING))
    bad["risk_bands"]["HIGH"] = [40, 74]
    with pytest.raises(ValueError):
        validate_scoring_config(bad)


# ---- 5-6: trust ---------------------------------------------------------------------------

def test_stale_evidence_reduces_trust(scenario):
    _, fresh = scenario("low-rain-valid-river")
    _, stale = scenario("stale-river")
    assert stale["trust"]["score"] < fresh["trust"]["score"]
    assert stale["trust"]["score"] <= SCORING["trust_caps"]["river_unusable"]
    assert stale["trust"]["freshness"] < fresh["trust"]["freshness"]
    assert stale["agents"]["river"]["status"] == "STALE"
    assert not stale["agents"]["river"]["accepted"]
    assert any(c["reason"].startswith("River evidence was not usable")
               for c in stale["trust"]["caps"])
    assert stale["evidence_summary"]["stale"] == 1


def test_conflicting_evidence_triggers_reverification(scenario):
    result, ins = scenario("conflicting-evidence", "ALERT")
    assert result["reverification"]["attempts"] >= 1
    assert result["reverification"]["outcome"] == "CONFLICT_PERSISTS"
    assert ins["agents"]["river"]["status"] == "CONFLICTING"
    assert ins["evidence_summary"]["conflicting"] == 1
    assert ins["evidence_summary"]["reverification"]["outcome"] == "CONFLICT_PERSISTS"
    assert ins["trust"]["consistency"] <= 40
    assert cap(ins, "request_evidence_reverification")["status"] == "ALLOWED"


def test_reverification_is_not_authorized_in_normal(scenario):
    result, ins = scenario("conflicting-evidence", "NORMAL")
    assert result["reverification"]["outcome"] == "NOT_AUTHORIZED"
    assert cap(ins, "request_evidence_reverification")["status"] == "DENIED"


def test_traffic_contradicting_river_lowers_consistency(scenario):
    _, ins = scenario("recovery-after-high-water")
    assert ins["agents"]["traffic"]["status"] == "CONFLICTING"
    assert ins["trust"]["consistency"] == 100 - \
        SCORING["trust_consistency_penalties"]["traffic_contradicts_river"]


def test_trust_score_is_deterministic_and_documented(scenario):
    result, ins = scenario("simulated-high-risk")
    again = compute_trust_score(result, SCORING)
    assert again == ins["trust"]
    weights = sum(f["weight"] for f in again["factors"])
    assert weights == 100 and {f["key"] for f in again["factors"]} == {
        "authenticity", "freshness", "quality", "completeness", "consistency",
        "timestamp_validity", "provenance"}


def test_capability_cards_never_claim_execution(scenario):
    _, ins = scenario("simulated-high-risk", "ALERT")
    for card in ins["capabilities"]:
        assert card["executes_actions"] is False
        assert card["status"] in ("ALLOWED", "RESTRICTED", "DENIED")
    assert cap(ins, "propose_evacuation_plan")["status"] == "RESTRICTED"
    assert "simulated evidence can never authorize" in cap(ins, "propose_evacuation_plan")[
        "reason"]
    assert cap(ins, "request_human_emergency_authorization")["human_approval_required"]


# ---- 7-13: continuous simulation and the full state cycle -----------------------------------

def demo_config(tmp_path, **overrides):
    config = load_demo_config()
    config.update(csv_path=str(tmp_path / "demo.csv"),
                  river_audit_path=str(tmp_path / "demo_river_audit.jsonl"), **overrides)
    return validate_demo_config(config)


def make_sim(service, tmp_path, scenario=CYCLE, **overrides):
    sim = DemoSimulation(service, demo_config(tmp_path, **overrides),
                         wall_clock=lambda: START, seed="test-seed")
    sim.reset(scenario, log=False)
    return sim


def _digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


@pytest.fixture(scope="module")
def cycle(service, tmp_path_factory):
    """One full automatic cycle; snapshots kept per tick. Also records the real
    state file and real river audit before and after."""
    tmp = tmp_path_factory.mktemp("cycle")
    before = (_digest(REAL_STATE_FILE), _digest(REAL_RIVER_AUDIT))
    sim = make_sim(service, tmp)
    snaps = [sim.snapshot()]
    for _ in range(60):
        sim.tick()
        snaps.append(sim.snapshot())
        if len(sim.transitions) >= 4:
            break
    after = (_digest(REAL_STATE_FILE), _digest(REAL_RIVER_AUDIT))
    return {"sim": sim, "snaps": snaps, "before": before, "after": after}


def at_transition(cycle, to_state, nth=0):
    hits = [s for s in cycle["snaps"] if s["insights"]["state"]["transition_this_evaluation"]
            and s["insights"]["state"]["current"] == to_state]
    return hits[nth]


def test_full_cycle_sequence(cycle):
    pairs = [(t["from"], t["to"]) for t in cycle["sim"].transitions]
    assert pairs == [("NORMAL", "ALERT"), ("ALERT", "EMERGENCY"), ("EMERGENCY", "RECOVERY"),
                     ("RECOVERY", "NORMAL")]
    for t in cycle["sim"].transitions:
        assert t["reason"] and t["evidence_id"] and t["persistence"]["observations"] >= 1


def test_normal_to_alert(cycle):
    snap = at_transition(cycle, "ALERT")
    state = snap["insights"]["state"]
    assert state["evaluated_from"] == "NORMAL" and state["condition"] in ("ALERT", "EMERGENCY")
    assert snap["insights"]["risk_score"]["category"] in ("HIGH", "CRITICAL")
    assert snap["simulation"]["active_conditions"] == "ALERT"


def test_alert_to_emergency(cycle):
    snap = at_transition(cycle, "EMERGENCY")
    state = snap["insights"]["state"]
    assert state["evaluated_from"] == "ALERT" and state["condition"] == "EMERGENCY"
    assert snap["insights"]["risk_score"]["category"] == "CRITICAL"
    transition = cycle["sim"].transitions[1]
    assert transition["persistence"]["observations"] >= 2  # configured escalation persistence


def test_emergency_to_recovery(cycle):
    snap = at_transition(cycle, "RECOVERY")
    state = snap["insights"]["state"]
    assert state["evaluated_from"] == "EMERGENCY" and state["condition"] in ("IMPROVED",
                                                                            "NORMAL")
    assert snap["insights"]["agents"]["river"]["metrics"]["threshold_status"] == "BELOW_WARNING"
    assert cycle["sim"].transitions[2]["persistence"]["span_hours"] >= 2


def test_recovery_to_normal(cycle):
    snap = at_transition(cycle, "NORMAL")
    assert snap["insights"]["state"]["evaluated_from"] == "RECOVERY"
    assert snap["insights"]["risk_score"]["category"] == "LOW"
    assert snap["insights"]["agents"]["traffic"]["metrics"]["route_status"] == "ROUTES_USABLE"
    transition = cycle["sim"].transitions[3]
    assert transition["persistence"]["span_hours"] >= 12  # configured recovery period


def test_no_state_skipped_and_hysteresis_holds(cycle):
    """Between transitions the state is held even while conditions change (e.g. ALERT
    condition during EMERGENCY does not drop the state back)."""
    states = [s["insights"]["state"]["current"] for s in cycle["snaps"]]
    collapsed = [s for i, s in enumerate(states) if i == 0 or s != states[i - 1]]
    assert collapsed == ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY", "NORMAL"]
    in_emergency = [s for s in cycle["snaps"] if s["insights"]["state"]["current"] == "EMERGENCY"]
    assert any(s["insights"]["state"]["condition"] == "ALERT" for s in in_emergency)


EXPECTED = {
    "NORMAL": {"read_river": "ALLOWED", "request_evidence_reverification": "DENIED",
               "prepare_advisory": "RESTRICTED", "propose_evacuation_plan": "DENIED",
               "request_human_emergency_authorization": "DENIED"},
    "ALERT": {"read_river": "ALLOWED", "request_evidence_reverification": "ALLOWED",
              "prepare_advisory": "RESTRICTED", "propose_evacuation_plan": "RESTRICTED",
              "request_human_emergency_authorization": "DENIED"},
    "EMERGENCY": {"read_river": "ALLOWED", "request_evidence_reverification": "ALLOWED",
                  "prepare_advisory": "RESTRICTED", "propose_evacuation_plan": "RESTRICTED",
                  "request_human_emergency_authorization": "RESTRICTED"},
    "RECOVERY": {"read_river": "ALLOWED", "request_evidence_reverification": "ALLOWED",
                 "prepare_advisory": "RESTRICTED", "propose_evacuation_plan": "DENIED",
                 "request_human_emergency_authorization": "DENIED"},
}


@pytest.mark.parametrize("to_state", ["ALERT", "EMERGENCY", "RECOVERY", "NORMAL"])
def test_capabilities_recalculated_after_every_transition(cycle, to_state):
    snap = at_transition(cycle, to_state)
    cards = {c["capability"]: c for c in snap["insights"]["capabilities"]}
    assert {k: c["status"] for k, c in cards.items()} == EXPECTED[to_state]
    assert all(c["state"] == to_state for c in cards.values())
    if to_state == "RECOVERY":
        for name in ("propose_evacuation_plan", "request_human_emergency_authorization"):
            assert cards[name]["change"] == "REVOKED" and "Revoked" in cards[name]["reason"]
        assert set(cycle["sim"].transitions[2]["capabilities_revoked"]) >= {
            "propose_evacuation_plan", "request_human_emergency_authorization"}
    if to_state == "EMERGENCY":
        assert cards["request_human_emergency_authorization"]["change"] == "GRANTED"


def test_simulated_evidence_never_enters_real_state(cycle):
    assert cycle["before"] == cycle["after"]  # real state file and real river audit untouched
    sim = cycle["sim"]
    assert sim.record["scope"] == "simulated"
    for snap in cycle["snaps"]:
        assert snap["is_simulated"] and snap["data_label"] == "SIMULATED LIVE"
        assert snap["simulation"]["state_record_persisted"] is False
        assert snap["simulation"]["real_state_affected"] is False
        assert snap["insights"]["state"]["applies_to"] == "SIMULATION_ONLY"
        assert snap["insights"]["real_world_authorization"] == "RESTRICTED"
        assert snap["simulation"]["thresholds"]["official"] is False


def test_simulation_updates_observations_continuously(cycle):
    snaps = cycle["snaps"]
    ids = [s["latest_observation"]["observation_id"] for s in snaps]
    assert len(set(ids)) == len(ids)  # a new, uniquely identified reading every tick
    times = [s["simulation"]["sim_time"] for s in snaps]
    assert times == sorted(times) and len(set(times)) == len(times)
    last = snaps[-1]
    assert len(last["series"]) == len(snaps)
    assert all(e["risk_category"] and e["trust_score"] is not None for e in last["series"])
    assert last["latest_observation"]["provenance"]["generator"].startswith("DemoRiverGenerator")
    levels = [s["latest_observation"]["level_m"] for s in snaps]
    # Gradual, not independent random numbers: no step larger than the configured max rise.
    assert max(abs(b - a) for a, b in zip(levels, levels[1:])) < 0.4
    evidence_ids = [s["insights"]["agents"]["river"]["evidence_id"] for s in snaps]
    assert len(set(evidence_ids)) == len(evidence_ids)


def test_generator_is_deterministic():
    spec = load_demo_config()["scenarios"]["ALERT"]
    a = next_level(74.0, spec, 1.0, "seed", START)
    assert a == next_level(74.0, spec, 1.0, "seed", START)
    assert a != next_level(74.0, spec, 1.0, "other-seed", START)
    assert 74.0 < a <= 74.0 + spec["max_rise_m_per_hour"] + spec["noise_m"]


def test_scenario_changes_conditions_not_state(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="NORMAL")
    sim.set_scenario("EMERGENCY")
    assert sim.record["state"] == "NORMAL" and sim.active_scenario() == "EMERGENCY"
    sim.tick()
    assert sim.record["state"] in ("NORMAL", "ALERT")  # never straight to EMERGENCY
    with pytest.raises(ValueError):
        sim.set_scenario("PANIC")
    with pytest.raises(ValueError):
        sim.control("set_state")


def test_reset_restarts_from_normal(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="EMERGENCY")
    for _ in range(8):
        sim.tick()
    assert sim.record["state"] != "NORMAL"
    old = sim.session_id
    sim.control("reset", scenario="NORMAL")
    snap = sim.snapshot()
    assert sim.session_id != old and sim.record["state"] == "NORMAL"
    assert snap["simulation"]["tick"] == 0 and snap["transitions"] == []
    assert snap["insights"]["state"]["current"] == "NORMAL"


def test_background_ticker_pause_and_resume(service, tmp_path):
    sim = make_sim(service, tmp_path, tick_seconds=0.1)
    sim.wall_clock = lambda: datetime.now(timezone.utc)
    try:
        sim.start()
        deadline = time.monotonic() + 60
        while sim.tick_count < 2 and time.monotonic() < deadline:
            time.sleep(0.1)
        assert sim.tick_count >= 2
        sim.pause()
        paused_at = sim.tick_count
        time.sleep(0.6)
        assert sim.tick_count == paused_at and sim.snapshot()["simulation"]["running"] is False
    finally:
        sim.stop()


# ---- API -----------------------------------------------------------------------------------

@pytest.fixture
def sim_api(service, tmp_path):
    sim = make_sim(service, tmp_path)
    server = create_server(service, "127.0.0.1", 0, simulation=sim)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", sim
    server.shutdown()
    server.server_close()
    sim.stop()


def call(url, body=None):
    data = None if body is None else json.dumps(body).encode()
    request = urllib.request.Request(url, data=data, method="GET" if body is None else "POST",
                                      headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_simulation_routes(sim_api):
    base, sim = sim_api
    status, doc = call(base + "/simulation")
    assert status == 200 and doc["data_label"] == "SIMULATED LIVE"
    assert doc["insights"]["state"]["current"] == "NORMAL"
    status, doc = call(base + "/simulation/control", {"action": "scenario",
                                                       "scenario": "ALERT"})
    assert status == 200 and doc["simulation"]["selected_scenario"] == "ALERT"
    assert doc["insights"]["state"]["current"] == "NORMAL"  # conditions only
    status, doc = call(base + "/simulation/control", {"action": "speed", "speed": 5})
    assert status == 200 and doc["simulation"]["speed"] == 5
    status, doc = call(base + "/simulation/control", {"action": "pause"})
    assert status == 200 and doc["simulation"]["running"] is False


@pytest.mark.parametrize("body", [{"action": "scenario", "state": "EMERGENCY"},
                                  {"state": "EMERGENCY"}, {"action": "teleport"},
                                  {"action": "scenario", "scenario": "../x"}, ["reset"]])
def test_simulation_control_rejects_state_and_bad_input(sim_api, body):
    base, sim = sim_api
    status, doc = call(base + "/simulation/control", body)
    assert status == 400 and "error" in doc
    assert sim.record["state"] == "NORMAL"


def test_other_routes_stay_read_only(sim_api):
    base, _ = sim_api
    for path in ("/simulation", "/river/evidence", "/simulation/control/x"):
        status, doc = call(base + path, {"action": "reset"})
        assert status == 405


def test_state_why_explains_the_accepted_state_not_just_the_evidence(cycle):
    for snap in cycle["snaps"]:
        st = snap["insights"]["state"]
        assert st["why"]
        if st["transition_this_evaluation"]:
            assert st["why"].startswith(f"Entered {st['current']}")
        elif st["progress"]:
            assert st["why"].startswith(f"{st['current']} is kept while the evidence builds up")
    held = [s["insights"]["state"] for s in cycle["snaps"]
            if s["insights"]["state"]["current"] == "ALERT"
            and s["insights"]["state"]["condition"] == "EMERGENCY"
            and not s["insights"]["state"]["transition_this_evaluation"]]
    assert held and "ALERT → EMERGENCY needs 2 observation(s)" in held[0]["why"]


def test_traffic_never_listed_as_raising_risk_in_summary(scenario):
    _, ins = scenario("simulated-high-risk")
    assert "roads" not in ins["risk_score"]["summary"].lower()


def test_csv_replace_retries_a_briefly_locked_file(tmp_path, monkeypatch):
    """Windows can deny os.replace while another process holds the file for a moment."""
    import src.simulation_engine as engine
    calls = {"n": 0}
    real_replace = engine.os.replace

    def flaky(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError(5, "Access is denied")
        real_replace(src, dst)
    monkeypatch.setattr(engine.os, "replace", flaky)
    monkeypatch.setattr(engine.time, "sleep", lambda s: None)
    source, target = tmp_path / "a.tmp", tmp_path / "a.csv"
    source.write_text("x")
    engine.replace_with_retry(source, target)
    assert target.read_text() == "x" and calls["n"] == 3


def test_headline_uses_engine_trend_wording(scenario):
    _, ins = scenario("low-rain-valid-river")
    trend = next(f for f in ins["risk_score"]["factors"] if f["key"] == "river_trend")
    assert trend["value_text"].lower() in ins["headline"]
