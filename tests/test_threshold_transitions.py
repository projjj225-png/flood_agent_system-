"""Threshold-driven automatic disaster-state transitions and the dynamic scenario profiles.

Everything runs through the real demonstration engine (src/simulation_engine.py): each
reading is written to the River Agent's CSV and evaluated by run_river_workflow (QC,
freshness, risk, evidence layer, Disaster State Manager, capability control). Nothing
assigns a state. TEST thresholds for UPPER_ANICUT_SIM: warning 75.0 m, danger 75.6 m,
recovery level 74.8 m (warning - 0.2 m, config/disaster_state.json).
"""

import copy
import hashlib
from datetime import timedelta

import pytest

from src.disaster_state_manager import validate_state_config
from src.river_monitoring_agent import PROJECT_DIR
from tests.test_flood_insights import REAL_RIVER_AUDIT, REAL_STATE_FILE, make_sim

WARNING, DANGER, RECOVERY_LEVEL = 75.0, 75.6, 74.8


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def run(sim, ticks):
    snaps = []
    for _ in range(ticks):
        sim.tick()
        snaps.append(sim.snapshot())
    return snaps


def state_of(snap):
    return snap["insights"]["state"]


@pytest.fixture(scope="module")
def cycle(tmp_path_factory):
    from tests.test_flood_insights import RiverEvidenceService
    sim = make_sim(RiverEvidenceService.from_file(), tmp_path_factory.mktemp("thr"))
    snaps = [sim.snapshot()]
    while len(sim.transitions) < 4 and sim.tick_count < 70:
        sim.tick()
        snaps.append(sim.snapshot())
    return sim, snaps


@pytest.fixture
def service():
    from tests.test_flood_insights import RiverEvidenceService
    return RiverEvidenceService.from_file()


# ---- 1-4. the four transitions follow the configured thresholds -----------------------------

def _transition(sim, to):
    return next(t for t in sim.transitions if t["to"] == to)


def test_normal_to_alert_when_warning_conditions_persist(cycle):
    sim, snaps = cycle
    t = _transition(sim, "ALERT")
    assert t["from"] == "NORMAL" and t["threshold"]["name"] == "warning"
    assert t["threshold"]["level_m"] == WARNING and t["threshold"]["official"] is False
    assert t["river_level_m"] >= WARNING and t["persistence"]["observations"] >= 2
    i = next(k for k, s in enumerate(snaps) if state_of(s)["current"] == "ALERT")
    # The previous evaluation was already at/above warning but did not confirm on its own.
    prev = state_of(snaps[i - 1])
    assert prev["current"] == "NORMAL" and prev["condition"] == "ALERT"
    assert prev["proposed_next"] == "ALERT"
    # No ALERT before the river reached the warning level, whatever the risk said.
    for s in snaps[:i - 1]:
        assert state_of(s)["threshold_check"]["level_m"] < WARNING or \
            state_of(s)["condition"] == "ALERT"


def test_alert_to_emergency_when_danger_conditions_persist(cycle):
    sim, _ = cycle
    t = _transition(sim, "EMERGENCY")
    assert t["from"] == "ALERT" and t["threshold"] == {
        "name": "danger", "level_m": DANGER, "label": "TEST / RESEARCH", "official": False}
    assert t["river_level_m"] >= DANGER and t["persistence"]["observations"] >= 2
    assert "danger level 75.6 m" in t["detail"]


def test_emergency_to_recovery_when_recovery_criteria_hold(cycle):
    sim, snaps = cycle
    t = _transition(sim, "RECOVERY")
    assert t["from"] == "EMERGENCY" and t["threshold"]["name"] == "recovery"
    assert t["threshold"]["level_m"] == RECOVERY_LEVEL and t["river_level_m"] < RECOVERY_LEVEL
    assert t["persistence"]["span_hours"] >= 2
    # Hysteresis: below warning but above the recovery level never left EMERGENCY.
    for s in snaps:
        st = state_of(s)
        lv = (st["threshold_check"] or {}).get("level_m")
        if st["current"] == "EMERGENCY" and lv is not None and RECOVERY_LEVEL <= lv < WARNING:
            assert st["condition"] == "ELEVATED"
            assert "not yet below the recovery level" in st["reasons"][0]


