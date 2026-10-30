"""Multi-station ingestion of the raw CWC export (src.cwc_adapter). Offline.

Small files in the export's exact 23-column layout; one test reads the real
103 MB export and is skipped if it is absent.
"""

import csv
import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest

from src.cwc_adapter import (
    CwcConversionError,
    load_cwc_stations,
    load_station_registry,
)
from src.river_monitoring_agent import PROJECT_DIR

RAW = PROJECT_DIR / "data" / "cwc" / "rwl_tel_hr_cwc_009_2021_2025.csv"
IST = timezone(timedelta(hours=5, minutes=30))
COLUMNS = ["SlNo", "Station", "Agency", "State LGD Code", "State", "District LGD Code",
           "District", "Tehsil", "Block", "Village", "River", "Basin", "Tributary",
           "Subtributary", "SubSubtributary", "Local River", "Latitude", "Longitude",
           "Is_DischargeDataAvailable", "RL_of_zeroGauge", "MeanSeaLevel",
           "Data Acquisition Time", "River Water Level Telemetry Hourly (meter)"]
IDENTITY = {
    "UPPER ANICUT": ("10.88861111", "78.58000000", "72.6000", "-"),
    "MUSIRI": ("10.93805556", "78.44027778", "78.0000", "-"),
    "Mettur Reservoir": ("11.80000000", "77.80000000", "204.2220", "-"),
    "UPPER ANICUT WEST": ("10.9", "78.6", "70.0", "-"),   # a similar name: must not match
}


def row(station, time, level, **override):
    lat, lon, rl, trib = IDENTITY.get(station.strip(), ("1", "1", "1", "-"))
    values = {c: "-" for c in COLUMNS}
    values.update({"SlNo": "1", "Station": station, "Agency": "CWC", "River": "Cauvery",
                   "Basin": "Cauvery", "Tributary": trib, "Latitude": lat, "Longitude": lon,
                   "RL_of_zeroGauge": rl, "MeanSeaLevel": "0", "Data Acquisition Time": time,
                   "River Water Level Telemetry Hourly (meter)": level})
    values.update(override)
    return values


def write_raw(path, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)
    return path


def registry(**changes):
    reg = load_station_registry()
    reg = json.loads(json.dumps(reg))
    reg.update(changes)
    return reg


@pytest.fixture
def raw(tmp_path):
    return write_raw(tmp_path / "raw.csv", [
        row("UPPER ANICUT", "01-01-2025 03:00", "74.103"),
        row("UPPER ANICUT", "01-01-2025 01:00", "74.101"),        # out of order
        row("UPPER ANICUT", "01-01-2025 02:00", "NaN"),           # missing value
        row("UPPER ANICUT", "01-01-2025 04:00", ""),              # empty value
        row("UPPER ANICUT", "01-01-2025 05:00", "abc"),           # not a number
        row("UPPER ANICUT", "01-01-2025 06:00", "inf"),           # not finite
        row("UPPER ANICUT", "01-01-2025 07:00", "74.2"),
        row("UPPER ANICUT", "01-01-2025 07:00", "74.2"),          # identical duplicate
        row("UPPER ANICUT", "01-01-2025 08:00", "74.3"),
        row("UPPER ANICUT", "01-01-2025 08:00", "74.9"),          # conflicting duplicate
        row("UPPER ANICUT", "2025-01-01 09:00", "74.4"),          # wrong timestamp format
        row("UPPER ANICUT WEST", "01-01-2025 03:00", "1.0"),      # similar name
        row("MUSIRI", "01-01-2025 01:00", "85.0"),
        row("MUSIRI", "01-01-2025 02:00", "85.1", Latitude="11.0"),  # identity mismatch
        row("Mettur Reservoir ", "01-01-2025 01:00", "230.0"),    # padded name in export
    ])


def test_registry_is_valid_and_names_the_primary_and_upstream_candidate():
    reg = load_station_registry()
    assert reg["primary_station"] == "UPPER_ANICUT" and reg["upstream_candidate"] == "MUSIRI"
    assert reg["stations"]["METTUR"]["kind"] == "reservoir"
    assert reg["stations"]["UPPER_ANICUT"]["rl_zero_gauge_m"] == 72.6


