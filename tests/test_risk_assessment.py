"""Tests for the Risk Assessment Engine and its workflow integration. Offline.

River evidence comes from the real RiverEvidenceService on fabricated CSVs;
weather comes from the real WeatherAgent parser on fabricated payloads.
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest

import src.river_agent_workflow as workflow
from src.audit_log import AuditLog, read_audit_log
from src.capability_control import REAL_WORLD_CAPABILITIES
from src.risk_assessment import (
    CRITICAL,
    HIGH,
    INDETERMINATE,
    LOW,
    MODERATE,
    WEATHER_SIMULATED_SOURCE_TYPE,
    assess_flood_risk,
    forecast_rainfall,
    load_risk_config,
    load_simulated_weather,
    validate_risk_config,
)
from src.river_agent_workflow import (
    RISK_SCENARIOS,
    run_risk_scenario,
    run_river_workflow,
    run_simulated_scenario,
)
from src.river_monitoring_agent import PROJECT_DIR
from src.river_service import compute_evidence_id
from src.weather_agent import WeatherAgent
from tests.test_river_service import clean_live_session, make_service

SIM_AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))
HEADER = "timestamp,station_id,river_level_m,source_type\n"
CONFIG = load_risk_config()
SIM_WEATHER = PROJECT_DIR / "data" / "simulated" / "weather"
REAL_WEATHER_SAMPLE = PROJECT_DIR / "outputs" / "weather_live_20261010T082315Z.json"


def audit(tmp_path, name="wf.jsonl"):
    return AuditLog(tmp_path / name, component="river_workflow")


def scenario(tmp_path, name, state="ALERT", log=None):
    return run_risk_scenario(name, make_service(tmp_path), log or audit(tmp_path),
                             disaster_state=state)


def simulated_evidence(tmp_path, levels, as_of=SIM_AS_OF, thresholds=True):
    """Evidence from the real service for a fabricated hourly series ending 11:00 UTC."""
    path = tmp_path / "river.csv"
    start = datetime(2026, 10, 10, 11, 0, tzinfo=timezone.utc) - timedelta(hours=len(levels) - 1)
    rows = "".join(f"{(start + timedelta(hours=i)).isoformat()},UPPER_ANICUT_TEST,{v},simulated\n"
                   for i, v in enumerate(levels))
    path.write_text(HEADER + rows, encoding="utf-8")
    service = make_service(tmp_path, simulated={"path": str(path),
                                                "station_id": "UPPER_ANICUT_TEST",
                                                "cross_check_paths": []},
                           thresholds_path=workflow.TEST_THRESHOLDS_PATH if thresholds else None)
    return service.get_evidence(mode="simulated", as_of=as_of)


def weather_result(past_mm_per_h, future_mm_per_h, retrieved_at=SIM_AS_OF, simulated=True):
    """WeatherAgent.parse_response on a fabricated payload: 96 h from 00:00 IST the day
    before retrieval."""
    start = (retrieved_at.astimezone(IST) - timedelta(days=1)).replace(
        hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    last_done = retrieved_at.astimezone(IST).replace(minute=0, tzinfo=None)
    times = [start + timedelta(hours=i) for i in range(96)]
    precip = [past_mm_per_h if t <= last_done else future_mm_per_h for t in times]
    hourly = {"time": [t.strftime("%Y-%m-%dT%H:%M") for t in times], "precipitation": precip,
              "rain": precip}
    for var in ("wind_gusts_10m", "wind_speed_10m", "precipitation_probability",
                "temperature_2m", "relative_humidity_2m"):
        hourly[var] = [10.0] * 96
    payload = {"latitude": 10.86, "longitude": 78.61, "elevation": 73.0,
               "utc_offset_seconds": 19800, "timezone": "Asia/Kolkata",
               "timezone_abbreviation": "GMT+5:30", "hourly": hourly,
               "hourly_units": {k: "mm" for k in hourly},
               "current": {"time": retrieved_at.astimezone(IST).strftime("%Y-%m-%dT%H:%M"),
                           "interval": 900, "temperature_2m": 27.0,
                           "relative_humidity_2m": 80.0, "precipitation": 0.0, "rain": 0.0,
                           "wind_speed_10m": 5.0, "wind_direction_10m": 90.0,
                           "wind_gusts_10m": 10.0},
               "current_units": {}}
    result = WeatherAgent().parse_response(payload, retrieved_at)
    if simulated:
        result["source_type"] = WEATHER_SIMULATED_SOURCE_TYPE
    return result


def rules(assessment):
    return assessment["rules_fired"]


def codes(items):
    return {i["code"] for i in items}


# ---- configuration -------------------------------------------------------------------

def test_default_config_is_valid_and_documents_categories():
    assert set(CONFIG["categories"]) == {LOW, MODERATE, HIGH, CRITICAL, INDETERMINATE}
    assert "not official" in json.dumps(CONFIG).lower() or "NOT flood thresholds" in \
        CONFIG["rainfall"]["reference"]


@pytest.mark.parametrize("mutate,message", [
    (lambda c: c["rainfall"].update(observed_24h_mm={"MODERATE": 100, "HIGH": 50}),
     "below"),
    (lambda c: c["rainfall"].update(forecast_mm={"HIGH": 50}), "exactly"),
    (lambda c: c["river"].update(rise_m_per_hour=0.5), "rapid"),
    (lambda c: c["river"].update(rise_m_per_hour="fast"), "positive number"),
    (lambda c: c.update(low_requires=["vibes"]), "unknown"),
    (lambda c: c["rainfall"].update(forecast_hours=0), "forecast_hours"),
    (lambda c: c.pop("river"), "'river'"),
])
def test_invalid_config_is_rejected(mutate, message):
    raw = json.loads((PROJECT_DIR / "config" / "risk_assessment.json").read_text("utf-8"))
    mutate(raw)
    with pytest.raises(ValueError, match=message):
        validate_risk_config(raw)


# ---- each risk category (data-driven simulated scenarios) ------------------------------

@pytest.mark.parametrize("name,level,confidence,review", [
    ("low-rain-valid-river", LOW, "MEDIUM", False),
    ("heavy-forecast-stable-river", MODERATE, "MEDIUM", False),
    ("rising-river-increasing-rain", HIGH, "MEDIUM", True),
    ("simulated-high-risk", CRITICAL, "MEDIUM", True),
    ("recovery-after-high-water", MODERATE, "MEDIUM", False),
    ("stale-river", INDETERMINATE, "NONE", True),
    ("missing-weather", INDETERMINATE, "LOW", True),
    ("conflicting-evidence", INDETERMINATE, "NONE", True),
])
def test_risk_scenarios(tmp_path, name, level, confidence, review):
    result = scenario(tmp_path, name)
    risk = result["risk_assessment"]
    assert risk["risk_level"] == level
    assert risk["evidence_quality"]["confidence"] == confidence
    assert risk["requires_human_review"] is review
    # Always clearly simulated, never presented as an actual current observation.
    assert risk["assessment_mode"] == "simulated" and risk["is_simulated"] is True
    assert risk["evidence_quality"]["confidence_applies_to"] == "SIMULATION_ONLY"
    assert "SIMULATED_DATA" in codes(risk["uncertainties"])
    river = risk["supporting_evidence"]["river"]
    assert "current_level_m" not in river and "simulated_level_m" in river
    assert result["river_level_m"] is None
    # The risk result never authorizes or assigns anything.
    assert risk["authorizes_actions"] is False and risk["assigns_disaster_state"] is False
    assert risk["calibrated_probability"] is None
    assert result["real_world_authorization"] == "RESTRICTED"


def test_every_risk_scenario_file_exists():
    for spec in RISK_SCENARIOS.values():
        assert (PROJECT_DIR / spec["river"]).is_file()
        assert spec["weather"] is None or (PROJECT_DIR / spec["weather"]).is_file()


def test_low_needs_every_required_input(tmp_path):
    risk = scenario(tmp_path, "low-rain-valid-river")["risk_assessment"]
    assert rules(risk) == ["R3_RIVER_BELOW_WARNING", "R6_RIVER_STEADY_OR_FALLING",
                           "R8_OBSERVED_RAINFALL_24H", "R9_FORECAST_RAINFALL"]
    assert codes(risk["recommended_monitoring_actions"]) == {"CONTINUE_ROUTINE_MONITORING"}
    # The test thresholds are reported as not official.
    assert risk["supporting_evidence"]["river"]["threshold_is_official"] is False
    assert "THRESHOLDS_NOT_OFFICIAL" in codes(risk["uncertainties"])
    assert "THRESHOLDS_NOT_OFFICIAL" in risk["evidence_quality"]["confidence_downgrades"]


def test_critical_comes_from_the_danger_threshold(tmp_path):
    risk = scenario(tmp_path, "simulated-high-risk")["risk_assessment"]
    r1 = next(f for f in risk["risk_factors"] if f["rule_id"] == "R1_RIVER_AT_OR_ABOVE_DANGER")
    assert r1["evidence"]["danger_level_m"] == 4.0 and r1["evidence"]["level_m"] == 4.4
    assert "not official" in r1["description"]
    assert "ELEVATED_RISK" in risk["human_review_reasons"]


def test_rain_alone_never_reaches_critical(tmp_path):
    evidence = simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0])
    risk = assess_flood_risk(evidence, weather_result(20.0, 20.0), config=CONFIG)
    assert risk["risk_level"] == HIGH  # 480 mm observed and forecast, river steady and low
    assert "R12_RIVER_RESPONSE_LAG" in rules(risk)


# ---- rising river and rainfall ----------------------------------------------------------

def test_rising_river_with_heavy_rain_is_escalated(tmp_path):
    risk = scenario(tmp_path, "rising-river-increasing-rain")["risk_assessment"]
    assert {"R5_RIVER_RISING", "R10_RAINFALL_INTENSIFYING",
            "R11_COMPOUND_RISE_AND_RAIN"} <= set(rules(risk))
    compound = next(f for f in risk["risk_factors"] if f["rule_id"] ==
                    "R11_COMPOUND_RISE_AND_RAIN")
    assert compound["evidence"] == {"from_level": MODERATE, "to_level": HIGH}
    assert "INCREASE_RIVER_MONITORING_FREQUENCY" in codes(risk["recommended_monitoring_actions"])


@pytest.mark.parametrize("levels,rule,level", [
    ([2.0, 2.15, 2.30, 2.45], "R5_RIVER_RISING", MODERATE),        # 0.15 m/h
    ([1.5, 1.85, 2.20, 2.55], "R4_RIVER_RAPID_RISE", HIGH),        # 0.35 m/h
    ([2.0, 2.02, 2.04, 2.06], "R6_RIVER_STEADY_OR_FALLING", LOW),   # 0.02 m/h
])
def test_rising_trend_rules(tmp_path, levels, rule, level):
    risk = assess_flood_risk(simulated_evidence(tmp_path, levels), weather_result(0.1, 0.1),
                             config=CONFIG)
    assert rule in rules(risk)
    assert risk["risk_level"] == level


def test_heavy_forecast_with_stable_river_accounts_for_lag(tmp_path):
    risk = scenario(tmp_path, "heavy-forecast-stable-river")["risk_assessment"]
    forecast = next(f for f in risk["risk_factors"] if f["rule_id"] == "R9_FORECAST_RAINFALL")
    assert forecast["hazard_level"] == MODERATE and forecast["evidence"]["total_mm"] == 130.8
    assert "R12_RIVER_RESPONSE_LAG" in rules(risk)
    assert "RIVER_RESPONSE_LAG" in codes(risk["uncertainties"])
    assert "RECHECK_RIVER_WITHIN_LAG" in codes(risk["recommended_monitoring_actions"])


def test_recovery_after_high_water(tmp_path):
    risk = scenario(tmp_path, "recovery-after-high-water")["risk_assessment"]
    recession = next(f for f in risk["risk_factors"]
                     if f["rule_id"] == "R7_RECEDING_AFTER_HIGH_WATER")
    assert recession["evidence"]["baseline_level_m"] == 3.4
    assert "MONITOR_RECESSION" in codes(risk["recommended_monitoring_actions"])


def test_forecast_rainfall_window():
    weather = weather_result(0.0, 2.0)
    out = forecast_rainfall(weather["hourly_weather"], SIM_AS_OF, 24)
    # 12:00 UTC = 17:30 IST: hours ending 18:00 IST .. 17:00 IST next day.
    assert out["status"] == "AVAILABLE" and out["total_mm"] == 48.0
    assert out["period_start"] == "2026-10-10T17:00:00+05:30"
    assert out["period_end"] == "2026-10-11T17:00:00+05:30"


def test_forecast_rainfall_is_never_partly_summed():
    weather = weather_result(0.0, 2.0)
    late = SIM_AS_OF + timedelta(hours=40)  # series ends before 24 h of forecast
    assert forecast_rainfall(weather["hourly_weather"], late, 24)["status"] == "UNAVAILABLE"
    hourly = copy.deepcopy(weather["hourly_weather"])
    hourly["variables"]["precipitation"][45] = None
    out = forecast_rainfall(hourly, SIM_AS_OF, 24)
    assert out["status"] == "UNAVAILABLE" and "null" in out["reason"]
    assert forecast_rainfall(None, SIM_AS_OF, 24)["status"] == "UNAVAILABLE"
    assert forecast_rainfall(hourly, datetime(2026, 10, 10), 24)["status"] == "UNAVAILABLE"


# ---- missing, stale and conflicting evidence ---------------------------------------------

def test_stale_river_is_not_low(tmp_path):
    risk = scenario(tmp_path, "stale-river")["risk_assessment"]
    assert risk["risk_level"] == INDETERMINATE and risk["hazard_indicated"] is None
    assert "EVIDENCE_STALE" in risk["evidence_quality"]["river_blocking_reasons"]
    assert "RIVER_EVIDENCE_STALE" in codes(risk["uncertainties"])
    assert "OBTAIN_CURRENT_RIVER_READING" in codes(risk["recommended_monitoring_actions"])


def test_stale_river_with_heavy_rain_still_reports_the_rain(tmp_path):
    evidence = simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0],
                                  as_of=SIM_AS_OF + timedelta(hours=8))
    risk = assess_flood_risk(evidence, weather_result(
        8.0, 8.0, retrieved_at=SIM_AS_OF + timedelta(hours=8)), config=CONFIG)
    assert risk["risk_level"] == INDETERMINATE
    assert risk["hazard_indicated"] == HIGH  # 192 mm observed: shown, not hidden
    assert "R12_RIVER_RESPONSE_LAG" not in rules(risk)  # no river evidence to lag behind


def test_missing_weather_blocks_low_but_not_high(tmp_path):
    risk = scenario(tmp_path, "missing-weather")["risk_assessment"]
    assert "R15_LOW_NOT_SUPPORTED" in rules(risk)
    assert {"weather_evidence"} <= {m["item"] for m in risk["missing_data"]}
    assert "RESTORE_WEATHER_EVIDENCE" in codes(risk["recommended_monitoring_actions"])
    high = assess_flood_risk(simulated_evidence(tmp_path, [3.1, 3.2, 3.3, 3.4]), None,
                             config=CONFIG)
    assert high["risk_level"] == HIGH  # positive hazard evidence still counts
    assert "INCOMPLETE_WEATHER" in high["evidence_quality"]["confidence_downgrades"]


def test_no_thresholds_blocks_low(tmp_path):
    evidence = simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0], thresholds=False)
    risk = assess_flood_risk(evidence, weather_result(0.1, 0.1), config=CONFIG)
    assert risk["risk_level"] == INDETERMINATE
    assert "river_thresholds" in {m["item"] for m in risk["missing_data"]}
    assert "CONFIGURE_THRESHOLDS" in codes(risk["recommended_monitoring_actions"])


def test_unresolved_conflict_is_indeterminate(tmp_path):
    result = scenario(tmp_path, "conflicting-evidence")
    risk = result["risk_assessment"]
    assert result["reverification"]["outcome"] == "CONFLICT_PERSISTS"
    assert "EVIDENCE_UNRESOLVED_CONFLICT" in risk["evidence_quality"]["river_blocking_reasons"]
    assert {"UNRESOLVED_SOURCE_CONFLICT"} <= codes(risk["uncertainties"])
    assert "SOURCE_CONFLICT_DETECTED" in risk["human_review_reasons"]
    assert "RESOLVE_SOURCE_CONFLICT" in codes(risk["recommended_monitoring_actions"])


def test_resolved_conflict_allows_assessment(tmp_path):
    weather = load_simulated_weather(SIM_WEATHER / "light_rain.json")
    result = run_simulated_scenario("conflict-resolves", make_service(
        tmp_path, thresholds_path=workflow.TEST_THRESHOLDS_PATH), audit(tmp_path),
        as_of=SIM_AS_OF, disaster_state="ALERT", weather=weather)
    risk = result["risk_assessment"]
    assert result["reverification"]["outcome"] == "RESOLVED"
    assert risk["risk_level"] == HIGH  # 3.35 m >= TEST warning 3.0 m
    assert "SOURCE_CONFLICT_RESOLVED" in codes(risk["uncertainties"])
    assert risk["requires_human_review"] is True


def test_conflict_in_normal_state_cannot_be_cleared(tmp_path):
    risk = scenario(tmp_path, "conflicting-evidence", state="NORMAL")["risk_assessment"]
    assert risk["supporting_evidence"]["river"]["reverification_outcome"] == "NOT_AUTHORIZED"
    assert risk["risk_level"] == INDETERMINATE


# ---- invalid inputs fail safe ------------------------------------------------------------

@pytest.mark.parametrize("evidence", [None, "not evidence", 42, [], {}, {"data_quality": 5},
                                      {"data_quality": {}, "freshness": "x", "trend": []}])
def test_invalid_river_input_is_indeterminate(evidence):
    risk = assess_flood_risk(evidence, weather_result(0.1, 0.1), config=CONFIG)
    assert risk["risk_level"] == INDETERMINATE
    assert risk["requires_human_review"] is True
    assert risk["evidence_quality"]["confidence"] == "NONE"


@pytest.mark.parametrize("weather", ["junk", {"status": "OK"}, {"rainfall_accumulation": {}}])
def test_invalid_weather_input_is_ignored_safely(tmp_path, weather):
    risk = assess_flood_risk(simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0]), weather,
                             config=CONFIG)
    assert risk["risk_level"] == INDETERMINATE
    assert risk["supporting_evidence"]["weather"]["used"] is False


def test_bad_rate_value_is_treated_as_missing_trend(tmp_path):
    evidence = simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0])
    evidence["trend"]["rate_of_change_m_per_hour"] = "fast"
    evidence["evidence_id"] = compute_evidence_id(evidence)  # test the rate, not integrity
    risk = assess_flood_risk(evidence, weather_result(0.1, 0.1), config=CONFIG)
    assert "river_trend" in {m["item"] for m in risk["missing_data"]}
    assert risk["risk_level"] == INDETERMINATE


def test_engine_error_fails_safe_in_workflow(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("engine broke")
    monkeypatch.setattr(workflow, "assess_flood_risk", boom)
    result = scenario(tmp_path, "low-rain-valid-river")
    risk = result["risk_assessment"]
    assert risk["risk_level"] == INDETERMINATE and risk["error"] == "RuntimeError: engine broke"
    assert risk["requires_human_review"] is True
    assert risk["assessment_mode"] == "simulated"


# ---- simulated vs real / historical ---------------------------------------------------------

def test_simulated_weather_is_labelled():
    weather = load_simulated_weather(SIM_WEATHER / "extreme_rain.json")
    assert weather["source_type"] == WEATHER_SIMULATED_SOURCE_TYPE
    assert "SIMULATED" in weather["source"] and weather["source_url"] is None
    assert weather["rainfall_accumulation"]["windows"]["24h"]["precipitation"]["total"] == 156.0


def test_non_simulated_file_is_refused_as_simulated_weather(tmp_path):
    path = tmp_path / "w.json"
    path.write_text(json.dumps({"retrieved_at": "2026-10-10T12:00:00+00:00", "payload": {}}),
                    encoding="utf-8")
    with pytest.raises(ValueError, match="simulated"):
        load_simulated_weather(path)


def test_simulated_weather_is_never_mixed_with_real_river(tmp_path):
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY",
                                weather=weather_result(0.1, 0.1, retrieved_at=datetime(
                                    2026, 10, 13, 8, 0, tzinfo=timezone.utc)))
    risk = result["risk_assessment"]
    assert risk["assessment_mode"] == "real"
    assert "WEATHER_MODE_MISMATCH" in risk["supporting_evidence"]["weather"]["not_used_reasons"]


def test_real_weather_is_never_mixed_with_simulated_river(tmp_path):
    evidence = simulated_evidence(tmp_path, [2.0, 2.0, 2.0, 2.0])
    risk = assess_flood_risk(evidence, weather_result(0.1, 0.1, simulated=False), config=CONFIG)
    assert "WEATHER_MODE_MISMATCH" in risk["evidence_quality"]["weather_not_used_reasons"]


def test_weather_must_be_time_aligned(tmp_path):
    sample = json.loads(REAL_WEATHER_SAMPLE.read_text(encoding="utf-8"))["result"]
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY", weather=sample)
    weather = result["risk_assessment"]["supporting_evidence"]["weather"]
    assert weather["used"] is False and "WEATHER_NOT_TIME_ALIGNED" in weather["not_used_reasons"]


def test_live_verified_fresh_without_thresholds(tmp_path):
    now = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    result = run_river_workflow(make_service(tmp_path, clean_live_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY",
                                weather=weather_result(0.0, 0.0, retrieved_at=now,
                                                       simulated=False))
    risk = result["risk_assessment"]
    assert risk["assessment_mode"] == "real"
    assert risk["supporting_evidence"]["river"]["current_level_m"] == 74.95
    assert risk["supporting_evidence"]["weather"]["used"] is True
    assert "WEATHER_MODEL_BASED" in codes(risk["uncertainties"])
    # No official thresholds exist for Upper Anicut, so LOW cannot be claimed.
    assert risk["risk_level"] == INDETERMINATE and "R15_LOW_NOT_SUPPORTED" in rules(risk)


def test_real_stale_live_data_is_indeterminate(tmp_path):
    from tests.nwdp_fixtures import ok_session
    result = run_river_workflow(make_service(tmp_path, ok_session()), audit(tmp_path),
                                mode="live", disaster_state="EMERGENCY")
    risk = result["risk_assessment"]
    assert risk["assessment_mode"] == "real" and risk["risk_level"] == INDETERMINATE
    assert "last_known_level_m" in risk["supporting_evidence"]["river"]
    assert "current_level_m" not in risk["supporting_evidence"]["river"]


def test_historical_replay_is_labelled(tmp_path):
    as_of = datetime(2025, 12, 30, 6, 0, tzinfo=IST)
    result = run_river_workflow(make_service(tmp_path), audit(tmp_path), mode="historical",
                                as_of=as_of, disaster_state="ALERT")
    risk = result["risk_assessment"]
    assert risk["assessment_mode"] == "historical"
    assert risk["evidence_quality"]["confidence_applies_to"] == "HISTORICAL_REPLAY"
    assert "HISTORICAL_REPLAY" in codes(risk["uncertainties"])
    assert "historical_level_m" in risk["supporting_evidence"]["river"]
    assert risk["risk_level"] == INDETERMINATE  # no official thresholds, no weather


# ---- workflow integration: audit, permissions, determinism -----------------------------------

def test_risk_assessment_is_audited(tmp_path):
    log = audit(tmp_path)
    result = scenario(tmp_path, "rising-river-increasing-rain", log=log)
    entries = read_audit_log(log.path)
    events = [e["event"] for e in entries]
    assert events.index("capability_authorization") < events.index(
        "risk_assessment_completed") < events.index("river_workflow_completed")
    risk_event = entries[events.index("risk_assessment_completed")]
    assert risk_event["run_id"] == result["run_id"]
    details = risk_event["details"]
    assert details["assessment_id"] == result["risk_assessment"]["assessment_id"]
    assert details["risk_level"] == HIGH and details["authorizes_actions"] is False
    assert details["rules_fired"] == result["risk_assessment"]["rules_fired"]
    assert entries[-1]["details"]["risk_assessment_id"] == result["risk_assessment_id"]
    assert result["risk_assessment"]["audit_written"] is True


def test_unwritable_audit_forces_review_and_denies_everything(tmp_path):
    (tmp_path / "wf.jsonl").mkdir()
    result = scenario(tmp_path, "low-rain-valid-river")
    risk = result["risk_assessment"]
    assert risk["audit_written"] is False
    assert risk["requires_human_review"] and "AUDIT_WRITE_FAILED" in risk["human_review_reasons"]
    assert all(d["allowed"] is False for d in result["capability_decisions"])


def test_risk_never_changes_permissions(tmp_path):
    """Same evidence, very different risk: identical capability decisions."""
    service = make_service(tmp_path, clean_live_session())
    now = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
    calm = run_river_workflow(service, audit(tmp_path, "a.jsonl"), mode="live",
                              disaster_state="ALERT")
    service = make_service(tmp_path, clean_live_session())
    storm = run_river_workflow(service, audit(tmp_path, "b.jsonl"), mode="live",
                               disaster_state="ALERT",
                               weather=weather_result(20.0, 20.0, retrieved_at=now,
                                                      simulated=False))
    assert storm["risk_assessment"]["hazard_indicated"] == HIGH
    strip = lambda r: [{k: v for k, v in d.items()} for d in r["capability_decisions"]]
    assert strip(calm) == strip(storm)
    assert calm["disaster_state"] == storm["disaster_state"] == "ALERT"
    assert calm["real_world_authorization"] == storm["real_world_authorization"]


def test_critical_simulation_authorizes_nothing(tmp_path):
    result = scenario(tmp_path, "simulated-high-risk", state="EMERGENCY")
    assert result["risk_assessment"]["risk_level"] == CRITICAL
    for cap in REAL_WORLD_CAPABILITIES:
        d = next(x for x in result["capability_decisions"] if x["capability"] == cap)
        assert d["allowed"] is False and "EVIDENCE_SIMULATED" in d["evidence_reasons"]
    assert result["disaster_state"] == "EMERGENCY"  # unchanged: supplied by the caller


def test_assessment_and_decision_ids_are_deterministic(tmp_path):
    a = scenario(tmp_path, "rising-river-increasing-rain")
    b = scenario(tmp_path, "rising-river-increasing-rain")
    assert a["run_id"] != b["run_id"]
    assert a["risk_assessment_id"] == b["risk_assessment_id"]
    assert a["decision_id"] == b["decision_id"]
    c = scenario(tmp_path, "simulated-high-risk")
    assert c["risk_assessment_id"] != a["risk_assessment_id"]


def test_existing_scenario_without_weather_or_thresholds(tmp_path):
    result = run_simulated_scenario("no-conflict", make_service(tmp_path), audit(tmp_path),
                                    as_of=SIM_AS_OF, disaster_state="ALERT")
    assert result["simulation_permission"] == "SIMULATION_ALLOWED"  # unchanged behaviour
    risk = result["risk_assessment"]
    # Rising 0.18 m/h is positive hazard evidence even with no weather or thresholds.
    assert risk["risk_level"] == MODERATE and "R5_RIVER_RISING" in rules(risk)
    assert {"river_thresholds", "weather_evidence"} <= {m["item"] for m in risk["missing_data"]}
    assert {"NO_THRESHOLD_COMPARISON", "INCOMPLETE_WEATHER"} <= set(
        risk["evidence_quality"]["confidence_downgrades"])
    assert risk["evidence_quality"]["confidence"] == "LOW"


# ---- CLIs ---------------------------------------------------------------------------------------

def _cli_config(tmp_path):
    from tests.test_river_service import BASE_CONFIG
    config = json.loads(json.dumps(BASE_CONFIG))
    config["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                         "audit_log": str(tmp_path / "service_audit.jsonl"),
                         "evidence": str(tmp_path / "evidence.json")}
    path = tmp_path / "config.json"
    path.write_text(json.dumps(config), encoding="utf-8")
    return str(path)


def test_workflow_cli_risk_scenario(tmp_path, capsys):
    code = workflow.main(["--mode", "simulated", "--risk-scenario", "simulated-high-risk",
                          "--state", "ALERT", "--config", _cli_config(tmp_path),
                          "--audit", str(tmp_path / "a.jsonl")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0
    assert out["risk_assessment"]["risk_level"] == CRITICAL
    assert out["real_world_authorization"] == "RESTRICTED"


def test_workflow_cli_weather_file(tmp_path, capsys):
    code = workflow.main(["--mode", "simulated", "--scenario", "no-conflict",
                          "--weather", str(SIM_WEATHER / "light_rain.json"),
                          "--config", _cli_config(tmp_path), "--audit", str(tmp_path / "a.jsonl")])
    out = json.loads(capsys.readouterr().out)
    assert code == 0 and out["risk_assessment"]["supporting_evidence"]["weather"]["used"]


def test_workflow_cli_rejects_risk_scenario_outside_simulated():
    with pytest.raises(SystemExit):
        workflow.main(["--mode", "historical", "--risk-scenario", "stale-river"])


def test_risk_cli_on_saved_files(tmp_path, capsys):
    from src.risk_assessment import main
    evidence = simulated_evidence(tmp_path, [3.7, 3.9, 4.1, 4.3])
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps(evidence, default=str), encoding="utf-8")
    assert main([str(path), "--weather", str(SIM_WEATHER / "extreme_rain.json"),
                 "--json"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["risk_level"] == CRITICAL and out["assessment_mode"] == "simulated"
    assert main([str(tmp_path / "missing.json")]) == 2