def test_recovery_to_normal_after_stable_recovery(cycle):
    sim, snaps = cycle
    t = _transition(sim, "NORMAL")
    assert t["from"] == "RECOVERY" and t["persistence"]["span_hours"] >= 12
    assert t["river_level_m"] < RECOVERY_LEVEL
    entered = next(i for i, s in enumerate(snaps) if state_of(s)["current"] == "RECOVERY")
    left = next(i for i, s in enumerate(snaps) if i > entered and state_of(s)["current"] == "NORMAL")
    assert left - entered >= 4  # RECOVERY is a real state that lasts, not a synonym for NORMAL
    recovery_caps = {c["capability"]: c["status"] for c in snaps[entered]["insights"]["capabilities"]}
    assert recovery_caps["propose_evacuation_plan"] == "DENIED"


def test_every_transition_records_full_provenance(cycle):
    sim, _ = cycle
    assert [(t["from"], t["to"]) for t in sim.transitions] == [
        ("NORMAL", "ALERT"), ("ALERT", "EMERGENCY"), ("EMERGENCY", "RECOVERY"),
        ("RECOVERY", "NORMAL")]
    for t in sim.transitions:
        assert t["at"] and t["evaluated_at"] and t["evidence_id"]
        assert t["supporting_evidence_ids"] and t["threshold"] and t["detail"]
        assert isinstance(t["river_level_m"], float)
    last = sim.record["last_transition"]
    assert set(last) >= {"from", "to", "at", "evidence_id", "threshold", "river_level_m",
                         "reason", "supporting_evidence_ids"}


# ---- 5. a single noisy reading does not cause a transition ------------------------------------

def test_single_noisy_reading_does_not_raise_alert(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="ALERT")
    while sim.readings[-1]["level_m"] < 74.6:
        sim.tick()
    sim.set_scenario("NORMAL")  # the river turns back below the warning level
    spike = sim.tick(inject_level=WARNING + 0.08)
    assert state_of(spike)["condition"] == "ALERT" and state_of(spike)["current"] == "NORMAL"
    assert state_of(spike)["proposed_next"] == "ALERT"
    after = run(sim, 6)
    assert all(state_of(s)["current"] == "NORMAL" for s in after)
    assert sim.transitions == []


# ---- 6. stale or missing evidence never produces a false NORMAL --------------------------------

@pytest.mark.parametrize("held_state", ["EMERGENCY", "RECOVERY"])
def test_outage_keeps_the_last_accepted_state(service, tmp_path, held_state):
    sim = make_sim(service, tmp_path)
    while sim.record["state"] != held_state:
        sim.tick()
        assert sim.tick_count < 60
    snaps = [sim.tick(withhold_readings=True) for _ in range(6)]
    stale = [s for s in snaps if state_of(s)["outcome"] == "INSUFFICIENT_EVIDENCE"]
    assert stale, "a 6 h outage must make the river evidence stale"
    for s in snaps:
        assert state_of(s)["current"] == held_state
    last = stale[-1]
    assert last["insights"]["risk_score"]["category"] == "INDETERMINATE"
    assert last["insights"]["risk_score"]["score"] is None
    assert last["insights"]["agents"]["river"]["status"] == "STALE"
    assert last["insights"]["trust"]["score"] <= 59
    assert "insufficient" in state_of(last)["why"]
    assert any(a["kind"] == "insufficient" for a in sim.snapshot()["activity"])


# ---- 7-8. scenarios set conditions only; real state untouched ----------------------------------

def test_selecting_a_scenario_never_assigns_the_state(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="NORMAL")
    for scenario in ("EMERGENCY", "RECOVERY", "ALERT"):
        sim.set_scenario(scenario)
        assert sim.record["state"] == "NORMAL" and sim.transitions == []
    sim.set_scenario("EMERGENCY")
    states = [state_of(s)["current"] for s in run(sim, 12)]
    assert states[0] == "NORMAL"
    assert "ALERT" in states and states.index("ALERT") < states.index("EMERGENCY")


def test_simulation_never_modifies_real_operational_state(service, tmp_path):
    before = (digest(REAL_STATE_FILE), digest(REAL_RIVER_AUDIT))
    sim = make_sim(service, tmp_path)
    run(sim, 30)
    assert (digest(REAL_STATE_FILE), digest(REAL_RIVER_AUDIT)) == before
    assert sim.record["scope"] == "simulated"


# ---- 9. the graph receives changing timestamped readings for every scenario --------------------

