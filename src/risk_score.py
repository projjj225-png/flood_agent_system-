"""Flood Risk Score (0-100): a deterministic presentation of the Risk Assessment Engine.

The score never decides anything. The CATEGORY (LOW / MODERATE / HIGH / CRITICAL /
INDETERMINATE) is the Risk Assessment Engine's rule-based ``risk_level``, the same
value the Disaster State Manager uses. The score only positions the assessment
inside that category's presentation band (config/flood_scoring.json), so the
number and the category can never disagree.

How the position is found (no averaging):
1. Each hazard indicator the engine evaluated gets an index 0-100 whose band
   matches the engine's own level for that indicator (e.g. a river at or above
   the warning level scores 50-74, i.e. HIGH, exactly as rule R2 says):
     river level vs the configured warning/danger levels   (rules R1/R2/R3)
     river rate of rise / recession                         (rules R4-R7)
     observed 24 h rainfall vs the engine's breakpoints     (rule R8)
     forecast rainfall vs the engine's breakpoints          (rule R9)
   Traffic is context only: it never changes the category and can only
   corroborate within the band.
2. combined = dominant + w x (100 - dominant) x second / 100
   (dominant = highest index, second = next highest, w = corroboration weight).
   One strong indicator is never diluted by weak ones.
3. score = combined, clamped into the category's band. Clamping is reported:
   e.g. a compound rule (R11) can lift the category above every single
   indicator, and only river evidence can make CRITICAL.

INDETERMINATE has no score (``score`` is None) and lists what is missing. Missing,
stale, conflicting or unusable evidence is never scored as zero or low.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from src.river_monitoring_agent import PROJECT_DIR, load_json_file

DEFAULT_SCORING_CONFIG_PATH = PROJECT_DIR / "config" / "flood_scoring.json"
CATEGORIES = ("LOW", "MODERATE", "HIGH", "CRITICAL")
INDETERMINATE = "INDETERMINATE"
METHOD = ("Category from the deterministic Risk Assessment Engine; score = dominant indicator "
          "plus corroboration from the next indicator, clamped into the category's "
          "presentation band. Prototype bands, not official CWC warning levels.")
RAIN_TERMS = ((204.5, "Extremely heavy"), (115.6, "Very heavy"), (64.5, "Heavy"),
              (15.6, "Moderate"), (2.5, "Light"), (0.0, "Very light / none"))


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def validate_scoring_config(config: Any) -> dict[str, Any]:
    if not isinstance(config, dict):
        raise ValueError("Scoring config must be a JSON object.")
    bands = config.get("risk_bands")
    if not isinstance(bands, dict) or any(not isinstance(bands.get(c), list)
                                          or len(bands[c]) != 2 for c in CATEGORIES):
        raise ValueError("Scoring config: risk_bands needs [min, max] for every category.")
    previous = -1
    for c in CATEGORIES:
        lo, hi = bands[c]
        if not (isinstance(lo, int) and isinstance(hi, int) and lo == previous + 1 and hi >= lo):
            raise ValueError("Scoring config: risk_bands must tile 0-100 in category order.")
        previous = hi
    if previous != 100:
        raise ValueError("Scoring config: risk_bands must end at 100.")
    weights = {k: v for k, v in _obj(config.get("trust_factor_weights")).items()
               if not k.startswith("_")}
    if abs(sum(weights.values()) - 100) > 1e-9:
        raise ValueError("Scoring config: trust_factor_weights must sum to 100.")
    agents = {k: v for k, v in _obj(config.get("trust_agent_weights")).items()
              if not k.startswith("_")}
    if set(agents) != {"river", "weather", "traffic"} or abs(sum(agents.values()) - 1) > 1e-9:
        raise ValueError("Scoring config: trust_agent_weights must cover river, weather and "
                         "traffic and sum to 1.")
    return config


def load_scoring_config(path: str | Path = DEFAULT_SCORING_CONFIG_PATH) -> dict[str, Any]:
    return validate_scoring_config(load_json_file(path, "Scoring config"))


# --------------------------------------------------------------------------
# Indicator indices (each band matches the engine's level for that indicator)
# --------------------------------------------------------------------------

def river_level_index(level: float, warning: float, danger: float) -> float:
    """Below warning: 0-24 (closer is higher); warning..danger: 50-74; >= danger: 75-100."""
    gap = max(danger - warning, 0.1)
    if level < warning:
        return 24 * _clamp((level - (warning - 2 * gap)) / (2 * gap))
    if level < danger:
        return 50 + 24 * _clamp((level - warning) / gap)
    return 75 + 25 * _clamp((level - danger) / gap)


def river_trend_index(rule_id: str, rate: float | None, rise: float, rapid: float) -> float:
    rate = rate or 0.0
    if rule_id == "R4_RIVER_RAPID_RISE":
        return 50 + 24 * _clamp((rate - rapid) / rapid)
    if rule_id == "R5_RIVER_RISING":
        return 25 + 24 * _clamp((rate - rise) / (rapid - rise))
    if rule_id == "R7_RECEDING_AFTER_HIGH_WATER":
        return 30.0
    return 24 * _clamp(rate / rise) if rate > 0 else 0.0


def rainfall_index(total: float, bands: dict[str, float]) -> float:
    moderate, high = bands["MODERATE"], bands["HIGH"]
    if total < moderate:
        return 24 * _clamp(total / moderate)
    if total < high:
        return 25 + 24 * _clamp((total - moderate) / (high - moderate))
    return 50 + 24 * _clamp((total - high) / high)


def rain_term(total_mm: float | None) -> str:
    """Plain-language rainfall term (IMD 24 h intensity words; descriptive only)."""
    if total_mm is None:
        return "Unknown"
    return next(term for limit, term in RAIN_TERMS if total_mm >= limit)


def _level_name(index: float) -> str:
    return "CRITICAL" if index >= 75 else "HIGH" if index >= 50 else \
        "MODERATE" if index >= 25 else "LOW"


# --------------------------------------------------------------------------
# Score
# --------------------------------------------------------------------------

def _factor(key: str, label: str, index: float | None, value_text: str, detail: str,
            rule_id: str | None, source: str, counts: bool = True,
            engine_level: str | None = None) -> dict[str, Any]:
    idx = None if index is None else round(index, 1)
    if idx is None:
        direction = "unknown"
    elif idx >= 25:
        direction = "raises"
    else:
        direction = "lowers"
    return {"key": key, "label": label, "index": idx,
            "level": engine_level or (None if idx is None else _level_name(idx)),
            "value_text": value_text, "detail": detail, "rule_id": rule_id, "source": source,
            "direction": direction, "counts_toward_score": counts and idx is not None}


def compute_risk_score(risk: dict[str, Any] | None, thresholds: dict[str, Any] | None,
                       traffic_view: dict[str, Any] | None,
                       risk_config: dict[str, Any], scoring: dict[str, Any]) -> dict[str, Any]:
    """Flood Risk Score block for display. Pure and deterministic; never raises for bad input."""
    risk = _obj(risk)
    category = risk.get("risk_level")
    bands = scoring["risk_bands"]
    factors_in = [f for f in risk.get("risk_factors") or [] if isinstance(f, dict)]
    by_rule = {f.get("rule_id"): f for f in factors_in}
    rules = set(by_rule)
    out: dict[str, Any] = {
        "available": False, "score": None, "category": category or INDETERMINATE,
        "band": None, "bands": {c: bands[c] for c in CATEGORIES},
        "confidence": _obj(risk.get("evidence_quality")).get("confidence"),
        "is_simulated": risk.get("is_simulated") is True,
        "assessment_id": risk.get("assessment_id"),
        "assessed_at": risk.get("assessment_timestamp"),
        "method": METHOD, "factors": [], "adjustments": [], "missing": [],
        "rules_fired": list(risk.get("rules_fired") or []),
        "official_thresholds": bool(_obj(thresholds).get("threshold_is_official")),
        "summary": "",
    }
    river_cfg, rain_cfg = risk_config["river"], risk_config["rainfall"]
    factors: list[dict[str, Any]] = []

    # River level
    level_rule = next((r for r in ("R1_RIVER_AT_OR_ABOVE_DANGER", "R2_RIVER_AT_OR_ABOVE_WARNING",
                                   "R3_RIVER_BELOW_WARNING") if r in rules), None)
    th = _obj(thresholds)
    warning, danger = _num(th.get("warning_level_m")), _num(th.get("danger_level_m"))
    if level_rule:
        ev = _obj(by_rule[level_rule].get("evidence"))
        level = _num(ev.get("level_m"))
        if level is not None and warning is not None and danger is not None:
            words = {"R1_RIVER_AT_OR_ABOVE_DANGER": "Above danger level",
                     "R2_RIVER_AT_OR_ABOVE_WARNING": "Above warning level",
                     "R3_RIVER_BELOW_WARNING": ("Elevated, below warning"
                                                if level >= warning - (danger - warning)
                                                else "Normal, below warning")}[level_rule]
            label = "TEST" if not out["official_thresholds"] else "official"
            factors.append(_factor(
                "river_level", "River level", river_level_index(level, warning, danger),
                words, f"{level:.2f} m vs {label} warning {warning:.2f} m / danger "
                f"{danger:.2f} m", level_rule, "River Agent",
                engine_level=by_rule[level_rule].get("hazard_level")))
    # River trend
    trend_rule = next((r for r in ("R4_RIVER_RAPID_RISE", "R5_RIVER_RISING",
                                   "R7_RECEDING_AFTER_HIGH_WATER", "R6_RIVER_STEADY_OR_FALLING")
                       if r in rules), None)
    if trend_rule:
        rate = _num(_obj(by_rule[trend_rule].get("evidence")).get("rate_m_per_hour"))
        words = {"R4_RIVER_RAPID_RISE": "Rising rapidly", "R5_RIVER_RISING": "Rising",
                 "R7_RECEDING_AFTER_HIGH_WATER": "Receding after high water",
                 "R6_RIVER_STEADY_OR_FALLING": ("Falling" if (rate or 0) < -0.02 else
                                                "Slowly rising" if (rate or 0) > 0.02 else
                                                "Steady")}[trend_rule]
        factors.append(_factor(
            "river_trend", "River trend",
            river_trend_index(trend_rule, rate, river_cfg["rise_m_per_hour"],
                              river_cfg["rapid_rise_m_per_hour"]),
            words, f"{rate:+.3f} m/h (prototype rise {river_cfg['rise_m_per_hour']} m/h, "
            f"rapid {river_cfg['rapid_rise_m_per_hour']} m/h)" if rate is not None
            else "rate unavailable", trend_rule, "River Agent",
            engine_level=by_rule[trend_rule].get("hazard_level")))
    # Rainfall
    if "R8_OBSERVED_RAINFALL_24H" in rules:
        total = _num(_obj(by_rule["R8_OBSERVED_RAINFALL_24H"].get("evidence")).get("total_mm"))
        if total is not None:
            factors.append(_factor(
                "observed_rainfall", "Rainfall (past 24 h)",
                rainfall_index(total, rain_cfg["observed_24h_mm"]), rain_term(total),
                f"{total:.1f} mm in 24 h (heavy from {rain_cfg['observed_24h_mm']['MODERATE']}"
                f" mm, very heavy from {rain_cfg['observed_24h_mm']['HIGH']} mm)",
                "R8_OBSERVED_RAINFALL_24H", "Weather Agent",
                engine_level=by_rule["R8_OBSERVED_RAINFALL_24H"].get("hazard_level")))
    if "R9_FORECAST_RAINFALL" in rules:
        total = _num(_obj(by_rule["R9_FORECAST_RAINFALL"].get("evidence")).get("total_mm"))
        if total is not None:
            factors.append(_factor(
                "forecast_rainfall", f"Forecast rainfall (next {rain_cfg['forecast_hours']} h)",
                rainfall_index(total, rain_cfg["forecast_mm"]), rain_term(total),
                f"{total:.1f} mm forecast (moderate band from "
                f"{rain_cfg['forecast_mm']['MODERATE']} mm, high from "
                f"{rain_cfg['forecast_mm']['HIGH']} mm)", "R9_FORECAST_RAINFALL",
                "Weather Agent",
                engine_level=by_rule["R9_FORECAST_RAINFALL"].get("hazard_level")))
    # Traffic (context only)
    tv = _obj(traffic_view)
    routes = {k: v for k, v in _obj(tv.get("routes")).items() if isinstance(v, str)}
    if routes and tv.get("status") not in (None, "UNAVAILABLE", "STALE", "SCOPE_MISMATCH"):
        table = scoring.get("traffic_context_index") or {}
        worst = max(routes.values(), key=lambda c: table.get(c, 0))
        blocked = [r for r, c in routes.items() if c == "BLOCKED"]
        factors.append(_factor(
            "traffic", "Roads (evacuation routes)", float(table.get(worst, 0)),
            "Route blocked" if blocked else "Congested" if worst in ("HEAVY", "SEVERE")
            else "Routes usable",
            (f"{len(blocked)} of {len(routes)} route(s) blocked" if blocked else
             f"worst route condition {worst.replace('_', ' ').lower()}"),
            None, "Traffic Agent", counts=True,
            engine_level=None))
        factors[-1]["context_only"] = True
    out["factors"] = factors

    if category not in CATEGORIES:
        out["category"] = INDETERMINATE
        out["missing"] = [{"item": m.get("item"), "reason": m.get("reason")}
                          for m in risk.get("missing_data") or [] if isinstance(m, dict)
                          and m.get("item") != "upstream_reservoir_releases"]
        blocking = _obj(by_rule.get("R13_INDETERMINATE_RIVER_UNUSABLE", {}).get("evidence"))
        out["blocking_reasons"] = list(blocking.get("blocking_reasons") or [])
        out["summary"] = ("No defensible flood-risk score: "
                          + ("; ".join(m["reason"] for m in out["missing"] if m.get("reason"))
                             or "the required evidence is missing or unusable") + ".")
        return out

    lo, hi = bands[category]
    indices = sorted((f["index"] for f in factors if f["counts_toward_score"]), reverse=True)
    hazard_indices = sorted((f["index"] for f in factors
                             if f["counts_toward_score"] and not f.get("context_only")),
                            reverse=True)
    dominant = hazard_indices[0] if hazard_indices else float(lo)
    rest = list(indices)
    if dominant in rest:
        rest.remove(dominant)
    second = rest[0] if rest else 0.0
    weight = float(_obj(scoring.get("risk_combination")).get("corroboration_weight", 0.25))
    combined = dominant + weight * (100 - dominant) * second / 100
    score = round(_clamp(combined, lo, hi))
    if combined < lo:
        compound = "R11_COMPOUND_RISE_AND_RAIN" in rules
        out["adjustments"].append(
            f"Raised to the {category} band minimum ({lo}): "
            + ("rising river combined with heavy rain (rule R11) escalates the category."
               if compound else f"the engine's category is {category}."))
    elif combined > hi:
        out["adjustments"].append(
            f"Capped at the {category} band maximum ({hi}): "
            + ("only river evidence can make CRITICAL." if category != "CRITICAL"
               else "the scale ends at 100."))
    dominant_factor = next((f for f in factors if f["index"] == dominant
                            and not f.get("context_only")), None)
    out.update(available=True, score=score, band=[lo, hi],
               combined_index=round(combined, 1),
               dominant_factor=dominant_factor["key"] if dominant_factor else None)
    raising = [f["label"].lower() for f in factors
               if f["direction"] == "raises" and not f.get("context_only")]
    out["summary"] = (f"{category.title()} flood risk ({score}/100). "
                      + (f"Driven by {dominant_factor['label'].lower()} "
                         f"({dominant_factor['value_text'].lower()})" if dominant_factor
                         else "No single dominant indicator")
                      + (f"; also raised by {', '.join(r for r in raising if dominant_factor and r != dominant_factor['label'].lower())}"
                         if len(raising) > 1 else "") + ".")
    return out
