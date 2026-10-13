"""Integration, failure-mode and end-to-end tests for the river evidence service.

The live API is replaced by FakeSession (offline). Fixture provenance is in
tests/nwdp_fixtures.py.
"""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.audit_log import read_audit_log
from src.river_monitoring_agent import (
    GRADE_VERIFIED_LIVE,
    GRADE_VERIFIED_SOURCE_STALE,
    PROJECT_DIR,
    RiverMonitoringAgent,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    THRESHOLD_UNKNOWN,
    evaluate_thresholds,
    validate_thresholds,
)
from src.river_service import (
    LABEL_FALLBACK,
    LABEL_HISTORICAL,
    LABEL_LIVE_UNAVAILABLE,
    LABEL_LIVE_UNVERIFIED,
    LABEL_LIVE_VERIFIED,
    LABEL_SIMULATED,
    SCHEMA_VERSION,
    RiverEvidenceService,
    compute_evidence_id,
    cross_check_sources,
    load_service_config,
    main,
)
from tests.nwdp_fixtures import (
    IST,
    REAL_LATEST_UTC,
    FakeSession,
    ok_session,
    real_records,
    synthetic_records,
)

BASE_CONFIG = load_service_config(PROJECT_DIR / "config" / "river_agent.json")
NOW = datetime(2026, 10, 13, 8, 0, tzinfo=timezone.utc)
CLEAN_LEVELS = ["74.90", "74.91", "74.92", "74.93", "74.94", "74.95"]
TEST_THRESHOLDS = {"UPPER_ANICUT": {"warning_level_m": 76.0, "danger_level_m": 78.0,
                                    "reference": "TEST VALUES - not official",
                                    "datum": "reduced_level"}}
REQUIRED_KEYS = {"schema_version", "evidence_id", "run_id", "generated_at", "mode_requested",
                 "source_label", "fallback", "live_fetch", "cross_source_check",
                 "decision_support", "audit", "agent_name", "status", "station_id",
                 "river_level_m", "observed_at", "freshness", "data_quality", "threshold_status",
                 "errors", "warnings"}


def make_service(tmp_path, session=None, clock=lambda: NOW, **overrides):
    config = json.loads(json.dumps(BASE_CONFIG))
    config["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                         "audit_log": str(tmp_path / "audit" / "river_audit.jsonl"),
                         "evidence": str(tmp_path / "river_evidence.json")}
    for key, value in overrides.items():
        config[key] = value
    return RiverEvidenceService(config, base_dir=PROJECT_DIR, session=session,
                                sleep=lambda s: None, clock=clock)


def clean_live_session(end_ist=datetime(2026, 10, 13, 13, 0, tzinfo=IST)):
    # 13:00 IST = 07:30 UTC; NOW = 08:00 UTC -> 30 minutes old (FRESH).
    return ok_session(records=synthetic_records(end_ist, CLEAN_LEVELS))


def events(tmp_path):
    return [e["event"] for e in read_audit_log(tmp_path / "audit" / "river_audit.jsonl")]


# ---- live: verified and fresh --------------------------------------------------

def test_live_verified_fresh_is_usable(tmp_path):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    assert REQUIRED_KEYS <= set(evidence)
    assert evidence["source_label"] == LABEL_LIVE_VERIFIED
    quality = evidence["data_quality"]
    assert quality["source_verification"]["status"] == "VERIFIED"
    assert quality["source_verification"]["snapshot_hash_matches"] is True
    assert quality["evidence_grade"] == GRADE_VERIFIED_LIVE
    assert quality["is_verified_live"] is True
    assert evidence["river_level_m"] == 74.95
    assert evidence["observed_at"] == "2026-10-13T07:30:00+00:00"
    assert evidence["retrieved_at"] == NOW.isoformat()
    assert evidence["freshness"]["status"] == "FRESH"
    assert evidence["decision_support"]["current_evidence_usable"] is True
    assert evidence["decision_support"]["river_level_m"] == 74.95
    assert evidence["status"] == STATUS_PARTIAL  # thresholds not configured
    assert evidence["source"].startswith("CWC river water level telemetry (hourly) via")


