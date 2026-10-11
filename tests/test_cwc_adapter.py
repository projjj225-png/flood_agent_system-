"""Tests for the CWC telemetry adapter. Offline: small temporary CSV files.

The tiny CWC-style files below copy the real export's column layout; their
values are short excerpts or made-up test values, not a full dataset.
"""

import csv
import json
from datetime import datetime, timezone

import pytest

from src.cwc_adapter import (
    CwcConversionError,
    convert_cwc_file,
    convert_timestamp,
    list_stations,
    main,
    normalize_station_id,
    parse_utc_offset,
)
from src.river_monitoring_agent import RiverMonitoringAgent

CWC_HEADER = [
    "SlNo", "Station", "Agency", "State LGD Code", "State", "District LGD Code", "District",
    "Tehsil", "Block", "Village", "River", "Basin", "Tributary", "Subtributary",
    "SubSubtributary", "Local River", "Latitude", "Longitude", "Is_DischargeDataAvailable",
    "RL_of_zeroGauge", "MeanSeaLevel", "Data Acquisition Time",
    "River Water Level Telemetry Hourly (meter)",
]
UA_META = ["CWC", "33", "Tamil Nadu", "573", "Tiruchirappalli", "TIRUCHIRAPPALLI", "-", "-",
           "Cauvery", "Cauvery", "-", "-", "-", "Cauvery", "10.88861111", "78.58000000",
           "Yes", "72.6000", "0"]
MUSIRI_META = ["CWC", "33", "Tamil Nadu", "573", "Tiruchirappalli", "MUSIRI", "-", "-",
               "Cauvery", "Cauvery", "-", "-", "-", "Cauvery", "10.93805556", "78.44027778",
               "Yes", "78.0000", "92.00"]


def write_cwc(tmp_path, rows, header=CWC_HEADER, name="cwc.csv"):
    path = tmp_path / name
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        for i, (station, meta, time_text, level) in enumerate(rows, start=1):
            writer.writerow([i, station, *meta, time_text, level])
    return path


DEFAULT_ROWS = [
    ("UPPER ANICUT", UA_META, "29-12-2025 22:00", "74.891"),
    ("MUSIRI", MUSIRI_META, "29-12-2025 22:00", "81.2"),
    ("UPPER ANICUT", UA_META, "29-12-2025 23:00", "74.897"),
    ("UPPER ANICUT", UA_META, "30-12-2025 00:00", "NaN"),
    ("UPPER ANICUT", UA_META, "30-12-2025 01:00", "74.904"),
]


def read_rows(path):
    with open(path, newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def test_normalize_station_id():
    assert normalize_station_id("UPPER ANICUT") == "UPPER_ANICUT"
    assert normalize_station_id("PALLIPALAYAM/Erode") == "PALLIPALAYAM_ERODE"
    assert normalize_station_id("K.M. VADI") == "K_M_VADI"
    with pytest.raises(CwcConversionError):
        normalize_station_id(" / ")


def test_timestamp_conversion_uses_explicit_offset():
    ist = parse_utc_offset("+05:30")
    assert convert_timestamp("30-12-2025 05:00", ist) == "2025-12-30T05:00:00+05:30"
    assert convert_timestamp("2025-12-30 05:00", ist) is None
    assert convert_timestamp("31-02-2025 05:00", ist) is None
    with pytest.raises(ValueError):
        parse_utc_offset("IST")


def test_converts_only_requested_station(tmp_path):
    out = tmp_path / "ua.csv"
    meta = convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "UPPER ANICUT", out)
    rows = read_rows(out)
    assert list(rows[0]) == ["timestamp", "station_id", "river_level_m", "source_type"]
    assert len(rows) == 4 and meta["rows_converted"] == 4
    assert {r["station_id"] for r in rows} == {"UPPER_ANICUT"}
    assert {r["source_type"] for r in rows} == {"historical"}
    assert rows[0]["timestamp"] == "2025-12-29T22:00:00+05:30"


def test_level_text_is_passed_through_unchanged(tmp_path):
    out = tmp_path / "ua.csv"
    convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "UPPER ANICUT", out)
    levels = [r["river_level_m"] for r in read_rows(out)]
    assert levels == ["74.891", "74.897", "NaN", "74.904"]  # NaN is not turned into 0


def test_station_match_is_case_insensitive(tmp_path):
    meta = convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "upper anicut", tmp_path / "o.csv")
    assert meta["station_name"] == "UPPER ANICUT"


