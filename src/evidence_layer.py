"""Evidence Layer: structured evidence records, validation and claim traceability.

Builds on top of the existing pipeline (River Agent evidence, Weather Agent
result, cross-source check, reverification, capability decisions, risk). It
re-derives nothing the existing gates decide; it makes every safety-critical
claim traceable to the evidence that supports or contradicts it, and adds one
deny-only safeguard (``real_world_evidence_sufficient``) in front of the
existing human-approval step.

1. Evidence records (``build_records``): one per source used - the primary
   river reading, each comparison source, the weather model result and every
   reverification attempt. Each record carries: evidence_id, kind,
   evidence_class (LIVE_VERIFIED / LIVE_STALE / LIVE_UNVERIFIED /
   HISTORICAL_NOT_CURRENT / SIMULATED / MODEL_ESTIMATE / MODEL_STALE /
   UNAVAILABLE), source, measurement_basis, observed_at, retrieved_at,
   location, value, verification, freshness, quality, uncertainty,
   limitations and validation.
2. Validation (``validate_*``): stale data, missing or invalid values, future
   timestamps, retrieval-before-observation, implausible values, source,
   location and time mismatches. Limits come from config/evidence_layer.json
   (prototype, never official).
3. Claims (``evaluate_claims``): each safety-critical claim lists the records
   that support or contradict it and why it is ACCEPTED, REJECTED,
   INSUFFICIENT, UNRESOLVED_CONFLICT or SIMULATED_ONLY.

Wording rules enforced here: Open-Meteo values are "numerical weather model
output", never sensor readings; a stale CWC reading is "last known, not
current"; independence of comparison sources is stated as configured, not
verified; thresholds not marked official are never called official.
"""
from __future__ import annotations

import hashlib
import json
import math
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.river_monitoring_agent import PROJECT_DIR, load_json_file, parse_timestamp

SCHEMA_VERSION = "1.0"
LAYER_NAME = "EvidenceLayer"
DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "evidence_layer.json"
DEFAULT_SETTINGS = {
    "max_weather_age_hours": 3.0,
    "max_future_skew_minutes": 5.0,
    "max_river_weather_time_offset_hours": 3.0,
    "max_weather_grid_distance_km": 10.0,
    "rainfall_plausible_max_mm_per_hour": 305.0,
    "rainfall_plausible_max_mm_24h": 1825.0,
    "reference": "Prototype assumptions (not approved by any authority).",
}

# Evidence classes (shown on the dashboard).
LIVE_VERIFIED = "LIVE_VERIFIED"
LIVE_STALE = "LIVE_STALE"
LIVE_UNVERIFIED = "LIVE_UNVERIFIED"
HISTORICAL = "HISTORICAL_NOT_CURRENT"
SIMULATED = "SIMULATED"
MODEL_ESTIMATE = "MODEL_ESTIMATE"
MODEL_STALE = "MODEL_STALE"
UNAVAILABLE = "UNAVAILABLE"

# Record validation statuses.
VALID, DEGRADED, INVALID = "VALID", "DEGRADED", "INVALID"
# Claim statuses.
ACCEPTED = "ACCEPTED"
REJECTED = "REJECTED"
INSUFFICIENT = "INSUFFICIENT"
UNRESOLVED_CONFLICT = "UNRESOLVED_CONFLICT"
SIMULATED_ONLY = "SIMULATED_ONLY"

# Validation codes that make a reading untrustworthy (REJECTED) rather than
# merely not current or missing (INSUFFICIENT).
REJECTING_CODES = frozenset({"FUTURE_TIMESTAMP", "RETRIEVED_BEFORE_OBSERVED", "INVALID_VALUE",
                             "IMPLAUSIBLE_VALUE", "SOURCE_MISMATCH", "UNVERIFIED_SOURCE",
                             "MODE_MISMATCH", "LOCATION_MISMATCH"})
WEATHER_SIMULATED = "SIMULATED_TEST_DATA"
RIVER_SIMULATED = "SIMULATED_TEST_DATA"

DISCLAIMER = ("Evidence traceability for decision support. Validation limits are prototype "
              "settings, not official CWC/IMD thresholds. Open-Meteo values are numerical "
              "weather model output, not sensor readings. This layer can only deny; it never "
              "authorizes or executes an action.")


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def load_settings(path: str | Path | None = None) -> dict[str, Any]:
    settings = dict(DEFAULT_SETTINGS)
    path = Path(path) if path else DEFAULT_CONFIG_PATH
    if path.is_file():
        data = load_json_file(path, "Evidence layer config")
        if not isinstance(data, dict):
            raise ValueError("Evidence layer config must be a JSON object.")
        settings.update({k: v for k, v in data.items() if not k.startswith("_")})
    for key, value in settings.items():
        if key == "reference":
            continue
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise ValueError(f"evidence_layer.{key} must be a positive number")
    return settings


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _iso(moment: datetime | None) -> str | None:
    return moment.astimezone(timezone.utc).isoformat() if moment else None


