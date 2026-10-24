"""Tests for the combined Weather + River evidence workflow. Offline."""

import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import src.flood_evidence_workflow as fe
from src.audit_log import AuditLog, read_audit_log
from src.flood_evidence_workflow import (
    DEFAULT_SETTINGS,
    haversine_km,
    load_settings,
    run_flood_evidence_workflow,
)
from src.river_monitoring_agent import PROJECT_DIR
from src.weather_agent import WeatherAgent
from tests.test_river_service import NOW, clean_live_session, make_service
from tests.test_weather_agent import FakeResponse as WeatherResponse
from tests.test_weather_agent import FakeSession as WeatherSession
from tests.test_weather_agent import make_payload

WEATHER_FILE = PROJECT_DIR / "outputs" / "weather_live_20261010T082315Z.json"
SIM_AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
IST = timezone(timedelta(hours=5, minutes=30))


def weather_agent(current_utc=NOW - timedelta(minutes=15), fail=False):
    if fail:
        return WeatherAgent(session=WeatherSession(exc=requests.exceptions.ConnectionError("down")))
    payload = make_payload()
    payload["current"]["time"] = current_utc.astimezone(IST).strftime("%Y-%m-%dT%H:%M")
    return WeatherAgent(session=WeatherSession(WeatherResponse(json_data=payload)))


def run(tmp_path, river_session=None, weather=None, settings=None, **kwargs):
    kwargs.setdefault("river_mode", "live")
    kwargs.setdefault("disaster_state", "EMERGENCY")
    return run_flood_evidence_workflow(
        make_service(tmp_path, river_session or clean_live_session()),
        AuditLog(tmp_path / "flood.jsonl"), weather_agent=weather or weather_agent(),
        settings={**DEFAULT_SETTINGS, **(settings or {})}, clock=lambda: NOW, **kwargs)


def check(result, name):
    return next(c for c in result["cross_agent_checks"] if c["check"] == name)


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


# ---- eligible path ---------------------------------------------------------------

def test_all_conditions_met_only_pending_human_approval(tmp_path):
    result = run(tmp_path)
    assert result["combined_assessment"]["eligible"] is True
    assert all(c["status"] == "PASS" for c in result["cross_agent_checks"])
    assert check(result, "time_alignment")["detail"].startswith(
        "River and weather observations are 0.25 h apart")
    d = decision(result, "request_human_emergency_authorization")
    assert d["allowed"] and d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"
    assert d["human_approval_required"] is True
    assert result["real_world_authorization"] == "HUMAN_AUTHORIZATION_REQUIRED"
    assert result["weather"]["source_label"] == "WEATHER_LIVE_MODEL"
    assert result["river"]["river_level_m"] == 74.95


def test_state_gate_still_applies(tmp_path):
    result = run(tmp_path, disaster_state="NORMAL")
    assert decision(result, "propose_evacuation_plan")["reason"] == "CAPABILITY_DENIED"
    assert decision(result, "prepare_advisory")["allowed"] is True


# ---- weather blocks even when the river is fine ----------------------------------------

