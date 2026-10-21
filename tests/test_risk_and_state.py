"""Unit tests for the risk engine and the disaster-state manager."""

from datetime import datetime, timezone

import pytest

from src.disaster_state_manager import StateStore, capability_changes, next_state
from src.risk_assessment import assess_flood_risk

EVAL = {"warning_level_m": 3.0, "danger_level_m": 4.0, "threshold_reference": "TEST",
        "threshold_is_official": False, "threshold_datum": None}


def river(level, status, direction="NO_CHANGE", rate=0.0):
    return {"last_known_level_m": level, "threshold_status": status,
            "river_trend_direction": direction, "rate_of_change_m_per_hour": rate,
            "threshold_evaluation": EVAL}


def risk(r, weather=None, usable=True, **kw):
    return assess_flood_risk(r, weather, usable, "TEST", **kw)["risk_level"]


# ---- risk ---------------------------------------------------------------------------

def test_unusable_evidence_is_never_low():
    result = assess_flood_risk(river(2.5, "BELOW_WARNING"), None, False, "HISTORICAL",
                               ["EVIDENCE_STALE"])
    assert result["risk_level"] == "UNKNOWN"
    assert result["reasons_unknown"] == ["EVIDENCE_STALE"]


def test_no_thresholds_is_unknown():
    assert risk(river(2.5, "UNKNOWN")) == "UNKNOWN"


@pytest.mark.parametrize("level,status,expected", [
    (2.5, "BELOW_WARNING", "LOW"), (3.2, "AT_OR_ABOVE_WARNING", "HIGH"),
    (4.2, "AT_OR_ABOVE_DANGER", "SEVERE")])
def test_base_levels(level, status, expected):
    assert risk(river(level, status)) == expected


def test_projection_escalates_only_when_rising_fast_enough():
    assert risk(river(2.7, "BELOW_WARNING", "RISING", 0.1)) == "MODERATE"   # 3 h to warning
    assert risk(river(2.7, "BELOW_WARNING", "RISING", 0.01)) == "LOW"       # 30 h
    assert risk(river(2.7, "BELOW_WARNING", "FALLING", -0.3)) == "LOW"
    assert risk(river(3.5, "AT_OR_ABOVE_WARNING", "RISING", 0.1)) == "SEVERE"  # 5 h to danger
    assert risk(river(3.5, "AT_OR_ABOVE_WARNING", "RISING", 0.05)) == "HIGH"   # 10 h


def test_heavy_rain_escalates_low_only():
    heavy = {"reasons": [], "rainfall_mm": {"24h": {"status": "AVAILABLE", "total_mm": 70.0}}}
    light = {"reasons": [], "rainfall_mm": {"24h": {"status": "AVAILABLE", "total_mm": 10.0}}}
    replay = {**heavy, "reasons": ["WEATHER_REPLAYED"]}
    assert risk(river(2.5, "BELOW_WARNING"), heavy) == "MODERATE"
    assert risk(river(2.5, "BELOW_WARNING"), light) == "LOW"
    assert risk(river(2.5, "BELOW_WARNING"), replay) == "LOW"   # unusable weather ignored
    assert risk(river(3.2, "AT_OR_ABOVE_WARNING"), heavy) == "HIGH"


def test_missing_rain_is_not_treated_as_zero_escalation_signal():
    missing = {"reasons": [], "rainfall_mm": {"24h": {"status": "UNAVAILABLE",
                                                      "total_mm": None}}}
    result = assess_flood_risk(river(2.5, "BELOW_WARNING"), missing, True, "TEST")
    assert any(f["factor"] == "rainfall" and f["value"] == "not usable"
               for f in result["factors"])


def test_unofficial_thresholds_are_indicative():
    result = assess_flood_risk(river(2.5, "BELOW_WARNING"), None, True, "TEST")
    assert result["indicative"] is True


# ---- state transitions -----------------------------------------------------------------

def run_states(start, risks, settings=None):
    state, pending, out = start, 0, []
    for r in risks:
        t = next_state(state, r, pending, settings)
        state, pending = t["to"], t["pending"]
        out.append(state)
    return out


def test_escalation_is_immediate():
    assert run_states("NORMAL", ["MODERATE"]) == ["ALERT"]
    assert run_states("NORMAL", ["SEVERE"]) == ["EMERGENCY"]
    assert run_states("ALERT", ["SEVERE"]) == ["EMERGENCY"]


def test_emergency_goes_through_recovery():
    states = run_states("EMERGENCY", ["LOW"] * 10)
    assert "NORMAL" in states and states.index("RECOVERY") < states.index("NORMAL")
    assert states[:3] == ["EMERGENCY", "EMERGENCY", "RECOVERY"]


def test_recovery_needs_sustained_low_and_resets_on_high():
    states = run_states("RECOVERY", ["LOW"] * 5 + ["HIGH"] + ["LOW"] * 5)
    assert "NORMAL" not in states  # the HIGH reset the counter
    assert run_states("RECOVERY", ["LOW"] * 6)[-1] == "NORMAL"
    assert run_states("RECOVERY", ["SEVERE"]) == ["EMERGENCY"]


def test_alert_clears_after_persistence():
    assert run_states("ALERT", ["LOW"] * 3) == ["ALERT", "ALERT", "NORMAL"]


def test_unknown_risk_holds_and_resets():
    assert run_states("EMERGENCY", ["UNKNOWN"] * 10) == ["EMERGENCY"] * 10
    # LOW, LOW, UNKNOWN, LOW, LOW: the UNKNOWN resets progress towards RECOVERY
    assert run_states("EMERGENCY", ["LOW", "LOW", "UNKNOWN", "LOW", "LOW"])[-1] == "EMERGENCY"


def test_recovery_revokes_emergency_capabilities():
    changes = capability_changes("EMERGENCY", "RECOVERY")
    assert changes["revoked"] == ["propose_evacuation_plan",
                                  "request_human_emergency_authorization"]
    assert changes["granted"] == ["prepare_recovery_assessment"]


def test_unknown_state_is_rejected():
    with pytest.raises(ValueError):
        next_state("PANIC", "LOW", 0)


# ---- persistence -----------------------------------------------------------------------

def test_store_persists_history(tmp_path):
    store = StateStore(tmp_path / "state.json")
    at = datetime(2026, 10, 21, tzinfo=timezone.utc)
    t = store.apply({"risk_level": "SEVERE"}, at, "ev1")
    assert (t["from"], t["to"]) == ("NORMAL", "EMERGENCY")
    data = store.load()
    assert data["state"] == "EMERGENCY" and data["history"][-1]["evidence_ref"] == "ev1"
    assert "request_human_emergency_authorization" in data["history"][-1]["granted"]


def test_real_and_simulated_state_files_cannot_be_mixed(tmp_path):
    path = tmp_path / "state.json"
    StateStore(path, simulated=True).apply({"risk_level": "SEVERE"},
                                           datetime(2026, 10, 21, tzinfo=timezone.utc))
    with pytest.raises(ValueError, match="simulated"):
        StateStore(path, simulated=False).load()


def test_corrupted_state_file_is_refused(tmp_path):
    path = tmp_path / "state.json"
    path.write_text('{"state": "PANIC"}', encoding="utf-8")
    with pytest.raises(ValueError):
        StateStore(path).load()
