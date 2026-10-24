"""Unit and failure-mode tests for the NWDP live connector. Fully offline."""

import json
from datetime import datetime, timezone

import pytest
import requests

from src.river_sources import (
    FAILED,
    VERIFIED,
    CkanClient,
    LiveSourceError,
    fetch_live_snapshot,
    records_to_normalized_csv,
    validate_live_config,
    verify_live_payloads,
)
from src.river_service import load_service_config
from src.river_monitoring_agent import PROJECT_DIR
from tests.nwdp_fixtures import (
    FIELD_IDS,
    IST,
    LEVEL_FIELD,
    REAL_LATEST_UTC,
    FakeResponse,
    FakeSession,
    datastore_payload,
    ok_session,
    real_records,
    record,
    resource_show_payload,
    synthetic_records,
)

CONFIG = load_service_config(PROJECT_DIR / "config" / "river_agent.json")
STATION = CONFIG["station"]
RETRIEVED = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)


def live_cfg(**overrides):
    return validate_live_config({**CONFIG["live"], **overrides}, STATION)


def verify(records=None, resource=None, fields=None, retrieved_at=RETRIEVED, **live_overrides):
    return verify_live_payloads(
        resource or resource_show_payload(),
        datastore_payload(real_records() if records is None else records, fields),
        live_cfg(**live_overrides), STATION, retrieved_at)


def check(result, name):
    return next(c for c in result["checks"] if c["name"] == name)


# ---- configuration ----------------------------------------------------------

def test_project_live_config_is_valid():
    cfg = live_cfg()
    assert cfg["api_base_url"] == "https://nwdp.nwic.gov.in/api/3/action/"
    assert cfg["timeout"] == (5, 30)


@pytest.mark.parametrize("override,message", [
    ({"api_base_url": "http://nwdp.nwic.gov.in/api/3/action/"}, "https"),
    ({"api_base_url": "https://example.com/api/3/action/"}, "allowed_hosts"),
    ({"resource_id": ""}, "resource_id"),
    ({"records_to_fetch": 0}, "records_to_fetch"),
    ({"max_attempts": 50}, "max_attempts"),
])
def test_invalid_live_config(override, message):
    with pytest.raises(ValueError, match=message):
        live_cfg(**override)


def test_station_settings_required_for_verification():
    with pytest.raises(ValueError, match="rl_zero_gauge_m"):
        validate_live_config(CONFIG["live"], {**STATION, "rl_zero_gauge_m": None})


def test_client_refuses_non_allowlisted_host():
    with pytest.raises(ValueError):
        CkanClient("https://evil.example/api/3/action/", ["nwdp.nwic.gov.in"])


# ---- verification -------------------------------------------------------------

def test_real_shaped_payload_verifies():
    result = verify()
    assert result["status"] == VERIFIED, result["failed_checks"]
    assert result["records_received"] == 30
    assert result["total_records_for_station"] == 1696
    assert result["latest_record_observed_at"] == REAL_LATEST_UTC.isoformat()
    assert result["resource"]["last_modified"] == "2026-10-13T00:20:54.787668"
    assert result["timestamp_interpretation"]["status"] == "ASSUMED"


@pytest.mark.parametrize("records,failed", [
    (real_records(Station="MUSIRI"), "station_name_matches"),
    (real_records(Agency="TNWRD"), "agency_matches"),
    (real_records(Latitude="10.93805556"), "coordinates_match"),
    (real_records(Longitude="not-a-number"), "coordinates_match"),
    (real_records(RL_of_zeroGauge="78.0000"), "gauge_zero_matches"),
    ([], "records_present"),
])
def test_identity_mismatches_fail_verification(records, failed):
    result = verify(records)
    assert result["status"] == FAILED
    assert failed in result["failed_checks"]


def test_resource_and_dataset_mismatch_fail():
    result = verify(resource=resource_show_payload(id="other", package_id="other-dataset",
                                                   name="Something else"))
    assert {"resource_id_matches", "dataset_id_matches", "resource_name_matches"} <= set(
        result["failed_checks"])


def test_missing_level_field_fails():
    fields = [f for f in FIELD_IDS if f != LEVEL_FIELD]
    result = verify(fields=fields)
    assert "required_fields_present" in result["failed_checks"]


def test_unit_must_be_declared_as_metre():
    result = verify(level_field="River Water Level Telemetry Hourly (feet)")
    assert "unit_is_metre" in result["failed_checks"]


def test_future_timestamps_fail_verification():
    # Retrieval before the newest record => timestamps are in the future.
    result = verify(retrieved_at=datetime(2026, 6, 3, 10, 0, tzinfo=timezone.utc))
    assert "no_future_timestamps" in result["failed_checks"]


def test_unparseable_timestamps_fail_verification():
    records = [record(1, "2026/06/03 21:00", "71.9"), record(2, "garbage", "71.9")]
    result = verify(records)
    assert "timestamps_parseable" in result["failed_checks"]


# ---- conversion -------------------------------------------------------------