def record_id(kind: str, content: dict[str, Any]) -> str:
    """Deterministic ID for records without a source-provided ID."""
    canonical = json.dumps({"kind": kind, **content}, sort_keys=True, default=str)
    return "ev-" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:24]


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    a = (math.sin((p2 - p1) / 2) ** 2
         + math.cos(p1) * math.cos(p2) * math.sin(math.radians(lon2 - lon1) / 2) ** 2)
    return 2 * 6371.0088 * math.asin(math.sqrt(a))


def _finding(code: str, severity: str, detail: str) -> dict[str, str]:
    return {"code": code, "severity": severity, "detail": detail}


def _validation(findings: list[dict[str, str]]) -> dict[str, Any]:
    status = (INVALID if any(f["severity"] == "error" for f in findings) else
              DEGRADED if any(f["severity"] == "warning" for f in findings) else VALID)
    return {"status": status, "findings": findings,
            "error_codes": [f["code"] for f in findings if f["severity"] == "error"]}


def _check_times(findings: list, observed: datetime | None, retrieved: datetime | None,
                 reference: datetime, skew: timedelta) -> None:
    if observed is not None and observed > reference + skew:
        findings.append(_finding("FUTURE_TIMESTAMP", "error",
                                 f"Observation time {_iso(observed)} is after the decision "
                                 f"time {_iso(reference)}."))
    if retrieved is not None and retrieved > reference + skew:
        findings.append(_finding("FUTURE_TIMESTAMP", "error",
                                 f"Retrieval time {_iso(retrieved)} is after the decision "
                                 f"time {_iso(reference)}."))
    if observed is not None and retrieved is not None and retrieved + skew < observed:
        findings.append(_finding("RETRIEVED_BEFORE_OBSERVED", "error",
                                 "Retrieval time is earlier than the observation time."))


# --------------------------------------------------------------------------
# River records
# --------------------------------------------------------------------------

