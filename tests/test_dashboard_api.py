"""Tests for the read-only dashboard adapter and its HTTP routes. Offline.

The live NWDP API is replaced by the existing fixtures (tests/nwdp_fixtures.py)
and Open-Meteo by an injected weather fetcher, so nothing touches the network.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone

import pytest
import requests

import src.dashboard_service as dashboard_module
from src.audit_log import AuditLog
from src.dashboard_service import DashboardService, sanitize
from src.disaster_state_manager import _record_digest, new_state_record
from src.river_agent_workflow import run_risk_scenario, run_river_workflow
from src.river_api import create_server
from src.river_monitoring_agent import PROJECT_DIR
from tests.nwdp_fixtures import FakeSession, ok_session
from tests.test_risk_assessment import weather_result
from tests.test_river_service import NOW, clean_live_session, make_service

LIVE_WEATHER = lambda: weather_result(0.0, 0.0, retrieved_at=NOW, simulated=False)


def dashboard(tmp_path, session=None, weather=LIVE_WEATHER, state_file=None, **kw):
    service = make_service(tmp_path, session)
    return DashboardService(service, state_file=state_file or tmp_path / "no_record.json",
                            audit_path=tmp_path / "dash_audit.jsonl", weather_fetcher=weather,
                            **kw)


@pytest.fixture
def api(tmp_path):
    session = clean_live_session()
    dash = dashboard(tmp_path, session)
    server = create_server(dash.service, "127.0.0.1", 0, dashboard=dash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}", session
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=30) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


# ---- routes and validation -------------------------------------------------------------

def test_root_lists_endpoints(api):
    base, _ = api
    status, body = get(base + "/")
    assert status == 200 and "GET /dashboard?mode=real" in body["endpoints"]


def test_scenarios_route(api):
    base, _ = api
    status, body = get(base + "/dashboard/scenarios")
    assert status == 200 and body["default_mode"] == "real"
    assert "simulated-high-risk" in [s["name"] for s in body["scenarios"]]
    assert body["data_label"] == "SIMULATED DATA"


@pytest.mark.parametrize("query", [
    "?mode=demo", "?mode=simulated&scenario=../../etc/passwd", "?mode=simulated&state=PANIC",
    "?mode=real&scenario=simulated-high-risk", "?mode=real&path=/tmp/x",
    "/sequence?name=everything", "/scenarios?x=1"])
def test_invalid_queries_are_rejected(api, query):
    base, _ = api
    status, body = get(base + "/dashboard" + query)
    assert status == 400 and "error" in body


def test_unknown_dashboard_route_is_404(api):
    base, _ = api
    assert get(base + "/dashboard/admin")[0] == 404


def test_dashboard_has_no_write_route(api):
    base, _ = api
    for path in ("/dashboard", "/dashboard?mode=real", "/dashboard/sequence"):
        request = urllib.request.Request(base + path, data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as info:
            urllib.request.urlopen(request, timeout=10)
        assert info.value.code == 405


# ---- real mode -----------------------------------------------------------------------------

def test_real_mode_verified_fresh(api):
    base, _ = api
    status, doc = get(base + "/dashboard?mode=real")
    assert status == 200 and doc["data_label"] == "REAL DATA" and doc["is_simulated"] is False
    river = doc["river"]
    assert river["source_label"] == "LIVE_VERIFIED" and river["level"]["role"] == "CURRENT_VERIFIED"
    assert river["level"]["value_m"] == 74.95
    assert river["series"]["status"] == "AVAILABLE" and len(river["series"]["points"]) == 6
    assert doc["weather"]["is_simulated"] is False
    assert doc["state"]["state_basis"] == "NO_RECORD_NORMAL_ASSUMED"
    assert doc["state"]["record_saved_by_dashboard"] is False
    assert doc["safety"]["executes_actions"] is False


def test_real_mode_stale_evidence_is_reported_not_hidden(tmp_path):
    doc, _ = dashboard(tmp_path, ok_session()).get("real")  # real NWDP data: months old
    river = doc["river"]
    assert river["freshness"]["status"] == "STALE"
    assert river["level"]["role"] == "LAST_KNOWN_NOT_CURRENT"
    assert doc["risk"]["risk_level"] == "INDETERMINATE"
    assert doc["state"]["evaluation"]["outcome"] == "INSUFFICIENT_EVIDENCE"
    assert doc["safety"]["real_world_authorization"] == "RESTRICTED"


def test_real_mode_outage_shows_unavailable_without_fallback(tmp_path):
    session = FakeSession({"resource_show": [requests.exceptions.ConnectionError("down")]})
    doc, _ = dashboard(tmp_path, session).get("real")
    river = doc["river"]
    assert river["source_label"] == "LIVE_UNAVAILABLE"  # live mode: never historical fallback
    assert river["level"] == {"value_m": None, "role": "UNAVAILABLE",
                              "observed_at": None, "datum": None}
    assert river["series"]["status"] == "UNAVAILABLE"
    assert doc["is_simulated"] is False and doc["risk"]["risk_level"] == "INDETERMINATE"


def test_weather_failure_is_shown(tmp_path):
    def failing():
        raise RuntimeError("Open-Meteo down: GET https://api.open-meteo.com/x?apikey=SEKRIT")
    doc, _ = dashboard(tmp_path, clean_live_session(), weather=failing).get("real")
    assert doc["weather"]["status"] == "NOT_PROVIDED"
    assert "Open-Meteo down" in doc["weather"]["fetch_error"]
    assert "SEKRIT" not in json.dumps(doc)


def test_real_matches_the_workflow_exactly(tmp_path):
    """The adapter reshapes; it never recalculates risk, state or decisions."""
    doc, _ = dashboard(tmp_path, clean_live_session()).get("real")
    direct = run_river_workflow(make_service(tmp_path, clean_live_session()),
                                AuditLog(tmp_path / "direct.jsonl"), mode="live",
                                disaster_state="NORMAL", weather=LIVE_WEATHER())
    assert doc["risk"]["assessment_id"] == direct["risk_assessment"]["assessment_id"]
    assert doc["decision_id"] == direct["decision_id"]
    assert [d["reason"] for d in doc["safety"]["decisions"]] == \
        [d["reason"] for d in direct["capability_decisions"]]


# ---- persisted state is read, never written -------------------------------------------------------

def _write_record(path, record):
    path.write_text(json.dumps(record), encoding="utf-8")
    return path.read_bytes()


def test_persisted_real_record_is_read_only(tmp_path):
    path = tmp_path / "real_state.json"
    before = _write_record(path, new_state_record("ALERT", "real"))
    dash = dashboard(tmp_path, clean_live_session(), state_file=path)
    doc, _ = dash.get("real")
    assert doc["state"]["persisted"]["state"] == "ALERT"
    assert doc["state"]["state_used_for_capability_checks"] == "ALERT"
    assert doc["state"]["state_basis"] == "PERSISTED_RECORD"
    dash.get("simulated", "simulated-high-risk", "ALERT")
    assert path.read_bytes() == before  # neither mode wrote it


@pytest.mark.parametrize("record,problem", [
    ({**new_state_record("ALERT", "real"), "state": "EMERGENCY"}, "record_id"),
    (new_state_record("EMERGENCY", "simulated"), "scope"),
])
def test_invalid_or_wrong_scope_record_falls_back_to_normal_visibly(tmp_path, record, problem):
    path = tmp_path / "real_state.json"
    _write_record(path, record)
    doc, _ = dashboard(tmp_path, clean_live_session(), state_file=path).get("real")
    assert problem in doc["state"]["persisted"]["problem"]
    assert doc["state"]["persisted"]["available"] is False
    assert doc["state"]["state_basis"] == "INVALID_RECORD_NORMAL_ASSUMED"
    assert doc["state"]["state_used_for_capability_checks"] == "NORMAL"


# ---- simulated mode -----------------------------------------------------------------------------

def test_simulated_mode_is_labelled_and_matches_the_scenario(tmp_path):
    dash = dashboard(tmp_path)
    doc, _ = dash.get("simulated", "simulated-high-risk", "ALERT")
    assert doc["data_label"] == "SIMULATED DATA" and doc["is_simulated"] is True
    assert doc["river"]["is_simulated"] and doc["river"]["level"]["role"] == "SIMULATED"
    assert doc["weather"]["is_simulated"] is True
    assert doc["state"]["evaluation"]["state_applies_to"] == "SIMULATION_ONLY"
    direct = run_risk_scenario("simulated-high-risk", make_service(tmp_path),
                               AuditLog(tmp_path / "d.jsonl"), disaster_state="ALERT")
    assert doc["risk"]["assessment_id"] == direct["risk_assessment"]["assessment_id"]
    assert doc["safety"]["real_world_authorization"] == "RESTRICTED"
    for d in doc["safety"]["decisions"]:
        if d["capability_class"] == "REAL_WORLD":
            assert d["allowed"] is False and "EVIDENCE_SIMULATED" in d["evidence_reasons"]


def test_weather_hours_are_split_into_estimates_and_forecasts(tmp_path):
    doc, _ = dashboard(tmp_path).get("simulated", "rising-river-increasing-rain")
    kinds = [p["kind"] for p in doc["weather"]["hourly"]]
    assert kinds[0] == "MODEL_ESTIMATE_PAST" and kinds[-1] == "FORECAST"
    assert kinds == sorted(kinds, key=lambda k: k == "FORECAST")  # estimates, then forecasts
    assert all(p["precipitation_mm"] is not None for p in doc["weather"]["hourly"])


def test_missing_weather_scenario_has_no_invented_values(tmp_path):
    doc, _ = dashboard(tmp_path).get("simulated", "missing-weather")
    assert doc["weather"]["status"] == "NOT_PROVIDED" and doc["weather"]["hourly"] == []


def test_sequence_route(api):
    base, _ = api
    status, doc = get(base + "/dashboard/sequence?name=flood-cycle")
    assert status == 200 and doc["data_label"] == "SIMULATED DATA" and len(doc["steps"]) == 45
    changes = [(s["state_before"], s["state_after"]) for s in doc["steps"]
               if s["state_before"] != s["state_after"]]
    assert changes == [("NORMAL", "ALERT"), ("ALERT", "EMERGENCY"),
                       ("EMERGENCY", "RECOVERY"), ("RECOVERY", "NORMAL")]


# ---- safety of the output ---------------------------------------------------------------------------

def test_no_absolute_paths_in_output(tmp_path):
    dash = dashboard(tmp_path, clean_live_session())
    for doc in (dash.get("real")[0], dash.get("simulated", "conflicting-evidence")[0]):
        text = json.dumps(doc)
        assert PROJECT_DIR.as_posix() not in text and str(PROJECT_DIR) not in text
        assert str(tmp_path) not in text and tmp_path.as_posix() not in text


def test_sanitize_keeps_urls_routes_and_units():
    base = PROJECT_DIR
    assert sanitize("https://api.open-meteo.com/v1/forecast?x=1", base) == \
        "https://api.open-meteo.com/v1/forecast?x=1"
    assert sanitize("rising 0.1 m/h", base) == "rising 0.1 m/h"
    assert sanitize(f"{base.as_posix()}/data/x.csv", base) == "data/x.csv"
    assert sanitize("C:\\Other\\place\\y.csv", base) == "y.csv"
    assert sanitize({"api_key": "k"}, base) == {"api_key": "[REDACTED]"}


def test_audit_events_are_included_and_redacted(tmp_path):
    doc, _ = dashboard(tmp_path, clean_live_session()).get("real")
    events = [e["event"] for e in doc["audit"]["events"]]
    assert events[0] == "river_workflow_started" and events[-1] == "river_workflow_completed"
    assert "risk_assessment_completed" in events
    assert any(e.startswith("disaster_state_") for e in events)
    assert all(e["written"] is True for e in doc["audit"]["events"])


def test_engine_failure_is_a_structured_500(tmp_path, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("workflow exploded")
    monkeypatch.setattr(dashboard_module, "run_river_workflow", boom)
    dash = dashboard(tmp_path, clean_live_session())
    server = create_server(dash.service, "127.0.0.1", 0, dashboard=dash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, body = get(f"http://127.0.0.1:{server.server_address[1]}/dashboard?mode=real")
        assert status == 500 and body["error"].startswith("Dashboard could not be assembled")
        assert "river" not in body  # nothing invented
    finally:
        server.shutdown()
        server.server_close()


# ---- caching: no duplicate fetches ----------------------------------------------------------------

def test_results_are_cached(tmp_path):
    session = clean_live_session()
    calls = []
    dash = dashboard(tmp_path, session, weather=lambda: calls.append(1) or LIVE_WEATHER())
    dash.get("real")
    fetched = len(session.calls)
    doc, age = dash.get("real")
    assert len(session.calls) == fetched and calls == [1]  # served from cache


def test_concurrent_requests_compute_once(tmp_path):
    calls = []
    dash = dashboard(tmp_path, clean_live_session(),
                     weather=lambda: calls.append(1) or LIVE_WEATHER())
    threads = [threading.Thread(target=dash.get, args=("real",)) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert calls == [1]


def test_cache_expires(tmp_path):
    now = [0.0]
    calls = []
    dash = dashboard(tmp_path, clean_live_session(), cache_seconds=300,
                     monotonic=lambda: now[0],
                     weather=lambda: calls.append(1) or LIVE_WEATHER())
    dash.get("real")
    now[0] = 301
    dash.service.session = clean_live_session()
    dash.get("real")
    assert calls == [1, 1]


# ---- the workflow flag changes nothing -------------------------------------------------------------

def test_include_evidence_changes_no_decision(tmp_path):
    a = run_river_workflow(make_service(tmp_path, clean_live_session()),
                           AuditLog(tmp_path / "a.jsonl"), mode="live", disaster_state="ALERT")
    b = run_river_workflow(make_service(tmp_path, clean_live_session()),
                           AuditLog(tmp_path / "b.jsonl"), mode="live", disaster_state="ALERT",
                           include_evidence=True)
    assert a["decision_id"] == b["decision_id"]
    assert "evidence" not in a and b["evidence"]["evidence_id"] == b["service_evidence_id"]
