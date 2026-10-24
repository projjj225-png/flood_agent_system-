"""End-to-end tests for the flood orchestrator and the dashboard. Offline."""

import json
from datetime import datetime, timezone

import pytest

from src.audit_log import AuditLog, read_audit_log
from src.dashboard import render
from src.disaster_state_manager import StateStore
from src.flood_evidence_workflow import DEFAULT_SETTINGS
from src.flood_orchestrator import event_service, run_cycle, simulate_event
from src.river_monitoring_agent import PROJECT_DIR, validate_thresholds
from tests.test_flood_evidence_workflow import weather_agent
from tests.test_river_service import BASE_CONFIG, NOW, clean_live_session, make_service

EVENT_START = datetime(2026, 10, 11, 0, 0, tzinfo=timezone.utc)
# TEST thresholds around the synthetic live series (74.90 -> 74.95 m, +0.01 m/h).
LIVE_TEST_THRESHOLDS = {"UPPER_ANICUT": {"warning_level_m": 74.90, "danger_level_m": 75.0,
                                         "reference": "TEST VALUES", "datum": "reduced_level"}}


@pytest.fixture(scope="module")
def event(tmp_path_factory):
    tmp = tmp_path_factory.mktemp("event")
    outputs = {"snapshot_dir": str(tmp / "live"), "audit_log": str(tmp / "svc.jsonl"),
               "evidence": str(tmp / "ev.json")}
    service = event_service(BASE_CONFIG, PROJECT_DIR, outputs)
    store = StateStore(tmp / "sim_state.json", simulated=True)
    log = AuditLog(tmp / "orch.jsonl")
    timeline = simulate_event(service, log, store, EVENT_START, hours=84)
    return timeline, store, log


def test_event_runs_full_state_cycle(event):
    timeline, _, _ = event
    transitions = [(r["state"], r["transition"]) for r in timeline if r["transition"]]
    assert transitions == [("ALERT", "ESCALATION"), ("EMERGENCY", "ESCALATION"),
                           ("RECOVERY", "EMERGENCY_TO_RECOVERY"),
                           ("NORMAL", "RECOVERY_COMPLETE")]


def test_event_risk_follows_levels_and_projection(event):
    timeline, _, _ = event
    first_alert = next(r for r in timeline if r["state"] == "ALERT")
    assert first_alert["level_m"] < 3.0 and "projected_warning" in first_alert["risk_factors"]
    first_emergency = next(r for r in timeline if r["state"] == "EMERGENCY")
    assert first_emergency["level_m"] < 4.0 and "projected_danger" in \
        first_emergency["risk_factors"]
    peak = max(timeline, key=lambda r: r["level_m"] or 0)
    assert peak["level_m"] == 4.66 and peak["risk"] == "SEVERE"


def test_event_revokes_on_recovery(event):
    timeline, _, _ = event
    recovery = next(r for r in timeline if r["transition"] == "EMERGENCY_TO_RECOVERY")
    assert recovery["revoked"] == ["propose_evacuation_plan",
                                   "request_human_emergency_authorization"]
    assert "propose_evacuation_plan" not in recovery["state_capabilities"]


def test_simulated_event_never_authorizes_real_world(event):
    timeline, _, log = event
    assert {r["real_world_authorization"] for r in timeline} == {"RESTRICTED"}
    for r in timeline:
        assert not {"prepare_advisory", "propose_evacuation_plan",
                    "request_human_emergency_authorization"} & set(r["capabilities_allowed"])
    events = [e["event"] for e in read_audit_log(log.path)]
    assert events.count("capabilities_changed") == 4
    assert events.count("state_transition") == 84


def test_first_hour_is_unknown_not_low(event):
    timeline, _, _ = event
    assert timeline[0]["risk"] == "UNKNOWN" and timeline[0]["state"] == "NORMAL"


# ---- real / live cycles ----------------------------------------------------------------

def cycle(tmp_path, session, thresholds=None, state=None, **kwargs):
    service = make_service(tmp_path, session)
    if thresholds:
        service.thresholds = validate_thresholds(thresholds)
    store = StateStore(tmp_path / "state.json")
    if state:
        store.path.write_text(json.dumps({"state": state, "pending": 0, "history": [],
                                          "simulated": False}), encoding="utf-8")
    kwargs.setdefault("weather_agent", weather_agent())
    return run_cycle(service, AuditLog(tmp_path / "orch.jsonl"), store, river_mode="live",
                     settings=dict(DEFAULT_SETTINGS), clock=lambda: NOW, **kwargs), store


def test_live_verified_evidence_drives_state_and_human_approval(tmp_path):
    result, store = cycle(tmp_path, clean_live_session(), LIVE_TEST_THRESHOLDS)
    assert result["risk"]["risk_level"] == "SEVERE"          # above warning, danger in 5 h
    assert (result["state_before"], result["state_after"]) == ("NORMAL", "EMERGENCY")
    d = next(x for x in result["capability_decisions"]
             if x["capability"] == "request_human_emergency_authorization")
    assert d["allowed"] and d["reason"] == "PERMITTED_PENDING_HUMAN_APPROVAL"
    assert result["real_world_authorization"] == "HUMAN_AUTHORIZATION_REQUIRED"
    assert store.load()["state"] == "EMERGENCY"


def test_stale_real_data_holds_state(tmp_path):
    from tests.nwdp_fixtures import ok_session
    result, store = cycle(tmp_path, ok_session(), LIVE_TEST_THRESHOLDS, state="EMERGENCY")
    assert result["risk"]["risk_level"] == "UNKNOWN"
    assert result["transition"]["reason"] == "HOLD_EVIDENCE_UNKNOWN"
    assert result["state_after"] == "EMERGENCY"  # missing evidence never lowers a state
    assert result["real_world_authorization"] == "RESTRICTED"


def test_no_thresholds_means_unknown_risk(tmp_path):
    result, _ = cycle(tmp_path, clean_live_session())
    assert result["combined_assessment"]["eligible"] is True
    assert result["risk"]["reasons_unknown"] == ["THRESHOLDS_NOT_AVAILABLE"]
    assert result["state_after"] == "NORMAL"


def test_mode_and_state_file_must_match(tmp_path):
    service = make_service(tmp_path)
    with pytest.raises(ValueError):
        run_cycle(service, AuditLog(tmp_path / "a.jsonl"),
                  StateStore(tmp_path / "s.json", simulated=False), river_mode="simulated",
                  as_of=EVENT_START)


# ---- dashboard -----------------------------------------------------------------------------

def test_dashboard_renders_and_labels_simulation(event, tmp_path):
    timeline, _, _ = event
    page = render({"label": "SIMULATED test", "thresholds": {"warning_level_m": 3.0,
                                                             "danger_level_m": 4.0},
                   "timeline": timeline}, None)
    assert "SIMULATED / TEST content" in page and "<svg" in page
    assert "EMERGENCY_TO_RECOVERY" in page and "warning 3.0 m" in page


def test_dashboard_real_cycle_has_no_simulation_banner(tmp_path):
    from tests.nwdp_fixtures import ok_session
    result, _ = cycle(tmp_path, ok_session())
    page = render(None, result)
    assert "SIMULATED / TEST content" not in page
    assert "no thresholds configured" in page and "RESTRICTED" in page