def test_weather_outage_restricts(tmp_path):
    result = run(tmp_path, weather=weather_agent(fail=True))
    assert result["river"]["evidence_assessment"]["eligible"] is True
    assert "WEATHER_UNAVAILABLE" in result["combined_assessment"]["reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_stale_weather_restricts(tmp_path):
    result = run(tmp_path, weather=weather_agent(current_utc=NOW - timedelta(hours=6)))
    assert check(result, "weather_freshness")["status"] == "FAIL"
    assert "WEATHER_STALE" in result["combined_assessment"]["reasons"]
    assert "CROSS_AGENT_TIME_SKEW" in result["combined_assessment"]["reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_location_mismatch_restricts(tmp_path):
    result = run(tmp_path, settings={"max_location_distance_km": 1.0})
    assert check(result, "location_alignment")["status"] == "FAIL"
    assert result["combined_assessment"]["cross_agent_reasons"] == [
        "CROSS_AGENT_LOCATION_MISMATCH"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_river_problem_restricts_with_good_weather(tmp_path):
    from tests.nwdp_fixtures import ok_session
    result = run(tmp_path, river_session=ok_session())  # real data: verified but stale
    assert result["weather"]["reasons"] == []
    assert "EVIDENCE_STALE" in result["combined_assessment"]["river_reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


# ---- replay -------------------------------------------------------------------------

def test_replayed_weather_never_authorizes(tmp_path):
    result = run_flood_evidence_workflow(
        make_service(tmp_path), AuditLog(tmp_path / "f.jsonl"), river_mode="simulated",
        disaster_state="EMERGENCY", as_of=SIM_AS_OF, scenario="conflict-resolves",
        weather_file=WEATHER_FILE, settings=dict(DEFAULT_SETTINGS), clock=lambda: NOW)
    reasons = result["combined_assessment"]["reasons"]
    assert {"EVIDENCE_SIMULATED", "WEATHER_REPLAYED"} <= set(reasons)
    assert result["river"]["reverification_outcome"] == "RESOLVED"
    assert result["weather"]["rainfall_mm"]["24h"]["total_mm"] is None  # never 0
    assert result["weather"]["rainfall_mm"]["3h"]["total_mm"] == 0.2
    assert check(result, "location_alignment")["detail"].startswith(
        "Weather grid cell is 4.53 km")
    assert result["real_world_authorization"] == "RESTRICTED"


def test_weather_after_replay_time_is_rejected(tmp_path):
    early = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)  # before the 08:15 UTC sample
    result = run_flood_evidence_workflow(
        make_service(tmp_path), AuditLog(tmp_path / "f.jsonl"), river_mode="simulated",
        as_of=early, weather_file=WEATHER_FILE, settings=dict(DEFAULT_SETTINGS))
    assert "WEATHER_AFTER_REFERENCE_TIME" in result["combined_assessment"]["reasons"]


def test_combined_id_is_deterministic_for_replays(tmp_path):
    kwargs = dict(river_mode="simulated", as_of=SIM_AS_OF, weather_file=WEATHER_FILE,
                  settings=dict(DEFAULT_SETTINGS), disaster_state="ALERT")
    a = run_flood_evidence_workflow(make_service(tmp_path), AuditLog(tmp_path / "a.jsonl"),
                                    **kwargs)
    b = run_flood_evidence_workflow(make_service(tmp_path), AuditLog(tmp_path / "b.jsonl"),
                                    **kwargs)
    assert a["run_id"] != b["run_id"] and a["combined_id"] == b["combined_id"]


# ---- observations are never decisions -------------------------------------------------------

def test_hydrological_observation_is_informational(tmp_path):
    result = run(tmp_path)
    obs = result["hydrological_observations"][0]
    assert obs["status"] == "INFO" and obs["blocking"] is False
    assert result["combined_assessment"]["eligible"] is True  # did not block


def test_rise_without_local_rain_is_flagged_but_not_blocking():
    weather = {"rainfall_mm": {"3h": {"status": "AVAILABLE", "total_mm": 0.0}}}
    obs = fe.hydrological_observations(weather, {"river_trend_direction": "RISING"})[0]
    assert "check upstream releases" in obs["detail"]
    assert obs["blocking"] is False


# ---- audit ------------------------------------------------------------------------------

def test_audit_trail_links_both_agents(tmp_path):
    result = run(tmp_path)
    entries = read_audit_log(tmp_path / "flood.jsonl")
    events = [e["event"] for e in entries]
    assert events[0] == "flood_evidence_started" and events[-1] == "flood_evidence_completed"
    assert "river_workflow_completed" in events and "cross_agent_assessment" in events
    assert events.count("capability_authorization") == len(result["capability_decisions"])
    assert {e["run_id"] for e in entries} == {result["run_id"]}
    assert entries[-1]["details"]["combined_id"] == result["combined_id"]


def test_audit_failure_restricts(tmp_path):
    (tmp_path / "flood.jsonl").mkdir()
    result = run(tmp_path)
    assert result["audit"]["write_errors"]
    assert result["real_world_authorization"] == "RESTRICTED"
    assert not any(d["allowed"] for d in result["capability_decisions"])


# ---- settings and CLI ---------------------------------------------------------------------

def test_settings_validation(tmp_path):
    bad = tmp_path / "bad.json"
    bad.write_text(json.dumps({"max_time_skew_hours": 0}), encoding="utf-8")
    with pytest.raises(ValueError):
        load_settings(bad)
    assert load_settings()["max_location_distance_km"] == 10.0  # committed config


def test_haversine_known_distance():
    assert round(haversine_km(10.86116, 78.61063, 10.88861111, 78.58), 2) == 4.53


def test_cli_end_to_end(tmp_path, capsys):
    from tests.test_river_service import BASE_CONFIG
    river_cfg = json.loads(json.dumps(BASE_CONFIG))
    river_cfg["outputs"] = {"snapshot_dir": str(tmp_path / "live"),
                            "audit_log": str(tmp_path / "svc.jsonl"),
                            "evidence": str(tmp_path / "ev.json")}
    (tmp_path / "river.json").write_text(json.dumps(river_cfg), encoding="utf-8")
    (tmp_path / "fe.json").write_text(json.dumps({
        "audit_log": str(tmp_path / "fe_audit.jsonl"),
        "output": str(tmp_path / "flood_evidence.json")}), encoding="utf-8")
    code = fe.main(["--river-mode", "simulated", "--scenario", "conflict-persists",
                    "--state", "ALERT", "--weather-file", str(WEATHER_FILE),
                    "--river-config", str(tmp_path / "river.json"),
                    "--config", str(tmp_path / "fe.json"), "--save"])
    printed = json.loads(capsys.readouterr().out)
    assert code == 0
    assert printed["river"]["reverification_outcome"] == "CONFLICT_PERSISTS"
    assert json.loads((tmp_path / "flood_evidence.json").read_text(encoding="utf-8"))[
        "combined_id"] == printed["combined_id"]


def test_cli_rejects_as_of_for_live():
    with pytest.raises(SystemExit):
        fe.main(["--river-mode", "live", "--as-of", "2026-10-10T12:00:00Z"])


def test_summarize_weather_reasons_directly():
    ref = datetime(2026, 10, 10, 9, 0, tzinfo=timezone.utc)
    sample = json.loads(WEATHER_FILE.read_text(encoding="utf-8"))["result"]
    assert fe.summarize_weather(sample, fe.WEATHER_LIVE, ref)["reasons"] == []
    unavailable = {**sample, "status": "DATA_UNAVAILABLE",
                   "current_weather": {**sample["current_weather"], "time": None}}
    assert fe.summarize_weather(unavailable, fe.WEATHER_LIVE, ref)["reasons"] == [
        "WEATHER_UNAVAILABLE"]
    early = datetime(2026, 10, 10, 8, 0, tzinfo=timezone.utc)
    assert fe.summarize_weather(sample, fe.WEATHER_LIVE, early)["reasons"] == [
        "WEATHER_AFTER_REFERENCE_TIME"]
