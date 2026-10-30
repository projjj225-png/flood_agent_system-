"""Hydrological analysis (src.hydro_analysis) and the stuck-value QC check. Offline."""

import math
import random
from datetime import datetime, timedelta, timezone

import pytest

from src.cwc_adapter import load_station_registry
from src.hydro_analysis import (
    HISTORICAL_CLASS,
    analyse_series,
    analysis_qc,
    build_station_analysis,
    completeness,
    find_gaps,
    upstream_relationship,
)
from src.river_monitoring_agent import (
    PROJECT_DIR,
    apply_quality_control,
    load_qc_file,
    validate_qc_config,
)

RAW = PROJECT_DIR / "data" / "cwc" / "rwl_tel_hr_cwc_009_2021_2025.csv"
T0 = datetime(2025, 6, 1, 0, 0, tzinfo=timezone.utc)
QC = validate_qc_config({"S": {"valid_min_m": 70, "valid_max_m": 80, "max_jump_m": 1.0,
                               "invalid_values_m": [72.0]}})["S"]


def obs(hour, level):
    return {"observed_at": T0 + timedelta(hours=hour), "river_level_m": level}


# ---- stuck-value QC check ------------------------------------------------------------

def test_stuck_check_is_off_unless_configured():
    readings = [obs(h, 74.5) for h in range(30)]
    accepted, rejected, summary = apply_quality_control(readings, QC)
    assert len(accepted) == 30 and "rejected_stuck" not in summary


def test_stuck_values_are_rejected_after_the_limit_and_the_run_start_kept():
    qc = {**QC, "max_constant_run_hours": 6}
    readings = [obs(h, 74.5) for h in range(12)] + [obs(12, 74.6), obs(13, 74.6)]
    accepted, rejected, summary = apply_quality_control(readings, qc)
    assert [o["observed_at"].hour for o in accepted] == [0, 1, 2, 3, 4, 5, 6, 12, 13]
    assert summary["rejected_stuck"] == 5
    assert {o["qc_code"] for o in rejected} == {"STUCK_VALUE"}


def test_stuck_limit_must_be_positive():
    with pytest.raises(ValueError, match="max_constant_run_hours"):
        validate_qc_config({"S": {"valid_min_m": 1, "valid_max_m": 2,
                                  "max_constant_run_hours": 0}})


def test_fill_values_stay_invalid():
    accepted, rejected, _ = apply_quality_control([obs(0, 74.0), obs(1, 72.0)], QC)
    assert [o["river_level_m"] for o in accepted] == [74.0]
    assert rejected[0]["qc_code"] == "PLACEHOLDER_VALUE"


def test_analysis_qc_is_only_ever_stricter():
    assert analysis_qc({**QC, "max_constant_run_hours": 3},
                       {"analysis_max_constant_run_hours": 6})["max_constant_run_hours"] == 3
    assert analysis_qc(QC, {"analysis_max_constant_run_hours": 6})["max_constant_run_hours"] == 6
    assert analysis_qc(None, {"analysis_max_constant_run_hours": 6}) is None


def test_shipped_qc_treats_musiris_81_17_m_as_a_fill_value():
    qc = load_qc_file(PROJECT_DIR / "config" / "river_qc.json")
    assert 81.17 in qc["MUSIRI"]["invalid_values_m"]
    assert qc["MUSIRI"]["max_constant_run_hours"] == 6
    assert qc["UPPER_ANICUT"].get("max_constant_run_hours") is None   # evidence unchanged


# ---- freshness, trends, statistics ---------------------------------------------------

def test_freshness_comes_from_the_observation_time_and_history_is_never_current():
    readings = [obs(h, 74.0 + h / 100) for h in range(10)]
    stale = analyse_series(readings, [], T0 + timedelta(days=300))
    assert stale["freshness"]["status"] == "STALE"
    assert stale["freshness"]["age_hours"] == pytest.approx(300 * 24 - 9)
    assert stale["data_class"] == HISTORICAL_CLASS and stale["is_current"] is False
    # Even a recent historical-file reading is labelled historical, not live.
    recent = analyse_series(readings, [], T0 + timedelta(hours=10))
    assert recent["freshness"]["status"] == "FRESH" and recent["is_current"] is False


def test_trends_use_actual_durations_with_irregular_sampling():
    readings = [obs(0, 74.0), obs(0.5, 74.02), obs(2.25, 74.09), obs(3, 74.12)]
    trends = analyse_series(readings, [], T0 + timedelta(hours=3))["trends"]
    three = trends["3h"]
    assert three["duration_hours"] == 3 and three["rate_of_change_m_per_hour"] == 0.04
    assert trends["1h"]["baseline_observed_at"] == (T0 + timedelta(hours=2.25)).isoformat()
    assert trends["1h"]["duration_hours"] == 0.75


def test_missing_hours_are_gaps_not_zeros_or_estimates():
    readings = [obs(h, 74.0) for h in range(5)] + [obs(h, 74.2) for h in range(20, 24)]
    a = analyse_series(readings, [], T0 + timedelta(hours=23))
    assert a["rolling_24h"]["readings"] == 9          # only measured readings
    assert a["rolling_24h"]["min_m"] == 74.0          # no zero crept in
    assert a["gaps"]["count"] == 1 and a["gaps"]["longest"][0]["hours"] == 16
    assert a["completeness"]["overall"] == pytest.approx(9 / 24, abs=1e-3)


