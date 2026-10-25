"""Risk Assessment Engine: deterministic, explainable flood-risk decision support.

Inputs are the outputs of the existing agents, used as they are:
- river: a RiverEvidenceService envelope (evidence grade, freshness, recent
  quality, threshold evaluation, trend, cross-source check);
- weather: a WeatherAgent result (rainfall_accumulation, hourly_weather);
- reverification: the River Agent workflow's reverification block.

Nothing is re-measured here. Freshness, quality, grades and conflicts come
from the River Monitoring Agent and the evidence gate
(capability_control.assess_evidence_for_authorization); the engine only
decides whether that evidence is good enough to describe a HAZARD.

Hazard and confidence are reported separately:
- risk_level: LOW / MODERATE / HIGH / CRITICAL, or INDETERMINATE when no
  defensible assessment can be made (missing, stale, poor or conflicting
  river evidence, or a LOW result without the evidence LOW requires).
  Absence of evidence is never read as low risk.
- hazard_indicated: the highest hazard any usable evidence points to, so
  heavy rain is not hidden behind INDETERMINATE.
- evidence_quality.confidence: how much the evidence supports the result.

All categories and breakpoints are configurable PROTOTYPE values
(config/risk_assessment.json). No official warning or danger level is
assumed and no calibrated flood probability is produced. The engine never
authorizes anything and never assigns a disaster state.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.capability_control import (
    EVIDENCE_HISTORICAL_FALLBACK,
    EVIDENCE_INTEGRITY_FAILED,
    EVIDENCE_MALFORMED,
    EVIDENCE_NOT_ASSESSED,
    EVIDENCE_QUALITY_NOT_ACCEPTABLE,
    EVIDENCE_STALE,
    EVIDENCE_UNAVAILABLE,
    EVIDENCE_UNRESOLVED_CONFLICT,
    assess_evidence_for_authorization,
)
from src.river_monitoring_agent import (
    GRADE_HISTORICAL,
    GRADE_SIMULATED,
    GRADE_VERIFIED_LIVE,
    PROJECT_DIR,
    THRESHOLD_AT_OR_ABOVE_DANGER,
    THRESHOLD_AT_OR_ABOVE_WARNING,
    THRESHOLD_BELOW_WARNING,
    TREND_FALLING,
    TREND_RISING,
    load_json_file,
    parse_timestamp,
)
from src.river_service import (
    LABEL_FALLBACK,
    LABEL_HISTORICAL,
    LABEL_LIVE_UNAVAILABLE,
    LABEL_LIVE_UNVERIFIED,
    LABEL_LIVE_VERIFIED,
    LABEL_SIMULATED,
)
from src.weather_agent import SECTION_AVAILABLE, STATUS_UNAVAILABLE, WeatherAgent

SCHEMA_VERSION = "1.0"
ENGINE_NAME = "RiskAssessmentEngine"
DEFAULT_RISK_CONFIG_PATH = PROJECT_DIR / "config" / "risk_assessment.json"

LOW, MODERATE, HIGH, CRITICAL = "LOW", "MODERATE", "HIGH", "CRITICAL"
INDETERMINATE = "INDETERMINATE"
HAZARD_LEVELS = (LOW, MODERATE, HIGH, CRITICAL)  # ordered, least to most severe
RISK_LEVELS = HAZARD_LEVELS + (INDETERMINATE,)

MODE_REAL, MODE_HISTORICAL, MODE_SIMULATED = "real", "historical", "simulated"
MODE_UNKNOWN = "unknown"

CONFIDENCE_HIGH, CONFIDENCE_MEDIUM, CONFIDENCE_LOW = "HIGH", "MEDIUM", "LOW"
CONFIDENCE_NONE = "NONE"
_CONFIDENCE_ORDER = (CONFIDENCE_LOW, CONFIDENCE_MEDIUM, CONFIDENCE_HIGH)

# Weather results produced from fabricated files carry this source_type.
WEATHER_SIMULATED_SOURCE_TYPE = "SIMULATED_TEST_DATA"

# Evidence-gate reasons that make river evidence unfit to describe a hazard.
# (EVIDENCE_SIMULATED / EVIDENCE_NOT_VERIFIED_LIVE only bar real-world
# authorization; simulated and historical replays are still assessable.)
RIVER_BLOCKING_REASONS = (EVIDENCE_NOT_ASSESSED, EVIDENCE_UNAVAILABLE, EVIDENCE_STALE,
                          EVIDENCE_QUALITY_NOT_ACCEPTABLE, EVIDENCE_HISTORICAL_FALLBACK,
                          EVIDENCE_UNRESOLVED_CONFLICT, EVIDENCE_MALFORMED,
                          EVIDENCE_INTEGRITY_FAILED)
# Grades whose provenance is good enough for a hazard assessment.
RIVER_ASSESSABLE_GRADES = (GRADE_VERIFIED_LIVE, GRADE_HISTORICAL, GRADE_SIMULATED)
EVIDENCE_SIMULATION_LABEL_MISMATCH = "EVIDENCE_SIMULATION_LABEL_MISMATCH"
LOW_REQUIREMENTS = ("river_thresholds", "river_trend", "observed_rainfall",
                    "forecast_rainfall")

DISCLAIMER = ("Decision support only. Categories and breakpoints are configurable prototype "
              "values, not official flood warning levels, and no calibrated flood probability "
              "is produced. This assessment does not assign a disaster state and does not "
              "authorize evacuation, emergency response or any other real-world action.")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _positive_number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) \
            or not math.isfinite(value) or value <= 0:
        raise ValueError(f"Risk config: {name} must be a positive number (got {value!r}).")
    return float(value)


def _bands(value: Any, name: str) -> dict[str, float]:
    if not isinstance(value, dict) or set(value) != {MODERATE, HIGH}:
        raise ValueError(f"Risk config: {name} needs exactly MODERATE and HIGH breakpoints.")
    bands = {level: _positive_number(value[level], f"{name}.{level}")
             for level in (MODERATE, HIGH)}
    if bands[MODERATE] >= bands[HIGH]:
        raise ValueError(f"Risk config: {name}.MODERATE must be below {name}.HIGH.")
    return bands


def validate_risk_config(config: Any) -> dict[str, Any]:
    """Check a risk config mapping. Raises ValueError for setup mistakes."""
    if not isinstance(config, dict):
        raise ValueError("Risk config must be a JSON object.")
    river = config.get("river")
    rain = config.get("rainfall")
    if not isinstance(river, dict) or not isinstance(rain, dict):
        raise ValueError("Risk config needs 'river' and 'rainfall' objects.")
    clean = {
        "categories": dict(config.get("categories") or {}),
        "river": {
            "rise_m_per_hour": _positive_number(river.get("rise_m_per_hour"),
                                                "river.rise_m_per_hour"),
            "rapid_rise_m_per_hour": _positive_number(river.get("rapid_rise_m_per_hour"),
                                                      "river.rapid_rise_m_per_hour"),
            "reference": river.get("reference"),
        },
        "rainfall": {
            "observed_24h_mm": _bands(rain.get("observed_24h_mm"), "rainfall.observed_24h_mm"),
            "forecast_mm": _bands(rain.get("forecast_mm"), "rainfall.forecast_mm"),
            "forecast_hours": rain.get("forecast_hours"),
            "intensifying_ratio": _positive_number(rain.get("intensifying_ratio", 1.5),
                                                   "rainfall.intensifying_ratio"),
            "intensifying_min_6h_mm": _positive_number(rain.get("intensifying_min_6h_mm", 10.0),
                                                       "rainfall.intensifying_min_6h_mm"),
            "reference": rain.get("reference"),
        },
        "river_response_lag_hours": _positive_number(config.get("river_response_lag_hours"),
                                                     "river_response_lag_hours"),
        "river_response_lag_reference": config.get("river_response_lag_reference"),
        "max_weather_time_offset_hours": _positive_number(
            config.get("max_weather_time_offset_hours"), "max_weather_time_offset_hours"),
        "low_requires": config.get("low_requires", list(LOW_REQUIREMENTS)),
    }
    if clean["river"]["rise_m_per_hour"] >= clean["river"]["rapid_rise_m_per_hour"]:
        raise ValueError("Risk config: river.rise_m_per_hour must be below "
                         "river.rapid_rise_m_per_hour.")
    hours = clean["rainfall"]["forecast_hours"]
    if isinstance(hours, bool) or not isinstance(hours, int) or not 1 <= hours <= 168:
        raise ValueError("Risk config: rainfall.forecast_hours must be an integer 1-168.")
    unknown = set(clean["low_requires"]) - set(LOW_REQUIREMENTS) \
        if isinstance(clean["low_requires"], list) else {"<not a list>"}
    if unknown:
        raise ValueError(f"Risk config: low_requires has unknown items {sorted(unknown)}; "
                         f"allowed: {', '.join(LOW_REQUIREMENTS)}.")
    return clean


def load_risk_config(path: str | Path = DEFAULT_RISK_CONFIG_PATH) -> dict[str, Any]:
    return validate_risk_config(load_json_file(path, "Risk config"))


# --------------------------------------------------------------------------
# Simulated weather (fabricated files, parsed by the real WeatherAgent)
# --------------------------------------------------------------------------

def load_simulated_weather(path: str | Path,
                           retrieved_at: datetime | None = None) -> dict[str, Any]:
    """Parse a fabricated Open-Meteo-shaped file with WeatherAgent.parse_response.

    The result keeps the Weather Agent schema but is relabelled as simulated so
    it can never pass for model output from the live API. ``retrieved_at``
    overrides the file's simulated retrieval time (for replay scenarios).
    """
    doc = load_json_file(path, "Simulated weather")
    if not isinstance(doc, dict) or doc.get("simulated") is not True:
        raise ValueError(f"{path}: simulated weather files must declare \"simulated\": true.")
    moment = retrieved_at or parse_timestamp(doc.get("retrieved_at"))
    if moment is None:
        raise ValueError(f"{path}: retrieved_at must be a timezone-aware ISO 8601 time.")
    result = WeatherAgent().parse_response(doc.get("payload"), moment)
    result["source"] = f"SIMULATED weather file (fabricated): {Path(path).as_posix()}"
    result["source_url"] = None
    result["source_type"] = WEATHER_SIMULATED_SOURCE_TYPE
    result["data_notes"] = ["FABRICATED test weather; not an observation or forecast of the "
                            "Cauvery basin."] + result["data_notes"]
    return result


# --------------------------------------------------------------------------
# Forecast rainfall (pure)
# --------------------------------------------------------------------------

def forecast_rainfall(hourly_weather: dict[str, Any] | None, from_time: datetime | None,
                      hours: int, variable: str = "precipitation") -> dict[str, Any]:
    """Total of the next ``hours`` hourly values after ``from_time``.

    Mirrors weather_agent.calculate_rainfall_accumulation, but looks forward:
    it sums the hours ending at floor(from_time)+1h ... floor(from_time)+N h
    (the first one is the hour in progress). Every hour must be present and
    non-null, otherwise the total is UNAVAILABLE; it is never partly summed.
    """
    out: dict[str, Any] = {"status": "UNAVAILABLE", "total_mm": None, "hours": hours,
                           "period_start": None, "period_end": None, "reason": None}
    if from_time is None or from_time.tzinfo is None:
        out["reason"] = "No timezone-aware forecast start time."
        return out
    if not isinstance(hourly_weather, dict) or hourly_weather.get("status") == "UNAVAILABLE":
        out["reason"] = "Hourly weather data is unavailable."
        return out
    try:
        times = [datetime.fromisoformat(t) for t in hourly_weather.get("time") or []]
    except (TypeError, ValueError):
        out["reason"] = "Hourly timestamps could not be parsed."
        return out
    if not times or any(t.tzinfo is None for t in times):
        out["reason"] = "Hourly series has no timezone-aware timestamps."
        return out
    values = (hourly_weather.get("variables") or {}).get(variable)
    if not isinstance(values, list) or len(values) != len(times):
        out["reason"] = f"Hourly '{variable}' data is unavailable."
        return out
    index = {t: i for i, t in enumerate(times)}
    start = from_time.astimezone(times[0].tzinfo).replace(minute=0, second=0, microsecond=0)
    needed = [start + timedelta(hours=k) for k in range(1, hours + 1)]
    out["period_start"], out["period_end"] = start.isoformat(), needed[-1].isoformat()
    missing = [t for t in needed if t not in index]
    if missing:
        out["reason"] = (f"Hourly series ends {times[-1].isoformat()}; {len(missing)} of the "
                         f"{hours} forecast hours are not covered.")
        return out
    window = [values[index[t]] for t in needed]
    if any(not isinstance(v, (int, float)) or isinstance(v, bool) for v in window):
        out["reason"] = f"Some of the {hours} forecast hours are null."
        return out
    out.update(status="AVAILABLE", total_mm=round(sum(window), 3))
    return out


# --------------------------------------------------------------------------
# Evidence views (read existing outputs; no re-measurement)
# --------------------------------------------------------------------------

def has_simulation_marker(evidence: Any) -> bool:
    """True if ANY field marks the evidence as simulated, whatever the label says."""
    if not isinstance(evidence, dict):
        return False
    quality = evidence.get("data_quality") if isinstance(evidence.get("data_quality"),
                                                         dict) else {}
    types_present = quality.get("source_types_present")
    return (evidence.get("source_label") == LABEL_SIMULATED
            or quality.get("evidence_grade") == GRADE_SIMULATED
            or quality.get("is_simulated") not in (False, None)
            or evidence.get("source_type") == "simulated"
            or isinstance(types_present, list) and "simulated" in types_present)


def assessment_mode_for(evidence: Any) -> str:
    """real / historical / simulated. Any simulation marker wins over the label, so
    simulated readings can never be reported as real or historical."""
    label = evidence.get("source_label") if isinstance(evidence, dict) else None
    if has_simulation_marker(evidence):
        return MODE_SIMULATED
    if label in (LABEL_LIVE_VERIFIED, LABEL_LIVE_UNVERIFIED, LABEL_LIVE_UNAVAILABLE):
        return MODE_REAL
    if label in (LABEL_HISTORICAL, LABEL_FALLBACK):
        return MODE_HISTORICAL
    if label == LABEL_SIMULATED:
        return MODE_SIMULATED
    return MODE_UNKNOWN


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _river_view(evidence: Any, reverification: dict[str, Any] | None) -> dict[str, Any]:
    ev = evidence if isinstance(evidence, dict) else {}
    # The gate validates and reports its own sections; these are the engine's extra reads.
    engine_problems = [f"{k}: expected object, got {type(ev[k]).__name__}" for k in (
        "trend", "threshold_evaluation", "decision_support")
        if ev.get(k) is not None and not isinstance(ev.get(k), dict)]
    gate = assess_evidence_for_authorization(ev or None, reverification)
    input_problems = list(gate.get("input_problems", [])) + engine_problems
    if input_problems:
        ev = {}  # nothing from malformed evidence is used
    quality = ev.get("data_quality") if isinstance(ev.get("data_quality"), dict) else {}
    grade = quality.get("evidence_grade")
    blocking = [r for r in gate["reasons"] if r in RIVER_BLOCKING_REASONS]
    if input_problems and EVIDENCE_MALFORMED not in blocking:
        blocking.append(EVIDENCE_MALFORMED)
    if gate["reasons"] != [EVIDENCE_NOT_ASSESSED] and grade not in RIVER_ASSESSABLE_GRADES:
        blocking.append(f"EVIDENCE_GRADE_{grade or 'MISSING'}")
    # Simulation markers must agree with the label and grade, in both directions.
    if ev and has_simulation_marker(ev) and (ev.get("source_label") != LABEL_SIMULATED
                                             or grade != GRADE_SIMULATED):
        blocking.append(EVIDENCE_SIMULATION_LABEL_MISMATCH)
    trend = ev.get("trend") if isinstance(ev.get("trend"), dict) else {}
    thresholds = (ev.get("threshold_evaluation")
                  if isinstance(ev.get("threshold_evaluation"), dict) else {})
    cross = ev.get("cross_source_check") if isinstance(ev.get("cross_source_check"), dict) else {}
    ds = ev.get("decision_support") if isinstance(ev.get("decision_support"), dict) else {}
    return {
        "provided": bool(ev) or bool(input_problems),
        "usable_for_hazard": not blocking,
        "blocking_reasons": blocking,
        "input_problems": input_problems,
        "gate": gate,
        "grade": grade,
        "status": ev.get("status"),
        "freshness": (ev.get("freshness") or {}).get("status")
        if isinstance(ev.get("freshness"), dict) else None,
        "age_hours": (ev.get("freshness") or {}).get("age_hours")
        if isinstance(ev.get("freshness"), dict) else None,
        "reference_time": parse_timestamp((ev.get("freshness") or {}).get("reference_time"))
        if isinstance(ev.get("freshness"), dict) else None,
        "recent_quality": (quality.get("recent_quality") or {}).get("status")
        if isinstance(quality.get("recent_quality"), dict) else None,
        "trend_status": trend.get("status"),
        "direction": trend.get("direction"),
        "rate": _number(trend.get("rate_of_change_m_per_hour")),
        "baseline_level_m": _number(trend.get("baseline_level_m")),
        "trend_reason": trend.get("reason"),
        "threshold_status": thresholds.get("status"),
        "threshold_is_official": bool(thresholds.get("threshold_is_official")),
        "threshold_reference": thresholds.get("threshold_reference"),
        "warning_level_m": _number(thresholds.get("warning_level_m")),
        "danger_level_m": _number(thresholds.get("danger_level_m")),
        "threshold_reason": thresholds.get("reason"),
        "level_m": _number(ds.get("last_known_level_m")),
        "observed_at": ds.get("last_known_observed_at"),
        "conflicts_current": cross.get("conflicts_affecting_current_evidence", 0) or 0,
        "conflicts_total": cross.get("conflicts", 0) or 0,
        "cross_status": cross.get("status"),
        "reverification_outcome": (reverification or {}).get("outcome"),
    }


def _weather_view(weather: Any, reference_time: datetime, mode: str,
                  cfg: dict[str, Any]) -> dict[str, Any]:
    view: dict[str, Any] = {"provided": weather is not None, "usable": False, "reasons": [],
                            "is_simulated": False, "retrieved_at": None, "observed_24h": None,
                            "observed_6h": None, "forecast": None}
    if weather is None:
        view["reasons"].append("WEATHER_NOT_PROVIDED")
        return view
    if not isinstance(weather, dict) or not isinstance(weather.get("rainfall_accumulation"), dict):
        view["reasons"].append("WEATHER_INVALID")
        return view
    view["is_simulated"] = weather.get("source_type") == WEATHER_SIMULATED_SOURCE_TYPE
    view["source"] = weather.get("source")
    view["source_type"] = weather.get("source_type")
    view["status"] = weather.get("status")
    if weather.get("status") == STATUS_UNAVAILABLE:
        view["reasons"].append("WEATHER_UNAVAILABLE")
    # Simulated and real evidence are never combined.
    if mode != MODE_UNKNOWN and view["is_simulated"] != (mode == MODE_SIMULATED):
        view["reasons"].append("WEATHER_MODE_MISMATCH")
    retrieved = parse_timestamp(weather["rainfall_accumulation"].get("reference_time")
                                or weather.get("retrieved_at"))
    view["retrieved_at"] = retrieved.isoformat() if retrieved else None
    limit = cfg["max_weather_time_offset_hours"]
    if retrieved is None:
        view["reasons"].append("WEATHER_TIME_UNKNOWN")
    elif abs((retrieved - reference_time).total_seconds()) > limit * 3600:
        view["reasons"].append("WEATHER_NOT_TIME_ALIGNED")
        view["time_offset_hours"] = round((retrieved - reference_time).total_seconds() / 3600, 2)
    if view["reasons"]:
        return view
    windows = weather["rainfall_accumulation"].get("windows") or {}
    for key in ("24h", "6h"):
        entry = ((windows.get(key) or {}).get("precipitation") or {})
        view[f"observed_{key}"] = {
            "status": entry.get("status"), "total_mm": _number(entry.get("total")),
            "period_start": (windows.get(key) or {}).get("period_start"),
            "period_end": (windows.get(key) or {}).get("period_end"),
            "reason": entry.get("reason")}
        if entry.get("status") != SECTION_AVAILABLE or view[f"observed_{key}"]["total_mm"] is None:
            view[f"observed_{key}"]["status"] = "UNAVAILABLE"
    view["forecast"] = forecast_rainfall(weather.get("hourly_weather"), retrieved,
                                         cfg["rainfall"]["forecast_hours"])
    view["usable"] = (view["observed_24h"]["status"] == SECTION_AVAILABLE
                      or view["forecast"]["status"] == SECTION_AVAILABLE)
    if not view["usable"]:
        view["reasons"].append("WEATHER_RAINFALL_UNAVAILABLE")
    return view


def _band_level(total: float, bands: dict[str, float]) -> str:
    return HIGH if total >= bands[HIGH] else MODERATE if total >= bands[MODERATE] else LOW


def _max_level(levels: list[str]) -> str | None:
    levels = [lvl for lvl in levels if lvl in HAZARD_LEVELS]
    return max(levels, key=HAZARD_LEVELS.index) if levels else None


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

def assess_flood_risk(
    river_evidence: dict[str, Any] | None,
    weather: dict[str, Any] | None = None,
    reverification: dict[str, Any] | None = None,
    config: dict[str, Any] | None = None,
    clock: Callable[[], datetime] | None = None,
) -> dict[str, Any]:
    """Assess flood risk from existing agent outputs. Never raises for bad evidence."""
    cfg = config if config is not None else load_risk_config()
    clock = clock or (lambda: datetime.now(timezone.utc))
    now = clock().astimezone(timezone.utc)
    mode = assessment_mode_for(river_evidence)
    river = _river_view(river_evidence, reverification)
    reference = river["reference_time"] or now
    wx = _weather_view(weather, reference, mode, cfg)
    river_cfg, rain_cfg = cfg["river"], cfg["rainfall"]
    lag_h = cfg["river_response_lag_hours"]

    factors: list[dict[str, Any]] = []
    trace: list[str] = []
    uncertainties: list[dict[str, str]] = []
    missing: list[dict[str, str]] = []

    def factor(rule_id: str, name: str, level: str | None, source: str, description: str,
               **evidence: Any) -> None:
        factors.append({"rule_id": rule_id, "factor": name, "hazard_level": level,
                        "source": source, "description": description, "evidence": evidence})

    def uncertain(code: str, description: str) -> None:
        uncertainties.append({"code": code, "description": description})

    def lack(item: str, reason: str) -> None:
        missing.append({"item": item, "reason": reason})

    threshold_label = ("official" if river["threshold_is_official"]
                       else "configured TEST/prototype (not official)")

    # ---- river ----------------------------------------------------------
    river_level = None
    threshold_level = None
    rising_notably = False
    if not river["provided"]:
        lack("river_evidence", "No river evidence was supplied or it could not be read.")
        trace.append("River evidence missing.")
    elif not river["usable_for_hazard"]:
        trace.append("River evidence not usable for a hazard assessment: "
                     + ", ".join(river["blocking_reasons"]) + ".")
        lack("current_river_evidence", "River evidence is not usable: "
             + ", ".join(river["blocking_reasons"]) + ".")
        if EVIDENCE_STALE in river["blocking_reasons"]:
            uncertain("RIVER_EVIDENCE_STALE",
                      f"Latest river reading is {river['age_hours']} h old "
                      f"(freshness {river['freshness']}); stale data are not evidence of "
                      "low risk.")
        if EVIDENCE_UNRESOLVED_CONFLICT in river["blocking_reasons"]:
            uncertain("UNRESOLVED_SOURCE_CONFLICT",
                      f"{river['conflicts_current']} current reading(s) disagree between "
                      f"sources; reverification outcome {river['reverification_outcome']}.")
        if EVIDENCE_QUALITY_NOT_ACCEPTABLE in river["blocking_reasons"]:
            uncertain("RIVER_QUALITY_NOT_ACCEPTABLE",
                      f"Recent river data quality is {river['recent_quality']}.")
        if EVIDENCE_INTEGRITY_FAILED in river["blocking_reasons"]:
            uncertain("RIVER_EVIDENCE_INTEGRITY_FAILED",
                      f"River evidence was not used: {river['gate'].get('integrity_problem')}.")
        if EVIDENCE_MALFORMED in river["blocking_reasons"]:
            uncertain("RIVER_EVIDENCE_MALFORMED", "River evidence has an unexpected structure "
                      "and was not used: " + "; ".join(river["input_problems"]) + ".")
        if EVIDENCE_SIMULATION_LABEL_MISMATCH in river["blocking_reasons"]:
            uncertain("SIMULATION_LABEL_MISMATCH", "River evidence carries simulation markers "
                      "that disagree with its label or grade; it is treated as simulated and "
                      "not used.")
    else:
        trace.append(f"River evidence usable (grade {river['grade']}, freshness "
                     f"{river['freshness']}, recent quality {river['recent_quality']}).")
        status = river["threshold_status"]
        if status == THRESHOLD_AT_OR_ABOVE_DANGER:
            threshold_level = CRITICAL
            factor("R1_RIVER_AT_OR_ABOVE_DANGER", "river_level_vs_threshold", CRITICAL, "river",
                   f"Level {river['level_m']} m is at or above the {threshold_label} danger "
                   f"level {river['danger_level_m']} m.", level_m=river["level_m"],
                   danger_level_m=river["danger_level_m"],
                   threshold_reference=river["threshold_reference"])
        elif status == THRESHOLD_AT_OR_ABOVE_WARNING:
            threshold_level = HIGH
            factor("R2_RIVER_AT_OR_ABOVE_WARNING", "river_level_vs_threshold", HIGH, "river",
                   f"Level {river['level_m']} m is at or above the {threshold_label} warning "
                   f"level {river['warning_level_m']} m.", level_m=river["level_m"],
                   warning_level_m=river["warning_level_m"],
                   threshold_reference=river["threshold_reference"])
        elif status == THRESHOLD_BELOW_WARNING:
            threshold_level = LOW
            factor("R3_RIVER_BELOW_WARNING", "river_level_vs_threshold", LOW, "river",
                   f"Level {river['level_m']} m is below the {threshold_label} warning level "
                   f"{river['warning_level_m']} m.", level_m=river["level_m"],
                   warning_level_m=river["warning_level_m"],
                   threshold_reference=river["threshold_reference"])
        else:
            lack("river_thresholds", river["threshold_reason"]
                 or "Threshold status is UNKNOWN.")
            uncertain("NO_THRESHOLD_COMPARISON",
                      "The level cannot be compared with any warning/danger level; no "
                      "official levels are assumed.")
        if status in (THRESHOLD_AT_OR_ABOVE_DANGER, THRESHOLD_AT_OR_ABOVE_WARNING,
                      THRESHOLD_BELOW_WARNING) and not river["threshold_is_official"]:
            uncertain("THRESHOLDS_NOT_OFFICIAL",
                      f"Thresholds are {threshold_label}: {river['threshold_reference']}.")

        rate = river["rate"]
        if river["trend_status"] != SECTION_AVAILABLE or rate is None:
            lack("river_trend", river["trend_reason"] or "River trend is unavailable.")
        elif river["direction"] == TREND_RISING and rate >= river_cfg["rapid_rise_m_per_hour"]:
            rising_notably = True
            factor("R4_RIVER_RAPID_RISE", "river_trend", HIGH, "river",
                   f"River rising {rate} m/h, at or above the prototype rapid-rise rate "
                   f"{river_cfg['rapid_rise_m_per_hour']} m/h.", rate_m_per_hour=rate)
        elif river["direction"] == TREND_RISING and rate >= river_cfg["rise_m_per_hour"]:
            rising_notably = True
            factor("R5_RIVER_RISING", "river_trend", MODERATE, "river",
                   f"River rising {rate} m/h, at or above the prototype rise rate "
                   f"{river_cfg['rise_m_per_hour']} m/h.", rate_m_per_hour=rate)
        elif (river["direction"] == TREND_FALLING and river["warning_level_m"] is not None
              and river["baseline_level_m"] is not None and river["level_m"] is not None
              and river["baseline_level_m"] >= river["warning_level_m"] > river["level_m"]):
            factor("R7_RECEDING_AFTER_HIGH_WATER", "river_trend", MODERATE, "river",
                   f"River receding ({rate} m/h) from {river['baseline_level_m']} m, above the "
                   f"{threshold_label} warning level, to {river['level_m']} m. Recession is "
                   "not yet confirmed; a renewed rise remains possible.",
                   rate_m_per_hour=rate, baseline_level_m=river["baseline_level_m"])
        else:
            factor("R6_RIVER_STEADY_OR_FALLING", "river_trend", LOW, "river",
                   f"River trend {river['direction']} at {rate} m/h (below the prototype "
                   f"rise rate {river_cfg['rise_m_per_hour']} m/h).", rate_m_per_hour=rate)
        river_level = _max_level([f["hazard_level"] for f in factors if f["source"] == "river"])

    if river["provided"] and river["conflicts_current"] and river["usable_for_hazard"]:
        uncertain("SOURCE_CONFLICT_RESOLVED",
                  f"{river['conflicts_current']} current reading(s) conflicted; independent "
                  "reverification RESOLVED them in favour of the primary source.")

    # ---- weather ---------------------------------------------------------
    rain_level = None
    if not wx["usable"]:
        reason = ", ".join(wx["reasons"])
        lack("weather_evidence", f"Weather evidence not used: {reason}.")
        trace.append(f"Weather evidence not used ({reason}).")
    else:
        trace.append(f"Weather evidence used ({wx['source_type']}, retrieved "
                     f"{wx['retrieved_at']}).")
        obs = wx["observed_24h"]
        if obs["status"] == SECTION_AVAILABLE:
            level = _band_level(obs["total_mm"], rain_cfg["observed_24h_mm"])
            factor("R8_OBSERVED_RAINFALL_24H", "observed_rainfall", level, "weather",
                   f"{obs['total_mm']} mm in the past 24 h (model analysis); prototype "
                   f"breakpoints {rain_cfg['observed_24h_mm']} mm.",
                   total_mm=obs["total_mm"], period_start=obs["period_start"],
                   period_end=obs["period_end"])
        else:
            lack("observed_rainfall", obs["reason"] or "24 h rainfall total unavailable.")
        fc = wx["forecast"]
        if fc["status"] == SECTION_AVAILABLE:
            level = _band_level(fc["total_mm"], rain_cfg["forecast_mm"])
            factor("R9_FORECAST_RAINFALL", "forecast_rainfall", level, "weather",
                   f"{fc['total_mm']} mm forecast for the next {fc['hours']} h; prototype "
                   f"breakpoints {rain_cfg['forecast_mm']} mm.", total_mm=fc["total_mm"],
                   period_start=fc["period_start"], period_end=fc["period_end"])
        else:
            lack("forecast_rainfall", fc["reason"] or "Rainfall forecast unavailable.")
        six = wx["observed_6h"]
        if (obs["status"] == SECTION_AVAILABLE and six["status"] == SECTION_AVAILABLE
                and six["total_mm"] >= rain_cfg["intensifying_min_6h_mm"]
                and six["total_mm"] / 6 > rain_cfg["intensifying_ratio"] * obs["total_mm"] / 24):
            factor("R10_RAINFALL_INTENSIFYING", "rainfall_trend", None, "weather",
                   f"Rain rate in the last 6 h ({round(six['total_mm'] / 6, 2)} mm/h) exceeds "
                   f"{rain_cfg['intensifying_ratio']}x the 24 h average "
                   f"({round(obs['total_mm'] / 24, 2)} mm/h).", total_6h_mm=six["total_mm"])
        if not wx["is_simulated"]:
            uncertain("WEATHER_MODEL_BASED",
                      "Rainfall values are Open-Meteo model output for a grid cell, not "
                      "rain-gauge observations; forecast skill falls with lead time.")
        rain_level = _max_level([f["hazard_level"] for f in factors if f["source"] == "weather"])

    # ---- combination ------------------------------------------------------
    hazard = _max_level([river_level, rain_level])
    if rising_notably and rain_level in (MODERATE, HIGH):
        cap = CRITICAL if threshold_level in (HIGH, CRITICAL) else HIGH
        escalated = HAZARD_LEVELS[min(HAZARD_LEVELS.index(hazard) + 1, HAZARD_LEVELS.index(cap))]
        if HAZARD_LEVELS.index(escalated) > HAZARD_LEVELS.index(hazard):
            factor("R11_COMPOUND_RISE_AND_RAIN", "compound", escalated, "combined",
                   f"Rising river combined with {rain_level} rainfall raises the level from "
                   f"{hazard} to {escalated} (capped at {cap}; CRITICAL needs the river at or "
                   "above a warning level).", from_level=hazard, to_level=escalated)
            hazard = escalated
    lag_applies = (river["usable_for_hazard"] and rain_level in (MODERATE, HIGH)
                   and not rising_notably)
    if lag_applies:
        factor("R12_RIVER_RESPONSE_LAG", "river_response_lag", None, "combined",
               f"Heavy rainfall but the river is not (yet) rising notably. River response can "
               f"lag rainfall (assumed up to {lag_h:g} h), so a steady river does not lower "
               "the rainfall-based level.", lag_hours=lag_h)
        uncertain("RIVER_RESPONSE_LAG", f"The river may still respond to recent or forecast "
                  f"rain within ~{lag_h:g} h. {cfg.get('river_response_lag_reference') or ''}"
                  .strip())

    low_blockers = []
    required = cfg["low_requires"]
    present = {"river_thresholds": threshold_level is not None,
               "river_trend": any(f["factor"] == "river_trend" for f in factors),
               "observed_rainfall": any(f["factor"] == "observed_rainfall" for f in factors),
               "forecast_rainfall": any(f["factor"] == "forecast_rainfall" for f in factors)}
    for item in required:
        if not present[item]:
            low_blockers.append(item)

    if not river["usable_for_hazard"]:
        risk_level = INDETERMINATE
        trace.append("R13: river evidence unusable -> INDETERMINATE"
                     + (f" (weather alone indicates at least {rain_level})." if rain_level
                        else "."))
        rule_13 = "R13_INDETERMINATE_RIVER_UNUSABLE"
        factor(rule_13, "determinacy", None, "combined",
               "No usable river evidence, so no defensible flood-risk level can be given.",
               blocking_reasons=river["blocking_reasons"])
        hazard = rain_level
    elif hazard is None:
        risk_level = INDETERMINATE
        trace.append("R14: no hazard indicator could be evaluated -> INDETERMINATE.")
        factor("R14_INDETERMINATE_NO_INDICATORS", "determinacy", None, "combined",
               "Neither river thresholds, trend nor rainfall could be evaluated.")
    elif hazard == LOW and low_blockers:
        risk_level = INDETERMINATE
        trace.append("R15: indicators are LOW but LOW requires " + ", ".join(low_blockers)
                     + " -> INDETERMINATE.")
        factor("R15_LOW_NOT_SUPPORTED", "determinacy", None, "combined",
               "Available indicators are low, but LOW also requires: "
               + ", ".join(low_blockers) + ". Missing evidence is not evidence of low risk.",
               missing=low_blockers)
    else:
        risk_level = hazard
        trace.append(f"Risk level {risk_level} from the highest hazard indicator.")
    if risk_level == INDETERMINATE and hazard == LOW:
        # Only elevated indications are reported alongside INDETERMINATE, so it
        # can never be read as "low".
        hazard = None

    # ---- confidence ------------------------------------------------------------
    downgrades = []
    if risk_level == INDETERMINATE:
        confidence = CONFIDENCE_NONE if not river["usable_for_hazard"] else CONFIDENCE_LOW
    else:
        if threshold_level is None:
            downgrades.append("NO_THRESHOLD_COMPARISON")
        elif not river["threshold_is_official"]:
            downgrades.append("THRESHOLDS_NOT_OFFICIAL")
        if not present["river_trend"]:
            downgrades.append("NO_RIVER_TREND")
        if not (present["observed_rainfall"] and present["forecast_rainfall"]):
            downgrades.append("INCOMPLETE_WEATHER")
        if river["grade"] != GRADE_VERIFIED_LIVE and mode == MODE_REAL:
            downgrades.append("RIVER_NOT_VERIFIED_LIVE")
        confidence = _CONFIDENCE_ORDER[max(0, len(_CONFIDENCE_ORDER) - 1 - len(downgrades))]

    if mode == MODE_SIMULATED:
        uncertain("SIMULATED_DATA", "All inputs are FABRICATED test data. This assessment "
                  "describes a simulation, not the actual river.")
    elif mode == MODE_HISTORICAL:
        uncertain("HISTORICAL_REPLAY", "Assessment of historical data at "
                  f"{reference.isoformat()}; it does not describe current conditions.")
    if mode in (MODE_REAL, MODE_HISTORICAL):
        uncertain("TIMESTAMP_AND_DATUM_ASSUMED", "Provider timestamps are assumed to be IST and "
                  "levels a reduced level; neither is confirmed by the provider.")
    uncertain("NO_CALIBRATED_PROBABILITY", "No validated flood model exists for this site, so "
              "no flood probability is given.")
    lack("upstream_reservoir_releases", "Releases from upstream reservoirs (e.g. Mettur) are "
         "not ingested, although they often dominate flows at Upper Anicut.")

    # ---- human review and monitoring actions ---------------------------------------
    review = []
    if risk_level in (HIGH, CRITICAL):
        review.append("ELEVATED_RISK")
    if risk_level == INDETERMINATE:
        review.append("ASSESSMENT_INDETERMINATE")
    if confidence in (CONFIDENCE_LOW, CONFIDENCE_NONE):
        review.append("LOW_CONFIDENCE")
    if river["conflicts_current"]:
        review.append("SOURCE_CONFLICT_DETECTED")
    if (river_evidence is not None and not isinstance(river_evidence, dict)) \
            or river["input_problems"] or "WEATHER_INVALID" in wx["reasons"] \
            or EVIDENCE_INTEGRITY_FAILED in river["blocking_reasons"]:
        review.append("INVALID_INPUT")
    if EVIDENCE_SIMULATION_LABEL_MISMATCH in river["blocking_reasons"]:
        review.append("SIMULATION_LABEL_MISMATCH")

    actions = []

    def act(code: str, description: str) -> None:
        if code not in [a["code"] for a in actions]:
            actions.append({"code": code, "description": description})

    if river["provided"] and EVIDENCE_STALE in river["blocking_reasons"]:
        act("OBTAIN_CURRENT_RIVER_READING", "Obtain a current reading for the station (fresh "
            "live data or a manual gauge reading) before relying on any river assessment.")
    if not river["provided"] or EVIDENCE_UNAVAILABLE in river["blocking_reasons"]:
        act("RESTORE_RIVER_EVIDENCE", "Restore the river data feed; river conditions are "
            "UNKNOWN until then.")
    if river["conflicts_current"] and not river["usable_for_hazard"]:
        act("RESOLVE_SOURCE_CONFLICT", "Resolve the conflicting readings through independent-"
            "source reverification (requires a state that permits it and an independent "
            "source).")
    if not wx["usable"]:
        act("RESTORE_WEATHER_EVIDENCE", "Provide time-aligned weather evidence (observed and "
            "forecast rainfall) for the same mode as the river evidence.")
    elif not present["observed_rainfall"]:
        act("OBTAIN_OBSERVED_RAINFALL", "Obtain the past-24 h rainfall total (e.g. run the "
            "Weather Agent with past_days=1).")
    if river["usable_for_hazard"] and threshold_level is None:
        act("CONFIGURE_THRESHOLDS", "Configure authoritative warning/danger levels with datum "
            "and reference; none are assumed.")
    if lag_applies:
        act("RECHECK_RIVER_WITHIN_LAG", f"Re-check river level and trend repeatedly over the "
            f"next ~{lag_h:g} h; the river may not yet have responded to the rain.")
    if rising_notably or risk_level in (HIGH, CRITICAL):
        act("INCREASE_RIVER_MONITORING_FREQUENCY", "Review river readings at every new "
            "observation and watch the rate of rise.")
    if any(f["rule_id"] == "R7_RECEDING_AFTER_HIGH_WATER" for f in factors):
        act("MONITOR_RECESSION", "Keep monitoring until the level stays below the warning "
            "level and keeps falling; watch for a renewed rise.")
    if risk_level in (MODERATE, HIGH, CRITICAL, INDETERMINATE):
        act("CHECK_UPSTREAM_RELEASES", "Check upstream reservoir release information with the "
            "responsible authority (not available to this system).")
    if review:
        act("REFER_TO_HUMAN_REVIEWER", "Refer this assessment to a human reviewer; it does not "
            "authorize any action.")
    if not actions:
        act("CONTINUE_ROUTINE_MONITORING", "Continue routine monitoring.")

    # ---- supporting evidence ---------------------------------------------------------
    river_support: dict[str, Any] = {
        "evidence_id": (river["gate"] or {}).get("evidence_id"),
        "source_label": (river["gate"] or {}).get("source_label"),
        "evidence_grade": river["grade"],
        "status": river["status"],
        "freshness": river["freshness"],
        "age_hours": river["age_hours"],
        "recent_quality": river["recent_quality"],
        "observed_at": river["observed_at"],
        "trend_direction": river["direction"],
        "rate_of_change_m_per_hour": river["rate"],
        "threshold_status": river["threshold_status"],
        "threshold_is_official": river["threshold_is_official"],
        "threshold_reference": river["threshold_reference"],
        "cross_source_status": river["cross_status"],
        "conflicts_affecting_current_evidence": river["conflicts_current"],
        "reverification_outcome": river["reverification_outcome"],
        "used_for_hazard": river["usable_for_hazard"],
    }
    level_key = "simulated_level_m" if mode == MODE_SIMULATED else (
        "historical_level_m" if mode == MODE_HISTORICAL else
        "current_level_m" if river["usable_for_hazard"] else "last_known_level_m")
    river_support[level_key] = river["level_m"]
    weather_support = {
        "source": wx.get("source"), "source_type": wx.get("source_type"),
        "status": wx.get("status"), "retrieved_at": wx["retrieved_at"],
        "is_simulated": wx["is_simulated"], "used": wx["usable"],
        "not_used_reasons": wx["reasons"],
        "observed_24h_mm": (wx["observed_24h"] or {}).get("total_mm"),
        "observed_6h_mm": (wx["observed_6h"] or {}).get("total_mm"),
        "forecast_mm": (wx["forecast"] or {}).get("total_mm"),
        "forecast_hours": (wx["forecast"] or {}).get("hours"),
    }

    assessment: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "engine": ENGINE_NAME,
        "assessment_timestamp": now.isoformat(),
        "reference_time": reference.isoformat(),
        "assessment_mode": mode,
        "is_simulated": mode == MODE_SIMULATED,
        "risk_level": risk_level,
        "risk_level_meaning": cfg["categories"].get(risk_level),
        "hazard_indicated": hazard,
        "risk_factors": factors,
        "rules_fired": [f["rule_id"] for f in factors],
        "decision_trace": trace,
        "supporting_evidence": {"river": river_support, "weather": weather_support},
        "evidence_quality": {
            "confidence": confidence,
            "confidence_downgrades": downgrades,
            "confidence_applies_to": ("SIMULATION_ONLY" if mode == MODE_SIMULATED
                                      else "HISTORICAL_REPLAY" if mode == MODE_HISTORICAL
                                      else "CURRENT_CONDITIONS"),
            "river_evidence_grade": river["grade"],
            "river_freshness": river["freshness"],
            "river_recent_quality": river["recent_quality"],
            "river_usable_for_hazard": river["usable_for_hazard"],
            "river_blocking_reasons": river["blocking_reasons"],
            "real_world_evidence_gate": {"eligible": river["gate"]["eligible"],
                                         "reasons": river["gate"]["reasons"]},
            "weather_used": wx["usable"],
            "weather_not_used_reasons": wx["reasons"],
        },
        "uncertainties": uncertainties,
        "missing_data": missing,
        "recommended_monitoring_actions": actions,
        "requires_human_review": bool(review),
        "human_review_reasons": review,
        "calibrated_probability": None,
        "authorizes_actions": False,
        "assigns_disaster_state": False,
        "disclaimer": DISCLAIMER,
    }
    assessment["assessment_id"] = compute_assessment_id(assessment)
    return assessment


# Volatile, or added after the ID is computed (the workflow adds audit_written).
_ASSESSMENT_ID_EXCLUDED = ("assessment_timestamp", "assessment_id", "audit_written")


def compute_assessment_id(assessment: dict[str, Any]) -> str:
    """Deterministic ID of an assessment's content. Recomputing it on a supplied
    assessment shows whether its content was edited after it was produced."""
    stable = {k: v for k, v in assessment.items() if k not in _ASSESSMENT_ID_EXCLUDED}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


def failed_assessment(error: str, mode: str = MODE_UNKNOWN,
                      clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Fail-safe result when the engine itself could not run."""
    now = (clock or (lambda: datetime.now(timezone.utc)))().astimezone(timezone.utc)
    result = {
        "schema_version": SCHEMA_VERSION, "engine": ENGINE_NAME,
        "assessment_timestamp": now.isoformat(), "reference_time": None,
        "assessment_mode": mode, "is_simulated": mode == MODE_SIMULATED,
        "risk_level": INDETERMINATE, "hazard_indicated": None,
        "risk_factors": [], "rules_fired": ["R0_ENGINE_ERROR"],
        "decision_trace": [f"Risk engine error: {error}"],
        "supporting_evidence": {}, "evidence_quality": {"confidence": CONFIDENCE_NONE},
        "uncertainties": [{"code": "ENGINE_ERROR", "description": error}],
        "missing_data": [], "error": error,
        "recommended_monitoring_actions": [{"code": "REFER_TO_HUMAN_REVIEWER",
                                            "description": "Risk could not be assessed."}],
        "requires_human_review": True, "human_review_reasons": ["ENGINE_ERROR"],
        "calibrated_probability": None, "authorizes_actions": False,
        "assigns_disaster_state": False, "disclaimer": DISCLAIMER,
    }
    result["assessment_id"] = compute_assessment_id(result)
    return result