def test_live_verified_fresh_with_thresholds_is_ok(tmp_path, monkeypatch):
    service = make_service(tmp_path, clean_live_session())
    service.thresholds = validate_thresholds(TEST_THRESHOLDS)
    evidence = service.get_evidence("live")
    assert evidence["threshold_status"] == "BELOW_WARNING"
    assert evidence["status"] == STATUS_OK
    assert "THRESHOLDS_NOT_OFFICIAL" in {w["code"] for w in evidence["warnings"]}


# ---- live: real-shaped data (stale and corrupted) ---------------------------------

def test_real_live_data_is_verified_but_stale(tmp_path):
    evidence = make_service(tmp_path, ok_session()).get_evidence("live")
    quality = evidence["data_quality"]
    assert evidence["source_label"] == LABEL_LIVE_VERIFIED
    assert quality["evidence_grade"] == GRADE_VERIFIED_SOURCE_STALE
    assert quality["is_verified_live"] is False
    assert evidence["freshness"]["status"] == "STALE"
    assert quality["qc"]["rejected_out_of_range"] > 0  # 151-350 m values rejected
    ds = evidence["decision_support"]
    assert ds["current_evidence_usable"] is False
    assert ds["river_level_m"] is None  # never handed out as current
    assert any("STALE" in r for r in ds["reasons_not_usable"])


def test_real_live_data_at_its_own_time_is_suspect(tmp_path):
    clock = lambda: REAL_LATEST_UTC + timedelta(minutes=30)
    evidence = make_service(tmp_path, ok_session(), clock=clock).get_evidence("live")
    assert evidence["data_quality"]["recent_quality"]["status"] == "SUSPECT"
    assert evidence["decision_support"]["current_evidence_usable"] is False


# ---- live: verification failures ----------------------------------------------------

def test_live_mode_wrong_station_is_unverified(tmp_path):
    session = ok_session(records=real_records(Station="MUSIRI"))
    evidence = make_service(tmp_path, session).get_evidence("live")
    assert evidence["source_label"] == LABEL_LIVE_UNVERIFIED
    assert evidence["data_quality"]["is_verified_live"] is False
    assert "station_name_matches" in evidence["live_fetch"]["failed_checks"]


def test_auto_mode_falls_back_when_verification_fails(tmp_path):
    session = ok_session(records=synthetic_records(datetime(2026, 10, 13, 13, 0, tzinfo=IST),
                                                   CLEAN_LEVELS))
    session.routes["datastore_search"][0].content = session.routes["datastore_search"][0] \
        .content.replace(b"78.58000000", b"78.70000000")
    evidence = make_service(tmp_path, session).get_evidence("auto")
    assert evidence["source_label"] == LABEL_FALLBACK
    assert evidence["fallback"]["used"] is True
    assert "coordinates_match" in evidence["fallback"]["reason"]
    assert "HISTORICAL_FALLBACK_USED" in {w["code"] for w in evidence["warnings"]}
    assert evidence["data_quality"]["evidence_grade"] == "HISTORICAL"
    assert evidence["decision_support"]["current_evidence_usable"] is False
    assert "fallback_used" in events(tmp_path)


# ---- outages -----------------------------------------------------------------------

@pytest.mark.parametrize("exc", [requests.exceptions.Timeout("slow"),
                                 requests.exceptions.ConnectionError("down")])
def test_live_outage_gives_data_unavailable(tmp_path, exc):
    session = FakeSession({"resource_show": [exc]})
    evidence = make_service(tmp_path, session).get_evidence("live")
    assert evidence["status"] == STATUS_UNAVAILABLE
    assert evidence["source_label"] == LABEL_LIVE_UNAVAILABLE
    assert evidence["river_level_m"] is None
    assert "LIVE_SOURCE_UNAVAILABLE" in {e["code"] for e in evidence["errors"]}
    assert len(evidence["live_fetch"]["attempts"]) == 3
    assert evidence["decision_support"]["current_evidence_usable"] is False
    assert events(tmp_path) == ["run_started", "live_fetch_failed", "evidence_produced"]


