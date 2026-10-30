"""Tests for the Real-Time River Simulation (src/river_simulation.py). Offline.

The clock is injected and advanced in 5-minute steps, so hours of "real time"
run in seconds. Every decision checked here comes from the existing pipeline:
River Agent validation and QC, the evidence gate, the risk engine and the
Disaster State Manager.
"""

import copy
import json
import math
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from src.audit_log import AuditLog
from src.capability_control import REAL_WORLD_CAPABILITIES
from src.dashboard_service import DashboardService
from src.river_api import create_server
from src.river_simulation import (
    SCENARIOS,
    RealtimeRiverSimulation,
    SyntheticRiverGenerator,
    floor_slot,
    level_bounds,
    load_simulation_config,
    realtime_weather,
    validate_simulation_config,
)
from tests.test_river_service import make_service

START = datetime(2026, 10, 30, 6, 2, 30, tzinfo=timezone.utc)  # mid-slot on purpose
STEP = timedelta(minutes=5)
REAL_QC = {"valid_min_m": 70.0, "valid_max_m": 80.0, "max_jump_m": 1.0,
           "invalid_values_m": [72.0], "reference": "test"}


class Clock:
    def __init__(self, start=START):
        self.now = start

    def __call__(self):
        return self.now

    def advance(self, delta=STEP):
        self.now += delta


def sim_config(tmp_path):
    config = load_simulation_config()
    config["output_dir"] = str(tmp_path / "simulated_live")
    return config


def make_sim(tmp_path, scenario, state="NORMAL", clock=None):
    clock = clock or Clock()
    service = make_service(tmp_path, clock=clock)
    sim = RealtimeRiverSimulation(service, scenario, state, sim_config(tmp_path), clock,
                                  seed="test-seed")
    return sim, clock, AuditLog(tmp_path / "sim_audit.jsonl", component="sim_test")


