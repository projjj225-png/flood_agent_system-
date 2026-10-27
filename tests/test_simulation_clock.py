"""Regression tests: simulated and historical replays are judged at their replay
time, not at the wall clock; live decisions still use the real clock. Offline.

The rest of the suite injects a clock equal to the simulated time, which hid the
bug: with a real clock every simulated run carried EVIDENCE_STALE_AT_DECISION, and
run_simulated_scenario without as_of graded all four scenarios STALE.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.audit_log import AuditLog
from src.disaster_state_manager import run_state_sequence
from src.river_agent_workflow import (
    decision_time,
    run_risk_scenario,
    run_river_workflow,
    run_simulated_scenario,
)
from tests.test_river_service import NOW, clean_live_session, make_service

SIM_AS_OF = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
WALL_CLOCK = datetime(2026, 10, 27, 13, 0, tzinfo=timezone.utc)  # 17 days later
STALE_AT_DECISION = "EVIDENCE_STALE_AT_DECISION"


def audit(tmp_path):
    return AuditLog(tmp_path / "wf.jsonl", component="river_workflow")


def decision(result, capability):
    return next(d for d in result["capability_decisions"] if d["capability"] == capability)


def all_reasons(result):
    return {r for d in result["capability_decisions"] for r in d["evidence_reasons"]}


# ---- the decision time itself ---------------------------------------------------------

def test_decision_time_is_replay_time_only_for_replay_modes(tmp_path):
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    assert decision_time(service, "simulated", SIM_AS_OF) == SIM_AS_OF
    assert decision_time(service, "historical", SIM_AS_OF) == SIM_AS_OF
    for mode in ("live", "auto"):  # a caller-chosen time never applies to current evidence
        assert decision_time(service, mode, SIM_AS_OF) == WALL_CLOCK
    assert decision_time(service, "simulated", None) == WALL_CLOCK


# ---- all three reverification scenarios x all four disaster states --------------------

EXPECTED = {
    # scenario: (outcome outside NORMAL, simulation permission outside NORMAL)
    "no-conflict": ("NOT_REQUIRED", "SIMULATION_ALLOWED"),
    "conflict-resolves": ("RESOLVED", "SIMULATION_ALLOWED"),
    "conflict-persists": ("CONFLICT_PERSISTS", "SIMULATION_RESTRICTED"),
    "reverification-fails": ("FAILED", "SIMULATION_RESTRICTED"),
}


@pytest.mark.parametrize("state", ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"])
@pytest.mark.parametrize("scenario", list(EXPECTED))
def test_scenarios_under_a_real_clock(tmp_path, scenario, state):
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    result = run_simulated_scenario(scenario, service, audit(tmp_path), as_of=SIM_AS_OF,
                                    disaster_state=state)
    outcome, permission = EXPECTED[scenario]
    if state == "NORMAL" and scenario != "no-conflict":
        # NORMAL does not permit reverification, so a conflict stays unresolved.
        outcome, permission = "NOT_AUTHORIZED", "SIMULATION_RESTRICTED"
    assert result["evidence_freshness"] == "FRESH"
    assert result["reverification"]["outcome"] == outcome
    assert result["simulation_permission"] == permission
    assert STALE_AT_DECISION not in all_reasons(result)
    # Fixing the clock grants nothing real: simulated evidence never authorizes.
    assert result["real_world_authorization"] == "RESTRICTED"
    for cap in ("prepare_advisory", "propose_evacuation_plan",
                "request_human_emergency_authorization"):
        d = decision(result, cap)
        assert d["allowed"] is False
        if d["reason"] == "EVIDENCE_INSUFFICIENT":
            assert "EVIDENCE_SIMULATED" in d["evidence_reasons"]
    assert result["state_evaluation"]["evaluated_at"] == SIM_AS_OF.isoformat()
    assert result["state_evaluation"]["outcome"] != "FAILED"


@pytest.mark.parametrize("scenario", list(EXPECTED))
def test_simulated_scenario_defaults_to_the_simulated_time(tmp_path, scenario):
    """Called without as_of (as code, not the CLI, does) under a real clock."""
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    result = run_simulated_scenario(scenario, service, audit(tmp_path),
                                    disaster_state="EMERGENCY")
    assert result["evidence_freshness"] == "FRESH"
    assert (result["reverification"]["outcome"], result["simulation_permission"]) == \
        EXPECTED[scenario]


def test_risk_scenario_under_a_real_clock(tmp_path):
    """The dashboard's simulated mode runs risk scenarios with the real clock."""
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    result = run_risk_scenario("simulated-high-risk", service, audit(tmp_path),
                               disaster_state="EMERGENCY")
    assert STALE_AT_DECISION not in all_reasons(result)
    assert result["risk_assessment"]["assessment_timestamp"] == SIM_AS_OF.isoformat()
    assert result["real_world_authorization"] == "RESTRICTED"