def river_record(evidence: Any, station_cfg: dict[str, Any], simulated_station: str | None,
                 reference: datetime, settings: dict[str, Any]) -> dict[str, Any]:
    """Structured, validated record for the primary river reading."""
    ev = _obj(evidence)
    quality = _obj(ev.get("data_quality"))
    fresh = _obj(ev.get("freshness"))
    label = ev.get("source_label")
    level = ev.get("river_level_m")
    observed = parse_timestamp(ev.get("observed_at")) if ev.get("observed_at") else None
    retrieved = parse_timestamp(ev.get("retrieved_at")) if ev.get("retrieved_at") else None
    simulated = (label == RIVER_SIMULATED or quality.get("evidence_grade") == "SIMULATED"
                 or quality.get("is_simulated") is True)
    verification = _obj(quality.get("source_verification"))
    meta = _obj(ev.get("station_metadata"))
    station_id = ev.get("station_id")

    if not ev or ev.get("status") == "DATA_UNAVAILABLE" or level is None:
        klass = UNAVAILABLE
    elif simulated:
        klass = SIMULATED
    elif label == "LIVE_VERIFIED":
        klass = LIVE_VERIFIED if quality.get("is_verified_live") is True else LIVE_STALE
    elif label == "LIVE_UNVERIFIED":
        klass = LIVE_UNVERIFIED
    else:
        klass = HISTORICAL

    findings: list[dict[str, str]] = []
    skew = timedelta(minutes=settings["max_future_skew_minutes"])
    if klass == UNAVAILABLE:
        errors = [e.get("code") for e in ev.get("errors") or [] if isinstance(e, dict)]
        findings.append(_finding("MISSING_VALUE", "error",
                                 "No usable river reading" + (f" ({', '.join(map(str, errors[:3]))})"
                                                              if errors else "") + "."))
    else:
        value = _num(level)
        if value is None or not math.isfinite(value):
            findings.append(_finding("INVALID_VALUE", "error", f"River level {level!r} is not "
                                                                "a finite number."))
        qc = _obj(quality.get("qc"))
        low, high = _num(qc.get("valid_min_m")), _num(qc.get("valid_max_m"))
        if value is not None and low is not None and high is not None:
            if not low <= value <= high:
                findings.append(_finding("IMPLAUSIBLE_VALUE", "error",
                                         f"{value} m is outside the station's QC range "
                                         f"{low}-{high} m (prototype range)."))
        else:
            findings.append(_finding("PLAUSIBILITY_NOT_CHECKED", "info",
                                     "No QC range configured for this station."))
        _check_times(findings, observed, retrieved, reference, skew)
        if fresh.get("status") != "FRESH":
            findings.append(_finding("STALE", "error",
                                     f"Last reading is {fresh.get('age_hours')} h old "
                                     f"(limit {fresh.get('max_age_hours')} h): it describes "
                                     "past conditions, not current conditions."))
        expected = {station_cfg.get("station_id"), simulated_station} - {None}
        if station_id not in expected:
            findings.append(_finding("SOURCE_MISMATCH", "error",
                                     f"Station {station_id!r} is not the configured station "
                                     f"({', '.join(sorted(map(str, expected)))})."))
        if klass in (LIVE_UNVERIFIED,) or (label and label.startswith("LIVE")
                                          and verification.get("status") != "VERIFIED"):
            findings.append(_finding("UNVERIFIED_SOURCE", "error",
                                     "Live-source verification did not pass: "
                                     + ", ".join(verification.get("failed_checks") or
                                                 [str(verification.get("status"))]) + "."))
        if klass == HISTORICAL:
            findings.append(_finding("NOT_LIVE", "warning",
                                     "Historical CSV data (or fallback), not a live reading."))
        if klass == SIMULATED:
            findings.append(_finding("SIMULATED_DATA", "info",
                                     "Fabricated test data; never real conditions."))
        recent = _obj(quality.get("recent_quality"))
        if recent.get("status") not in (None, "ACCEPTABLE"):
            findings.append(_finding("QUALITY_" + str(recent.get("status")), "warning",
                                     str(recent.get("reason") or "Recent data quality is not "
                                                                  "acceptable.")))
        if _obj(recent.get("completeness")).get("status") == "INCOMPLETE":
            findings.append(_finding("INCOMPLETE_RECENT_DATA", "warning",
                                     str(_obj(recent.get("completeness")).get("reason"))))

    uncertainty = []
    ts = _obj(meta.get("timestamp_interpretation"))
    if ts.get("status") == "ASSUMED" or station_cfg.get("timestamp_offset_status") == "ASSUMED":
        uncertainty.append("Provider timestamps carry no timezone; IST (+05:30) is assumed.")
    if station_cfg.get("data_datum_status") == "ASSUMED":
        uncertainty.append("Datum assumed to be reduced level; not stated by the provider.")
    if _obj(quality.get("qc")).get("configured"):
        uncertainty.append("QC bounds are prototype values derived from one data file.")
    trend = _obj(ev.get("trend"))
    if trend.get("method"):
        uncertainty.append(f"Trend: {trend.get('method')}")

    limitations = {
        LIVE_VERIFIED: "Verified live CWC telemetry via NWDP; values still pass through QC.",
        LIVE_STALE: "Verified CWC source, but the reading is old: last known, NOT current.",
        LIVE_UNVERIFIED: "Live data that failed source verification; not trusted.",
        HISTORICAL: "Historical CWC telemetry (or fallback): not current conditions.",
        SIMULATED: "Fabricated test data: demonstrates logic only, never real conditions.",
        UNAVAILABLE: "No river reading could be obtained.",
    }[klass]
    live_fetch = _obj(ev.get("live_fetch"))
    resource = _obj(verification.get("resource"))
    source = {
        "label": label, "name": ev.get("source"),
        "provider": verification.get("provider") or live_fetch.get("provider"),
        "reference": resource.get("id") or Path(str(_obj(ev.get("input_file")).get("path")
                                                    or "")).name or None,
        "content_sha256": (live_fetch.get("snapshot_sha256")
                           or _obj(ev.get("input_file")).get("sha256")),
        "independence": "PRIMARY_SOURCE",
    }
    lat, lon = station_cfg.get("latitude"), station_cfg.get("longitude")
    location = ({"station_id": station_id, "latitude": lat, "longitude": lon,
                 "basis": "configured station coordinates"}
                if station_id == station_cfg.get("station_id") else
                {"station_id": station_id, "latitude": None, "longitude": None,
                 "basis": "simulated test station (no real coordinates)"})
    return {
        "evidence_id": ev.get("evidence_id") or record_id("RIVER_LEVEL", {"source": source}),
        "kind": "RIVER_LEVEL",
        "evidence_class": klass,
        "measurement_basis": ("FABRICATED_TEST_DATA" if simulated else
                              "NONE" if klass == UNAVAILABLE else "CWC_TELEMETRY_SENSOR"),
        "source": source,
        "observed_at": _iso(observed),
        "retrieved_at": _iso(retrieved),
        "retrieval_note": (None if retrieved else
                           "File source: no retrieval time; read for this decision."),
        "location": location,
        "value": {"river_level_m": level, "unit": "m",
                  "datum": station_cfg.get("data_datum"),
                  "trend_direction": trend.get("direction"),
                  "rate_of_change_m_per_hour": trend.get("rate_of_change_m_per_hour"),
                  "trend_status": trend.get("status")},
        "verification": {"status": verification.get("status") or "NOT_APPLICABLE",
                         "is_verified_live": quality.get("is_verified_live") is True,
                         "failed_checks": verification.get("failed_checks") or []},
        "freshness": {k: fresh.get(k) for k in ("status", "age_hours", "max_age_hours",
                                               "reference_time")},
        "quality": {"evidence_grade": quality.get("evidence_grade"),
                    "recent_quality": _obj(quality.get("recent_quality")).get("status"),
                    "completeness": _obj(_obj(quality.get("recent_quality")).get(
                        "completeness")).get("status"),
                    "qc_accepted": _obj(quality.get("qc")).get("accepted"),
                    "qc_checked": _obj(quality.get("qc")).get("checked")},
        "uncertainty": uncertainty,
        "limitations": [limitations],
        "validation": _validation(findings),
    }