def summary(assessment: dict[str, Any]) -> str:
    """A short human-readable summary."""
    lines = [f"Risk level: {assessment['risk_level']} (mode {assessment['assessment_mode']}, "
             f"confidence {assessment['evidence_quality'].get('confidence')})",
             f"Hazard indicated: {assessment.get('hazard_indicated')}"]
    lines += [f"  - [{f['rule_id']}] {f['description']}" for f in assessment["risk_factors"]]
    lines.append("Human review required: " + ("yes (" + ", ".join(
        assessment["human_review_reasons"]) + ")" if assessment["requires_human_review"]
        else "no"))
    lines += [f"  * {a['code']}" for a in assessment["recommended_monitoring_actions"]]
    lines.append(assessment["disclaimer"])
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assess flood risk from saved evidence files (offline).")
    parser.add_argument("river_evidence", help="River evidence JSON (RiverEvidenceService "
                                               "envelope, e.g. outputs/river_evidence.json)")
    parser.add_argument("--weather", default=None, help="Weather Agent result JSON, a saved "
                        "weather_live_*.json sample, or a simulated weather file")
    parser.add_argument("--config", default=str(DEFAULT_RISK_CONFIG_PATH))
    parser.add_argument("--json", action="store_true", help="Print the full assessment")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    try:
        config = load_risk_config(args.config)
        evidence = load_json_file(args.river_evidence, "River evidence")
        weather = None
        if args.weather:
            doc = load_json_file(args.weather, "Weather")
            weather = (load_simulated_weather(args.weather) if isinstance(doc, dict)
                       and doc.get("simulated") is True else
                       doc.get("result") if isinstance(doc, dict) and "result" in doc else doc)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    assessment = assess_flood_risk(copy.deepcopy(evidence), weather, None, config)
    print(json.dumps(assessment, indent=2, ensure_ascii=False, default=str) if args.json
          else summary(assessment))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