def test_metadata_records_provenance_and_assumptions(tmp_path):
    meta_path = tmp_path / "meta.json"
    convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "UPPER ANICUT", tmp_path / "o.csv",
                     meta_path)
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["station_id"] == "UPPER_ANICUT"
    assert meta["agency_as_stated_in_file"] == "CWC"
    assert meta["attributes_from_file"]["RL_of_zeroGauge"] == "72.6000"
    assert meta["attributes_from_file"]["Latitude"] == "10.88861111"
    assert meta["timestamp_interpretation"]["status"] == "ASSUMED"
    assert meta["timestamp_interpretation"]["assumed_utc_offset"] == "+05:30"
    assert len(meta["raw_file"]["sha256"]) == 64
    assert meta["attributes_inconsistent_across_rows"] == []


def test_inconsistent_metadata_is_reported(tmp_path):
    changed = list(UA_META)
    changed[17] = "70.0000"  # RL_of_zeroGauge differs in one row
    rows = DEFAULT_ROWS + [("UPPER ANICUT", changed, "30-12-2025 02:00", "74.9")]
    meta = convert_cwc_file(write_cwc(tmp_path, rows), "UPPER ANICUT", tmp_path / "o.csv")
    assert meta["attributes_inconsistent_across_rows"] == ["RL_of_zeroGauge"]


def test_unparseable_timestamp_is_passed_through_and_counted(tmp_path):
    rows = DEFAULT_ROWS + [("UPPER ANICUT", UA_META, "99-99-2025 01:00", "74.9")]
    out = tmp_path / "o.csv"
    meta = convert_cwc_file(write_cwc(tmp_path, rows), "UPPER ANICUT", out)
    assert meta["unparseable_timestamps_passed_through"] == 1
    assert read_rows(out)[-1]["timestamp"] == "99-99-2025 01:00"


def test_station_not_found(tmp_path):
    with pytest.raises(CwcConversionError, match="not found"):
        convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "NOWHERE", tmp_path / "o.csv")
    assert not (tmp_path / "o.csv").exists()


def test_not_a_cwc_file(tmp_path):
    path = tmp_path / "plain.csv"
    path.write_text("timestamp,station_id,river_level_m,source_type\n", encoding="utf-8")
    with pytest.raises(CwcConversionError, match="missing column"):
        convert_cwc_file(path, "UPPER ANICUT", tmp_path / "o.csv")


def test_missing_input_file(tmp_path):
    with pytest.raises(CwcConversionError, match="not found"):
        list_stations(tmp_path / "missing.csv")


def test_list_stations(tmp_path):
    assert list_stations(write_cwc(tmp_path, DEFAULT_ROWS)) == {"UPPER ANICUT": 4, "MUSIRI": 1}


def test_cli(tmp_path, capsys):
    src = write_cwc(tmp_path, DEFAULT_ROWS)
    out = tmp_path / "o.csv"
    assert main(["--input", str(src), "--station", "UPPER ANICUT", "--output", str(out),
                 "--metadata", str(tmp_path / "m.json")]) == 0
    assert out.exists() and (tmp_path / "m.json").exists()
    assert main(["--input", str(src), "--station", "NOWHERE", "--output", str(out)]) == 1
    assert main(["--input", str(src), "--list-stations"]) == 0
    assert "UPPER ANICUT" in capsys.readouterr().out


def test_converted_file_runs_through_agent(tmp_path):
    out, meta_path = tmp_path / "ua.csv", tmp_path / "meta.json"
    meta = convert_cwc_file(write_cwc(tmp_path, DEFAULT_ROWS), "UPPER ANICUT", out, meta_path)
    agent = RiverMonitoringAgent(
        out, station_id="UPPER_ANICUT", station_metadata=meta,
        as_of=datetime(2025, 12, 29, 20, 0, tzinfo=timezone.utc))  # 01:30 IST
    result = agent.run()
    assert result["observed_at"] == "2025-12-29T19:30:00+00:00"  # 01:00 IST in UTC
    assert result["river_level_m"] == 74.904
    assert result["data_quality"]["evidence_grade"] == "HISTORICAL"
    assert result["data_quality"]["rejected_rows"] == 1  # the NaN row, never zero
    assert result["station_metadata"]["station_name"] == "UPPER ANICUT"
    assert result["source"].startswith("CWC river water level telemetry")