def comparison_records(evidence: Any, reference: datetime,
                       simulated: bool) -> list[dict[str, Any]]:
    """One record per comparison source in the service's cross-source check."""
    cross = _obj(_obj(evidence).get("cross_source_check"))
    details = [d for d in cross.get("conflict_details") or cross.get("examples") or []
               if isinstance(d, dict)]
    records = []
    for source in cross.get("compared_with") or []:
        conflicts = [d for d in details if d.get("other_source") == source]
        name = Path(str(source)).name
        content = {"source": name, "conflicts": [(d.get("observed_at"), d.get("other_m"))
                                                 for d in conflicts]}
        latest = max((parse_timestamp(d["observed_at"]) for d in conflicts
                      if d.get("observed_at")), default=None)
        records.append({
            "evidence_id": record_id("RIVER_LEVEL_COMPARISON", content),
            "kind": "RIVER_LEVEL_COMPARISON",
            "evidence_class": SIMULATED if simulated else HISTORICAL,
            "measurement_basis": "FABRICATED_TEST_DATA" if simulated else "CONFIGURED_FILE",
            "source": {"label": "COMPARISON_SOURCE", "name": name, "reference": name,
                       "independence": "CONFIGURED_NOT_VERIFIED"},
            "observed_at": _iso(latest),
            "retrieved_at": None,
            "retrieval_note": "File source: read for this decision.",
            "location": {"station_id": _obj(evidence).get("station_id")},
            "value": {"conflicting_readings": [
                {"observed_at": d.get("observed_at"), "other_m": d.get("other_m"),
                 "primary_m": d.get("primary_m"), "difference_m": d.get("difference_m"),
                 "affects_current_evidence": bool(d.get("affects_current_evidence"))}
                for d in conflicts],
                "overlapping_timestamps_all_sources": cross.get("overlapping_timestamps"),
                "tolerance_m": cross.get("tolerance_m")},
            "verification": {"status": "NOT_VERIFIED",
                             "detail": "Independence is asserted by configuration only."},
            "freshness": {"status": "NOT_ASSESSED"},
            "quality": {},
            "uncertainty": ["Source independence is configured, not verified."],
            "limitations": ["Used only to detect disagreement with the primary reading."],
            "validation": _validation([]),
            "contradicts_current_reading": any(d.get("affects_current_evidence")
                                               for d in conflicts),
        })
    return records


def live_attempt_record(evidence: Any) -> dict[str, Any] | None:
    """A failed or disabled live-API attempt, kept as UNAVAILABLE evidence."""
    live = _obj(_obj(evidence).get("live_fetch"))
    if not live.get("attempted") and live.get("status") != "DISABLED":
        return None
    if live.get("status") == "FETCHED":
        return None  # the fetched snapshot is the primary river record
    attempts = [a for a in live.get("attempts") or [] if isinstance(a, dict)]
    content = {"error": live.get("error"), "attempts": [(a.get("action"), a.get("started_at"),
                                                         a.get("outcome")) for a in attempts]}
    return {
        "evidence_id": record_id("LIVE_SOURCE_ATTEMPT", content),
        "kind": "LIVE_SOURCE_ATTEMPT",
        "evidence_class": UNAVAILABLE,
        "measurement_basis": "NONE",
        "source": {"label": "LIVE_CWC_NWDP", "name": live.get("provider"),
                   "independence": "PRIMARY_SOURCE"},
        "observed_at": None,
        "retrieved_at": attempts[-1].get("started_at") if attempts else None,
        "location": {},
        "value": {"status": live.get("status"), "error": live.get("error"),
                  "http_attempts": len(attempts)},
        "verification": {"status": "NOT_APPLICABLE"},
        "freshness": {"status": "UNAVAILABLE"},
        "quality": {},
        "uncertainty": [],
        "limitations": ["The live source could not be used; no value is substituted."],
        "validation": _validation([_finding("SOURCE_UNAVAILABLE", "error",
                                            str(live.get("error") or live.get("status")))]),
    }