def test_conversion_preserves_text_and_orders_by_id():
    records = [record(3, "03-06-2026 21:00", "NaN"), record(2, "03-06-2026 20:00", "71.721"),
               record(1, "bad time", "71.7")]
    text = records_to_normalized_csv(records, live_cfg(), STATION, "UPPER_ANICUT", RETRIEVED)
    lines = text.strip().split("\n")
    assert lines[0] == "timestamp,station_id,river_level_m,source_type,retrieved_at"
    assert lines[1].startswith("bad time,UPPER_ANICUT,71.7,live,")
    assert lines[2] == "2026-06-03T20:00:00+05:30,UPPER_ANICUT,71.721,live,2026-10-13T08:00:00+00:00"
    assert lines[3].split(",")[2] == "NaN"  # never turned into 0


# ---- HTTP retries and failures ----------------------------------------------

def client(session, sleeps):
    return CkanClient("https://nwdp.nwic.gov.in/api/3/action/", ["nwdp.nwic.gov.in"],
                      session=session, max_attempts=3, backoff_seconds=(2, 5),
                      sleep=sleeps.append,
                      clock=lambda: RETRIEVED)


def test_retry_after_timeout_then_success():
    sleeps = []
    session = FakeSession({"resource_show": [requests.exceptions.Timeout("slow"),
                                             FakeResponse(200, resource_show_payload())]})
    c = client(session, sleeps)
    payload, _, raw = c.call("resource_show", {"id": "x"})
    assert payload["success"] is True and raw
    assert sleeps == [2]
    assert [a["outcome"] for a in c.attempts] == ["error", "ok"]
    assert len(c.attempts[1]["sha256"]) == 64


def test_persistent_timeouts_raise_after_max_attempts():
    sleeps = []
    session = FakeSession({"resource_show": [requests.exceptions.Timeout("slow")]})
    with pytest.raises(LiveSourceError) as info:
        client(session, sleeps).call("resource_show", {})
    assert info.value.code == "NETWORK_TIMEOUT"
    assert "3 attempt" in info.value.message
    assert sleeps == [2, 5]


@pytest.mark.parametrize("status", [429, 500, 503])
def test_retryable_http_status(status):
    sleeps = []
    session = FakeSession({"resource_show": [FakeResponse(status, {"error": "busy"}),
                                             FakeResponse(200, resource_show_payload())]})
    payload, _, _ = client(session, sleeps).call("resource_show", {})
    assert payload["success"] and sleeps == [2]


@pytest.mark.parametrize("status", [400, 403, 404])
def test_client_errors_are_not_retried(status):
    sleeps = []
    session = FakeSession({"resource_show": [FakeResponse(status, {"error": "no"})]})
    with pytest.raises(LiveSourceError) as info:
        client(session, sleeps).call("resource_show", {})
    assert info.value.code == "HTTP_ERROR" and sleeps == []
    assert len(session.calls) == 1


def test_invalid_json_and_ckan_failure():
    session = FakeSession({"resource_show": [FakeResponse(200, raw=b"<html>maintenance</html>")]})
    with pytest.raises(LiveSourceError) as info:
        client(session, []).call("resource_show", {})
    assert info.value.code == "INVALID_JSON"
    session = FakeSession({"resource_show": [FakeResponse(200, {"success": False,
                                                                "error": {"message": "Not found"}})]})
    with pytest.raises(LiveSourceError) as info:
        client(session, []).call("resource_show", {})
    assert info.value.code == "API_ERROR"


def test_connection_error_code():
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("dns")]})
    with pytest.raises(LiveSourceError) as info:
        client(session, []).call("resource_show", {})
    assert info.value.code == "NETWORK_ERROR"


# ---- full snapshot ------------------------------------------------------------

def test_fetch_live_snapshot_writes_audit_files(tmp_path):
    session = ok_session()
    snap = fetch_live_snapshot(CONFIG["live"], STATION, tmp_path, session=session,
                               sleep=lambda s: None, clock=lambda: RETRIEVED)
    assert snap["verification"]["status"] == VERIFIED
    assert snap["snapshot_path"].is_file()
    for path in snap["raw_paths"].values():
        assert json.loads(open(path, encoding="utf-8").read())["success"] is True
    import hashlib
    assert hashlib.sha256(snap["snapshot_path"].read_bytes()).hexdigest() == snap["snapshot_sha256"]
    # request parameters actually sent
    action, params, timeout = session.calls[1]
    assert action == "datastore_search"
    assert json.loads(params["filters"]) == {"Station": "UPPER ANICUT"}
    assert params["sort"] == "_id desc" and params["limit"] == 72
    assert timeout == (5, 30)


def test_fetch_live_snapshot_outage_reports_attempts(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    with pytest.raises(LiveSourceError) as info:
        fetch_live_snapshot(CONFIG["live"], STATION, tmp_path, session=session,
                            sleep=lambda s: None, clock=lambda: RETRIEVED)
    assert len(info.value.attempts) == 3
    assert not any(tmp_path.iterdir())  # nothing saved, nothing invented


def test_synthetic_records_helper_is_newest_first():
    rows = synthetic_records(datetime(2026, 10, 13, 13, 0, tzinfo=IST), ["74.9", "74.91"])
    assert rows[0]["Data Acquisition Time"] == "13-10-2026 13:00"