def walk(sim, clock, audit, minutes):
    """Tick every 5 minutes for ``minutes``; returns the result of every tick."""
    results = []
    for _ in range(minutes // 5 + 1):
        results.append(sim.tick(audit)[0])
        clock.advance()
    return results


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


# ---- configuration ---------------------------------------------------------------------

def test_shipped_config_is_valid_and_separate_from_the_real_station():
    config = load_simulation_config()
    assert set(config["scenarios"]) == set(SCENARIOS)
    assert config["station_id"] == "UPPER_ANICUT_SIM" != config["twin_of"] == "UPPER_ANICUT"
    assert config["qc_from_station"] == "UPPER_ANICUT"
    assert config["interval_minutes"] == 5
    assert "not official" in config["thresholds"]["UPPER_ANICUT_SIM"]["reference"].lower()
    for spec in config["scenarios"].values():  # every curve stays inside the real QC range
        low, high = level_bounds(spec)
        assert 70.0 <= low and high <= 80.0


@pytest.mark.parametrize("change, message", [
    (lambda c: c.update(station_id="UPPER_ANICUT"), "must not reuse"),
    (lambda c: c.update(interval_minutes=7), "divides 60"),
    (lambda c: c["scenarios"].pop("falling"), "exactly"),
    (lambda c: c["scenarios"]["stable"].update(base_m=float("nan")), "finite"),
    (lambda c: c["scenarios"]["stable"].update(noise_m=-1), ">= 0"),
    (lambda c: c.update(thresholds={}), "thresholds must include"),
])
def test_invalid_config_is_refused(change, message):
    config = json.loads(json.dumps(load_simulation_config()))
    change(config)
    with pytest.raises(ValueError, match=message):
        validate_simulation_config(config)


def test_generator_refuses_a_curve_that_could_leave_the_qc_range():
    spec = {**load_simulation_config()["scenarios"]["stable"], "base_m": 79.5}
    with pytest.raises(ValueError, match="QC range"):
        SyntheticRiverGenerator("stable", spec, "SIM", START, "s", qc=REAL_QC)


def test_generator_refuses_to_run_without_qc_rules():
    spec = load_simulation_config()["scenarios"]["stable"]
    with pytest.raises(ValueError, match="QC"):
        SyntheticRiverGenerator("stable", spec, "SIM", START, "s", qc=None)


# ---- generator ---------------------------------------------------------------------------

@pytest.mark.parametrize("scenario", SCENARIOS)
def test_readings_are_valid_unique_and_on_the_current_5_minute_grid(scenario):
    spec = load_simulation_config()["scenarios"][scenario]
    gen = SyntheticRiverGenerator(scenario, spec, "UPPER_ANICUT_SIM", START, "seed",
                                  qc=REAL_QC)
    now = START
    for _ in range(48):  # 4 hours of ticks
        gen.extend(now)
        now += STEP
    times = [r["observed_at"] for r in gen.readings]
    levels = [r["river_level_m"] for r in gen.readings]
    assert times == sorted(set(times))                       # unique, increasing
    assert all(b - a == STEP for a, b in zip(times, times[1:]))
    assert all(t.minute % 5 == 0 and t.second == 0 for t in times)
    assert times[-1] == floor_slot(now - STEP, 5)            # latest slot of the clock
    assert all(math.isfinite(v) and 70.0 <= v <= 80.0 for v in levels)
    assert 72.0 not in levels                                # the CWC fill value
    assert all(a != b for a, b in zip(levels, levels[1:]))   # no repeated placeholder-like runs
    assert all(round(v, 3) == v for v in levels)


def test_a_new_reading_appears_only_when_a_new_slot_starts():
    spec = load_simulation_config()["scenarios"]["stable"]
    gen = SyntheticRiverGenerator("stable", spec, "SIM", START, "s", qc=REAL_QC)
    first = gen.extend(START)
    assert len(first) == 6 * 12 + 1                          # 6 h history + current slot
    assert gen.extend(START + timedelta(minutes=2)) == []     # same slot: nothing new
    assert gen.extend(START + timedelta(minutes=3)) == [floor_slot(START, 5) + STEP]


def test_placeholder_and_repeated_values_are_never_emitted():
    flat = {"base_m": 72.0, "amplitude_m": 1.0, "rate_m_per_hour": 0.0, "wave_m": 0.0,
            "wave_period_hours": 6, "noise_m": 0.0, "weather": "x"}
    gen = SyntheticRiverGenerator("stable", flat, "SIM", START, "s", qc=REAL_QC)
    gen.extend(START)
    levels = [r["river_level_m"] for r in gen.readings]
    assert 72.0 not in levels
    assert all(a != b for a, b in zip(levels, levels[1:]))


def test_readings_are_deterministic_per_session_seed_and_differ_between_seeds():
    spec = load_simulation_config()["scenarios"]["stable"]
    a, b, c = (SyntheticRiverGenerator("stable", spec, "SIM", START, seed, qc=REAL_QC)
               for seed in ("one", "one", "two"))
    for gen in (a, b, c):
        gen.extend(START)
    assert a.readings == b.readings != c.readings


def test_retention_drops_only_the_oldest_readings():
    spec = load_simulation_config()["scenarios"]["stable"]
    gen = SyntheticRiverGenerator("stable", spec, "SIM", START, "s", history_hours=1,
                                  retention_hours=2, qc=REAL_QC)
    gen.extend(START + timedelta(hours=5))
    assert gen.readings[-1]["observed_at"] - gen.readings[0]["observed_at"] == \
        timedelta(hours=2)


# ---- through the existing evidence pipeline ----------------------------------------------

def test_generated_file_passes_the_river_agents_own_validation_and_qc(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "slowly-rising")
    result, recomputed = sim.tick(audit)
    assert recomputed
    ev = result["evidence"]
    quality = ev["data_quality"]
    assert quality["rows_read"] == quality["valid_rows"] == len(sim.generator.readings)
    assert quality["rejected_rows"] == 0
    assert quality["qc"]["accepted"] == quality["qc"]["checked"]
    assert quality["qc"]["invalid_values_m"] == [72.0]        # the real station's rules
    assert quality["qc"]["valid_min_m"] == 70.0 and quality["qc"]["max_jump_m"] == 1.0
    assert quality["recent_quality"]["status"] == "ACCEPTABLE"
    assert ev["station_id"] == "UPPER_ANICUT_SIM"
    assert ev["source_label"] == "SIMULATED_TEST_DATA"
    assert quality["evidence_grade"] == "SIMULATED" and quality["is_simulated"] is True
    assert quality["is_verified_live"] is False


def test_corrupt_rows_in_the_simulated_file_are_rejected_by_the_existing_pipeline(tmp_path):
    """The generator never writes these; if a file did contain them, the River Agent
    (not the simulation) rejects them."""
    sim, clock, audit = make_sim(tmp_path, "stable")
    sim.generator.extend(clock())
    sim.generator.write_csv(sim.csv_path)
    last = sim.generator.readings[-1]["observed_at"]
    with open(sim.csv_path, "a", encoding="utf-8", newline="") as f:
        f.write(f"{(last - timedelta(minutes=2)).isoformat()},UPPER_ANICUT_SIM,nan,simulated\n")
        f.write(f"{(last - timedelta(minutes=3)).isoformat()},UPPER_ANICUT_SIM,inf,simulated\n")
        f.write(f"{(last - timedelta(minutes=1)).isoformat()},UPPER_ANICUT_SIM,72.0,simulated\n")
        f.write(f"{(last - timedelta(minutes=4)).isoformat()},UPPER_ANICUT_SIM,79.9,simulated\n")
    ev = sim.service.get_evidence(mode="simulated")
    quality = ev["data_quality"]
    codes = {e["code"] for e in ev["errors"] + ev["warnings"]}
    assert "NON_NUMERIC_LEVEL" in codes
    assert quality["qc"]["rejected_placeholder"] == 1
    assert quality["qc"]["rejected_spike"] == 1
    assert ev["river_level_m"] == sim.generator.readings[-1]["river_level_m"]  # untouched


@pytest.mark.parametrize("scenario, direction", [
    ("slowly-rising", "RISING"), ("rapidly-rising", "RISING"), ("falling", "FALLING")])
def test_trend_freshness_and_quality_come_from_valid_observations(tmp_path, scenario,
                                                                  direction):
    sim, clock, audit = make_sim(tmp_path, scenario)
    result = sim.tick(audit)[0]
    ev = result["evidence"]
    assert ev["freshness"]["status"] == "FRESH"
    assert ev["freshness"]["reference_mode"] == "CURRENT_CLOCK"    # judged at the clock
    assert 0 <= ev["freshness"]["age_minutes"] < 5
    assert ev["observed_at"] == floor_slot(clock(), 5).isoformat()
    assert ev["trend"]["status"] == "AVAILABLE" and ev["trend"]["direction"] == direction
    assert ev["trend"]["observations_in_window"] == 37         # 3 h of 5-minute readings
    assert result["simulation_permission"] == "SIMULATION_ALLOWED"


def test_readings_go_stale_if_the_generator_stops(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "stable")
    sim.tick(audit)
    clock.advance(timedelta(hours=4))
    ev = sim.service.get_evidence(mode="simulated")    # file not extended: 4 h old
    assert ev["freshness"]["status"] == "STALE"


def test_weather_is_retimed_to_the_present_and_labelled_simulated():
    path = load_simulation_config()["scenarios"]["stable"]["weather"]
    from src.river_monitoring_agent import PROJECT_DIR
    weather = realtime_weather(PROJECT_DIR / path, START)
    assert weather["source_type"] == "SIMULATED_TEST_DATA"
    assert weather["retrieved_at"] == START.isoformat()
    assert "SIMULATED LIVE" in weather["source"]
    assert weather["rainfall_accumulation"]["windows"]["24h"]["precipitation"]["status"] == \
        "AVAILABLE"


# ---- risk and the four disaster states ---------------------------------------------------

def test_stable_river_stays_normal(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "stable")
    results = walk(sim, clock, audit, 120)
    assert {r["risk_assessment"]["risk_level"] for r in results} == {"LOW"}
    assert {r["state_evaluation"]["resulting_state"] for r in results} == {"NORMAL"}


def test_slowly_rising_river_below_warning_is_high_risk_but_not_alert(tmp_path):
    # ALERT needs the river itself at or above the configured warning level; a rising river
    # with heavy rain below it is HIGH risk, reported as ELEVATED, and the state stays NORMAL.
    sim, clock, audit = make_sim(tmp_path, "slowly-rising")
    results = walk(sim, clock, audit, 60)
    first = results[0]
    assert "R5_RIVER_RISING" in first["risk_assessment"]["rules_fired"]
    assert first["risk_assessment"]["risk_level"] == "HIGH"
    assert all(r["state_evaluation"]["condition"] == "ELEVATED" for r in results
               if r["evidence"]["threshold_evaluation"]["status"] == "BELOW_WARNING")
    assert sim.record["state"] == "NORMAL"


def test_rapidly_rising_river_escalates_to_emergency(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "rapidly-rising")
    results = walk(sim, clock, audit, 180)  # EMERGENCY needs two readings at/above danger
    states = [r["state_evaluation"]["resulting_state"] for r in results]
    assert states[0] == "NORMAL"                     # one reading never confirms ALERT
    assert states.index("ALERT") < states.index("EMERGENCY")  # NORMAL may only go to ALERT
    assert sim.record["state"] == "EMERGENCY"
    assert all(r["risk_assessment"]["risk_level"] == "CRITICAL" for r in results)
    # EMERGENCY permits the request by state, but simulated evidence never satisfies it.
    emergency = next(r for r in results if r["disaster_state"] == "EMERGENCY")
    d = decision(emergency, "request_human_emergency_authorization")
    assert d["allowed"] is False and d["reason"] == "EVIDENCE_INSUFFICIENT"
    assert "EVIDENCE_SIMULATED" in d["evidence_reasons"]


def test_falling_river_moves_emergency_to_recovery_and_revokes_emergency_capabilities(
        tmp_path):
    sim, clock, audit = make_sim(tmp_path, "falling", state="EMERGENCY")
    results = walk(sim, clock, audit, 180)
    assert sim.record["state"] == "RECOVERY"
    entered = next(i for i, r in enumerate(results)
                   if r["state_evaluation"]["resulting_state"] == "RECOVERY")
    assert entered >= 2 * 60 // 5                    # persistence: >= 2 h of improvement
    in_recovery = [r for r in results if r["disaster_state"] == "RECOVERY"]
    assert in_recovery
    for r in in_recovery:
        for cap in ("request_human_emergency_authorization", "propose_evacuation_plan"):
            d = decision(r, cap)
            assert d["allowed"] is False and d["reason"] == "CAPABILITY_DENIED"  # by state
        assert decision(r, "read_river")["allowed"] is True


def test_simulated_evidence_never_authorizes_real_world_action(tmp_path):
    for scenario, state in (("stable", "NORMAL"), ("rapidly-rising", "ALERT"),
                            ("falling", "EMERGENCY"), ("slowly-rising", "RECOVERY")):
        sim, clock, audit = make_sim(tmp_path / scenario, scenario, state)
        for r in walk(sim, clock, audit, 30):
            assert r["real_world_authorization"] == "RESTRICTED"
            assert r["real_world_capabilities_pending_human_approval"] == []
            assert r["river_level_m"] is None          # never handed out as real
            for d in r["capability_decisions"]:
                if d["capability"] in REAL_WORLD_CAPABILITIES:
                    assert d["allowed"] is False
            assert r["evidence_layer"]["real_world_evidence_sufficient"] is False
            assert r["state_evaluation"]["authorizes_actions"] is False


def test_state_record_is_simulated_scope_and_never_written(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "rapidly-rising")
    walk(sim, clock, audit, 30)
    assert sim.record["scope"] == "simulated"
    assert sim.info()["state_record_persisted"] is False
    assert not list(tmp_path.rglob("*state*.json"))


def test_missed_slots_are_evaluated_at_30_minute_spacing(tmp_path):
    sim, clock, audit = make_sim(tmp_path, "rapidly-rising")
    sim.tick(audit)
    clock.advance(timedelta(hours=2))
    sim.tick(audit)
    catch_up = [e for e in sim.timeline if e["catch_up"]]
    assert len(catch_up) == 4                          # 30, 60, 90 min ... before now
    times = [datetime.fromisoformat(e["evaluated_at"]) for e in catch_up]
    assert all(b - a == timedelta(minutes=30) for a, b in zip(times, times[1:]))
    assert sim.record["state"] == "EMERGENCY"          # the missed hours still counted


# ---- dashboard and API -------------------------------------------------------------------

def live_dashboard(tmp_path, clock):
    service = make_service(tmp_path, clock=clock)
    return DashboardService(service, state_file=tmp_path / "no_record.json",
                            audit_path=tmp_path / "dash_audit.jsonl",
                            weather_fetcher=lambda: pytest.fail("no live weather"),
                            simulation_config=sim_config(tmp_path))


def test_dashboard_document_is_labelled_simulated_live(tmp_path):
    clock = Clock()
    dash = live_dashboard(tmp_path, clock)
    doc, age = dash.get("simulated_live", "rapidly-rising")
    assert "error" not in doc, doc.get("error")
    assert doc["mode"] == "simulated_live" and doc["is_simulated"] is True
    assert doc["data_label"] == "SIMULATED LIVE"
    river = doc["river"]
    assert river["is_simulated"] is True and river["level"]["role"] == "SIMULATED"
    assert river["station_id"] == "UPPER_ANICUT_SIM"
    assert river["series"]["status"] == "AVAILABLE" and river["series"]["points"]
    assert river["freshness"]["status"] == "FRESH"
    assert doc["risk"]["is_simulated"] is True
    sim = doc["simulation"]
    assert sim["label"] == "SIMULATED LIVE" and sim["authorizes_real_world_actions"] is False
    assert "NOT Central Water Commission observations" in sim["disclaimer"]
    assert sim["thresholds"]["official"] is False
    assert doc["safety"]["real_world_authorization"] == "RESTRICTED"
    assert doc["state"]["state_basis"] == "SIMULATION_SESSION_RECORD"
    assert doc["state"]["record_saved_by_dashboard"] is False
    assert doc["state"]["label"] == "AUTOMATIC STATE EVALUATION"
    assert doc["state"]["automatic"]["agent_evidence"]["traffic"]["evidence_class"] in (
        "SIMULATED", "CONFLICTING")
    assert doc["state"]["automatic"]["record_saved"]["saved"] is False
    assert not (tmp_path / "no_record.json").exists()  # simulated: never the real file
    assert str(tmp_path) not in json.dumps(doc)       # no absolute paths leak


def test_dashboard_recomputes_only_when_a_new_reading_is_due(tmp_path):
    clock = Clock()
    dash = live_dashboard(tmp_path, clock)
    first, _ = dash.get("simulated_live", "stable")
    clock.advance(timedelta(minutes=1))
    again, age = dash.get("simulated_live", "stable")
    assert again is first and age >= 0
    clock.advance(timedelta(minutes=4))
    newer, _ = dash.get("simulated_live", "stable")
    assert newer["river"]["level"]["observed_at"] > first["river"]["level"]["observed_at"]
    assert newer["simulation"]["readings_generated"] == \
        first["simulation"]["readings_generated"] + 1


def test_dashboard_sessions_are_separate_per_scenario_and_start_in_normal(tmp_path):
    clock = Clock()
    dash = live_dashboard(tmp_path, clock)
    a, _ = dash.get("simulated_live", "stable")
    b, _ = dash.get("simulated_live", "falling")
    assert a["simulation"]["session_id"] != b["simulation"]["session_id"]
    for doc in (a, b):
        assert doc["simulation"]["start_state"] == "NORMAL"
        assert doc["state"]["evaluation"]["current_state"] == "NORMAL"


@pytest.mark.parametrize("scenario, state", [("flood", None), ("stable", "PANIC"),
                                             ("../../etc", None), ("falling", "EMERGENCY"),
                                             ("stable", "NORMAL")])
def test_dashboard_rejects_unknown_inputs_and_any_chosen_state(tmp_path, scenario, state):
    with pytest.raises(ValueError):
        live_dashboard(tmp_path, Clock()).get("simulated_live", scenario, state)


def test_scenario_list_includes_the_live_simulation(tmp_path):
    listing = live_dashboard(tmp_path, Clock()).scenarios()
    assert "simulated_live" in listing["modes"] and listing["default_mode"] == "real"
    live = listing["live_simulation"]
    assert live["available"] and live["interval_minutes"] == 5
    assert [s["name"] for s in live["scenarios"]] == list(SCENARIOS)
    assert live["data_label"] == "SIMULATED LIVE"


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_http_route_serves_the_live_simulation(tmp_path):
    dash = live_dashboard(tmp_path, Clock())
    server = create_server(dash.service, "127.0.0.1", 0, dashboard=dash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = get(base + "/dashboard?mode=simulated_live&scenario=falling")
        assert status == 200 and body["data_label"] == "SIMULATED LIVE"
        # The state cannot be chosen over HTTP either.
        status, refused = get(base + "/dashboard?mode=simulated_live&scenario=falling"
                                     "&state=EMERGENCY")
        assert status == 400 and "state" in refused["error"]
        assert body["safety"]["real_world_authorization"] == "RESTRICTED"
        status, body = get(base + "/dashboard?mode=simulated_live&scenario=nope")
        assert status == 400 and "scenario must be one of" in body["error"]
        status, body = get(base + "/dashboard?mode=simulated_live&path=x")
        assert status == 400
        status, body = get(base + "/dashboard/scenarios")
        assert status == 200 and body["live_simulation"]["available"] is True
    finally:
        server.shutdown()
        server.server_close()