def test_auto_outage_falls_back_to_historical_and_is_stale(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    evidence = make_service(tmp_path, session).get_evidence("auto")
    assert evidence["source_label"] == LABEL_FALLBACK
    assert evidence["river_level_m"] == 74.9  # last historical reading, 30-12-2025
    assert evidence["freshness"]["status"] == "STALE"
    assert evidence["status"] == STATUS_PARTIAL
    assert evidence["decision_support"]["current_evidence_usable"] is False


def test_http_500_then_success_is_retried(tmp_path):
    from tests.nwdp_fixtures import FakeResponse, datastore_payload, resource_show_payload
    session = FakeSession({
        "resource_show": [FakeResponse(503, {"error": "busy"}),
                          FakeResponse(200, resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(
            synthetic_records(datetime(2026, 10, 13, 13, 0, tzinfo=IST), CLEAN_LEVELS)))],
    })
    evidence = make_service(tmp_path, session).get_evidence("live")
    assert evidence["source_label"] == LABEL_LIVE_VERIFIED
    assert [a["outcome"] for a in evidence["live_fetch"]["attempts"]] == ["error", "ok", "ok"]


def test_live_disabled(tmp_path):
    live = {**BASE_CONFIG["live"], "enabled": False}
    evidence = make_service(tmp_path, live=live).get_evidence("auto")
    assert evidence["source_label"] == LABEL_FALLBACK
    assert evidence["live_fetch"]["status"] == "DISABLED"


# ---- tampering / mislabelled input ------------------------------------------------------

def test_verification_does_not_transfer_to_another_file(tmp_path):
    service = make_service(tmp_path, clean_live_session())
    evidence = service.get_evidence("live")
    verification = evidence["live_fetch"]["verification"]
    snapshot = tmp_path / "live" / "tampered.csv"
    text = open(evidence["live_fetch"]["snapshot_path"], encoding="utf-8").read()
    snapshot.write_text(text.replace("74.95", "70.95"), encoding="utf-8")
    result = RiverMonitoringAgent(snapshot, station_id="UPPER_ANICUT",
                                  source_verification=verification, clock=lambda: NOW).run()
    assert result["data_quality"]["source_verification"]["status"] == "FAILED"
    assert result["data_quality"]["source_verification"]["snapshot_hash_matches"] is False
    assert result["data_quality"]["is_verified_live"] is False
    assert "LIVE_SOURCE_NOT_VERIFIED" in {w["code"] for w in result["warnings"]}


def test_csv_labelled_live_without_verification_is_unverified(tmp_path):
    path = tmp_path / "claims_live.csv"
    path.write_text("timestamp,station_id,river_level_m,source_type\n"
                    "2026-10-13T07:00:00Z,UPPER_ANICUT,74.9,live\n"
                    "2026-10-13T07:30:00Z,UPPER_ANICUT,74.91,live\n", encoding="utf-8")
    result = RiverMonitoringAgent(path, clock=lambda: NOW).run()
    assert result["data_quality"]["evidence_grade"] == "UNVERIFIED_LIVE"
    assert result["data_quality"]["is_verified_live"] is False
    assert result["data_quality"]["source_verification"]["status"] == "NOT_APPLICABLE"


# ---- historical / simulated ------------------------------------------------------------

def test_historical_replay_and_deterministic_evidence_id(tmp_path):
    as_of = datetime(2025, 12, 30, 6, 0, tzinfo=IST)
    first = make_service(tmp_path).get_evidence("historical", as_of=as_of)
    second = make_service(tmp_path).get_evidence("historical", as_of=as_of)
    assert first["source_label"] == LABEL_HISTORICAL
    assert first["river_level_m"] == 74.9
    assert first["run_id"] != second["run_id"]
    assert first["evidence_id"] == second["evidence_id"] == compute_evidence_id(first)
    other = make_service(tmp_path).get_evidence(
        "historical", as_of=datetime(2024, 6, 6, 12, 0, tzinfo=IST))
    assert other["evidence_id"] != first["evidence_id"]


def test_simulated_mode_is_labelled_and_never_verified(tmp_path):
    evidence = make_service(tmp_path).get_evidence(
        "simulated", as_of=datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc))
    assert evidence["source_label"] == LABEL_SIMULATED
    assert evidence["data_quality"]["evidence_grade"] == "SIMULATED"
    assert "VERIFIED_LIVE" not in json.dumps(evidence)
    assert evidence["decision_support"]["current_evidence_usable"] is False


