"""Tests for the Evidence Layer (src/evidence_layer.py) and its integration with
the River Agent workflow, the capability gate, the audit log and the dashboard.

Offline: the NWDP API is replaced by FakeSession fixtures (tests/nwdp_fixtures.py)
and Open-Meteo by fabricated WeatherAgent results (tests/test_risk_assessment.py).
"""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

import src.river_agent_workflow as workflow
from src.audit_log import AuditLog, read_audit_log
from src.capability_control import REAL_WORLD_CAPABILITIES
from src.dashboard_service import evidence_block
from src.evidence_layer import (
    ACCEPTED,
    INSUFFICIENT,
    REJECTED,
    SIMULATED_ONLY,
    UNRESOLVED_CONFLICT,
    build_evidence_layer,
    load_settings,
    river_record,
    weather_record,
)
from src.river_agent_workflow import run_river_workflow, run_simulated_scenario
from tests.nwdp_fixtures import (
    IST,
    FakeResponse,
    FakeSession,
    datastore_payload,
    ok_session,
    resource_show_payload,
    synthetic_records,
)
from tests.test_risk_assessment import weather_result
from tests.test_river_service import CLEAN_LEVELS, NOW, clean_live_session, make_service
from tests.test_river_workflow import SIM_AS_OF, write_source

STATION = {"station_id": "UPPER_ANICUT", "latitude": 10.88861111, "longitude": 78.58}
SETTINGS = load_settings()
C1, C4, C6 = ("C1_RIVER_CURRENT_LEVEL", "C4_MODELLED_RAINFALL_24H",
              "C6_EVIDENCE_SUFFICIENT_FOR_REAL_WORLD_ACTION")


def run(tmp_path, session=None, name="wf.jsonl", **kw):
    kw.setdefault("mode", "live")
    kw.setdefault("disaster_state", "EMERGENCY")
    return run_river_workflow(make_service(tmp_path, session), AuditLog(tmp_path / name), **kw)


def sim(tmp_path, scenario, state="EMERGENCY", service=None, **kw):
    return run_simulated_scenario(scenario, service or make_service(tmp_path),
                                  AuditLog(tmp_path / "wf.jsonl"), as_of=SIM_AS_OF,
                                  disaster_state=state, **kw)


def claim(result, claim_id):
    return next(c for c in result["evidence_layer"]["claims"] if c["claim_id"] == claim_id)


def river(result):
    return next(r for r in result["evidence_layer"]["records"] if r["kind"] == "RIVER_LEVEL")


def codes(record):
    return set(record["validation"]["error_codes"])


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


def live_weather(**changes):
    w = weather_result(0.0, 0.0, retrieved_at=NOW, simulated=False)
    for key, value in changes.items():
        w[key] = value
    return w


def wrec(weather, simulated=False, river_observed=NOW - timedelta(minutes=30)):
    return weather_record(weather, simulated, river_observed, STATION, NOW, SETTINGS)


# ---- structured records -------------------------------------------------------------------

REQUIRED_RECORD_KEYS = {"evidence_id", "kind", "evidence_class", "measurement_basis", "source",
                        "observed_at", "retrieved_at", "location", "value", "verification",
                        "freshness", "quality", "uncertainty", "limitations", "validation"}


def test_fresh_verified_live_record_is_complete_and_accepted(tmp_path):
    result = run(tmp_path, clean_live_session())
    rec = river(result)
    assert REQUIRED_RECORD_KEYS <= set(rec)
    assert rec["evidence_class"] == "LIVE_VERIFIED"
    assert rec["validation"]["status"] == "VALID"
    assert rec["observed_at"] == "2026-10-13T07:30:00+00:00"
    assert rec["retrieved_at"] and rec["source"]["content_sha256"]
    assert rec["location"]["station_id"] == "UPPER_ANICUT"
    assert rec["freshness"]["status"] == "FRESH" and rec["uncertainty"] and rec["limitations"]
    assert claim(result, C1)["status"] == ACCEPTED
    assert claim(result, C1)["supporting_evidence_ids"] == [rec["evidence_id"]]
    assert result["evidence_layer"]["real_world_evidence_sufficient"] is True