def reverification_records(reverification: Any) -> list[dict[str, Any]]:
    rev = _obj(reverification)
    records = []
    for attempt in rev.get("attempt_log") or []:
        if not isinstance(attempt, dict):
            continue
        records.append({
            "evidence_id": record_id("REVERIFICATION_ATTEMPT", attempt),
            "kind": "REVERIFICATION_ATTEMPT",
            "evidence_class": "RECHECK",
            "measurement_basis": "RECHECK_OF_PRIMARY_AND_INDEPENDENT_SOURCES",
            "source": {"label": "REVERIFICATION", "name": "Re-check of primary source",
                       "independence": "SEE independent_sources"},
            "observed_at": None,
            "retrieved_at": attempt.get("started_at"),
            "location": {},
            "value": {k: attempt.get(k) for k in ("attempt", "outcome", "fresh_evidence_id",
                                                  "independent_sources", "independent_errors",
                                                  "conflicts_remaining", "error")},
            "verification": {"status": "NOT_APPLICABLE"},
            "freshness": {"status": "NOT_ASSESSED"},
            "quality": {},
            "uncertainty": [],
            "limitations": ["Records the attempt; resolution requires an independent "
                            "source to agree with the primary reading."],
            "validation": _validation([]),
        })
    return records


# --------------------------------------------------------------------------
# Weather record
# --------------------------------------------------------------------------

def weather_record(weather: Any, mode_simulated: bool, river_observed: datetime | None,
                   station_cfg: dict[str, Any], reference: datetime,
                   settings: dict[str, Any]) -> dict[str, Any] | None:
    if weather is None:
        return None
    w = _obj(weather)
    current = _obj(w.get("current_weather"))
    observed = parse_timestamp(current.get("time")) if current.get("time") else None
    retrieved = parse_timestamp(w.get("retrieved_at")) if w.get("retrieved_at") else None
    simulated = w.get("source_type") == WEATHER_SIMULATED
    findings: list[dict[str, str]] = []
    skew = timedelta(minutes=settings["max_future_skew_minutes"])
    available = bool(w) and w.get("status") != "DATA_UNAVAILABLE" and observed is not None
    age = round((reference - observed).total_seconds() / 3600, 3) if observed else None

    if not available:
        codes = ", ".join(str(e.get("code")) for e in w.get("errors") or []
                          if isinstance(e, dict))
        findings.append(_finding("MISSING_VALUE", "error",
                                 f"Weather evidence unavailable: {codes or 'no current time'}."))
    else:
        _check_times(findings, observed, retrieved, reference, skew)
        if age is not None and age > settings["max_weather_age_hours"]:
            findings.append(_finding("STALE", "error",
                                     f"Model current conditions are {age} h old (limit "
                                     f"{settings['max_weather_age_hours']} h)."))
        if simulated != mode_simulated:
            findings.append(_finding("MODE_MISMATCH", "error",
                                     "Simulated and real evidence must not be combined."))
        if river_observed is not None and observed is not None:
            offset = abs((river_observed - observed).total_seconds()) / 3600
            if offset > settings["max_river_weather_time_offset_hours"]:
                findings.append(_finding("TIME_MISMATCH", "warning",
                                         f"Weather time and river observation are "
                                         f"{round(offset, 2)} h apart (limit "
                                         f"{settings['max_river_weather_time_offset_hours']} h)."))
    grid = _obj(w.get("returned_grid_location"))
    distance = None
    if None not in (_num(grid.get("latitude")), _num(grid.get("longitude")),
                    station_cfg.get("latitude"), station_cfg.get("longitude")):
        distance = round(haversine_km(grid["latitude"], grid["longitude"],
                                      station_cfg["latitude"], station_cfg["longitude"]), 2)
        if distance > settings["max_weather_grid_distance_km"]:
            findings.append(_finding("LOCATION_MISMATCH", "error",
                                     f"Model grid cell is {distance} km from the station "
                                     f"(limit {settings['max_weather_grid_distance_km']} km)."))
    rain = {}
    for key, entry in _obj(_obj(w.get("rainfall_accumulation")).get("windows")).items():
        precip = _obj(_obj(entry).get("precipitation"))
        total = precip.get("total")
        hours = _num(_obj(entry).get("hours")) or _num(str(key).rstrip("h") if str(key).rstrip(
            "h").isdigit() else None)
        rain[key] = {"status": precip.get("status"), "total_mm": total,
                     "reason": precip.get("reason")}
        if precip.get("status") == "AVAILABLE":
            value = _num(total)
            if value is None or not math.isfinite(value) or value < 0:
                findings.append(_finding("INVALID_VALUE", "error",
                                         f"{key} rainfall total {total!r} is not a valid "
                                         "non-negative number."))
            elif hours:
                cap = min(settings["rainfall_plausible_max_mm_per_hour"] * hours,
                          settings["rainfall_plausible_max_mm_24h"] * max(1.0, hours / 24))
                if value > cap:
                    findings.append(_finding("IMPLAUSIBLE_VALUE", "error",
                                             f"{key} rainfall {value} mm exceeds the physical "
                                             f"plausibility cap {cap} mm."))
    klass = (UNAVAILABLE if not available else SIMULATED if simulated else
             MODEL_STALE if any(f["code"] == "STALE" for f in findings) else MODEL_ESTIMATE)
    return {
        "evidence_id": record_id("WEATHER_MODEL", {"retrieved_at": w.get("retrieved_at"),
                                                   "current_time": current.get("time"),
                                                   "source": w.get("source")}),
        "kind": "WEATHER_MODEL",
        "evidence_class": klass,
        "measurement_basis": ("FABRICATED_TEST_DATA" if simulated else
                              "NUMERICAL_WEATHER_MODEL (not a rain-gauge or sensor reading)"),
        "source": {"label": w.get("source_type"), "name": w.get("source"),
                   "reference": w.get("source_url"), "independence": "SEPARATE_PROVIDER"},
        "observed_at": _iso(observed),
        "retrieved_at": _iso(retrieved),
        "location": {"requested": _obj(w.get("requested_location")),
                     "model_grid": {"latitude": grid.get("latitude"),
                                    "longitude": grid.get("longitude")},
                     "distance_to_station_km": distance},
        "value": {"rainfall_mm": rain, "current_values": _obj(current.get("values"))},
        "verification": {"status": "NOT_VERIFIABLE",
                         "detail": "Model output cannot be verified as a measurement."},
        "freshness": {"status": "UNAVAILABLE" if not available else
                      "STALE" if klass == MODEL_STALE else "FRESH",
                      "age_hours": age, "max_age_hours": settings["max_weather_age_hours"]},
        "quality": {"status": w.get("status"),
                    "errors": [e.get("code") for e in w.get("errors") or []
                               if isinstance(e, dict)]},
        "uncertainty": ["Numerical weather model output; may differ from rain on the ground.",
                        "Model grid cell, not the station location."],
        "limitations": ["Local modelled rain is a weak indicator at Upper Anicut, where "
                        "upstream rain and dam releases dominate."],
        "validation": _validation(findings),
    }


