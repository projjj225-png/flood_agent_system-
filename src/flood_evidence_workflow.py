"""Combined Weather + River evidence workflow.

    python -m src.flood_evidence_workflow --river-mode auto --state ALERT
    python -m src.flood_evidence_workflow --river-mode simulated --scenario conflict-resolves \
        --state ALERT --weather-file outputs/weather_live_20261010T082315Z.json

Steps (deterministic, audited in outputs/audit/flood_evidence_audit.jsonl):
1. Weather evidence: WeatherAgent.run() (live Open-Meteo, model-based) or a
   saved sample (--weather-file, labelled REPLAY). The Weather Agent itself
   is unchanged.
2. River evidence: river_agent_workflow.run_river_workflow() (live / auto /
   historical / simulated), including real cross-source conflict detection
   and authorized reverification.
3. Cross-agent checks (blocking): both evidence sets present, weather fresh
   relative to the reference time and not from the future, weather and river
   observations close in time, and close in space (weather grid cell vs.
   river station). Limits are prototype settings in config/flood_evidence.json.
4. Hydrological observations (NOT blocking): river trend direction next to
   recent modelled rainfall. Reported for context only; no validated
   rainfall-river rule exists here (upstream dam releases and travel time
   are not modelled), so these never change a decision.
5. Permissions: capability_control.authorize_with_evidence with the
   COMBINED assessment. Real-world capabilities need eligible river evidence
   AND usable live weather evidence AND passing cross-agent checks; even then
   only PERMITTED_PENDING_HUMAN_APPROVAL. Nothing is executed.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog
from src.capability_control import (
    REAL_WORLD_CAPABILITIES,
    DisasterState,
    authorize_with_evidence_and_audit,
)
from src.river_agent_workflow import (
    DEFAULT_CAPABILITIES,
    SCENARIO_SOURCES,
    SCENARIOS,
    run_river_workflow,
)
from src.river_monitoring_agent import PROJECT_DIR, load_json_file, parse_timestamp
from src.river_service import (
    MODE_AUTO,
    MODE_LIVE,
    MODE_SIMULATED,
    MODES,
    RiverEvidenceService,
)
from src.weather_agent import STATUS_UNAVAILABLE as WEATHER_UNAVAILABLE_STATUS
from src.weather_agent import WeatherAgent

SCHEMA_VERSION = "1.0"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "flood_evidence.json"
DEFAULT_SETTINGS = {
    "max_weather_age_hours": 3.0,
    "max_time_skew_hours": 3.0,
    "max_location_distance_km": 10.0,
    "audit_log": "outputs/audit/flood_evidence_audit.jsonl",
    "output": "outputs/flood_evidence.json",
}

WEATHER_LIVE = "WEATHER_LIVE_MODEL"
WEATHER_REPLAY = "WEATHER_REPLAY"
WEATHER_NOT_REQUESTED = "WEATHER_NOT_REQUESTED"

# Combined-assessment reason codes added on top of the river reasons.
WEATHER_UNAVAILABLE = "WEATHER_UNAVAILABLE"
WEATHER_STALE = "WEATHER_STALE"
WEATHER_REPLAYED = "WEATHER_REPLAYED"
WEATHER_FROM_FUTURE = "WEATHER_AFTER_REFERENCE_TIME"
CROSS_AGENT_TIME_SKEW = "CROSS_AGENT_TIME_SKEW"
CROSS_AGENT_LOCATION_MISMATCH = "CROSS_AGENT_LOCATION_MISMATCH"
CROSS_AGENT_NOT_EVALUATED = "CROSS_AGENT_CHECK_NOT_EVALUATED"


def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if path.is_file():
        data = load_json_file(path, "Flood evidence config")
        if not isinstance(data, dict):
            raise ValueError("Flood evidence config must be a JSON object.")
        settings.update({k: v for k, v in data.items() if not k.startswith("_")})
    for key in ("max_weather_age_hours", "max_time_skew_hours", "max_location_distance_km"):
        value = settings[key]
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"{key} must be a positive number")
    return settings


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


# --------------------------------------------------------------------------
# Weather evidence
# --------------------------------------------------------------------------

def summarize_weather(result: dict[str, Any], label: str,
                      reference_time: datetime) -> dict[str, Any]:
    """Weather evidence summary and its own eligibility reasons (pure)."""
    current = result.get("current_weather") or {}
    observed = parse_timestamp(current.get("time")) if current.get("time") else None
    reasons: list[str] = []
    if result.get("status") == WEATHER_UNAVAILABLE_STATUS or observed is None:
        reasons.append(WEATHER_UNAVAILABLE)
    age_h = None
    if observed is not None:
        age_h = round((reference_time - observed).total_seconds() / 3600, 3)
        if age_h < 0:
            reasons.append(WEATHER_FROM_FUTURE)
    if label == WEATHER_REPLAY:
        reasons.append(WEATHER_REPLAYED)
    windows = ((result.get("rainfall_accumulation") or {}).get("windows") or {})
    rainfall = {name: {"status": w.get("precipitation", {}).get("status"),
                       "total_mm": w.get("precipitation", {}).get("total"),
                       "reason": w.get("precipitation", {}).get("reason")}
                for name, w in windows.items()}
    grid = result.get("returned_grid_location") or {}
    return {
        "source_label": label,
        "status": result.get("status"),
        "source": result.get("source"),
        "source_type": result.get("source_type"),
        "retrieved_at": result.get("retrieved_at"),
        "current_time": observed.astimezone(timezone.utc).isoformat() if observed else None,
        "age_hours": age_h,
        "current_values": current.get("values"),
        "rainfall_mm": rainfall,  # missing totals stay null with a reason, never 0
        "grid_location": {"latitude": grid.get("latitude"), "longitude": grid.get("longitude")},
        "errors": [e.get("code") for e in result.get("errors", [])],
        "reasons": reasons,
        "note": "Model-based current conditions and forecast; not rain-gauge observations.",
    }


# --------------------------------------------------------------------------
# Cross-agent checks (pure)
# --------------------------------------------------------------------------

def _check(name: str, status: str, detail: str, reason_code: str | None = None,
           blocking: bool = True) -> dict[str, Any]:
    return {"check": name, "status": status, "detail": detail, "blocking": blocking,
            "reason_code": reason_code if status != "PASS" and blocking else None}


def cross_agent_checks(weather: dict[str, Any], river: dict[str, Any],
                       river_station: dict[str, Any], settings: dict[str, Any]
                       ) -> list[dict[str, Any]]:
    checks = []
    max_age = settings["max_weather_age_hours"]
    age = weather.get("age_hours")
    if age is None:
        checks.append(_check("weather_freshness", "NOT_EVALUATED",
                             "No weather observation time.", WEATHER_UNAVAILABLE))
    elif age < 0:
        checks.append(_check("weather_freshness", "FAIL",
                             f"Weather time is {abs(age)} h after the reference time.",
                             WEATHER_FROM_FUTURE))
    else:
        checks.append(_check("weather_freshness", "PASS" if age <= max_age else "FAIL",
                             f"Weather current conditions are {age} h old (limit {max_age} h).",
                             WEATHER_STALE))

    river_time = parse_timestamp(river.get("observed_at")) if river.get("observed_at") else None
    weather_time = parse_timestamp(weather["current_time"]) if weather.get("current_time") else None
    if river_time is None or weather_time is None:
        checks.append(_check("time_alignment", "NOT_EVALUATED",
                             "Missing river or weather observation time.",
                             CROSS_AGENT_NOT_EVALUATED))
    else:
        skew = round(abs((river_time - weather_time).total_seconds()) / 3600, 3)
        limit = settings["max_time_skew_hours"]
        checks.append(_check("time_alignment", "PASS" if skew <= limit else "FAIL",
                             f"River and weather observations are {skew} h apart "
                             f"(limit {limit} h).", CROSS_AGENT_TIME_SKEW))

    grid = weather.get("grid_location") or {}
    lat, lon = river_station.get("latitude"), river_station.get("longitude")
    if None in (grid.get("latitude"), grid.get("longitude"), lat, lon):
        checks.append(_check("location_alignment", "NOT_EVALUATED",
                             "Missing weather grid or river station coordinates.",
                             CROSS_AGENT_NOT_EVALUATED))
    else:
        distance = round(haversine_km(grid["latitude"], grid["longitude"], lat, lon), 2)
        limit = settings["max_location_distance_km"]
        checks.append(_check("location_alignment", "PASS" if distance <= limit else "FAIL",
                             f"Weather grid cell is {distance} km from the river station "
                             f"(limit {limit} km).", CROSS_AGENT_LOCATION_MISMATCH))
    return checks


def hydrological_observations(weather: dict[str, Any],
                              river: dict[str, Any]) -> list[dict[str, Any]]:
    """Context only (never blocking): river trend next to recent modelled rain."""
    rain3 = (weather.get("rainfall_mm") or {}).get("3h", {})
    direction = (river.get("river_trend_direction") or None)
    rain_text = ("unavailable" if rain3.get("status") != "AVAILABLE"
                 else f"{rain3.get('total_mm')} mm")
    observation = {
        "check": "rain_vs_river_trend", "status": "INFO", "blocking": False,
        "detail": f"River trend {direction or 'unavailable'}; modelled rain in last 3 h "
                  f"{rain_text}.",
        "note": ("Not used for decisions. River level at Upper Anicut depends on upstream "
                 "catchment rain and dam releases (e.g. Mettur) with travel time; local "
                 "modelled rain alone does not confirm or contradict the river trend."),
    }
    if direction == "RISING" and rain3.get("status") == "AVAILABLE" and rain3.get("total_mm") == 0:
        observation["detail"] += " Rise without local modelled rain: check upstream releases."
    return [observation]


def combine_assessments(river_assessment: dict[str, Any], weather: dict[str, Any],
                        checks: list[dict[str, Any]]) -> dict[str, Any]:
    """Eligible only if river, weather and every blocking cross-agent check pass."""
    reasons = list(river_assessment.get("reasons") or [])
    for code in weather.get("reasons", []):
        if code not in reasons:
            reasons.append(code)
    for check in checks:
        if check["blocking"] and check["reason_code"] and check["reason_code"] not in reasons:
            reasons.append(check["reason_code"])
    return {"eligible": not reasons and bool(river_assessment.get("eligible")),
            "reasons": reasons,
            "evidence_id": river_assessment.get("evidence_id"),
            "river_reasons": list(river_assessment.get("reasons") or []),
            "weather_reasons": list(weather.get("reasons", [])),
            "cross_agent_reasons": [c["reason_code"] for c in checks
                                    if c["blocking"] and c["reason_code"]]}


# --------------------------------------------------------------------------
# Workflow
# --------------------------------------------------------------------------

def run_flood_evidence_workflow(
    service: RiverEvidenceService,
    audit_log: AuditLog,
    river_mode: str = MODE_AUTO,
    disaster_state: str = DisasterState.NORMAL.value,
    capabilities: tuple[str, ...] | list[str] = DEFAULT_CAPABILITIES,
    as_of: datetime | None = None,
    scenario: str | None = None,
    weather_agent: WeatherAgent | None = None,
    weather_file: str | Path | None = None,
    settings: dict[str, Any] | None = None,
    agent_id: str = "flood_evidence_orchestrator",
    clock: Callable[[], datetime] | None = None,
    run_id: str | None = None,
    use_weather: bool = True,
) -> dict[str, Any]:
    settings = settings or load_settings()
    clock = clock or (lambda: datetime.now(timezone.utc))
    run_id = run_id or uuid.uuid4().hex
    audit_log.log(run_id, "flood_evidence_started", river_mode=river_mode, scenario=scenario,
                  disaster_state=str(disaster_state), weather_file=str(weather_file or ""),
                  capabilities=list(capabilities))

    # 1. Weather
    if not use_weather:
        weather_result = {"status": WEATHER_UNAVAILABLE_STATUS, "current_weather": {},
                          "errors": [{"code": "WEATHER_NOT_REQUESTED"}]}
        weather_label = WEATHER_NOT_REQUESTED
    elif weather_file:
        document = load_json_file(weather_file, "Weather sample")
        weather_result = document.get("result", document) if isinstance(document, dict) else {}
        weather_label = WEATHER_REPLAY
    else:
        agent = weather_agent or WeatherAgent()
        weather_result = agent.run()
        weather_label = WEATHER_LIVE

    # 2. River (its own audit trail, same run id); no capability decisions there.
    cross_paths = independent = None
    if river_mode == MODE_SIMULATED:
        sources = SCENARIO_SOURCES[scenario or "no-conflict"]
        cross_paths, independent = sources["cross_check_paths"], sources["independent_paths"]
    river = run_river_workflow(service, audit_log, mode=river_mode,
                               disaster_state=disaster_state, capabilities=(),
                               as_of=as_of, cross_check_paths=cross_paths,
                               independent_paths=independent, agent_id="river_agent",
                               run_id=run_id, scenario=scenario)
    river_assessment = river["evidence_assessment"]

    # Reference time: the replay time, or now for live runs.
    reference = as_of.astimezone(timezone.utc) if as_of else clock().astimezone(timezone.utc)
    weather = summarize_weather(weather_result, weather_label, reference)
    station = service.config.get("station", {})
    river_summary = {
        "source_label": river["source_label"],
        "station": river["station"],
        "observed_at": river["observed_at"],
        "river_level_m": river["river_level_m"],
        "simulated_river_level_m": river["simulated_river_level_m"],
        "freshness": river["evidence_freshness"],
        "recent_quality": river["evidence_quality"],
        "source_verified": river["source_verified"],
        "cross_source_check": river["cross_source_check"],
        "reverification_outcome": river["reverification"]["outcome"],
        "evidence_assessment": river_assessment,
        "service_evidence_id": river["service_evidence_id"],
        "river_decision_id": river["decision_id"],
        "river_trend_direction": river["trend_direction"],
        "rate_of_change_m_per_hour": river["rate_of_change_m_per_hour"],
        "threshold_status": river["threshold_status"],
        "threshold_evaluation": river["threshold_evaluation"],
        "last_known_level_m": river["last_known_level_m"],
        "simulation_evidence_eligible": river["simulation_evidence_eligible"],
    }

    # 3-4. Cross-agent checks and observations
    checks = cross_agent_checks(weather, river_summary, station, settings)
    observations = hydrological_observations(weather, river_summary)
    combined = combine_assessments(river_assessment, weather, checks)
    audit_log.log(run_id, "cross_agent_assessment",
                  weather_label=weather_label, weather_status=weather["status"],
                  checks=[{k: c[k] for k in ("check", "status", "detail")} for c in checks],
                  combined_eligible=combined["eligible"], combined_reasons=combined["reasons"])

    # 5. Permissions on the combined assessment
    decisions = [authorize_with_evidence_and_audit(disaster_state, cap, agent_id, combined,
                                                   audit_log, run_id) for cap in capabilities]
    pending = [d["capability"] for d in decisions
               if d["allowed"] and d["capability"] in REAL_WORLD_CAPABILITIES]
    authorization = "HUMAN_AUTHORIZATION_REQUIRED" if pending else "RESTRICTED"

    result: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "run_id": run_id,
        "generated_at": clock().astimezone(timezone.utc).isoformat(),
        "reference_time": reference.isoformat(),
        "disaster_state": str(getattr(disaster_state, "value", disaster_state)),
        "river_mode": river_mode,
        "scenario": scenario,
        "weather": weather,
        "river": river_summary,
        "cross_agent_checks": checks,
        "hydrological_observations": observations,
        "combined_assessment": combined,
        "capability_decisions": decisions,
        "real_world_authorization": authorization,
        "real_world_capabilities_pending_human_approval": pending,
        "settings": {k: settings[k] for k in ("max_weather_age_hours", "max_time_skew_hours",
                                              "max_location_distance_km")},
        "reason": ("All river, weather and cross-agent conditions met; real-world capabilities "
                   "may be put to a human for approval." if pending else
                   "Real-world capabilities restricted: " + ", ".join(combined["reasons"])
                   + "."),
    }
    stable = copy.deepcopy({k: v for k, v in result.items()
                            if k not in ("run_id", "generated_at")})
    stable["capability_decisions"] = [{k: v for k, v in d.items() if k != "audit_written"}
                                      for d in decisions]
    stable["river"].pop("river_decision_id", None)
    result["combined_id"] = hashlib.sha256(json.dumps(
        stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    audit_log.log(run_id, "flood_evidence_completed", combined_id=result["combined_id"],
                  real_world_authorization=authorization,
                  combined_reasons=combined["reasons"],
                  capabilities_allowed=[d["capability"] for d in decisions if d["allowed"]])
    result["audit"] = {"log_path": str(audit_log.path) if audit_log.path else None,
                       "events_written": audit_log.events_written,
                       "write_errors": list(audit_log.write_errors)}
    if audit_log.write_errors:
        result["real_world_authorization"] = "RESTRICTED"
        result["real_world_capabilities_pending_human_approval"] = []
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Combined Weather + River evidence workflow.")
    parser.add_argument("--river-mode", choices=MODES, default=MODE_AUTO)
    parser.add_argument("--scenario", choices=SCENARIOS, default=None,
                        help="River simulated mode only (default no-conflict).")
    parser.add_argument("--state", default=DisasterState.NORMAL.value)
    parser.add_argument("--capability", action="append", default=None)
    parser.add_argument("--as-of", default=None,
                        help="Replay time (river historical/simulated; simulated default "
                             "2026-10-10T12:00:00+00:00).")
    parser.add_argument("--weather-file", default=None,
                        help="Use a saved Weather Agent sample instead of a live call.")
    parser.add_argument("--river-config", default=None)
    parser.add_argument("--config", default=None, help="flood_evidence.json settings")
    parser.add_argument("--save", action="store_true", help="Write outputs/flood_evidence.json")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    if args.scenario and args.river_mode != MODE_SIMULATED:
        parser.error("--scenario is only valid with --river-mode simulated")
    as_of_text = args.as_of or ("2026-10-10T12:00:00+00:00"
                                if args.river_mode == MODE_SIMULATED else None)
    as_of = None
    if as_of_text:
        if args.river_mode in (MODE_LIVE, MODE_AUTO):
            parser.error("--as-of cannot be used with live/auto river modes")
        as_of = parse_timestamp(as_of_text)
        if as_of is None:
            parser.error("--as-of must be a timezone-aware ISO 8601 time")
    try:
        settings = load_settings(args.config)
        service = (RiverEvidenceService.from_file(args.river_config) if args.river_config
                   else RiverEvidenceService.from_file())
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    audit = AuditLog(service.resolve_path(settings["audit_log"]), component="flood_evidence")
    result = run_flood_evidence_workflow(
        service, audit, river_mode=args.river_mode, disaster_state=args.state,
        capabilities=tuple(args.capability) if args.capability else DEFAULT_CAPABILITIES,
        as_of=as_of, scenario=args.scenario, weather_file=args.weather_file, settings=settings)
    print(json.dumps(result, indent=2, ensure_ascii=False, default=str))
    if args.save:
        path = service.resolve_path(settings["output"])
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(result, indent=2, ensure_ascii=False, default=str) + "\n",
                        encoding="utf-8")
        print(f"Saved combined evidence to {path}", file=sys.stderr)
    return 2 if result["audit"]["write_errors"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