@pytest.mark.parametrize("mode", ["live", "auto"])
def test_as_of_not_allowed_for_live(tmp_path, mode):
    with pytest.raises(ValueError, match="as_of"):
        make_service(tmp_path).get_evidence(mode, as_of=NOW)


def test_invalid_mode(tmp_path):
    with pytest.raises(ValueError):
        make_service(tmp_path).get_evidence("guess")


# ---- conflicting evidence --------------------------------------------------------------

def test_cross_source_conflict_downgrades_status(tmp_path):
    other = tmp_path / "other_source.csv"
    other.write_text("timestamp,station_id,river_level_m,source_type\n"
                     "2026-10-13T07:30:00Z,UPPER_ANICUT,76.50,historical\n"
                     "2026-10-13T06:30:00Z,UPPER_ANICUT,74.94,historical\n", encoding="utf-8")
    service = make_service(tmp_path, clean_live_session(),
                           cross_check={"enabled": True, "paths": [str(other)],
                                        "tolerance_m": 0.005})
    service.thresholds = validate_thresholds(TEST_THRESHOLDS)
    evidence = service.get_evidence("live")
    check = evidence["cross_source_check"]
    assert check["status"] == "CONFLICT"
    assert check["overlapping_timestamps"] == 2 and check["conflicts"] == 1
    assert check["conflicts_affecting_current_evidence"] == 1
    assert check["examples"][0]["other_m"] == 76.5
    assert "CONFLICTING_SOURCES" in {w["code"] for w in evidence["warnings"]}
    assert evidence["status"] == STATUS_PARTIAL  # would be OK without the conflict
    assert evidence["decision_support"]["current_evidence_usable"] is False


def test_cross_check_no_overlap_with_history(tmp_path):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    assert evidence["cross_source_check"]["status"] == "NO_OVERLAP"


def test_cross_check_function_ignores_future_rows(tmp_path):
    a, b = tmp_path / "a.csv", tmp_path / "b.csv"
    header = "timestamp,station_id,river_level_m,source_type\n"
    a.write_text(header + "2026-10-13T09:00:00Z,S,1.0,historical\n", encoding="utf-8")
    b.write_text(header + "2026-10-13T09:00:00Z,S,2.0,historical\n", encoding="utf-8")
    block = cross_check_sources(a, "S", [b], NOW)
    assert block["overlapping_timestamps"] == 0


# ---- thresholds: official / datum ------------------------------------------------------

def test_official_thresholds_need_reference():
    with pytest.raises(ValueError, match="reference"):
        validate_thresholds({"S": {"warning_level_m": 1, "danger_level_m": 2, "official": True}})
    with pytest.raises(ValueError, match="datum"):
        validate_thresholds({"S": {"warning_level_m": 1, "danger_level_m": 2, "datum": "msl"}})


def test_datum_mismatch_gives_unknown():
    thresholds = validate_thresholds({"S": {"warning_level_m": 3.0, "danger_level_m": 4.0,
                                            "datum": "gauge_height", "official": True,
                                            "reference": "TEST"}})["S"]
    result = evaluate_thresholds(74.9, thresholds, data_datum="reduced_level")
    assert result["status"] == THRESHOLD_UNKNOWN
    assert result["reason"].startswith("Datum mismatch")
    assert evaluate_thresholds(3.5, thresholds, data_datum="gauge_height")["status"] \
        == "AT_OR_ABOVE_WARNING"
    assert evaluate_thresholds(3.5, thresholds)["threshold_is_official"] is True


# ---- audit ----------------------------------------------------------------------------