def _readings(snap):
    return [(r["t"], r["level_m"]) for r in snap["readings"] if not r["warmup"]]


def _check_timestamps(snap, minutes=15):
    from datetime import datetime
    times = [datetime.fromisoformat(r["t"]) for r in snap["readings"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert all(b - a == timedelta(minutes=minutes) for a, b in zip(times, times[1:]))


def test_normal_profile_fluctuates_but_stays_low(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="NORMAL")
    snap = run(sim, 12)[-1]
    _check_timestamps(snap)
    levels = [v for _, v in _readings(snap)]
    assert len(levels) == 12 * 4
    assert max(levels) - min(levels) >= 0.05          # visibly not a flat line
    assert max(levels) < RECOVERY_LEVEL                # but well below every threshold
    assert len(snap["rainfall"]) >= 24 and all("t" in r and "mm" in r for r in snap["rainfall"])


def test_alert_profile_rises_through_warning(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="ALERT")
    snap = run(sim, 12)[-1]
    levels = [v for _, v in _readings(snap)]
    assert levels[-1] > levels[0] + 1.0 and max(levels) >= WARNING and max(levels) < DANGER
    steps = [b - a for a, b in zip(levels, levels[1:])]
    assert max(abs(x) for x in steps) < 0.25          # no impossible jumps
    assert len({round(x, 3) for x in steps}) > 10     # fluctuations, not a straight line


def test_emergency_profile_peaks_and_stays_high(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="EMERGENCY")
    snaps = run(sim, 18)
    levels = [v for _, v in _readings(snaps[-1])]
    assert max(levels) >= DANGER + 0.3
    assert all(v >= DANGER for v in levels[-16:])     # sustained over the last 4 h
    assert state_of(snaps[-1])["current"] == "EMERGENCY"


def test_recovery_profile_declines_with_fluctuations(service, tmp_path):
    sim = make_sim(service, tmp_path, scenario="EMERGENCY")
    run(sim, 14)
    sim.set_scenario("RECOVERY")
    snaps = run(sim, 8)
    tail = [v for _, v in _readings(snaps[-1])][-32:]
    assert tail[0] - tail[-1] > 1.0                    # a real decline
    steps = [b - a for a, b in zip(tail, tail[1:])]
    assert len({round(x, 3) for x in steps}) > 10 and max(abs(x) for x in steps) < 0.25
    assert state_of(snaps[-1])["current"] in ("EMERGENCY", "RECOVERY")  # never straight to NORMAL


def test_every_step_updates_all_dashboard_values(service, tmp_path):
    sim = make_sim(service, tmp_path)
    a, b = run(sim, 2)
    assert b["latest_observation"]["observation_id"] != a["latest_observation"]["observation_id"]
    assert b["simulation"]["sim_time"] > a["simulation"]["sim_time"]
    assert len(b["series"]) == len(a["series"]) + 1
    assert b["readings"][-1]["t"] > a["readings"][-1]["t"]
    assert b["rainfall"][-1]["t"] > a["rainfall"][-1]["t"]
    for key in ("risk_score", "trust", "state", "agents", "capabilities"):
        assert key in b["insights"]
    assert b["insights"]["agents"]["river"]["evidence_id"] != a["insights"]["agents"]["river"]["evidence_id"]


# ---- configuration safety -----------------------------------------------------------------------

def _raw():
    import json
    return json.loads((PROJECT_DIR / "config" / "disaster_state.json").read_text("utf-8"))


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["conditions"]["ALERT"].update(river_threshold_min="SOMEWHERE"),
     "river_threshold_min"),
    (lambda c: c["river_thresholds"].update(recovery_margin_m=0), "recovery_margin_m"),
    (lambda c: c.pop("river_thresholds"), "recovery_margin_m"),
    (lambda c: c["conditions"]["NORMAL"].update(below_recovery_level="yes"),
     "below_recovery_level"),
    (lambda c: (c["conditions"]["ALERT"].update(river_threshold_min="AT_OR_ABOVE_DANGER"),
                c["conditions"]["EMERGENCY"].update(river_threshold_min="AT_OR_ABOVE_WARNING")),
     "lower river threshold"),
])
def test_threshold_config_errors_fail_safely(mutate, message):
    config = copy.deepcopy(_raw())
    mutate(config)
    with pytest.raises(ValueError, match=message):
        validate_state_config(config)
