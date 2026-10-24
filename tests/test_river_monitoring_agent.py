"""Tests for the River Monitoring Agent. Offline: temporary CSV files only.

All river levels below are fabricated test values, not real measurements.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest

from src.river_monitoring_agent import (
    AGENT_NAME,
    FRESHNESS_FRESH,
    FRESHNESS_STALE,
    FRESHNESS_UNAVAILABLE,
    GRADE_HISTORICAL,
    GRADE_MIXED,
    GRADE_SIMULATED,
    GRADE_UNVERIFIED_LIVE,
    GRADE_VERIFIED_LIVE,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    THRESHOLD_AT_OR_ABOVE_DANGER,
    THRESHOLD_AT_OR_ABOVE_WARNING,
    THRESHOLD_BELOW_WARNING,
    THRESHOLD_UNKNOWN,
    RiverMonitoringAgent,
    assess_freshness,
    calculate_trend,
    evaluate_thresholds,
    load_observations,
    main,
    parse_timestamp,
    save_evidence,
    validate_thresholds,
)

STATION = "UPPER_ANICUT_TEST"
HEADER = "timestamp,station_id,river_level_m,source_type"
SAMPLE_ROWS = [
    "2026-10-10T08:00:00Z,UPPER_ANICUT_TEST,2.80,simulated",
    "2026-10-10T09:00:00Z,UPPER_ANICUT_TEST,2.95,simulated",
    "2026-10-10T10:00:00Z,UPPER_ANICUT_TEST,3.10,simulated",
    "2026-10-10T11:00:00Z,UPPER_ANICUT_TEST,3.35,simulated",
]
AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
TEST_THRESHOLDS = {STATION: {"warning_level_m": 3.0, "danger_level_m": 4.0,
                             "reference": "TEST VALUES - not official"}}

REQUIRED_OUTPUT_FIELDS = {
    "agent_name", "status", "location", "station_id", "source", "source_type",
    "observed_at", "retrieved_at", "river_level_m", "level_change_m",
    "rate_of_change_m_per_hour", "freshness", "data_quality", "threshold_status",
    "errors", "warnings", "data_notes",
}


def write_csv(tmp_path, rows, header=HEADER, name="levels.csv"):
    path = tmp_path / name
    lines = ([header] if header is not None else []) + list(rows)
    path.write_text("\n".join(lines) + ("\n" if lines else ""), encoding="utf-8")
    return path


def run_agent(tmp_path, rows=SAMPLE_ROWS, header=HEADER, **kwargs):
    kwargs.setdefault("as_of", AS_OF)
    agent = RiverMonitoringAgent(write_csv(tmp_path, rows, header), **kwargs)
    return agent.run()


def codes(items):
    return {item["code"] for item in items}


def obs(hour, level, minute=0):
    return {"observed_at": datetime(2026, 10, 10, hour, minute, tzinfo=timezone.utc),
            "river_level_m": level}


def assert_measurements_null(result):
    for field in ("observed_at", "river_level_m", "level_change_m",
                  "rate_of_change_m_per_hour", "source_type"):
        assert result[field] is None, field
    assert result["threshold_status"] == THRESHOLD_UNKNOWN
    assert result["freshness"]["status"] == FRESHNESS_UNAVAILABLE


# ---- 1. valid observations --------------------------------------------------

def test_valid_observations(tmp_path):
    result = run_agent(tmp_path)
    assert REQUIRED_OUTPUT_FIELDS <= set(result)
    assert result["agent_name"] == AGENT_NAME
    assert result["status"] == STATUS_PARTIAL  # thresholds not configured
    assert result["station_id"] == STATION
    assert result["observed_at"] == "2026-10-10T11:00:00+00:00"
    assert result["river_level_m"] == 3.35
    assert result["retrieved_at"] is None
    assert result["freshness"]["status"] == FRESHNESS_FRESH
    assert result["data_quality"]["valid_rows"] == 4
    assert result["data_quality"]["rejected_rows"] == 0
    assert result["errors"] == []
    assert result["location"]["latitude"] == 10.888611


def test_timestamps_with_offset_are_normalized_to_utc(tmp_path):
    rows = ["2026-10-10T15:30:00+05:30,UPPER_ANICUT_TEST,3.0,historical",
            "2026-10-10T16:30:00+05:30,UPPER_ANICUT_TEST,3.2,historical"]
    result = run_agent(tmp_path, rows)
    assert result["observed_at"] == "2026-10-10T11:00:00+00:00"
    assert parse_timestamp("2026-10-10T08:00:00Z").utcoffset() == timedelta(0)


# ---- 2-4. file-level problems -----------------------------------------------

def test_missing_input_file(tmp_path):
    result = RiverMonitoringAgent(tmp_path / "missing.csv", as_of=AS_OF).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert "FILE_NOT_FOUND" in codes(result["errors"])
    assert_measurements_null(result)


def test_empty_file(tmp_path):
    path = tmp_path / "empty.csv"
    path.write_text("", encoding="utf-8")
    result = RiverMonitoringAgent(path, as_of=AS_OF).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert "EMPTY_FILE" in codes(result["errors"])
    assert_measurements_null(result)


def test_header_only_file(tmp_path):
    result = run_agent(tmp_path, rows=[])
    assert result["status"] == STATUS_UNAVAILABLE
    assert "NO_OBSERVATIONS" in codes(result["errors"])


def test_missing_required_columns(tmp_path):
    result = run_agent(tmp_path, rows=["2026-10-10T08:00:00Z,UPPER_ANICUT_TEST,2.8"],
                       header="timestamp,station_id,river_level_m")
    assert result["status"] == STATUS_UNAVAILABLE
    missing = [e for e in result["errors"] if e["code"] == "MISSING_COLUMNS"]
    assert missing and "source_type" in missing[0]["message"]


def test_malformed_row_with_extra_fields_is_rejected(tmp_path):
    rows = SAMPLE_ROWS + ["2026-10-10T11:30:00Z,UPPER_ANICUT_TEST,3.4,simulated,oops"]
    result = run_agent(tmp_path, rows)
    assert "MALFORMED_ROW" in codes(result["errors"])
    assert result["river_level_m"] == 3.35


# ---- 5. invalid timestamps --------------------------------------------------

@pytest.mark.parametrize("bad", ["not-a-time", "2026-13-40T08:00:00Z",
                                 "2026-10-10T08:00:00"])  # last one is naive
def test_invalid_timestamps_are_rejected(tmp_path, bad):
    rows = SAMPLE_ROWS + [f"{bad},UPPER_ANICUT_TEST,9.99,simulated"]
    result = run_agent(tmp_path, rows)
    assert "INVALID_TIMESTAMP" in codes(result["errors"])
    assert result["data_quality"]["rejected_rows"] == 1
    assert result["river_level_m"] == 3.35  # bad row never used
    assert result["status"] == STATUS_PARTIAL


def test_retrieved_before_observed_is_rejected(tmp_path):
    rows = ["2026-10-10T10:00:00Z,UPPER_ANICUT_TEST,3.1,historical,2026-10-10T09:00:00Z"]
    result = run_agent(tmp_path, rows, header=HEADER + ",retrieved_at")
    assert "INVALID_TIMESTAMP" in codes(result["errors"])
    assert result["status"] == STATUS_UNAVAILABLE


# ---- 6. negative and non-numeric levels -------------------------------------

@pytest.mark.parametrize("bad,code", [("-0.5", "NEGATIVE_LEVEL"), ("abc", "NON_NUMERIC_LEVEL"),
                                      ("nan", "NON_NUMERIC_LEVEL"), ("inf", "NON_NUMERIC_LEVEL")])
def test_negative_and_non_numeric_levels_are_rejected(tmp_path, bad, code):
    rows = SAMPLE_ROWS + [f"2026-10-10T11:30:00Z,UPPER_ANICUT_TEST,{bad},simulated"]
    result = run_agent(tmp_path, rows)
    assert code in codes(result["errors"])
    assert result["observed_at"] == "2026-10-10T11:00:00+00:00"
    assert result["river_level_m"] == 3.35


def test_invalid_station_and_source_type_are_rejected(tmp_path):
    rows = SAMPLE_ROWS + ["2026-10-10T11:30:00Z,BAD STATION!,3.0,simulated",
                          "2026-10-10T11:30:00Z,UPPER_ANICUT_TEST,3.0,guessed"]
    result = run_agent(tmp_path, rows)
    assert {"INVALID_STATION_ID", "INVALID_SOURCE_TYPE"} <= codes(result["errors"])
    assert result["data_quality"]["rejected_rows"] == 2


# ---- 7. duplicates ----------------------------------------------------------

def test_exact_duplicates_are_removed_with_warning(tmp_path):
    result = run_agent(tmp_path, SAMPLE_ROWS + [SAMPLE_ROWS[-1]])
    assert result["data_quality"]["duplicate_rows_removed"] == 1
    assert "DUPLICATE_OBSERVATION" in codes(result["warnings"])
    assert result["river_level_m"] == 3.35


def test_conflicting_duplicates_are_all_rejected(tmp_path):
    rows = SAMPLE_ROWS + ["2026-10-10T11:00:00Z,UPPER_ANICUT_TEST,9.00,simulated"]
    result = run_agent(tmp_path, rows)
    assert "CONFLICTING_DUPLICATE" in codes(result["errors"])
    assert result["data_quality"]["conflicting_duplicate_rows"] == 2
    # Neither 3.35 nor 9.00 can be trusted; the latest usable reading is 10:00.
    assert result["observed_at"] == "2026-10-10T10:00:00+00:00"
    assert result["river_level_m"] == 3.10


# ---- 8. stale observations --------------------------------------------------

def test_stale_observation(tmp_path):
    result = run_agent(tmp_path, as_of=datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc))
    assert result["freshness"]["status"] == FRESHNESS_STALE
    assert result["freshness"]["age_hours"] == 9.0
    assert result["status"] == STATUS_PARTIAL
    assert "STALE_OBSERVATION" in codes(result["warnings"])


def test_max_age_is_configurable(tmp_path):
    late = datetime(2026, 10, 10, 20, 0, tzinfo=timezone.utc)
    result = run_agent(tmp_path, as_of=late, max_age_hours=12)
    assert result["freshness"]["status"] == FRESHNESS_FRESH


def test_freshness_boundary_is_inclusive():
    observed = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    assert assess_freshness(observed, observed + timedelta(hours=3), 3)["status"] == FRESHNESS_FRESH
    assert assess_freshness(observed, observed + timedelta(hours=3, seconds=1), 3)["status"] \
        == FRESHNESS_STALE


def test_freshness_uses_injected_clock_when_no_as_of(tmp_path):
    clock = lambda: datetime(2026, 10, 10, 11, 30, tzinfo=timezone.utc)
    agent = RiverMonitoringAgent(write_csv(tmp_path, SAMPLE_ROWS), clock=clock)
    result = agent.run()
    assert result["freshness"]["reference_mode"] == "CURRENT_CLOCK"
    assert result["freshness"]["age_minutes"] == 30.0


def test_retrieval_time_is_kept_separate_from_observation_time(tmp_path):
    rows = ["2026-10-10T10:00:00Z,UPPER_ANICUT_TEST,3.10,historical,2026-10-10T10:20:00Z",
            "2026-10-10T11:00:00Z,UPPER_ANICUT_TEST,3.35,historical,2026-10-10T11:15:00Z"]
    result = run_agent(tmp_path, rows, header=HEADER + ",retrieved_at")
    assert result["observed_at"] == "2026-10-10T11:00:00+00:00"
    assert result["retrieved_at"] == "2026-10-10T11:15:00+00:00"
    assert result["freshness"]["age_minutes"] == 60.0  # from observation, not retrieval
    assert result["freshness"]["retrieval_lag_minutes"] == 15.0


# ---- 9. historical replay ---------------------------------------------------

def test_historical_replay_ignores_later_observations(tmp_path):
    replay = datetime(2026, 10, 10, 9, 30, tzinfo=timezone.utc)
    result = run_agent(tmp_path, as_of=replay)
    assert result["freshness"]["reference_mode"] == "AS_OF_REPLAY"
    assert result["freshness"]["reference_time"] == "2026-10-10T09:30:00+00:00"
    assert result["observed_at"] == "2026-10-10T09:00:00+00:00"
    assert result["river_level_m"] == 2.95
    assert result["data_quality"]["excluded_after_reference_time"] == 2
    assert result["level_change_m"] == 0.15


def test_replay_excludes_rows_not_yet_retrieved(tmp_path):
    rows = ["2026-10-10T10:00:00Z,UPPER_ANICUT_TEST,3.10,historical,2026-10-10T10:05:00Z",
            "2026-10-10T11:00:00Z,UPPER_ANICUT_TEST,3.35,historical,2026-10-10T12:30:00Z"]
    result = run_agent(tmp_path, rows, header=HEADER + ",retrieved_at")
    assert result["observed_at"] == "2026-10-10T10:00:00+00:00"
    assert result["data_quality"]["excluded_after_reference_time"] == 1


def test_replay_is_deterministic(tmp_path):
    path = write_csv(tmp_path, SAMPLE_ROWS)
    first = RiverMonitoringAgent(path, as_of=AS_OF).run()
    second = RiverMonitoringAgent(path, as_of=AS_OF).run()
    assert json.dumps(first, sort_keys=True) == json.dumps(second, sort_keys=True)


def test_naive_as_of_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        RiverMonitoringAgent(write_csv(tmp_path, SAMPLE_ROWS), as_of=datetime(2026, 10, 10, 12))


# ---- 10-11. level change and rate of rise -----------------------------------

def test_level_change_calculation(tmp_path):
    result = run_agent(tmp_path)
    assert result["level_change_m"] == 0.55  # 3.35 - 2.80 over the 3 h window
    assert result["trend"]["baseline_observed_at"] == "2026-10-10T08:00:00+00:00"
    assert result["trend"]["direction"] == "RISING"


def test_rate_of_rise_calculation(tmp_path):
    result = run_agent(tmp_path)
    assert result["trend"]["duration_hours"] == 3.0
    assert result["rate_of_change_m_per_hour"] == pytest.approx(0.55 / 3, abs=1e-4)


def test_trend_window_limits_baseline():
    trend = calculate_trend([obs(8, 2.80), obs(9, 2.95), obs(10, 3.10), obs(11, 3.35)],
                            window_hours=1)
    assert trend["level_change_m"] == 0.25
    assert trend["rate_of_change_m_per_hour"] == 0.25


def test_falling_trend_and_sub_hour_interval():
    trend = calculate_trend([obs(10, 3.40), obs(10, 3.25, minute=30)])
    assert trend["level_change_m"] == -0.15
    assert trend["rate_of_change_m_per_hour"] == -0.3
    assert trend["direction"] == "FALLING"


def test_out_of_order_rows_are_sorted(tmp_path):
    shuffled = [SAMPLE_ROWS[i] for i in (3, 1, 0, 2)]
    result = run_agent(tmp_path, shuffled)
    assert result["river_level_m"] == 3.35
    assert result["level_change_m"] == 0.55
    assert "OUT_OF_ORDER_TIMESTAMPS" in codes(result["warnings"])


def test_zero_duration_interval_gives_no_rate():
    trend = calculate_trend([obs(10, 3.0), obs(10, 3.2)])
    assert trend["status"] == "UNAVAILABLE"
    assert trend["rate_of_change_m_per_hour"] is None
    assert "Zero-duration" in trend["reason"]


# ---- 12. insufficient observations ------------------------------------------

def test_single_observation_has_unavailable_trend(tmp_path):
    result = run_agent(tmp_path, SAMPLE_ROWS[:1])
    assert result["river_level_m"] == 2.80
    assert result["level_change_m"] is None
    assert result["rate_of_change_m_per_hour"] is None
    assert result["trend"]["status"] == "UNAVAILABLE"
    assert "TREND_UNAVAILABLE" in codes(result["warnings"])
    assert result["status"] == STATUS_PARTIAL


def test_gap_larger_than_window_gives_unavailable_trend():
    trend = calculate_trend([obs(2, 2.0), obs(11, 3.0)], window_hours=3)
    assert trend["status"] == "UNAVAILABLE"
    assert trend["level_change_m"] is None


# ---- 13-14. thresholds ------------------------------------------------------

def test_missing_thresholds_give_unknown(tmp_path):
    result = run_agent(tmp_path)
    assert result["threshold_status"] == THRESHOLD_UNKNOWN
    assert result["threshold_evaluation"]["warning_level_m"] is None
    assert "THRESHOLDS_NOT_CONFIGURED" in codes(result["warnings"])


def test_thresholds_for_another_station_give_unknown(tmp_path):
    other = {"SOME_OTHER_STATION": TEST_THRESHOLDS[STATION]}
    assert run_agent(tmp_path, thresholds=other)["threshold_status"] == THRESHOLD_UNKNOWN


def test_configured_threshold_comparison(tmp_path):
    result = run_agent(tmp_path, thresholds=TEST_THRESHOLDS)
    assert result["threshold_status"] == THRESHOLD_AT_OR_ABOVE_WARNING
    assert result["threshold_evaluation"]["threshold_reference"] == "TEST VALUES - not official"
    assert result["status"] == STATUS_OK


@pytest.mark.parametrize("level,expected", [
    (2.99, THRESHOLD_BELOW_WARNING), (3.0, THRESHOLD_AT_OR_ABOVE_WARNING),
    (3.99, THRESHOLD_AT_OR_ABOVE_WARNING), (4.0, THRESHOLD_AT_OR_ABOVE_DANGER),
])
def test_threshold_boundaries(level, expected):
    thresholds = validate_thresholds(TEST_THRESHOLDS)[STATION]
    assert evaluate_thresholds(level, thresholds)["status"] == expected


def test_stale_threshold_comparison_is_flagged(tmp_path):
    late = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
    result = run_agent(tmp_path, thresholds=TEST_THRESHOLDS, as_of=late)
    assert result["threshold_evaluation"]["based_on_stale_data"] is True
    assert result["status"] == STATUS_PARTIAL


@pytest.mark.parametrize("bad", [
    {STATION: {"warning_level_m": 4.0, "danger_level_m": 3.0}},
    {STATION: {"warning_level_m": 3.0}},
    {STATION: {"warning_level_m": "3", "danger_level_m": 4.0}},
    {STATION: {"warning_level_m": -1.0, "danger_level_m": 4.0}},
])
def test_invalid_threshold_config_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_thresholds(bad)


def test_river_reading_never_declares_emergency(tmp_path):
    rows = ["2026-10-10T10:00:00Z,UPPER_ANICUT_TEST,8.0,simulated",
            "2026-10-10T11:00:00Z,UPPER_ANICUT_TEST,9.0,simulated"]
    result = run_agent(tmp_path, rows, thresholds=TEST_THRESHOLDS)
    assert result["threshold_status"] == THRESHOLD_AT_OR_ABOVE_DANGER
    text = json.dumps(result)
    for state in ('"EMERGENCY"', '"ALERT"', '"NORMAL"', '"RECOVERY"'):
        assert state not in text
    assert "disaster_state" not in result


# ---- 15. invalid or unavailable input ---------------------------------------

def test_all_rows_invalid_is_data_unavailable(tmp_path):
    rows = ["bad,UPPER_ANICUT_TEST,2.8,simulated",
            "2026-10-10T09:00:00Z,UPPER_ANICUT_TEST,,simulated"]
    result = run_agent(tmp_path, rows)
    assert result["status"] == STATUS_UNAVAILABLE
    assert {"INVALID_TIMESTAMP", "MISSING_VALUE", "NO_VALID_OBSERVATIONS"} <= codes(result["errors"])
    assert_measurements_null(result)


def test_requested_station_not_in_file(tmp_path):
    result = run_agent(tmp_path, station_id="OTHER_STATION")
    assert result["status"] == STATUS_UNAVAILABLE
    assert "NO_VALID_OBSERVATIONS" in codes(result["errors"])
    assert result["data_quality"]["other_station_rows"] == 4


def test_multiple_stations_require_selection(tmp_path):
    rows = SAMPLE_ROWS + ["2026-10-10T11:00:00Z,SECOND_TEST,1.5,simulated"]
    ambiguous = run_agent(tmp_path, rows)
    assert ambiguous["status"] == STATUS_UNAVAILABLE
    assert "AMBIGUOUS_STATION" in codes(ambiguous["errors"])
    assert set(ambiguous["station_summaries"]) == {STATION, "SECOND_TEST"}
    chosen = run_agent(tmp_path, rows, station_id="SECOND_TEST")
    assert chosen["river_level_m"] == 1.5
    assert chosen["station_summaries"][STATION]["latest_river_level_m"] == 3.35


def test_to_dict_before_run_is_unavailable(tmp_path):
    result = RiverMonitoringAgent(write_csv(tmp_path, SAMPLE_ROWS), as_of=AS_OF).to_dict()
    assert result["status"] == STATUS_UNAVAILABLE
    assert "NOT_RUN" in codes(result["errors"])


def test_to_dict_returns_independent_copy(tmp_path):
    agent = RiverMonitoringAgent(write_csv(tmp_path, SAMPLE_ROWS), as_of=AS_OF)
    agent.run()
    agent.to_dict()["river_level_m"] = 0
    assert agent.to_dict()["river_level_m"] == 3.35


# ---- 16. JSON-compatible output ---------------------------------------------

def test_output_is_json_compatible(tmp_path):
    result = run_agent(tmp_path, thresholds=TEST_THRESHOLDS)
    assert json.loads(json.dumps(result)) == result


def test_save_evidence_writes_json(tmp_path):
    result = run_agent(tmp_path)
    path = save_evidence(result, tmp_path / "out" / "river_evidence.json")
    assert json.loads(path.read_text(encoding="utf-8")) == result


def test_save_also_writes_unavailable_result(tmp_path):
    out = tmp_path / "river_evidence.json"
    save_evidence(run_agent(tmp_path), out)
    unavailable = RiverMonitoringAgent(tmp_path / "missing.csv", as_of=AS_OF).run()
    save_evidence(unavailable, out)
    assert json.loads(out.read_text(encoding="utf-8"))["status"] == STATUS_UNAVAILABLE


def test_cli_json_and_save(tmp_path, capsys):
    path = write_csv(tmp_path, SAMPLE_ROWS)
    out = tmp_path / "evidence.json"
    code = main(["--input", str(path), "--as-of", "2026-10-10T12:00:00Z", "--json",
                 "--save", "--output", str(out)])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["river_level_m"] == 3.35
    assert json.loads(out.read_text(encoding="utf-8")) == printed


def test_cli_missing_file_exits_with_1(tmp_path, capsys):
    assert main(["--input", str(tmp_path / "missing.csv")]) == 1


# ---- 17. missing-data uncertainty is preserved ------------------------------

def test_missing_level_is_not_converted_to_zero(tmp_path):
    rows = SAMPLE_ROWS + ["2026-10-10T11:30:00Z,UPPER_ANICUT_TEST,,simulated"]
    result = run_agent(tmp_path, rows)
    assert result["river_level_m"] == 3.35
    assert result["observed_at"] == "2026-10-10T11:00:00+00:00"
    assert "MISSING_VALUE" in codes(result["errors"])
    assert result["status"] == STATUS_PARTIAL


def test_unavailable_evidence_is_never_reported_as_safe(tmp_path):
    result = RiverMonitoringAgent(tmp_path / "missing.csv", as_of=AS_OF,
                                  thresholds=TEST_THRESHOLDS).run()
    assert result["threshold_status"] == THRESHOLD_UNKNOWN
    assert result["threshold_status"] != THRESHOLD_BELOW_WARNING
    assert result["river_level_m"] is None
    assert result["level_change_m"] is None


def test_load_observations_reports_counts(tmp_path):
    rows = SAMPLE_ROWS + ["bad,UPPER_ANICUT_TEST,2.8,simulated"]
    loaded = load_observations(write_csv(tmp_path, rows))
    assert loaded["stats"]["rows_read"] == 5
    assert loaded["stats"]["valid_rows"] == 4
    assert loaded["stats"]["rejected_rows"] == 1
    assert len(loaded["sha256"]) == 64
    assert "Row 6" in loaded["errors"][0]["message"]


# ---- 18. simulated data is never labelled verified live ---------------------

def test_simulated_data_is_never_verified_live(tmp_path):
    result = run_agent(tmp_path, thresholds=TEST_THRESHOLDS)
    quality = result["data_quality"]
    assert result["source_type"] == "simulated"
    assert quality["evidence_grade"] == GRADE_SIMULATED
    assert quality["is_simulated"] is True
    assert quality["is_verified_live"] is False
    assert quality["verification_status"] == "UNVERIFIED"
    assert GRADE_VERIFIED_LIVE not in json.dumps(result)
    assert "SIMULATED_DATA" in codes(result["warnings"])


@pytest.mark.parametrize("source,grade", [("historical", GRADE_HISTORICAL),
                                          ("live", GRADE_UNVERIFIED_LIVE)])
def test_other_source_types_are_not_verified(tmp_path, source, grade):
    rows = [r.replace("simulated", source) for r in SAMPLE_ROWS]
    result = run_agent(tmp_path, rows)
    assert result["data_quality"]["evidence_grade"] == grade
    assert result["data_quality"]["is_verified_live"] is False
    if source == "live":
        assert "UNVERIFIED_LIVE_LABEL" in codes(result["warnings"])


def test_mixed_source_types_are_flagged(tmp_path):
    rows = SAMPLE_ROWS[:2] + [r.replace("simulated", "historical") for r in SAMPLE_ROWS[2:]]
    result = run_agent(tmp_path, rows)
    assert result["data_quality"]["evidence_grade"] == GRADE_MIXED
    assert result["data_quality"]["is_simulated"] is True
    assert "MIXED_SOURCE_TYPES" in codes(result["warnings"])


# ---- quality control (added for real CWC telemetry) -------------------------

from src.river_monitoring_agent import (  # noqa: E402
    QUALITY_ACCEPTABLE,
    QUALITY_SUSPECT,
    QUALITY_UNAVAILABLE,
    apply_quality_control,
    assess_recent_quality,
    validate_qc_config,
)

UA = "UPPER_ANICUT"
UA_QC = {UA: {"valid_min_m": 70.0, "valid_max_m": 80.0, "max_jump_m": 1.0,
              "invalid_values_m": [72.0], "reference": "test copy of prototype QC"}}


def ua_rows(pairs, start=datetime(2025, 12, 28, 0, 0, tzinfo=timezone.utc)):
    """Hourly historical rows for UPPER_ANICUT from a list of level strings."""
    return [f"{(start + timedelta(hours=i)).isoformat()},{UA},{level},historical"
            for i, level in enumerate(pairs)]


def qc_obs(levels, gap_after=None):
    start = datetime(2025, 12, 28, 0, 0, tzinfo=timezone.utc)
    out, t = [], start
    for i, level in enumerate(levels):
        out.append({"observed_at": t, "river_level_m": level, "row_number": i + 2,
                    "source_type": "historical"})
        t += timedelta(hours=24 if gap_after == i else 1)
    return out


def ua_qc():
    return validate_qc_config(UA_QC)[UA]


def test_qc_rejects_out_of_range_values():
    accepted, rejected, summary = apply_quality_control(
        qc_obs([74.9, 1059.878, 74.91, 2.851, 74.92]), ua_qc())
    assert [o["river_level_m"] for o in accepted] == [74.9, 74.91, 74.92]
    assert {o["qc_code"] for o in rejected} == {"OUT_OF_RANGE"}
    assert summary["rejected_out_of_range"] == 2


def test_qc_rejects_placeholder_value():
    accepted, rejected, summary = apply_quality_control(
        qc_obs([73.04, 73.08, 72.0, 73.04]), ua_qc())
    assert summary["rejected_placeholder"] == 1
    assert rejected[0]["qc_code"] == "PLACEHOLDER_VALUE"
    assert 72.0 not in [o["river_level_m"] for o in accepted]


def test_qc_on_real_upper_anicut_pattern():
    # Excerpt of the real file, 27-28 Dec 2025 (UTC-shifted): a 86.501 spike
    # and a 72 placeholder among readings near 74.9 m.
    levels = [74.912, 74.928, 86.501, 74.935, 74.929, 74.908, 72.0, 74.902]
    accepted, rejected, summary = apply_quality_control(qc_obs(levels), ua_qc())
    assert {(o["river_level_m"], o["qc_code"]) for o in rejected} == {
        (86.501, "OUT_OF_RANGE"), (72.0, "PLACEHOLDER_VALUE")}
    assert len(accepted) == 6


def test_qc_rejects_spike_against_recent_median():
    levels = [74.9, 74.91, 74.92, 74.93, 75.94, 74.94]  # 75.94: 1.01 m above median
    accepted, rejected, summary = apply_quality_control(qc_obs(levels), ua_qc())
    assert [o["river_level_m"] for o in rejected] == [75.94]
    assert rejected[0]["qc_code"] == "SPIKE"


def test_qc_spike_check_skipped_after_gap():
    levels = [74.9, 74.91, 74.92, 74.93, 76.5, 76.52]
    accepted, rejected, summary = apply_quality_control(qc_obs(levels, gap_after=3), ua_qc())
    assert rejected == []  # after a 24 h gap there are no recent references
    assert summary["spike_check_skipped"] >= 4


def test_qc_sustained_real_rise_is_not_locked_out():
    # A genuine step that persists: the first 3 readings are held back as
    # spikes, then the 5-reading median catches up and later ones are accepted.
    levels = [74.0, 74.0, 74.0, 74.0, 75.5, 75.5, 75.5, 75.5, 75.5]
    accepted, rejected, _ = apply_quality_control(qc_obs(levels), ua_qc())
    assert len(rejected) == 3
    assert accepted[-1]["river_level_m"] == 75.5


def test_qc_allows_negative_levels_when_range_allows():
    qc = validate_qc_config({"S": {"valid_min_m": -2.0, "valid_max_m": 10.0}})["S"]
    accepted, rejected, _ = apply_quality_control(qc_obs([-0.19, -0.15, -0.12]), qc)
    assert len(accepted) == 3 and rejected == []


@pytest.mark.parametrize("bad", [
    {UA: {"valid_min_m": 80.0, "valid_max_m": 70.0}},
    {UA: {"valid_max_m": 80.0}},
    {UA: {"valid_min_m": 70.0, "valid_max_m": 80.0, "max_jump_m": 0}},
    {UA: {"valid_min_m": 70.0, "valid_max_m": 80.0, "invalid_values_m": 72.0}},
    {UA: {"valid_min_m": 70.0, "valid_max_m": float("nan")}},
])
def test_invalid_qc_config_is_rejected(bad):
    with pytest.raises(ValueError):
        validate_qc_config(bad)


def test_recent_quality_statuses():
    ref = datetime(2025, 12, 30, 0, 0, tzinfo=timezone.utc)
    hours = [ref - timedelta(hours=h) for h in range(10)]
    assert assess_recent_quality([], [], ref)["status"] == QUALITY_UNAVAILABLE
    assert assess_recent_quality(hours[:8], hours[8:], ref)["status"] == QUALITY_ACCEPTABLE  # 20%
    suspect = assess_recent_quality(hours[:7], hours[7:], ref)  # 30%
    assert suspect["status"] == QUALITY_SUSPECT
    assert suspect["rejected_fraction"] == 0.3
    old = [ref - timedelta(hours=30)]
    assert assess_recent_quality(hours, old, ref)["rejected_in_window"] == 0


def test_agent_marks_suspect_recent_data(tmp_path):
    # Alternating corrupted readings, like the real file in June 2024.
    levels = ["74.9", "89.871", "74.91", "96.869", "74.92", "1059.878", "74.93", "93.877"]
    result = run_agent(tmp_path, ua_rows(levels), qc=UA_QC, thresholds={
        UA: {"warning_level_m": 76.0, "danger_level_m": 78.0, "reference": "TEST VALUES"}},
        as_of=datetime(2025, 12, 28, 8, 0, tzinfo=timezone.utc))
    recent = result["data_quality"]["recent_quality"]
    assert recent["status"] == QUALITY_SUSPECT
    assert recent["rejected_in_window"] == 4
    assert "SUSPECT_RECENT_DATA" in codes(result["warnings"])
    assert result["river_level_m"] == 74.93
    assert result["status"] == STATUS_PARTIAL


def test_old_rejections_do_not_block_ok(tmp_path):
    rows = (["2025-12-20T00:00:00+00:00,UPPER_ANICUT,NaN,historical"]
            + ua_rows(["74.9", "74.91", "74.92", "74.93"]))
    result = run_agent(tmp_path, rows, qc=UA_QC, thresholds={
        UA: {"warning_level_m": 76.0, "danger_level_m": 78.0, "reference": "TEST VALUES"}},
        as_of=datetime(2025, 12, 28, 4, 0, tzinfo=timezone.utc))
    assert "NON_NUMERIC_LEVEL" in codes(result["errors"])  # still reported
    assert result["data_quality"]["recent_quality"]["rejected_in_window"] == 0
    assert result["status"] == STATUS_OK


def test_recent_rejection_blocks_ok(tmp_path):
    rows = ua_rows(["74.9", "74.91", "NaN", "74.92", "74.93"])
    result = run_agent(tmp_path, rows, qc=UA_QC, thresholds={
        UA: {"warning_level_m": 76.0, "danger_level_m": 78.0, "reference": "TEST VALUES"}},
        as_of=datetime(2025, 12, 28, 5, 0, tzinfo=timezone.utc))
    assert result["data_quality"]["recent_quality"]["status"] == QUALITY_ACCEPTABLE
    assert result["status"] == STATUS_PARTIAL


def test_undated_rejected_row_blocks_ok(tmp_path):
    rows = ["garbage,UPPER_ANICUT,74.9,historical"] + ua_rows(["74.9", "74.91", "74.92"])
    result = run_agent(tmp_path, rows, qc=UA_QC, thresholds={
        UA: {"warning_level_m": 76.0, "danger_level_m": 78.0, "reference": "TEST VALUES"}},
        as_of=datetime(2025, 12, 28, 3, 0, tzinfo=timezone.utc))
    assert result["data_quality"]["rejected_rows_without_time"] == 1
    assert result["status"] == STATUS_PARTIAL


def test_replay_does_not_report_future_bad_rows(tmp_path):
    rows = ua_rows(["74.9", "74.91", "74.92", "NaN", "1059.878"])
    result = run_agent(tmp_path, rows, qc=UA_QC,
                       as_of=datetime(2025, 12, 28, 2, 30, tzinfo=timezone.utc))
    assert result["errors"] == []
    assert result["data_quality"]["qc"]["rejected_out_of_range"] == 0


def test_qc_runs_only_on_data_known_at_reference_time(tmp_path):
    rows = ua_rows(["74.9", "74.91", "74.92", "74.93", "79.0", "79.0", "79.0"])
    early = run_agent(tmp_path, rows, qc=UA_QC,
                      as_of=datetime(2025, 12, 28, 3, 30, tzinfo=timezone.utc))
    assert early["data_quality"]["qc"]["checked"] == 4


def test_all_readings_rejected_by_qc_is_unavailable(tmp_path):
    result = run_agent(tmp_path, ua_rows(["1059.878", "2.851", "72.0"]), qc=UA_QC,
                       as_of=datetime(2025, 12, 28, 3, 0, tzinfo=timezone.utc))
    assert result["status"] == STATUS_UNAVAILABLE
    assert "NO_VALID_OBSERVATIONS" in codes(result["errors"])
    assert result["river_level_m"] is None
    assert result["station_summaries"][UA]["latest_river_level_m"] is None


def test_qc_not_configured_warning(tmp_path):
    result = run_agent(tmp_path, ua_rows(["74.9", "74.91"]),
                       as_of=datetime(2025, 12, 28, 2, 0, tzinfo=timezone.utc))
    assert "QC_NOT_CONFIGURED" in codes(result["warnings"])
    assert result["data_quality"]["qc"]["configured"] is False


def test_station_metadata_is_included(tmp_path):
    meta = {"station_id": UA, "station_name": "UPPER ANICUT",
            "source_description": "CWC river water level telemetry (test)"}
    result = run_agent(tmp_path, ua_rows(["74.9", "74.91"]), station_metadata=meta,
                       as_of=datetime(2025, 12, 28, 2, 0, tzinfo=timezone.utc))
    assert result["station_metadata"] == meta
    assert result["source"] == "CWC river water level telemetry (test)"
    other = run_agent(tmp_path, SAMPLE_ROWS, station_metadata=meta)
    assert "STATION_METADATA_MISMATCH" in codes(other["warnings"])


def test_cli_with_qc_and_metadata(tmp_path, capsys):
    path = write_csv(tmp_path, ua_rows(["74.9", "74.91", "1059.878", "74.92"]))
    qc_path = tmp_path / "qc.json"
    qc_path.write_text(json.dumps(UA_QC), encoding="utf-8")
    meta_path = tmp_path / "meta.json"
    meta_path.write_text(json.dumps({"station_id": UA}), encoding="utf-8")
    code = main(["--input", str(path), "--qc", str(qc_path), "--station-metadata",
                 str(meta_path), "--as-of", "2025-12-28T04:00:00Z", "--json"])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["data_quality"]["qc"]["rejected_out_of_range"] == 1
    assert printed["river_level_m"] == 74.92


def test_project_qc_config_file_is_valid():
    from pathlib import Path
    config = json.loads((Path(__file__).resolve().parent.parent / "config" / "river_qc.json")
                        .read_text(encoding="utf-8"))
    assert UA in validate_qc_config(config)


# ---- completeness and post-gap confirmation -----------------------------------

from src.river_monitoring_agent import assess_completeness  # noqa: E402


def test_completeness_counts_missing_readings():
    ref = datetime(2025, 12, 28, 12, 0, tzinfo=timezone.utc)
    start = ref - timedelta(hours=24)
    every_hour = [ref - timedelta(hours=h) for h in range(24)]
    assert assess_completeness(every_hour, start, 1.0)["status"] == "COMPLETE"
    sparse = every_hour[::2]  # every other hour
    block = assess_completeness(sparse, start, 1.0)
    assert (block["expected"], block["present"], block["missing"]) == (24, 12, 12)
    assert block["status"] == "INCOMPLETE"
    assert assess_completeness(sparse, start, None)["status"] == "NOT_CHECKED"


def test_completeness_counts_from_series_start():
    ref = datetime(2025, 12, 28, 12, 0, tzinfo=timezone.utc)
    recent = [ref - timedelta(hours=h) for h in range(4)]  # series began 3 h ago
    block = assess_completeness(recent, ref - timedelta(hours=24), 1.0, series_start=recent[-1])
    assert (block["expected"], block["missing"], block["status"]) == (4, 0, "COMPLETE")


def test_incomplete_data_blocks_ok(tmp_path):
    rows = ua_rows(["74.9", "74.91", "74.92", "74.93"])
    rows = [rows[0], rows[3]]  # 3 h span, 2 of 4 hourly readings
    thresholds = {UA: {"warning_level_m": 76.0, "danger_level_m": 78.0, "reference": "TEST"}}
    result = run_agent(tmp_path, rows, qc={UA: {"valid_min_m": 70.0, "valid_max_m": 80.0}},
                       thresholds=thresholds, expected_interval_hours=1.0,
                       as_of=datetime(2025, 12, 28, 3, 30, tzinfo=timezone.utc))
    assert result["data_quality"]["recent_quality"]["completeness"]["status"] == "INCOMPLETE"
    assert result["status"] == STATUS_PARTIAL


def test_lone_reading_after_gap_is_unconfirmed():
    levels = [74.9, 74.91, 74.92, 74.93, 74.95]
    accepted, rejected, summary = apply_quality_control(qc_obs(levels, gap_after=3), ua_qc())
    assert [o["qc_code"] for o in rejected] == ["UNCONFIRMED_AFTER_GAP"]
    assert rejected[0]["river_level_m"] == 74.95
    assert summary["rejected_unconfirmed"] == 1


def test_post_gap_reading_confirmed_by_neighbour():
    levels = [74.9, 74.91, 74.92, 74.93, 76.5, 76.52]
    accepted, rejected, _ = apply_quality_control(qc_obs(levels, gap_after=3), ua_qc())
    assert rejected == []


def test_post_gap_disagreeing_pair_is_rejected():
    levels = [74.9, 74.91, 74.92, 74.93, 79.5, 74.95]
    accepted, rejected, _ = apply_quality_control(qc_obs(levels, gap_after=3), ua_qc())
    assert {o["river_level_m"] for o in rejected} == {79.5, 74.95}
