"""Regression tests on the committed REAL CWC data for Upper Anicut (2021-2025).

These pin down what the River Agent concludes from the actual telemetry, so a
change in validation/QC/freshness logic that alters real-data behaviour is
noticed. Skipped if the converted data file is absent.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.river_monitoring_agent import (
    PROJECT_DIR,
    RiverMonitoringAgent,
    load_json_file,
    load_observations,
    load_qc_file,
)

DATA = PROJECT_DIR / "data" / "cwc" / "upper_anicut_hourly_2021_2025.csv"
META = PROJECT_DIR / "data" / "cwc" / "upper_anicut_station.json"
QC = PROJECT_DIR / "config" / "river_qc.json"
IST = timezone(timedelta(hours=5, minutes=30))

pytestmark = pytest.mark.skipif(not DATA.is_file(), reason="real CWC data file not present")


def run(as_of):
    return RiverMonitoringAgent(DATA, station_id="UPPER_ANICUT", qc=load_qc_file(QC),
                                station_metadata=load_json_file(META, "meta"),
                                data_datum="reduced_level", as_of=as_of).run()


def test_file_contents():
    loaded = load_observations(DATA)
    assert loaded["stats"]["rows_read"] == 25453
    assert loaded["stats"]["valid_rows"] == 25374
    assert loaded["stats"]["rejected_rows"] == 79  # NaN values, never zero
    assert loaded["observations"][0]["observed_at"] == datetime(2021, 1, 1, 5, 0,
                                                                tzinfo=timezone.utc)


def test_whole_record_qc_counts():
    result = run(datetime(2025, 12, 30, 6, 0, tzinfo=IST))
    qc = result["data_quality"]["qc"]
    assert (qc["accepted"], qc["rejected_placeholder"], qc["rejected_out_of_range"],
            qc["rejected_spike"], qc["rejected_unconfirmed"]) == (13010, 4059, 7459, 636, 210)


def test_latest_clean_reading():
    result = run(datetime(2025, 12, 30, 6, 0, tzinfo=IST))
    assert result["river_level_m"] == 74.9
    assert result["observed_at"] == "2025-12-29T23:30:00+00:00"
    assert result["freshness"]["status"] == "FRESH"
    assert result["level_change_m"] == -0.004
    assert result["data_quality"]["recent_quality"]["status"] == "ACCEPTABLE"
    assert result["data_quality"]["evidence_grade"] == "HISTORICAL"
    assert result["threshold_status"] == "UNKNOWN"


def test_corrupted_period_is_suspect():
    result = run(datetime(2024, 6, 6, 12, 0, tzinfo=IST))
    recent = result["data_quality"]["recent_quality"]
    assert recent["status"] == "SUSPECT"
    assert (recent["rejected_in_window"], recent["readings_in_window"]) == (12, 24)


def test_flood_season_2022_is_stale_and_suspect():
    result = run(datetime(2022, 10, 5, 12, 0, tzinfo=IST))
    assert result["freshness"]["status"] == "STALE"
    assert result["data_quality"]["recent_quality"]["status"] == "SUSPECT"
    assert result["status"] == "PARTIAL"


def test_without_qc_garbage_would_pass():
    """Documents why QC is needed: without it a 1000+ m reading becomes 'latest'."""
    result = RiverMonitoringAgent(DATA, station_id="UPPER_ANICUT",
                                  as_of=datetime(2024, 8, 5, 4, 0, tzinfo=IST)).run()
    assert result["river_level_m"] > 100
    assert "QC_NOT_CONFIGURED" in {w["code"] for w in result["warnings"]}


def test_corrupted_post_gap_value_no_longer_passes():
    """Before post-gap confirmation, 75.859 m (27-09-2022, corrupted) was the
    'latest' reading at this replay time. Now it is rejected."""
    result = run(datetime(2022, 10, 5, 12, 0, tzinfo=IST))
    assert result["river_level_m"] != 75.859
    assert result["data_quality"]["qc"]["rejected_unconfirmed"] > 0


def test_missing_hours_are_counted():
    result = RiverMonitoringAgent(DATA, station_id="UPPER_ANICUT", qc=load_qc_file(QC),
                                  expected_interval_hours=1.0,
                                  as_of=datetime(2025, 12, 30, 6, 0, tzinfo=IST)).run()
    completeness = result["data_quality"]["recent_quality"]["completeness"]
    assert (completeness["expected"], completeness["present"], completeness["missing"]) == \
        (23, 17, 6)
    assert completeness["status"] == "INCOMPLETE"
    assert "INCOMPLETE_RECENT_DATA" in {w["code"] for w in result["warnings"]}