def test_stale_risk_scenario_is_still_stale(tmp_path):
    """The replay time does not make old simulated data fresh."""
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    result = run_risk_scenario("stale-river", service, audit(tmp_path))
    assert result["evidence_freshness"] == "STALE"
    assert result["risk_assessment"]["risk_level"] == "INDETERMINATE"


def test_state_sequence_does_not_depend_on_the_wall_clock(tmp_path):
    at_sim = run_state_sequence("flood-cycle", make_service(tmp_path, clock=lambda: SIM_AS_OF),
                                audit(tmp_path))
    at_wall = run_state_sequence("flood-cycle",
                                 make_service(tmp_path, clock=lambda: WALL_CLOCK),
                                 audit(tmp_path))
    assert at_wall == at_sim
    assert sum(s["state_before"] != s["state_after"] for s in at_wall) == 4


# ---- current evidence: freshness checks are unchanged ------------------------------

def test_live_decision_still_uses_the_real_clock(tmp_path, monkeypatch):
    """Evidence fresh when produced but 4 h old when decided is still denied."""
    now = [NOW]
    service = make_service(tmp_path, clean_live_session(), clock=lambda: now[0])
    produce = service.get_evidence

    def produce_then_age(*args, **kwargs):
        evidence = produce(*args, **kwargs)
        now[0] = NOW + timedelta(hours=4)
        return evidence

    monkeypatch.setattr(service, "get_evidence", produce_then_age)
    result = run_river_workflow(service, audit(tmp_path), mode="live",
                                disaster_state="EMERGENCY")
    assert result["evidence_freshness"] == "FRESH"
    d = decision(result, "request_human_emergency_authorization")
    assert d["allowed"] is False and STALE_AT_DECISION in d["evidence_reasons"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_live_mode_ignores_a_replay_time(tmp_path):
    """as_of cannot back-date a live decision: the service refuses it, nothing is usable."""
    service = make_service(tmp_path, clean_live_session(), clock=lambda: NOW)
    result = run_river_workflow(service, audit(tmp_path), mode="live", as_of=SIM_AS_OF,
                                disaster_state="EMERGENCY")
    assert result["evidence_error"] and "as_of" in result["evidence_error"]
    assert result["real_world_authorization"] == "RESTRICTED"


def test_historical_replay_never_authorizes(tmp_path):
    service = make_service(tmp_path, clock=lambda: WALL_CLOCK)
    result = run_river_workflow(service, audit(tmp_path), mode="historical",
                                as_of=datetime(2025, 12, 30, 6, 0, tzinfo=timezone.utc),
                                disaster_state="EMERGENCY")
    assert result["real_world_authorization"] == "RESTRICTED"
    assert "EVIDENCE_NOT_VERIFIED_LIVE" in all_reasons(result)
    # Stale at its own replay time, so still stale at decision time.
    assert result["evidence_freshness"] == "STALE"
    assert STALE_AT_DECISION in all_reasons(result)