def test_record_extremes_and_rejection_reasons():
    accepted = [obs(0, 73.1), obs(1, 75.0), obs(2, 74.0)]
    rejected = [{**obs(3, 72.0), "qc_code": "PLACEHOLDER_VALUE"},
                {**obs(4, 1070.8), "qc_code": "OUT_OF_RANGE"}]
    a = analyse_series(accepted, rejected, T0 + timedelta(hours=4))
    assert a["record"]["max_m"] == 75.0 and a["record"]["min_m"] == 73.1
    assert a["rejected_by_reason"] == {"PLACEHOLDER_VALUE": 1, "OUT_OF_RANGE": 1}
    assert a["latest"]["basis"] == "MEASURED"


def test_no_accepted_readings_is_reported_not_invented():
    a = analyse_series([], [], T0)
    assert a["status"] == "NO_ACCEPTED_READINGS" and a["latest"] is None
    assert a["freshness"]["status"] == "UNAVAILABLE"


def test_gap_and_completeness_helpers():
    times = [T0, T0 + timedelta(hours=1), T0 + timedelta(hours=10)]
    assert find_gaps(times)["count"] == 1
    assert completeness([])["overall"] is None


# ---- upstream relationship (multiple stations) ---------------------------------------

SPEC_UP = {"latitude": 10.938, "longitude": 78.44, "rl_zero_gauge_m": 78.0, "river": "Cauvery",
           "tributary": "-", "datum": "reduced_level", "datum_status": "ASSUMED"}
SPEC_DOWN = {**SPEC_UP, "latitude": 10.889, "longitude": 78.58, "rl_zero_gauge_m": 72.6}
RULES = {"max_lag_hours": 12, "min_pairs": 200, "min_abs_correlation": 0.3}


def linked_series(lag, n=800, seed=3, linked=True):
    rng = random.Random(seed)
    up_level, down_level, up_changes = 84.0, 74.0, []
    up, down = [], []
    for h in range(n):
        change = rng.gauss(0, 0.02)
        up_changes.append(change)
        up_level += change
        down_change = (up_changes[h - lag] if linked and h >= lag else 0) * 0.8 \
            + rng.gauss(0, 0.005)
        down_level += down_change
        up.append(obs(h, up_level))
        down.append(obs(h, down_level))
    return up, down


def test_a_lagged_relationship_is_found_with_its_travel_time():
    up, down = linked_series(lag=4)
    rel = upstream_relationship(up, down, SPEC_UP, SPEC_DOWN, RULES)
    assert rel["verdict"] == "VERIFIED" and rel["estimated_lag_hours"] == 4
    assert rel["used_in_forecast"] is True and rel["used_in_risk_assessment"] is False


def test_unrelated_stations_are_not_verified_even_on_the_same_river():
    up, down = linked_series(lag=4, linked=False)
    rel = upstream_relationship(up, down, SPEC_UP, SPEC_DOWN, RULES)
    assert rel["verdict"] == "NOT_VERIFIED" and rel["used_in_forecast"] is False
    assert rel["metadata"]["same_river"] and rel["metadata"]["zero_gauge_falls_downstream"]


def test_too_few_aligned_readings_is_insufficient_data():
    up, down = linked_series(lag=1, n=50)
    assert upstream_relationship(up, down, SPEC_UP, SPEC_DOWN, RULES)["verdict"] == \
        "INSUFFICIENT_DATA"


def test_a_datum_offset_cannot_create_or_hide_a_relationship():
    up, down = linked_series(lag=2)
    shifted = [{**o, "river_level_m": o["river_level_m"] + 900} for o in up]
    a = upstream_relationship(up, down, SPEC_UP, SPEC_DOWN, RULES)["best"]
    b = upstream_relationship(shifted, down, SPEC_UP, SPEC_DOWN, RULES)["best"]
    assert a["correlation"] == b["correlation"]


# ---- the real export -----------------------------------------------------------------

@pytest.mark.skipif(not RAW.is_file(), reason="raw CWC export not present")
def test_real_export_analysis():
    now = datetime(2026, 10, 30, 6, 0, tzinfo=timezone.utc)
    doc, accepted = build_station_analysis(now)
    ua = doc["stations"]["UPPER_ANICUT"]["analysis"]
    assert ua["latest"]["observed_at"] == "2025-12-30T05:00:00+05:30"
    assert ua["freshness"]["status"] == "STALE" and ua["is_current"] is False
    assert ua["data_class"] == HISTORICAL_CLASS
    # Same rejections as the River Agent regression test, plus analysis-only stuck runs.
    reasons = ua["rejected_by_reason"]
    assert (reasons["OUT_OF_RANGE"], reasons["PLACEHOLDER_VALUE"], reasons["SPIKE"]) == \
        (7459, 4059, 636)
    assert all(70 <= o["river_level_m"] <= 80 and o["river_level_m"] != 72.0
               for o in accepted["UPPER_ANICUT"])
    assert 81.17 not in {o["river_level_m"] for o in accepted["MUSIRI"]}
    rel = doc["upstream_relationship"]
    assert rel["verdict"] == "NOT_VERIFIED" and rel["used_in_forecast"] is False
    assert doc["excluded_stations"][0]["kind"] == "reservoir"
    assert doc["affects_risk_assessment"] is False