def test_audit_log_records_live_run(tmp_path):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    log = read_audit_log(tmp_path / "audit" / "river_audit.jsonl")
    assert [e["event"] for e in log] == ["run_started", "live_fetch_ok", "live_verification",
                                         "evidence_produced"]
    assert {e["run_id"] for e in log} == {evidence["run_id"]}
    produced = log[-1]["details"]
    assert produced["evidence_id"] == evidence["evidence_id"]
    assert produced["observed_at"] == evidence["observed_at"]
    assert log[1]["details"]["snapshot_sha256"] == evidence["live_fetch"]["snapshot_sha256"]
    assert evidence["audit"]["events_written"] == 4


def test_audit_failure_is_reported_not_fatal(tmp_path):
    service = make_service(tmp_path)
    (tmp_path / "audit" / "river_audit.jsonl").mkdir(parents=True)  # a directory: unwritable
    evidence = service.get_evidence("historical",
                                    as_of=datetime(2025, 12, 30, 6, 0, tzinfo=IST))
    assert evidence["river_level_m"] == 74.9
    assert evidence["audit"]["write_errors"]
    assert "AUDIT_LOG_INCOMPLETE" in {w["code"] for w in evidence["warnings"]}


# ---- end-to-end CLI -----------------------------------------------------------------------

def test_cli_historical_json_and_save(tmp_path, capsys):
    config = json.loads(json.dumps(BASE_CONFIG))
    config["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                         "audit_log": str(tmp_path / "audit.jsonl"),
                         "evidence": str(tmp_path / "evidence.json")}
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps(config), encoding="utf-8")
    import src.river_service as svc
    original = svc.PROJECT_DIR
    code = main(["--config", str(config_path), "--mode", "historical",
                 "--as-of", "2025-12-30T06:00:00+05:30", "--json", "--save"])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0 and svc.PROJECT_DIR == original
    assert printed["schema_version"] == SCHEMA_VERSION
    assert json.loads((tmp_path / "evidence.json").read_text(encoding="utf-8"))[
        "evidence_id"] == printed["evidence_id"]


def test_cli_bad_config_exits_2(tmp_path, capsys):
    bad = tmp_path / "bad.json"
    bad.write_text("{not json", encoding="utf-8")
    assert main(["--config", str(bad), "--mode", "historical"]) == 2


def test_project_files_are_consistent():
    """The committed config points at files that exist and agree with each other."""
    meta = json.loads((PROJECT_DIR / "data" / "cwc" / "upper_anicut_station.json")
                      .read_text(encoding="utf-8"))
    station = BASE_CONFIG["station"]
    assert meta["station_id"] == station["station_id"]
    assert meta["station_name"] == station["provider_station_name"]
    assert float(meta["attributes_from_file"]["RL_of_zeroGauge"]) == station["rl_zero_gauge_m"]
    assert float(meta["attributes_from_file"]["Latitude"]) == station["latitude"]
    assert BASE_CONFIG["thresholds_path"] is None  # no invented official thresholds


def test_decision_support_checks_each_rule_independently(tmp_path):
    """Defence in depth: every rule blocks usability on its own."""
    from src.river_service import decision_support
    base = make_service(tmp_path, clean_live_session()).get_evidence("live")
    assert decision_support(base)["current_evidence_usable"] is True
    for mutate, needle in [
        (lambda e: e["freshness"].update(status="STALE"), "Freshness"),
        (lambda e: e["data_quality"]["recent_quality"].update(status="SUSPECT"), "quality"),
        (lambda e: e["data_quality"].update(is_verified_live=False), "Not verified"),
        (lambda e: e.update(status="DATA_UNAVAILABLE"), "DATA_UNAVAILABLE"),
        (lambda e: e["fallback"].update(used=True), "fallback"),
        (lambda e: e["cross_source_check"].update(conflicts_affecting_current_evidence=1),
         "disagree"),
    ]:
        evidence = json.loads(json.dumps(base))
        mutate(evidence)
        result = decision_support(evidence)
        assert result["current_evidence_usable"] is False
        assert result["river_level_m"] is None
        assert any(needle in reason for reason in result["reasons_not_usable"]), needle