def test_evidence_ids_are_deterministic(tmp_path):
    a = run(tmp_path, clean_live_session(), name="a.jsonl")
    b = run(tmp_path, clean_live_session(), name="b.jsonl")
    ids = lambda r: [x["evidence_id"] for x in r["evidence_layer"]["records"]]
    assert ids(a) == ids(b)
    assert a["decision_id"] == b["decision_id"]


# ---- fresh vs stale -----------------------------------------------------------------------

def test_stale_live_cwc_data_is_not_current(tmp_path):
    result = run(tmp_path, ok_session())  # real NWDP fixture: months old at NOW
    rec = river(result)
    assert rec["evidence_class"] == "LIVE_STALE"
    assert "STALE" in codes(rec)
    stale = next(f for f in rec["validation"]["findings"] if f["code"] == "STALE")
    assert "not current conditions" in stale["detail"]
    c1 = claim(result, C1)
    assert c1["status"] != ACCEPTED and c1["supporting_evidence_ids"] == []
    assert claim(result, C6)["status"] != ACCEPTED
    assert result["real_world_authorization"] == "RESTRICTED"


def test_historical_fallback_is_never_current(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    result = run(tmp_path, session, mode="auto")
    rec = river(result)
    assert rec["evidence_class"] == "HISTORICAL_NOT_CURRENT"
    assert claim(result, C1)["status"] == INSUFFICIENT
    assert any("past conditions" in e for e in claim(result, C1)["explanation"])


def test_future_dated_live_rows_are_not_accepted(tmp_path):
    # Readings ending 2 h after the service clock: the source fails verification.
    session = clean_live_session(end_ist=datetime(2026, 10, 13, 15, 30, tzinfo=IST))
    result = run(tmp_path, session)
    assert river(result)["evidence_class"] == "LIVE_UNVERIFIED"
    assert claim(result, C1)["status"] == REJECTED
    assert result["real_world_authorization"] == "RESTRICTED"


@pytest.mark.parametrize("field,shift,code", [
    ("observed_at", timedelta(hours=2), "FUTURE_TIMESTAMP"),
    ("retrieved_at", timedelta(hours=-1), "RETRIEVED_BEFORE_OBSERVED"),
])
def test_bad_river_timestamps_are_rejected(tmp_path, field, shift, code):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    edited = copy.deepcopy(evidence)
    base = NOW if field == "observed_at" else datetime(2026, 10, 13, 7, 30, tzinfo=timezone.utc)
    edited[field if field in edited else "observed_at"] = (base + shift).isoformat()
    if field == "retrieved_at":
        edited.setdefault("live_fetch", {})["retrieved_at"] = (base + shift).isoformat()
        edited["generated_at"] = (base + shift).isoformat()
    rec = river_record(edited, STATION, None, NOW, SETTINGS)
    assert code in codes(rec), rec["validation"]
    assert rec["validation"]["status"] == "INVALID"


@pytest.mark.parametrize("level", [None, "abc", float("nan"), -5000.0, 5000.0])
def test_missing_invalid_or_implausible_level_is_not_accepted(tmp_path, level):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    edited = copy.deepcopy(evidence)
    edited["river_level_m"] = level
    rec = river_record(edited, STATION, None, NOW, SETTINGS)
    assert codes(rec) & {"MISSING_VALUE", "INVALID_VALUE", "IMPLAUSIBLE_VALUE"}
    assert rec["validation"]["status"] != "VALID"


# ---- API failures -------------------------------------------------------------------------

@pytest.mark.parametrize("exc", [requests.exceptions.Timeout("slow"),
                                 requests.exceptions.ConnectionError("down")])
def test_api_failure_is_recorded_as_unavailable_evidence(tmp_path, exc):
    session = FakeSession({"resource_show": [exc]})
    result = run(tmp_path, session)
    layer = result["evidence_layer"]
    attempt = next(r for r in layer["records"] if r["kind"] == "LIVE_SOURCE_ATTEMPT")
    assert attempt["evidence_class"] == "UNAVAILABLE"
    assert "SOURCE_UNAVAILABLE" in codes(attempt)
    assert river(result)["value"]["river_level_m"] is None  # nothing invented
    assert claim(result, C1)["status"] == INSUFFICIENT
    assert layer["real_world_evidence_sufficient"] is False
    assert result["real_world_authorization"] == "RESTRICTED"


def test_http_error_then_success_is_live(tmp_path):
    session = FakeSession({
        "resource_show": [FakeResponse(503, {"error": "busy"}),
                          FakeResponse(200, resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(
            synthetic_records(datetime(2026, 10, 13, 13, 0, tzinfo=IST), CLEAN_LEVELS)))]})
    result = run(tmp_path, session)
    assert river(result)["evidence_class"] == "LIVE_VERIFIED"
    assert not any(r["kind"] == "LIVE_SOURCE_ATTEMPT" for r in result["evidence_layer"]["records"])


# ---- source conflicts and reverification --------------------------------------------------

def test_conflict_without_independent_source_stays_unresolved(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    result = run(tmp_path, clean_live_session(), cross_check_paths=[other])
    comparisons = [r for r in result["evidence_layer"]["records"]
                   if r["kind"] == "RIVER_LEVEL_COMPARISON"]
    contra = [r for r in comparisons if r.get("contradicts_current_reading")]
    assert contra and contra[0]["source"]["independence"] == "CONFIGURED_NOT_VERIFIED"
    c1 = claim(result, C1)
    assert c1["status"] == UNRESOLVED_CONFLICT
    assert contra[0]["evidence_id"] in c1["contradicting_evidence_ids"]
    assert claim(result, C6)["status"] == UNRESOLVED_CONFLICT
    rev = result["reverification"]
    assert rev["outcome"] == "FAILED" and len(rev["attempt_log"]) >= 1
    assert result["real_world_authorization"] == "RESTRICTED"


def test_conflict_resolved_by_fresh_and_independent_evidence(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    result = run(tmp_path, clean_live_session(), cross_check_paths=[other],
                 independent_paths=[independent])
    rev = result["reverification"]
    assert rev["outcome"] == "RESOLVED"
    assert [a["outcome"] for a in rev["attempt_log"]] == ["RESOLVED"]
    rechecks = [r for r in result["evidence_layer"]["records"] if r["evidence_class"] == "RECHECK"]
    assert rechecks
    assert claim(result, C1)["status"] == ACCEPTED
    d = decision(result, "request_human_emergency_authorization")
    assert d["allowed"] is True and d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"
    assert d["human_approval_required"] is True


def test_reverification_retries_are_bounded_and_logged(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    end = datetime(2026, 10, 13, 13, 0, tzinfo=IST)
    session = FakeSession({
        "resource_show": [FakeResponse(200, resource_show_payload())],
        "datastore_search": [FakeResponse(200, datastore_payload(
            synthetic_records(end, CLEAN_LEVELS))), requests.exceptions.Timeout("slow")]})
    cfg = copy.deepcopy(make_service(tmp_path).config["reverification"])
    cfg["max_attempts"] = 3
    service = make_service(tmp_path, session, reverification=cfg)
    result = run_river_workflow(service, AuditLog(tmp_path / "wf.jsonl"), mode="live",
                                disaster_state="EMERGENCY", cross_check_paths=[other],
                                independent_paths=[independent])
    rev = result["reverification"]
    assert rev["outcome"] == "FAILED"
    assert [a["attempt"] for a in rev["attempt_log"]] == [1, 2, 3]
    assert all(a["outcome"] == "FAILED" and a["error"] for a in rev["attempt_log"])
    assert "after 3 attempt" in rev["reason"]
    assert claim(result, C1)["status"] == UNRESOLVED_CONFLICT  # uncertainty preserved
    events = [e for e in read_audit_log(tmp_path / "wf.jsonl")
              if e["event"] == "evidence_reverification_attempt"]
    assert len(events) == 3
    assert result["real_world_authorization"] == "RESTRICTED"


def test_reverification_succeeds_on_second_attempt(tmp_path):
    other = write_source(tmp_path, "other.csv", 76.10)
    independent = write_source(tmp_path, "independent.csv", 74.95)
    end = datetime(2026, 10, 13, 13, 0, tzinfo=IST)
    ok = FakeResponse(200, datastore_payload(synthetic_records(end, CLEAN_LEVELS)))
    timeout = requests.exceptions.Timeout("slow")
    session = FakeSession({"resource_show": [FakeResponse(200, resource_show_payload())],
                           "datastore_search": [ok] + [timeout] * 10 + [ok]})
    service = make_service(tmp_path, session)
    retries = len(service.get_evidence("live")["live_fetch"]["attempts"])  # warm-up: 1 ok
    session.routes["datastore_search"] = [ok] + [timeout] * (retries + 1) + [ok]
    result = run_river_workflow(service, AuditLog(tmp_path / "wf.jsonl"), mode="live",
                                disaster_state="EMERGENCY", cross_check_paths=[other],
                                independent_paths=[independent])
    outcomes = [a["outcome"] for a in result["reverification"]["attempt_log"]]
    assert outcomes[0] == "FAILED" and outcomes[-1] == "RESOLVED" and len(outcomes) == 2
    assert result["reverification"]["outcome"] == "RESOLVED"


def test_max_attempts_is_clamped(tmp_path):
    for bad in (0, 99, "3", True, None):
        cfg = {**make_service(tmp_path).config["reverification"], "max_attempts": bad}
        assert workflow._max_attempts(make_service(tmp_path, reverification=cfg)) == \
            workflow.DEFAULT_REVERIFY_ATTEMPTS


@pytest.mark.parametrize("scenario,outcome,c1", [
    ("conflict-resolves", "RESOLVED", SIMULATED_ONLY),
    ("conflict-persists", "CONFLICT_PERSISTS", UNRESOLVED_CONFLICT),
    ("reverification-fails", "FAILED", UNRESOLVED_CONFLICT),
])
def test_simulated_reverification_outcomes(tmp_path, scenario, outcome, c1):
    result = sim(tmp_path, scenario)
    assert result["reverification"]["outcome"] == outcome
    assert claim(result, C1)["status"] == c1
    assert claim(result, C6)["status"] != ACCEPTED
    assert result["evidence_layer"]["real_world_evidence_sufficient"] is False


# ---- capability restrictions --------------------------------------------------------------

@pytest.mark.parametrize("state", ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"])
def test_simulated_evidence_never_authorizes_real_world(tmp_path, state):
    result = sim(tmp_path, "no-conflict", state=state)
    assert river(result)["evidence_class"] == "SIMULATED"
    for cap in REAL_WORLD_CAPABILITIES:
        assert decision(result, cap)["allowed"] is False
    assert result["real_world_authorization"] == "RESTRICTED"


def test_state_policy_still_applies_with_accepted_evidence(tmp_path):
    result = run(tmp_path, clean_live_session(), disaster_state="NORMAL")
    assert claim(result, C6)["status"] == ACCEPTED
    assert decision(result, "propose_evacuation_plan")["reason"] == "CAPABILITY_DENIED"


def test_layer_can_only_deny_never_allow(tmp_path, monkeypatch):
    """A layer claiming sufficiency cannot override the gate's denial of stale data."""
    def always_sufficient(*args, **kwargs):
        return {"layer": "EvidenceLayer", "records": [], "claims": [],
                "real_world_evidence_sufficient": True, "deny_reasons": [],
                "audit_complete": True}
    monkeypatch.setattr(workflow, "build_evidence_layer", always_sufficient)
    result = run(tmp_path, ok_session())
    for cap in REAL_WORLD_CAPABILITIES:
        assert decision(result, cap)["allowed"] is False
    assert result["real_world_authorization"] == "RESTRICTED"


def test_insufficient_layer_withdraws_gate_permission(tmp_path, monkeypatch):
    def insufficient(*args, **kwargs):
        return {"layer": "EvidenceLayer", "records": [], "claims": [],
                "real_world_evidence_sufficient": False, "deny_reasons": ["C6_INSUFFICIENT"],
                "audit_complete": True}
    monkeypatch.setattr(workflow, "build_evidence_layer", insufficient)
    result = run(tmp_path, clean_live_session())
    for cap in REAL_WORLD_CAPABILITIES:
        d = decision(result, cap)
        assert d["allowed"] is False and d["reason"] == "EVIDENCE_LAYER_INSUFFICIENT"
        assert "C6_INSUFFICIENT" in d["evidence_reasons"]
    assert decision(result, "read_river")["allowed"] is True  # read-only unaffected
    assert result["real_world_authorization"] == "RESTRICTED"
    assert "capability_denied_by_evidence_layer" in {
        e["event"] for e in read_audit_log(tmp_path / "wf.jsonl")}


def test_crashing_layer_fails_closed(tmp_path, monkeypatch):
    def boom(*args, **kwargs):
        raise RuntimeError("bug")
    monkeypatch.setattr(workflow, "build_evidence_layer", boom)
    result = run(tmp_path, clean_live_session())
    assert result["evidence_layer"]["deny_reasons"] == ["EVIDENCE_LAYER_FAILED"]
    assert result["real_world_authorization"] == "RESTRICTED"
    assert "evidence_layer_failed" in {e["event"] for e in read_audit_log(tmp_path / "wf.jsonl")}


# ---- audit records ------------------------------------------------------------------------

def test_layer_events_are_audited(tmp_path):
    run(tmp_path, clean_live_session())
    events = read_audit_log(tmp_path / "wf.jsonl")
    names = [e["event"] for e in events]
    for name in ("evidence_record_acquired", "evidence_validated", "claim_evaluated",
                 "evidence_layer_completed", "capability_authorization"):
        assert name in names, name
    assert names.count("claim_evaluated") == 6
    done = next(e for e in events if e["event"] == "evidence_layer_completed")
    details = done.get("details", done)
    assert details["real_world_evidence_sufficient"] is True


class FailingAudit:
    """Accepts everything except the named events."""

    def __init__(self, fail_on):
        self.fail_on, self.events = set(fail_on), []

    def log(self, run_id, event, **details):
        self.events.append(event)
        return event not in self.fail_on


def layer_for(tmp_path, audit):
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    from src.capability_control import assess_evidence_for_authorization
    gate = assess_evidence_for_authorization(evidence)
    assert gate["eligible"] is True
    return build_evidence_layer(evidence, None, None, gate, None, STATION, None, NOW, audit,
                                "run")


@pytest.mark.parametrize("fail_on", ["claim_evaluated", "evidence_layer_completed",
                                     "evidence_record_acquired"])
def test_audit_failure_makes_evidence_insufficient(tmp_path, fail_on):
    layer = layer_for(tmp_path, FailingAudit([fail_on]))
    assert layer["audit_complete"] is False
    assert layer["real_world_evidence_sufficient"] is False
    assert layer["deny_reasons"].count("EVIDENCE_AUDIT_INCOMPLETE") == 1


def test_complete_audit_is_sufficient(tmp_path):
    layer = layer_for(tmp_path, FailingAudit([]))
    assert layer["audit_complete"] is True and layer["real_world_evidence_sufficient"] is True


# ---- weather validation -------------------------------------------------------------------

def test_open_meteo_is_model_output_not_a_sensor():
    rec = wrec(live_weather())
    assert rec["evidence_class"] == "MODEL_ESTIMATE"
    assert rec["measurement_basis"].startswith("NUMERICAL_WEATHER_MODEL")
    assert "not a rain-gauge or sensor" in rec["measurement_basis"]
    assert rec["verification"]["status"] == "NOT_VERIFIABLE"


def test_stale_weather():
    old = weather_result(0.0, 0.0, retrieved_at=NOW - timedelta(hours=6), simulated=False)
    rec = wrec(old)
    assert rec["evidence_class"] == "MODEL_STALE" and "STALE" in codes(rec)


def test_future_weather_timestamp_is_rejected():
    future = weather_result(0.0, 0.0, retrieved_at=NOW + timedelta(hours=2), simulated=False)
    assert "FUTURE_TIMESTAMP" in codes(wrec(future))


def test_implausible_rain_is_rejected():
    rec = wrec(weather_result(400.0, 0.0, retrieved_at=NOW, simulated=False))
    assert "IMPLAUSIBLE_VALUE" in codes(rec)
    assert rec["validation"]["status"] == "INVALID"


def test_negative_rain_is_invalid():
    rec = wrec(weather_result(-1.0, 0.0, retrieved_at=NOW, simulated=False))
    assert "INVALID_VALUE" in codes(rec) and rec["validation"]["status"] == "INVALID"


def test_weather_far_from_station_is_location_mismatch():
    w = live_weather()
    w["returned_grid_location"] = {"latitude": 13.0, "longitude": 80.2}  # Chennai
    assert "LOCATION_MISMATCH" in codes(wrec(w))


def test_simulated_weather_with_real_river_is_mode_mismatch():
    w = weather_result(0.0, 0.0, retrieved_at=NOW, simulated=True)
    rec = wrec(w, simulated=False)
    assert "MODE_MISMATCH" in codes(rec)


def test_weather_far_in_time_from_river_is_flagged():
    rec = wrec(live_weather(), river_observed=NOW - timedelta(hours=8))
    finding = next(f for f in rec["validation"]["findings"] if f["code"] == "TIME_MISMATCH")
    assert finding["severity"] == "warning" and "8.0 h apart" in finding["detail"]


def test_unavailable_weather_is_not_zero():
    rec = wrec({"status": "DATA_UNAVAILABLE", "errors": [{"code": "WEATHER_API_FAILED"}]})
    assert rec["evidence_class"] == "UNAVAILABLE"
    assert "MISSING_VALUE" in codes(rec)
    assert "WEATHER_API_FAILED" in rec["validation"]["findings"][0]["detail"]


def test_rejected_weather_claim_lists_contradicting_evidence(tmp_path):
    w = live_weather()
    w["returned_grid_location"] = {"latitude": 13.0, "longitude": 80.2}
    result = run(tmp_path, clean_live_session(), weather=w)
    c4 = claim(result, C4)
    assert c4["status"] == REJECTED and c4["contradicting_evidence_ids"]
    assert "not a sensor reading" in c4["statement"]


def test_accepted_weather_claim_says_model(tmp_path):
    result = run(tmp_path, clean_live_session(), weather=live_weather())
    c4 = claim(result, C4)
    assert c4["status"] == ACCEPTED
    assert any("not a rain-gauge" in e for e in c4["explanation"])


# ---- thresholds and settings --------------------------------------------------------------

def test_thresholds_never_presented_as_official(tmp_path):
    result = sim(tmp_path, "no-conflict")
    c3 = claim(result, "C3_RIVER_THRESHOLD_STATUS")
    text = " ".join([c3["statement"]] + c3["explanation"])
    assert "official CWC" not in text.replace("NOT official CWC", "").replace(
        "not official CWC", "")


def test_settings_are_configurable(tmp_path):
    path = tmp_path / "ev.json"
    path.write_text(json.dumps({"max_weather_age_hours": 0.25}), encoding="utf-8")
    settings = load_settings(path)
    assert settings["max_weather_age_hours"] == 0.25
    assert settings["rainfall_plausible_max_mm_24h"] == SETTINGS["rainfall_plausible_max_mm_24h"]
    old = weather_result(0.0, 0.0, retrieved_at=NOW - timedelta(hours=1), simulated=False)
    rec = weather_record(old, False, None, STATION, NOW, settings)
    assert "STALE" in codes(rec)


def test_config_comment_disclaims_official_status():
    from src.evidence_layer import DEFAULT_CONFIG_PATH
    text = json.loads(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))["_comment"]
    assert "none is an official" in text


# ---- dashboard ----------------------------------------------------------------------------

def test_dashboard_block_distinguishes_classes(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    block = evidence_block(run(tmp_path, session, mode="auto", weather=live_weather()))
    assert block["available"] is True
    classes = {r["evidence_class"] for r in block["records"]}
    assert {"HISTORICAL_NOT_CURRENT", "UNAVAILABLE", "MODEL_ESTIMATE"} <= classes
    for klass in ("LIVE_VERIFIED", "LIVE_STALE", "SIMULATED", "UNAVAILABLE", "MODEL_ESTIMATE"):
        assert klass in block["class_descriptions"]
    assert block["real_world_evidence_sufficient"] is False
    assert len(block["claims"]) == 6


def test_dashboard_block_without_layer():
    assert evidence_block({})["available"] is False


def test_gate_eligibility_alone_does_not_make_c6_accepted(tmp_path):
    """C6 needs C1 accepted as well: an implausible reading is denied even if the gate
    (computed on the original evidence) said eligible."""
    evidence = make_service(tmp_path, clean_live_session()).get_evidence("live")
    from src.capability_control import assess_evidence_for_authorization
    gate = assess_evidence_for_authorization(evidence)
    assert gate["eligible"] is True
    edited = copy.deepcopy(evidence)
    edited["river_level_m"] = 5000.0
    layer = build_evidence_layer(edited, None, None, gate, None, STATION, None, NOW,
                                 FailingAudit([]), "run")
    c6 = next(c for c in layer["claims"] if c["claim_id"] == C6)
    assert c6["status"] == REJECTED
    assert layer["real_world_evidence_sufficient"] is False