# --------------------------------------------------------------------------
# Claims
# --------------------------------------------------------------------------

def _claim(claim_id: str, statement: str, status: str, supporting: list[str],
           contradicting: list[str], explanation: list[str],
           value: Any = None) -> dict[str, Any]:
    return {"claim_id": claim_id, "statement": statement, "status": status, "value": value,
            "safety_critical": True, "supporting_evidence_ids": supporting,
            "contradicting_evidence_ids": contradicting, "explanation": explanation}


def _status_from_record(record: dict[str, Any] | None, what: str) -> tuple[str, list[str]]:
    if record is None or record["evidence_class"] == UNAVAILABLE:
        return INSUFFICIENT, [f"No {what} evidence is available."]
    codes = record["validation"]["error_codes"]
    rejecting = [c for c in codes if c in REJECTING_CODES]
    if rejecting:
        return REJECTED, [f"{what.capitalize()} evidence rejected: "
                          + "; ".join(f["detail"] for f in record["validation"]["findings"]
                                      if f["code"] in rejecting)]
    if record["evidence_class"] == SIMULATED:
        return SIMULATED_ONLY, ["Simulated test data: valid only inside the simulation."]
    if codes:
        return INSUFFICIENT, ["; ".join(f["detail"] for f in record["validation"]["findings"]
                                        if f["severity"] == "error")]
    if record["evidence_class"] == HISTORICAL:
        return INSUFFICIENT, ["Historical data replay: describes the past, not current "
                              "conditions."]
    return ACCEPTED, [f"{what.capitalize()} evidence passed every validation check."]


