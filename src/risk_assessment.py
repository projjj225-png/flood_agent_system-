"""Deterministic flood-risk assessment from combined river + weather evidence.

Risk levels: UNKNOWN < LOW < MODERATE < HIGH < SEVERE.

Rules (applied in order; every applied rule is listed in ``factors``):
1. Evidence gate: if the evidence may not be used (combined assessment not
   eligible for real data, or simulation not eligible for simulated data),
   the risk is UNKNOWN. Missing or unusable evidence is NEVER LOW.
2. No configured thresholds for the station -> UNKNOWN (none are invented).
3. Base level from the river threshold status:
   BELOW_WARNING -> LOW, AT_OR_ABOVE_WARNING -> HIGH, AT_OR_ABOVE_DANGER -> SEVERE.
4. Trend projection (linear, deterministic): while RISING, if the current
   rate would reach the warning level within ``trend_lead_time_hours`` a LOW
   becomes MODERATE; if it would reach the danger level within that time a
   HIGH becomes SEVERE. Projections only escalate.
5. Rainfall: if usable 24 h modelled rainfall is at least the configured
   "heavy" amount (IMD intensity category, see config), LOW becomes MODERATE.
   Rain only escalates: local rain is a weak indicator at Upper Anicut,
   where upstream rain and dam releases dominate.

Thresholds not marked official make the result ``indicative`` (reported).
This module only classifies; it changes no state and authorizes nothing.
"""
from __future__ import annotations

from typing import Any

RISK_UNKNOWN = "UNKNOWN"
RISK_LOW = "LOW"
RISK_MODERATE = "MODERATE"
RISK_HIGH = "HIGH"
RISK_SEVERE = "SEVERE"
RISK_ORDER = (RISK_UNKNOWN, RISK_LOW, RISK_MODERATE, RISK_HIGH, RISK_SEVERE)

DEFAULT_RISK_SETTINGS = {
    "trend_lead_time_hours": 6.0,
    # IMD rainfall intensity categories for 24 h accumulation (mm).
    "heavy_rain_24h_mm": 64.5,
    "rain_category_reference": "India Meteorological Department rainfall intensity "
                               "categories (24 h): heavy rain >= 64.5 mm",
}

BASE_FROM_THRESHOLD = {"BELOW_WARNING": RISK_LOW, "AT_OR_ABOVE_WARNING": RISK_HIGH,
                       "AT_OR_ABOVE_DANGER": RISK_SEVERE}


def _hours_to(target: float | None, level: float | None, rate: float | None) -> float | None:
    if None in (target, level, rate) or rate <= 0 or level >= target:
        return None
    return round((target - level) / rate, 2)


def assess_flood_risk(river: dict[str, Any], weather: dict[str, Any] | None,
                      evidence_usable: bool, evidence_basis: str,
                      unusable_reasons: list[str] | None = None,
                      settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Classify risk. ``river`` is the combined workflow's river summary."""
    settings = {**DEFAULT_RISK_SETTINGS, **(settings or {})}
    evaluation = river.get("threshold_evaluation") or {}
    result: dict[str, Any] = {
        "risk_level": RISK_UNKNOWN, "base_level": None, "factors": [],
        "evidence_basis": evidence_basis, "evidence_usable": bool(evidence_usable),
        "thresholds": {k: evaluation.get(k) for k in (
            "warning_level_m", "danger_level_m", "threshold_reference",
            "threshold_is_official", "threshold_datum")},
        "indicative": not evaluation.get("threshold_is_official", False),
        "projection": None, "reasons_unknown": [],
        "settings": {k: settings[k] for k in ("trend_lead_time_hours", "heavy_rain_24h_mm")},
    }
    if not evidence_usable:
        result["reasons_unknown"] = list(unusable_reasons or ["EVIDENCE_NOT_USABLE"])
        return result
    status = river.get("threshold_status")
    if status not in BASE_FROM_THRESHOLD:
        result["reasons_unknown"] = ["THRESHOLDS_NOT_AVAILABLE"]
        return result

    level = river.get("last_known_level_m")
    level = river.get("river_level_m") if level is None else level
    base = BASE_FROM_THRESHOLD[status]
    risk = base
    result["base_level"] = base
    result["factors"].append({"factor": "threshold_status", "value": status, "effect": base})

    rate = river.get("rate_of_change_m_per_hour")
    rising = river.get("river_trend_direction") == "RISING"
    lead = settings["trend_lead_time_hours"]
    warning, danger = evaluation.get("warning_level_m"), evaluation.get("danger_level_m")
    to_warning = _hours_to(warning, level, rate) if rising else None
    to_danger = _hours_to(danger, level, rate) if rising else None
    result["projection"] = {"method": "linear extrapolation of the current rate",
                            "rate_m_per_hour": rate, "lead_time_hours": lead,
                            "hours_to_warning": to_warning, "hours_to_danger": to_danger}
    if risk == RISK_LOW and to_warning is not None and to_warning <= lead:
        risk = RISK_MODERATE
        result["factors"].append({"factor": "projected_warning",
                                  "value": f"{to_warning} h", "effect": RISK_MODERATE})
    if risk == RISK_HIGH and to_danger is not None and to_danger <= lead:
        risk = RISK_SEVERE
        result["factors"].append({"factor": "projected_danger",
                                  "value": f"{to_danger} h", "effect": RISK_SEVERE})

    rain = ((weather or {}).get("rainfall_mm") or {}).get("24h") or {}
    weather_usable = bool(weather) and not (weather.get("reasons") or []) \
        and rain.get("status") == "AVAILABLE"
    if weather_usable and rain.get("total_mm") is not None \
            and rain["total_mm"] >= settings["heavy_rain_24h_mm"] and risk == RISK_LOW:
        risk = RISK_MODERATE
        result["factors"].append({"factor": "heavy_rain_24h", "value": f"{rain['total_mm']} mm",
                                  "effect": RISK_MODERATE,
                                  "reference": settings["rain_category_reference"]})
    elif not weather_usable:
        result["factors"].append({"factor": "rainfall", "value": "not usable",
                                  "effect": "none (rain can only escalate)"})
    result["risk_level"] = risk
    return result