@pytest.mark.parametrize("change, message", [
    (lambda r: r["stations"]["MUSIRI"].update(kind="lake"), "kind"),
    (lambda r: r["stations"]["MUSIRI"].update(latitude="10.9"), "latitude"),
    (lambda r: r.update(upstream_candidate="METTUR"), "river_gauge"),
    (lambda r: r.update(primary_station="NOWHERE"), "river_gauge"),
    (lambda r: r["stations"]["MUSIRI"].update(analysis_max_constant_run_hours=0), "positive"),
])
def test_registry_mistakes_are_refused(tmp_path, change, message):
    reg = json.loads(json.dumps(load_station_registry()))
    change(reg)
    path = tmp_path / "reg.json"
    path.write_text(json.dumps(reg), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        load_station_registry(path)


def test_station_filtering_is_exact(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT"])["UPPER_ANICUT"]
    assert out["rows_read"] == 11                       # not the 'UPPER ANICUT WEST' row
    assert all(o["river_level_m"] > 70 for o in out["observations"])


def test_padded_station_names_in_the_export_are_matched(raw):
    out = load_cwc_stations(raw, registry(), ["METTUR"])["METTUR"]
    assert out["found"] and out["kind"] == "reservoir" and out["rows_read"] == 1


def test_timestamps_are_parsed_as_observation_times_and_sorted(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT"])["UPPER_ANICUT"]
    times = [o["observed_at"] for o in out["observations"]]
    assert times == sorted(times)
    assert times[0] == datetime(2025, 1, 1, 1, 0, tzinfo=IST)
    assert out["counts"]["unparseable_timestamps"] == 1
    prov = out["provenance"]
    assert prov["timestamp_status"] == "ASSUMED" and prov["assumed_utc_offset"] == "+05:30"
    assert "OBSERVATION" in prov["time_basis"]


def test_missing_and_invalid_values_are_counted_never_zero(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT"])["UPPER_ANICUT"]
    assert out["counts"]["non_numeric_levels"] == 4         # NaN, empty, abc, inf
    levels = [o["river_level_m"] for o in out["observations"]]
    assert 0.0 not in levels
    times = {o["observed_at"].hour for o in out["observations"]}
    assert not times & {2, 4, 5, 6}                          # gaps stay gaps


def test_duplicates_are_kept_once_and_conflicts_dropped(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT"])["UPPER_ANICUT"]
    assert out["counts"]["duplicate_rows_removed"] == 1
    assert out["counts"]["conflicting_timestamps_dropped"] == 1
    hours = [o["observed_at"].hour for o in out["observations"]]
    assert hours.count(7) == 1 and 8 not in hours           # 74.3 vs 74.9: neither is trusted


def test_identity_mismatches_are_reported(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT", "MUSIRI"])
    assert out["UPPER_ANICUT"]["identity"]["verified"] is True
    musiri = out["MUSIRI"]["identity"]
    assert musiri["verified"] is False and musiri["mismatch_rows"] == 1
    assert "Latitude" in musiri["mismatches"][0]


def test_several_stations_are_read_in_one_pass_without_mixing(raw):
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT", "MUSIRI", "METTUR"])
    assert {o["river_level_m"] for o in out["MUSIRI"]["observations"]} == {85.0, 85.1}
    assert all(o["river_level_m"] < 80 for o in out["UPPER_ANICUT"]["observations"])


def test_station_absent_from_the_file_is_reported_not_invented(tmp_path):
    path = write_raw(tmp_path / "raw.csv", [row("MUSIRI", "01-01-2025 01:00", "85.0")])
    out = load_cwc_stations(path, registry(), ["UPPER_ANICUT"])["UPPER_ANICUT"]
    assert out["found"] is False and out["observations"] == []
    assert out["last_observed_at"] is None


def test_unknown_station_id_is_refused(raw):
    with pytest.raises(CwcConversionError, match="Unknown station"):
        load_cwc_stations(raw, registry(), ["KABINI"])


def test_the_original_file_is_never_modified(raw):
    before = hashlib.sha256(raw.read_bytes()).hexdigest()
    out = load_cwc_stations(raw, registry(), ["UPPER_ANICUT"])
    assert hashlib.sha256(raw.read_bytes()).hexdigest() == before
    assert out["UPPER_ANICUT"]["provenance"]["sha256"] == before


@pytest.mark.skipif(not RAW.is_file(), reason="raw CWC export not present")
def test_real_export_coverage():
    out = load_cwc_stations(RAW, load_station_registry(), ["UPPER_ANICUT", "MUSIRI", "METTUR"])
    ua = out["UPPER_ANICUT"]
    assert ua["rows_read"] == 25453 and ua["counts"]["non_numeric_levels"] == 79
    assert ua["identity"]["verified"] and out["MUSIRI"]["identity"]["verified"]
    assert ua["last_observed_at"] == "2025-12-30T05:00:00+05:30"
    assert out["MUSIRI"]["rows_read"] == 22009
    assert out["METTUR"]["kind"] == "reservoir"
    assert ua["provenance"]["sha256"] == \
        "e0ae1026357ea56ab3d1c28fd4f30a9619b713126d84bf7ed9238e54ae0824d6"