def evaluate_claims(river: dict[str, Any] | None, comparisons: list[dict[str, Any]],
                    weather: dict[str, Any] | None, reverification: Any,
                    gate: dict[str, Any], risk: Any, evidence: Any) -> list[dict[str, Any]]:
    rev = _obj(reverification)
    claims = []
    river_ids = [river["evidence_id"]] if river else []
    contradicting = [c["evidence_id"] for c in comparisons if c.get("contradicts_current_reading")]
    resolved = rev.get("outcome") == "RESOLVED" and not gate.get("reverification_problem")

    status, why = _status_from_record(river, "river")
    if contradicting and status in (ACCEPTED, SIMULATED_ONLY):
        if resolved:
            why.append(f"{len(contradicting)} contradicting source(s) were re-checked and an "
                       "independent source supported the primary reading.")
        else:
            status = UNRESOLVED_CONFLICT
            why.append(f"Contradicted by {len(contradicting)} source(s); reverification "
                       f"outcome {rev.get('outcome')}: uncertainty preserved.")
    elif contradicting:
        why.append(f"Also contradicted by {len(contradicting)} source(s).")
    level = river["value"]["river_level_m"] if river else None
    when = river["observed_at"] if river else None
    claims.append(_claim(
        "C1_RIVER_CURRENT_LEVEL",
        f"The current river level at the station is {level} m (reading observed {when}).",
        status,
        river_ids if status in (ACCEPTED, SIMULATED_ONLY) else [],
        contradicting + (river_ids if status in (REJECTED,) else []), why, level))
    c1 = status

    trend = (river or {}).get("value", {})
    if c1 not in (ACCEPTED, SIMULATED_ONLY):
        t_status, t_why = c1, [f"Depends on C1 ({c1})."]
    elif trend.get("trend_status") != "AVAILABLE":
        t_status, t_why = INSUFFICIENT, ["Not enough readings in the trend window."]
    else:
        t_status, t_why = c1, ["Derived from the accepted readings in the trend window."]
    claims.append(_claim("C2_RIVER_TREND",
                         f"The river is {trend.get('trend_direction')} at "
                         f"{trend.get('rate_of_change_m_per_hour')} m/h.", t_status,
                         river_ids if t_status in (ACCEPTED, SIMULATED_ONLY) else [], [], t_why,
                         trend.get("trend_direction")))

    thr = _obj(_obj(evidence).get("threshold_evaluation"))
    t_level = _obj(evidence).get("threshold_status", "UNKNOWN")
    if t_level == "UNKNOWN" or thr.get("warning_level_m") is None:
        th_status = INSUFFICIENT
        th_why = ["No warning/danger thresholds configured; none are assumed or invented."]
    else:
        th_status = c1 if c1 != ACCEPTED else ACCEPTED
        th_why = [f"Compared with warning {thr.get('warning_level_m')} m / danger "
                  f"{thr.get('danger_level_m')} m."]
        th_why.append("Thresholds are marked OFFICIAL (reference: "
                      f"{thr.get('threshold_reference')})." if thr.get("threshold_is_official")
                      else "Thresholds are prototype/test values, NOT official CWC thresholds.")
        if c1 != ACCEPTED:
            th_why.append(f"Depends on C1 ({c1}).")
    claims.append(_claim("C3_RIVER_THRESHOLD_STATUS",
                         f"The river level is {t_level} relative to the configured thresholds.",
                         th_status, river_ids if th_status in (ACCEPTED, SIMULATED_ONLY) else [],
                         [], th_why, t_level))

    w_status, w_why = _status_from_record(weather, "weather")
    rain = ((weather or {}).get("value") or {}).get("rainfall_mm", {}).get("24h", {})
    if w_status == ACCEPTED and rain.get("status") != "AVAILABLE":
        w_status, w_why = INSUFFICIENT, [f"24 h modelled total unavailable: {rain.get('reason')}"]
    if w_status == ACCEPTED:
        w_why.append("Numerical weather model output (Open-Meteo), not a rain-gauge reading.")
    claims.append(_claim("C4_MODELLED_RAINFALL_24H",
                         f"Modelled rainfall over the last 24 h is {rain.get('total_mm')} mm "
                         "(numerical weather model, not a sensor reading).", w_status,
                         [weather["evidence_id"]] if weather and w_status in (ACCEPTED,
                                                                              SIMULATED_ONLY)
                         else [],
                         [weather["evidence_id"]] if weather and w_status == REJECTED else [],
                         w_why, rain.get("total_mm")))

    r = _obj(risk)
    level_r = r.get("risk_level")
    if level_r in (None, "INDETERMINATE"):
        r_status = INSUFFICIENT
        r_why = ["The risk engine could not make a defensible assessment."]
    elif c1 == SIMULATED_ONLY:
        r_status, r_why = SIMULATED_ONLY, ["Assessed on simulated evidence."]
    elif c1 != ACCEPTED:
        r_status, r_why = INSUFFICIENT, [f"River evidence is {c1}; the risk level is not a "
                                         "statement about current conditions."]
    else:
        r_status, r_why = ACCEPTED, ["Assessed on accepted current river evidence."]
    r_why.append("Rules fired: " + (", ".join(map(str, r.get("rules_fired") or [])) or "none"))
    claims.append(_claim("C5_FLOOD_RISK_LEVEL", f"The assessed flood risk is {level_r}.",
                         r_status, river_ids + ([weather["evidence_id"]] if weather and
                                                w_status == ACCEPTED else [])
                         if r_status in (ACCEPTED, SIMULATED_ONLY) else [], [], r_why, level_r))

    gate_ok = gate.get("eligible") is True
    reasons = list(gate.get("reasons") or [])
    if gate_ok and c1 == ACCEPTED:
        a_status, a_why = ACCEPTED, ["Capability evidence gate eligible and C1 accepted; "
                                     "real-world capabilities may only be put to a human."]
    else:
        a_status = (UNRESOLVED_CONFLICT if c1 == UNRESOLVED_CONFLICT else
                    REJECTED if c1 == REJECTED else INSUFFICIENT)
        a_why = ([f"Evidence gate: {', '.join(reasons)}."] if reasons else []) + \
                [f"C1_RIVER_CURRENT_LEVEL is {c1}."]
    claims.append(_claim("C6_EVIDENCE_SUFFICIENT_FOR_REAL_WORLD_ACTION",
                         "The evidence is sufficient to put real-world capabilities to a human "
                         "for approval.", a_status,
                         river_ids if a_status == ACCEPTED else [],
                         contradicting if a_status == UNRESOLVED_CONFLICT else [], a_why,
                         a_status == ACCEPTED))
    return claims


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_evidence_layer(evidence: Any, weather: Any, reverification: Any,
                         evidence_assessment: dict[str, Any], risk: Any,
                         station_cfg: dict[str, Any], simulated_station: str | None,
                         decision_time: datetime, audit_log: Any, run_id: str,
                         settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Records + validation + claims, audited. Never raises for bad evidence."""
    settings = settings or load_settings()
    fresh_ref = parse_timestamp(_obj(_obj(evidence).get("freshness")).get("reference_time")
                                or "") if _obj(evidence).get("freshness") else None
    reference = fresh_ref or decision_time
    simulated = _obj(evidence).get("source_label") == RIVER_SIMULATED
    river = river_record(evidence, station_cfg, simulated_station, reference, settings) \
        if evidence is not None else None
    comparisons = comparison_records(evidence, reference, simulated)
    river_observed = parse_timestamp(river["observed_at"]) if river and river["observed_at"] \
        else None
    weather_rec = weather_record(weather, simulated, river_observed, station_cfg, reference,
                                 settings)
    rechecks = reverification_records(reverification)
    records = [r for r in [river, live_attempt_record(evidence), weather_rec] if r] \
        + comparisons + rechecks
    claims = evaluate_claims(river, comparisons, weather_rec, reverification,
                             evidence_assessment, risk, evidence)

    audit_ok = True
    for record in records:
        audit_ok &= bool(audit_log.log(
            run_id, "evidence_record_acquired", evidence_id=record["evidence_id"],
            kind=record["kind"], evidence_class=record["evidence_class"],
            source_label=record["source"].get("label"), observed_at=record["observed_at"],
            retrieved_at=record["retrieved_at"]))
        audit_ok &= bool(audit_log.log(
            run_id, "evidence_validated", evidence_id=record["evidence_id"],
            status=record["validation"]["status"],
            findings=[f["code"] for f in record["validation"]["findings"]]))
    for claim in claims:
        audit_ok &= bool(audit_log.log(
            run_id, "claim_evaluated", claim_id=claim["claim_id"], status=claim["status"],
            supporting=claim["supporting_evidence_ids"],
            contradicting=claim["contradicting_evidence_ids"],
            explanation=claim["explanation"]))
    sufficiency = next(c for c in claims
                       if c["claim_id"] == "C6_EVIDENCE_SUFFICIENT_FOR_REAL_WORLD_ACTION")
    deny = [] if sufficiency["status"] == ACCEPTED else [f"C6_{sufficiency['status']}"]
    if not audit_ok:
        deny.append("EVIDENCE_AUDIT_INCOMPLETE")
    layer = {
        "schema_version": SCHEMA_VERSION, "layer": LAYER_NAME,
        "reference_time": _iso(reference),
        "settings": {k: settings[k] for k in sorted(settings)},
        "records": records, "claims": claims,
        "summary": {
            "records_by_class": {k: sum(r["evidence_class"] == k for r in records)
                                 for k in sorted({r["evidence_class"] for r in records})},
            "invalid_records": [r["evidence_id"] for r in records
                                if r["validation"]["status"] == INVALID],
            "claims_by_status": {k: sum(c["status"] == k for c in claims)
                                 for k in sorted({c["status"] for c in claims})},
        },
        "real_world_evidence_sufficient": not deny,
        "deny_reasons": deny,
        "audit_complete": audit_ok,
        "disclaimer": DISCLAIMER,
    }
    if not audit_log.log(run_id, "evidence_layer_completed",
                         real_world_evidence_sufficient=layer["real_world_evidence_sufficient"],
                         deny_reasons=deny, claims=layer["summary"]["claims_by_status"],
                         records=layer["summary"]["records_by_class"]):
        layer["audit_complete"] = False
        layer["real_world_evidence_sufficient"] = False
        layer["deny_reasons"] = deny + ["EVIDENCE_AUDIT_INCOMPLETE"] * (
            "EVIDENCE_AUDIT_INCOMPLETE" not in deny)
    return layer
