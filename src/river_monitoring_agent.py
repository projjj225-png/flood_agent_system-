"""River Monitoring Agent for the flood disaster management prototype.

Reads historical or simulated river-level observations from a CSV file for
the Cauvery River near Upper Anicut, Tamil Nadu, validates them, and returns a
structured evidence record: the latest observed level, a derived trend, data
freshness, data quality and (only if configured) a threshold interpretation.

Design rules (same spirit as the Weather Agent):
- Loading/validation, freshness, trend and threshold evaluation are separate
  functions so each can be tested on its own. None of them use the network.
- Missing or invalid data is reported as unavailable (null + reason). A missing
  level is never replaced with zero, and unavailable evidence is never "safe".
- Simulated or historical data is never labelled as a verified live
  measurement. This version has no verified live source at all.
- No official Upper Anicut warning/danger levels are built in. Thresholds must
  be supplied by the caller; otherwise the threshold status is UNKNOWN.
- This agent reports evidence only. It never assigns a disaster state
  (NORMAL/ALERT/EMERGENCY/RECOVERY); that is a later module's job.
- Every timestamp in the output is timezone-aware ISO 8601 in UTC.

Input CSV schema (header required, extra columns ignored with a warning):
    timestamp,station_id,river_level_m,source_type[,retrieved_at]
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import re
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

# Shared conventions from the Weather Agent, so both agents' outputs line up.
from src.weather_agent import (
    LATITUDE,
    LONGITUDE,
    LOCATION_DESCRIPTION,
    SECTION_AVAILABLE,
    SECTION_UNAVAILABLE,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    TIMEZONE,
    _error,
)

AGENT_NAME = "RiverMonitoringAgent"

REQUIRED_COLUMNS = ("timestamp", "station_id", "river_level_m", "source_type")
OPTIONAL_COLUMNS = ("retrieved_at",)

# Allowed values of the source_type column.
SOURCE_SIMULATED = "simulated"
SOURCE_HISTORICAL = "historical"
SOURCE_LIVE = "live"  # claimed live by whoever wrote the CSV; never verified here
ALLOWED_SOURCE_TYPES = (SOURCE_SIMULATED, SOURCE_HISTORICAL, SOURCE_LIVE)

# Evidence grades for downstream modules. VERIFIED_LIVE is produced only when
# the live connector's verification passed for exactly this input file (hash
# match), every station row is source_type "live", and the reading is FRESH.
GRADE_VERIFIED_LIVE = "VERIFIED_LIVE"
# Source identity verified by the live connector, but the latest reading is
# not fresh: provenance is known, currency is not.
GRADE_VERIFIED_SOURCE_STALE = "VERIFIED_SOURCE_STALE"
GRADE_UNVERIFIED_LIVE = "UNVERIFIED_LIVE"
GRADE_HISTORICAL = "HISTORICAL"
GRADE_SIMULATED = "SIMULATED"
GRADE_MIXED = "MIXED_UNVERIFIED"
GRADE_UNAVAILABLE = "UNAVAILABLE"

FRESHNESS_FRESH = "FRESH"
FRESHNESS_STALE = "STALE"
FRESHNESS_UNAVAILABLE = "UNAVAILABLE"

TREND_RISING = "RISING"
TREND_FALLING = "FALLING"
TREND_NO_CHANGE = "NO_CHANGE"

THRESHOLD_BELOW_WARNING = "BELOW_WARNING"
THRESHOLD_AT_OR_ABOVE_WARNING = "AT_OR_ABOVE_WARNING"
THRESHOLD_AT_OR_ABOVE_DANGER = "AT_OR_ABOVE_DANGER"
THRESHOLD_UNKNOWN = "UNKNOWN"

# Project defaults (configurable). These are prototype policy choices, NOT
# official hydrological standards.
DEFAULT_MAX_AGE_HOURS = 3.0
DEFAULT_TREND_WINDOW_HOURS = 3.0

STATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9_.\-]{1,64}$")
MAX_ROW_ISSUES_REPORTED = 50

# Quality control (QC). Spike check: compare each level with the median of up
# to SPIKE_REFERENCE_COUNT earlier in-range values from the preceding
# SPIKE_REFERENCE_HOURS. Only past values are used, so replay stays causal.
SPIKE_REFERENCE_COUNT = 5
SPIKE_MIN_REFERENCES = 3
SPIKE_REFERENCE_HOURS = 6.0
# Recent-quality check: share of rejected readings in the window before the
# reference time above which the evidence is marked SUSPECT.
DEFAULT_QUALITY_WINDOW_HOURS = 24.0
DEFAULT_SUSPECT_FRACTION = 0.2

# Datums a level or threshold can be expressed in.
DATUM_GAUGE_HEIGHT = "gauge_height"
DATUM_REDUCED_LEVEL = "reduced_level"
ALLOWED_DATUMS = (DATUM_GAUGE_HEIGHT, DATUM_REDUCED_LEVEL)

SOURCE_VERIFIED = "VERIFIED"
SOURCE_VERIFICATION_FAILED = "FAILED"
SOURCE_VERIFICATION_NOT_APPLICABLE = "NOT_APPLICABLE"

QUALITY_ACCEPTABLE = "ACCEPTABLE"
QUALITY_SUSPECT = "SUSPECT"
QUALITY_UNAVAILABLE = "UNAVAILABLE"

DATA_NOTES = [
    "River levels come from a CSV file: historical (e.g. converted CWC telemetry), simulated, "
    "or a snapshot fetched from the live NWDP/CWC API. Only a snapshot that passed the live "
    "connector's verification (see data_quality.source_verification) counts as verified.",
    "Readings rejected by validation or quality control are not used, corrected or replaced; "
    "they are counted in data_quality.",
    "Rows with source_type 'simulated' are fabricated test values and must never "
    "be presented as actual river measurements.",
    "No official Upper Anicut warning or danger levels are built in; threshold "
    "status is UNKNOWN unless thresholds are configured explicitly "
    "(see threshold_evaluation.threshold_is_official).",
    "This agent reports evidence only. It does not assign disaster states and "
    "does not declare an emergency from a river reading.",
    "river_level_m is in metres as reported by the data file; the datum (gauge zero "
    "or reduced level) is not part of the CSV schema; see station_metadata if provided.",
    "location describes the study site; station coordinates are not part of the CSV schema.",
]

# Which output fields are observed, derived or interpreted (for downstream
# modules and for the report).
FIELD_CATEGORIES = {
    "observed": ["observed_at", "retrieved_at", "river_level_m", "source_type", "station_id"],
    "derived": ["level_change_m", "rate_of_change_m_per_hour", "trend", "freshness"],
    "interpretation": ["threshold_status", "threshold_evaluation"],
}

PROJECT_DIR = Path(__file__).resolve().parent.parent
OUTPUT_DIR = PROJECT_DIR / "outputs"
DEFAULT_OUTPUT_PATH = OUTPUT_DIR / "river_evidence.json"


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------

def _iso(moment: datetime | None) -> str | None:
    """UTC ISO 8601 string, or None."""
    return None if moment is None else moment.astimezone(timezone.utc).isoformat()


def parse_timestamp(text: Any) -> datetime | None:
    """Parse a timezone-aware ISO 8601 timestamp and return it in UTC.

    Naive timestamps (no 'Z' or offset) are rejected rather than guessed,
    because IST and UTC differ by 5 h 30 min.
    """
    if not isinstance(text, str) or not text.strip():
        return None
    try:
        parsed = datetime.fromisoformat(text.strip())
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _require_aware(name: str, moment: datetime | None) -> None:
    if moment is not None and moment.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


# --------------------------------------------------------------------------
# Loading and validation (no network)
# --------------------------------------------------------------------------

def validate_row(row: dict[str, Any], row_number: int
                 ) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    """Validate one CSV row. Returns (observation or None, issues).

    row_number is the line number in the file (header = line 1).
    """
    issues: list[dict[str, Any]] = []

    def reject(code: str, field: str, message: str):
        issues.append(_error(code, f"Row {row_number}: {message}", field))
        return None, issues

    # csv.DictReader stores surplus fields under the key None and fills
    # missing trailing fields with None.
    if row.get(None):
        return reject("MALFORMED_ROW", "row", "has more fields than the header.")
    values = {key: (row.get(key).strip() if isinstance(row.get(key), str) else row.get(key))
              for key in REQUIRED_COLUMNS + OPTIONAL_COLUMNS}
    missing = [key for key in REQUIRED_COLUMNS if values[key] is None or values[key] == ""]
    if missing:
        return reject("MISSING_VALUE", ",".join(missing),
                      f"missing value(s) for {', '.join(missing)} (not replaced with defaults).")

    observed_at = parse_timestamp(values["timestamp"])
    if observed_at is None:
        return reject("INVALID_TIMESTAMP", "timestamp",
                      f"timestamp {values['timestamp']!r} is not a timezone-aware ISO 8601 value.")

    station_id = values["station_id"]
    if not STATION_ID_PATTERN.match(station_id):
        return reject("INVALID_STATION_ID", "station_id",
                      f"station_id {station_id!r} is not a valid identifier.")

    try:
        level = float(values["river_level_m"])
    except ValueError:
        return reject("NON_NUMERIC_LEVEL", "river_level_m",
                      f"river_level_m {values['river_level_m']!r} is not a number.")
    if not math.isfinite(level):
        return reject("NON_NUMERIC_LEVEL", "river_level_m",
                      f"river_level_m {values['river_level_m']!r} is not a finite number.")
    # Negative and out-of-range levels are handled by apply_quality_control(),
    # because the valid range depends on the station's datum.

    source_type = values["source_type"].lower()
    if source_type not in ALLOWED_SOURCE_TYPES:
        return reject("INVALID_SOURCE_TYPE", "source_type",
                      f"source_type {values['source_type']!r} is not one of "
                      f"{', '.join(ALLOWED_SOURCE_TYPES)}.")

    retrieved_at = None
    if values["retrieved_at"]:
        retrieved_at = parse_timestamp(values["retrieved_at"])
        if retrieved_at is None:
            return reject("INVALID_TIMESTAMP", "retrieved_at",
                          f"retrieved_at {values['retrieved_at']!r} is not a timezone-aware "
                          "ISO 8601 value.")
        if retrieved_at < observed_at:
            return reject("INVALID_TIMESTAMP", "retrieved_at",
                          "retrieved_at is earlier than the observation timestamp.")

    return {
        "row_number": row_number,
        "observed_at": observed_at,
        "retrieved_at": retrieved_at,
        "station_id": station_id,
        "river_level_m": level,
        "source_type": source_type,
    }, issues


def load_observations(path: str | Path) -> dict[str, Any]:
    """Read and validate a river-level CSV file.

    Never raises for data problems. Returns a dict with:
      observations  valid, de-duplicated observations sorted by time
      errors        file-level or row-level problems (severity "error")
      warnings      non-fatal issues (severity "warning")
      fatal         True if the file as a whole could not be used
      stats         counts for the data_quality section
      sha256        hash of the file bytes (provenance), or None
      file_errors / file_warnings   file-level issues only
      row_issues    [(observed_at or None, issue)] for every row-level issue
                    (uncapped; the agent filters these by reference time)
    """
    path = Path(path)
    out: dict[str, Any] = {
        "observations": [], "errors": [], "warnings": [], "fatal": False, "sha256": None,
        # (station_id, observed_at) of rejected rows whose time and station were
        # readable; used to measure recent data quality.
        "rejected_points": [],
        "file_errors": [], "file_warnings": [], "row_issues": [],
        "stats": {"rows_read": 0, "valid_rows": 0, "rejected_rows": 0,
                  "duplicate_rows_removed": 0, "conflicting_duplicate_rows": 0,
                  "out_of_order_rows": 0, "rejected_rows_without_time": 0},
    }

    def fatal(code: str, message: str, field: str | None = "input_file"):
        out["errors"].append(_error(code, message, field))
        out["file_errors"].append(_error(code, message, field))
        out["fatal"] = True
        return out

    if not path.exists():
        return fatal("FILE_NOT_FOUND", f"Input file not found: {path}")
    if not path.is_file():
        return fatal("FILE_NOT_FOUND", f"Input path is not a file: {path}")
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return fatal("FILE_UNREADABLE", f"Could not read input file: {exc}")
    out["sha256"] = hashlib.sha256(raw).hexdigest()
    try:
        text = raw.decode("utf-8-sig")  # tolerates a BOM written by Excel
    except UnicodeDecodeError as exc:
        return fatal("FILE_UNREADABLE", f"Input file is not valid UTF-8: {exc}")
    if not text.strip():
        return fatal("EMPTY_FILE", "Input file is empty.")

    reader = csv.DictReader(text.splitlines())
    header = [h.strip() if h else h for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing_cols = [c for c in REQUIRED_COLUMNS if c not in header]
    if missing_cols:
        return fatal("MISSING_COLUMNS",
                     f"Required column(s) missing: {', '.join(missing_cols)}. "
                     f"Expected header: {','.join(REQUIRED_COLUMNS)}", "header")
    if len(set(header)) != len(header):
        return fatal("DUPLICATE_COLUMNS", "Header contains duplicate column names.", "header")
    extra = [c for c in header if c not in REQUIRED_COLUMNS + OPTIONAL_COLUMNS]
    if extra:
        out["file_warnings"].append(_error("EXTRA_COLUMNS",
                                           f"Ignoring unexpected column(s): {', '.join(extra)}.",
                                           "header", severity="warning"))

    row_issues: list[tuple[datetime | None, dict[str, Any]]] = []
    valid: list[dict[str, Any]] = []
    previous_time: dict[str, datetime] = {}
    for row in reader:
        # DictReader skips empty lines itself; line_num gives the true file line.
        line_number = reader.line_num
        out["stats"]["rows_read"] += 1
        observation, issues = validate_row(row, line_number)
        if observation is None:
            out["stats"]["rejected_rows"] += 1
            moment = parse_timestamp(row.get("timestamp"))
            station = (row.get("station_id") or "").strip()
            if moment is not None and station:
                out["rejected_points"].append((station, moment))
            else:
                out["stats"]["rejected_rows_without_time"] += 1
            row_issues.extend((moment, issue) for issue in issues)
            continue
        station = observation["station_id"]
        if station in previous_time and observation["observed_at"] < previous_time[station]:
            out["stats"]["out_of_order_rows"] += 1
        previous_time[station] = observation["observed_at"]
        valid.append(observation)

    if out["stats"]["rows_read"] == 0:
        return fatal("NO_OBSERVATIONS", "Input file has a header but no data rows.")

    # Duplicates: same station and timestamp.
    by_key: dict[tuple[str, datetime], list[dict[str, Any]]] = {}
    for obs in valid:
        by_key.setdefault((obs["station_id"], obs["observed_at"]), []).append(obs)
    kept: list[dict[str, Any]] = []
    for (station, moment), group in by_key.items():
        if len(group) == 1:
            kept.append(group[0])
            continue
        distinct = {(o["river_level_m"], o["source_type"]) for o in group}
        rows = ", ".join(str(o["row_number"]) for o in group)
        if len(distinct) == 1:
            # Exact duplicates: keep the first, report the rest.
            kept.append(group[0])
            out["stats"]["duplicate_rows_removed"] += len(group) - 1
            row_issues.append((moment, _error(
                "DUPLICATE_OBSERVATION",
                f"Rows {rows}: identical observations for {station} at {_iso(moment)}; "
                "kept one.", "timestamp", severity="warning")))
        else:
            # Conflicting duplicates: we cannot know which is right, so use none.
            out["stats"]["conflicting_duplicate_rows"] += len(group)
            out["rejected_points"].append((station, moment))
            row_issues.append((moment, _error(
                "CONFLICTING_DUPLICATE",
                f"Rows {rows}: different values for {station} at {_iso(moment)}; "
                "all were rejected.", "river_level_m")))

    kept.sort(key=lambda o: (o["station_id"], o["observed_at"]))
    out["observations"] = kept
    out["stats"]["valid_rows"] = len(kept)
    if out["stats"]["out_of_order_rows"]:
        out["file_warnings"].append(_error(
            "OUT_OF_ORDER_TIMESTAMPS",
            f"{out['stats']['out_of_order_rows']} row(s) were out of time order; "
            "observations were sorted by timestamp.", "timestamp", severity="warning"))

    out["row_issues"] = row_issues
    errors, warnings = split_issues([issue for _, issue in row_issues], "ROW_ISSUES_TRUNCATED",
                                    "row issues", "data_quality")
    out["errors"] = out["file_errors"] + errors
    out["warnings"] = out["file_warnings"] + warnings
    return out


def split_issues(issues: list[dict[str, Any]], truncated_code: str, what: str,
                 totals_in: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split issues by severity, listing at most MAX_ROW_ISSUES_REPORTED of them."""
    errors, warnings = [], []
    for issue in issues[:MAX_ROW_ISSUES_REPORTED]:
        (errors if issue["severity"] == "error" else warnings).append(issue)
    if len(issues) > MAX_ROW_ISSUES_REPORTED:
        warnings.append(_error(
            truncated_code,
            f"Only the first {MAX_ROW_ISSUES_REPORTED} of {len(issues)} {what} are listed; "
            f"see {totals_in} for totals.", None, severity="warning"))
    return errors, warnings


# --------------------------------------------------------------------------
# Freshness, trend and thresholds (pure functions)
# --------------------------------------------------------------------------

def assess_freshness(observed_at: datetime | None, reference_time: datetime,
                     max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                     retrieved_at: datetime | None = None) -> dict[str, Any]:
    """How old the latest observation is at reference_time.

    Age is always measured from the observation timestamp, not from when the
    data was retrieved or the file was read.
    """
    _require_aware("reference_time", reference_time)
    _require_aware("observed_at", observed_at)
    _require_aware("retrieved_at", retrieved_at)
    result: dict[str, Any] = {
        "status": FRESHNESS_UNAVAILABLE,
        "reference_time": _iso(reference_time),
        "max_age_hours": max_age_hours,
        "age_minutes": None,
        "age_hours": None,
        "retrieval_time_available": retrieved_at is not None,
        "retrieval_lag_minutes": None,
        "reason": None,
    }
    if observed_at is None:
        result["reason"] = "No valid observation is available; freshness cannot be assessed."
        return result
    age = reference_time - observed_at
    if age < timedelta(0):
        result["reason"] = "Observation is later than the reference time."
        return result
    result["age_minutes"] = round(age.total_seconds() / 60, 2)
    result["age_hours"] = round(age.total_seconds() / 3600, 4)
    if retrieved_at is not None:
        result["retrieval_lag_minutes"] = round(
            (retrieved_at - observed_at).total_seconds() / 60, 2)
    if age <= timedelta(hours=max_age_hours):
        result["status"] = FRESHNESS_FRESH
    else:
        result["status"] = FRESHNESS_STALE
        result["reason"] = (f"Latest observation is {result['age_hours']} h old, older than "
                            f"the configured maximum of {max_age_hours} h.")
    return result


def calculate_trend(observations: list[dict[str, Any]],
                    window_hours: float = DEFAULT_TREND_WINDOW_HOURS) -> dict[str, Any]:
    """Level change and rate of change for one station's observations.

    Method: compare the latest observation with the EARLIEST observation whose
    timestamp is within `window_hours` before it (latest - window <= t < latest).
    Input order does not matter (observations are sorted here). If no earlier
    observation exists in the window, or the interval has zero duration, the
    trend is UNAVAILABLE.
    """
    result: dict[str, Any] = {
        "status": SECTION_UNAVAILABLE,
        "method": (f"Latest observation compared with the earliest observation within the "
                   f"preceding {window_hours} h window."),
        "window_hours": window_hours,
        "baseline_observed_at": None,
        "baseline_level_m": None,
        "latest_observed_at": None,
        "latest_level_m": None,
        "duration_hours": None,
        "level_change_m": None,
        "rate_of_change_m_per_hour": None,
        "direction": None,
        "observations_in_window": 0,
        "reason": None,
    }
    ordered = sorted(observations, key=lambda o: o["observed_at"])
    if not ordered:
        result["reason"] = "No valid observations."
        return result
    latest = ordered[-1]
    result["latest_observed_at"] = _iso(latest["observed_at"])
    result["latest_level_m"] = latest["river_level_m"]
    window_start = latest["observed_at"] - timedelta(hours=window_hours)
    in_window = [o for o in ordered if window_start <= o["observed_at"] <= latest["observed_at"]]
    result["observations_in_window"] = len(in_window)
    if len(in_window) < 2:
        result["reason"] = (f"Insufficient observations: need at least 2 within the "
                            f"{window_hours} h window ending at the latest observation, "
                            f"found {len(in_window)}.")
        return result
    baseline = in_window[0]
    duration_h = (latest["observed_at"] - baseline["observed_at"]).total_seconds() / 3600
    result["baseline_observed_at"] = _iso(baseline["observed_at"])
    result["baseline_level_m"] = baseline["river_level_m"]
    if duration_h <= 0:
        result["reason"] = "Zero-duration interval; rate of change cannot be calculated."
        return result
    change = round(latest["river_level_m"] - baseline["river_level_m"], 3)
    result["duration_hours"] = round(duration_h, 4)
    result["level_change_m"] = change
    result["rate_of_change_m_per_hour"] = round(change / duration_h, 4)
    result["direction"] = (TREND_RISING if change > 0
                           else TREND_FALLING if change < 0 else TREND_NO_CHANGE)
    result["status"] = SECTION_AVAILABLE
    return result


def validate_thresholds(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Check a {station_id: {warning_level_m, danger_level_m, reference}} mapping.

    Raises ValueError for malformed configuration (a setup mistake that should
    be fixed, not silently ignored). Stations that are simply absent are fine.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("Threshold configuration must be a mapping of station_id -> thresholds.")
    clean: dict[str, dict[str, Any]] = {}
    for station, entry in config.items():
        if station.startswith("_"):
            continue  # comment keys such as "_comment"
        if not isinstance(entry, dict):
            raise ValueError(f"Thresholds for {station!r} must be an object.")
        levels = {}
        for key in ("warning_level_m", "danger_level_m"):
            value = entry.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)) \
                    or not math.isfinite(value) or value < 0:
                raise ValueError(f"{station!r}: {key} must be a non-negative number "
                                 f"(got {value!r}).")
            levels[key] = float(value)
        if levels["warning_level_m"] >= levels["danger_level_m"]:
            raise ValueError(f"{station!r}: warning_level_m must be below danger_level_m.")
        reference = entry.get("reference")
        reference = reference if isinstance(reference, str) and reference.strip() else None
        official = entry.get("official", False)
        if not isinstance(official, bool):
            raise ValueError(f"{station!r}: official must be true or false.")
        if official and reference is None:
            raise ValueError(f"{station!r}: official thresholds need a 'reference' "
                             "(authority, document, date).")
        datum = entry.get("datum")
        if datum is not None and datum not in ALLOWED_DATUMS:
            raise ValueError(f"{station!r}: datum must be one of {', '.join(ALLOWED_DATUMS)}.")
        clean[station] = {**levels, "reference": reference, "official": official,
                          "datum": datum}
    return clean


def evaluate_thresholds(level_m: float | None, thresholds: dict[str, Any] | None,
                        based_on_stale_data: bool = False,
                        data_datum: str | None = None) -> dict[str, Any]:
    """Compare a level with configured thresholds. UNKNOWN if either is missing.

    If both the thresholds and the data declare a datum and they differ, the
    comparison would be meaningless, so the status is UNKNOWN.
    This is an interpretation of one reading only. It is not a disaster state.
    """
    result: dict[str, Any] = {
        "status": THRESHOLD_UNKNOWN,
        "warning_level_m": None,
        "danger_level_m": None,
        "threshold_reference": None,
        "threshold_is_official": False,
        "threshold_datum": None,
        "data_datum": data_datum,
        "compared_level_m": level_m,
        "based_on_stale_data": based_on_stale_data,
        "reason": None,
    }
    if not thresholds:
        result["reason"] = ("No warning/danger thresholds are configured for this station. "
                            "No official values are assumed.")
        return result
    result["warning_level_m"] = thresholds["warning_level_m"]
    result["danger_level_m"] = thresholds["danger_level_m"]
    result["threshold_reference"] = thresholds.get("reference")
    result["threshold_is_official"] = bool(thresholds.get("official", False))
    result["threshold_datum"] = thresholds.get("datum")
    if result["threshold_datum"] and data_datum and result["threshold_datum"] != data_datum:
        result["reason"] = (f"Datum mismatch: thresholds are '{result['threshold_datum']}' but "
                            f"the data are '{data_datum}'; no comparison was made.")
        return result
    if level_m is None:
        result["reason"] = "No valid river level is available to compare."
        return result
    if level_m >= thresholds["danger_level_m"]:
        result["status"] = THRESHOLD_AT_OR_ABOVE_DANGER
    elif level_m >= thresholds["warning_level_m"]:
        result["status"] = THRESHOLD_AT_OR_ABOVE_WARNING
    else:
        result["status"] = THRESHOLD_BELOW_WARNING
    if based_on_stale_data:
        result["reason"] = "Comparison uses a stale observation; it may not reflect current conditions."
    return result


def evidence_grade(source_types: set[str]) -> str:
    """Provenance grade of a set of source types. Never VERIFIED_LIVE here."""
    if not source_types:
        return GRADE_UNAVAILABLE
    if len(source_types) > 1:
        return GRADE_MIXED
    return {SOURCE_SIMULATED: GRADE_SIMULATED,
            SOURCE_HISTORICAL: GRADE_HISTORICAL,
            SOURCE_LIVE: GRADE_UNVERIFIED_LIVE}[next(iter(source_types))]


# --------------------------------------------------------------------------
# Quality control (pure functions)
# --------------------------------------------------------------------------

def validate_qc_config(config: dict[str, Any] | None) -> dict[str, dict[str, Any]]:
    """Check a {station_id: {valid_min_m, valid_max_m, max_jump_m?, invalid_values_m?,
    reference}} mapping.

    Raises ValueError for malformed configuration. These are data-validation
    bounds, not warning/danger thresholds.
    """
    if config is None:
        return {}
    if not isinstance(config, dict):
        raise ValueError("QC configuration must be a mapping of station_id -> settings.")

    def number(station: str, key: str, value: Any) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) \
                or not math.isfinite(value):
            raise ValueError(f"{station!r}: {key} must be a finite number (got {value!r}).")
        return float(value)

    clean: dict[str, dict[str, Any]] = {}
    for station, entry in config.items():
        if station.startswith("_"):
            continue
        if not isinstance(entry, dict):
            raise ValueError(f"QC settings for {station!r} must be an object.")
        low = number(station, "valid_min_m", entry.get("valid_min_m"))
        high = number(station, "valid_max_m", entry.get("valid_max_m"))
        if low >= high:
            raise ValueError(f"{station!r}: valid_min_m must be below valid_max_m.")
        jump = entry.get("max_jump_m")
        if jump is not None:
            jump = number(station, "max_jump_m", jump)
            if jump <= 0:
                raise ValueError(f"{station!r}: max_jump_m must be positive.")
        invalid = entry.get("invalid_values_m", [])
        if not isinstance(invalid, list):
            raise ValueError(f"{station!r}: invalid_values_m must be a list of numbers.")
        invalid = sorted({round(number(station, "invalid_values_m", v), 3) for v in invalid})
        reference = entry.get("reference")
        clean[station] = {"valid_min_m": low, "valid_max_m": high, "max_jump_m": jump,
                          "invalid_values_m": invalid,
                          "reference": reference if isinstance(reference, str) and reference
                          else None}
    return clean


def apply_quality_control(observations: list[dict[str, Any]],
                          qc: dict[str, Any] | None = None
                          ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any]]:
    """Split one station's observations into (accepted, rejected, summary).

    Without QC settings, only negative levels are rejected (the datum is
    unknown, so nothing else can be judged). With settings:
      0. Placeholder check: exact values listed in invalid_values_m (known
         fill values, compared at 3 decimals) are rejected.
      1. Range check: valid_min_m <= level <= valid_max_m.
      2. Spike check (if max_jump_m is set): |level - median of reference
         values| <= max_jump_m. Reference values are up to 5 earlier in-range
         readings from the preceding 6 h (raw, so one bad acceptance cannot
         lock the check). With fewer than 3 references the check is skipped
         and counted.
    Rejected readings are never corrected or replaced; they are just not used.
    """
    ordered = sorted(observations, key=lambda o: o["observed_at"])
    summary: dict[str, Any] = {
        "configured": qc is not None,
        "valid_min_m": qc["valid_min_m"] if qc else None,
        "valid_max_m": qc["valid_max_m"] if qc else None,
        "max_jump_m": qc["max_jump_m"] if qc else None,
        "invalid_values_m": list(qc["invalid_values_m"]) if qc else [],
        "reference": qc["reference"] if qc else None,
        "checked": len(ordered), "accepted": 0,
        "rejected_negative": 0, "rejected_placeholder": 0, "rejected_out_of_range": 0,
        "rejected_spike": 0,
        "spike_check_skipped": 0,
    }
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    in_range_history: list[dict[str, Any]] = []

    def reject(obs: dict[str, Any], code: str, counter: str, message: str) -> None:
        summary[counter] += 1
        rejected.append({**obs, "qc_code": code, "qc_message": message})

    for obs in ordered:
        level = obs["river_level_m"]
        if qc is None:
            if level < 0:
                reject(obs, "NEGATIVE_LEVEL", "rejected_negative",
                       f"river_level_m {level} is negative and no QC range is configured.")
            else:
                accepted.append(obs)
            continue
        if round(level, 3) in qc["invalid_values_m"]:
            reject(obs, "PLACEHOLDER_VALUE", "rejected_placeholder",
                   f"river_level_m {level} is a configured placeholder value, not a measurement.")
            continue
        if not qc["valid_min_m"] <= level <= qc["valid_max_m"]:
            reject(obs, "OUT_OF_RANGE", "rejected_out_of_range",
                   f"river_level_m {level} is outside the QC range "
                   f"{qc['valid_min_m']}-{qc['valid_max_m']} m.")
            continue
        if qc["max_jump_m"] is not None:
            since = obs["observed_at"] - timedelta(hours=SPIKE_REFERENCE_HOURS)
            refs = [o["river_level_m"] for o in in_range_history[-SPIKE_REFERENCE_COUNT:]
                    if o["observed_at"] >= since]
            in_range_history.append(obs)
            if len(refs) < SPIKE_MIN_REFERENCES:
                summary["spike_check_skipped"] += 1
            else:
                median = statistics.median(refs)
                if abs(level - median) > qc["max_jump_m"]:
                    reject(obs, "SPIKE", "rejected_spike",
                           f"river_level_m {level} differs from the recent median "
                           f"{round(median, 3)} m by more than {qc['max_jump_m']} m.")
                    continue
        accepted.append(obs)
    summary["accepted"] = len(accepted)
    return accepted, rejected, summary


def assess_recent_quality(accepted_times: list[datetime], rejected_times: list[datetime],
                          reference_time: datetime,
                          window_hours: float = DEFAULT_QUALITY_WINDOW_HOURS,
                          suspect_fraction: float = DEFAULT_SUSPECT_FRACTION) -> dict[str, Any]:
    """Share of rejected readings in (reference_time - window, reference_time].

    Rejected readings include unusable rows (e.g. NaN), conflicting duplicates
    and QC rejections. Above `suspect_fraction` the evidence is SUSPECT.
    """
    start = reference_time - timedelta(hours=window_hours)
    good = sum(start < t <= reference_time for t in accepted_times)
    bad = sum(start < t <= reference_time for t in rejected_times)
    total = good + bad
    result = {
        "status": QUALITY_UNAVAILABLE,
        "window_hours": window_hours,
        "window_start": _iso(start),
        "window_end": _iso(reference_time),
        "readings_in_window": total,
        "rejected_in_window": bad,
        "rejected_fraction": None,
        "suspect_fraction_limit": suspect_fraction,
        "reason": None,
    }
    if total == 0:
        result["reason"] = "No readings in the quality window."
        return result
    fraction = bad / total
    result["rejected_fraction"] = round(fraction, 4)
    if fraction > suspect_fraction:
        result["status"] = QUALITY_SUSPECT
        result["reason"] = (f"{bad} of {total} readings in the last {window_hours} h were "
                            f"rejected ({round(100 * fraction, 1)}% > "
                            f"{round(100 * suspect_fraction, 1)}%); the sensor or telemetry "
                            "may be faulty, so accepted readings are also doubtful.")
    else:
        result["status"] = QUALITY_ACCEPTABLE
    return result


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------

class RiverMonitoringAgent:
    """Turns a river-level CSV file into a structured evidence record."""

    def __init__(
        self,
        input_path: str | Path,
        station_id: str | None = None,
        thresholds: dict[str, Any] | None = None,
        max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
        trend_window_hours: float = DEFAULT_TREND_WINDOW_HOURS,
        as_of: datetime | None = None,
        clock: Any = None,
        qc: dict[str, Any] | None = None,
        station_metadata: dict[str, Any] | None = None,
        source_verification: dict[str, Any] | None = None,
        data_datum: str | None = None,
        quality_window_hours: float = DEFAULT_QUALITY_WINDOW_HOURS,
        suspect_fraction: float = DEFAULT_SUSPECT_FRACTION,
    ):
        if station_id is not None and not STATION_ID_PATTERN.match(station_id):
            raise ValueError(f"Invalid station_id: {station_id!r}")
        for name, value in (("max_age_hours", max_age_hours),
                            ("trend_window_hours", trend_window_hours),
                            ("quality_window_hours", quality_window_hours)):
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise ValueError(f"{name} must be a positive number")
        if isinstance(suspect_fraction, bool) or not isinstance(suspect_fraction, (int, float)) \
                or not 0 <= suspect_fraction < 1:
            raise ValueError("suspect_fraction must be between 0 and 1")
        if station_metadata is not None and not isinstance(station_metadata, dict):
            raise ValueError("station_metadata must be a dict")
        _require_aware("as_of", as_of)
        self.input_path = Path(input_path)
        self.station_id = station_id
        self.thresholds = validate_thresholds(thresholds)
        self.qc = validate_qc_config(qc)
        self.station_metadata = copy.deepcopy(station_metadata)
        if source_verification is not None and not isinstance(source_verification, dict):
            raise ValueError("source_verification must be a dict")
        # Result of src.river_sources.verify_live_payloads for the snapshot
        # file passed as input_path. It is trusted only if its snapshot_sha256
        # matches the file actually read.
        self.source_verification = copy.deepcopy(source_verification)
        if data_datum is not None and data_datum not in ALLOWED_DATUMS:
            raise ValueError(f"data_datum must be one of {', '.join(ALLOWED_DATUMS)}")
        self.data_datum = data_datum
        self.max_age_hours = float(max_age_hours)
        self.trend_window_hours = float(trend_window_hours)
        self.quality_window_hours = float(quality_window_hours)
        self.suspect_fraction = float(suspect_fraction)
        # as_of fixes the reference time for deterministic historical replay.
        self.as_of = as_of.astimezone(timezone.utc) if as_of else None
        # Injectable clock (returns an aware datetime) for tests.
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self._result: dict[str, Any] | None = None

    @property
    def location(self) -> dict[str, Any]:
        return {"latitude": LATITUDE, "longitude": LONGITUDE,
                "description": LOCATION_DESCRIPTION, "timezone": TIMEZONE}

    def _reference_time(self) -> tuple[datetime, str]:
        if self.as_of is not None:
            return self.as_of, "AS_OF_REPLAY"
        now = self.clock()
        _require_aware("clock()", now)
        return now.astimezone(timezone.utc), "CURRENT_CLOCK"

    def _base_result(self, reference_time: datetime, mode: str,
                     sha256: str | None) -> dict[str, Any]:
        """Result skeleton in which every measurement is unavailable (null)."""
        meta = self.station_metadata or {}
        source = meta.get("source_description") if isinstance(
            meta.get("source_description"), str) else None
        return {
            "agent_name": AGENT_NAME,
            "status": STATUS_UNAVAILABLE,
            "location": self.location,
            "station_id": self.station_id,
            "source": source or ("CSV file (historical or simulated observations): "
                                 f"{self.input_path.as_posix()}"),
            "input_file": {"path": self.input_path.as_posix(), "sha256": sha256},
            "station_metadata": copy.deepcopy(self.station_metadata),
            "source_type": None,
            "observed_at": None,
            "retrieved_at": None,
            "river_level_m": None,
            "level_change_m": None,
            "rate_of_change_m_per_hour": None,
            "trend": calculate_trend([], self.trend_window_hours),
            "freshness": {**assess_freshness(None, reference_time, self.max_age_hours),
                          "reference_mode": mode},
            "data_quality": {
                "rows_read": 0, "valid_rows": 0, "rejected_rows": 0,
                "duplicate_rows_removed": 0, "conflicting_duplicate_rows": 0,
                "out_of_order_rows": 0, "rejected_rows_without_time": 0,
                "station_observations_used": 0,
                "excluded_after_reference_time": 0, "other_station_rows": 0,
                "qc": apply_quality_control([], None)[2],
                "recent_quality": assess_recent_quality(
                    [], [], reference_time, self.quality_window_hours, self.suspect_fraction),
                "source_types_present": [],
                "evidence_grade": GRADE_UNAVAILABLE,
                "is_simulated": None,
                "is_verified_live": False,
                "verification_status": "UNVERIFIED",
                "source_verification": self._verification_block(None, set()),
            },
            "threshold_status": THRESHOLD_UNKNOWN,
            "threshold_evaluation": evaluate_thresholds(None, None),
            "station_summaries": {},
            "field_categories": copy.deepcopy(FIELD_CATEGORIES),
            "errors": [],
            "warnings": [],
            "data_notes": list(DATA_NOTES),
        }

    def _verification_block(self, file_sha256: str | None,
                            source_types: set[str]) -> dict[str, Any]:
        """Decide whether the live connector's verification applies to this input."""
        sv = self.source_verification
        if sv is None:
            return {"status": SOURCE_VERIFICATION_NOT_APPLICABLE,
                    "reason": "No live-source verification was supplied (CSV input)."}
        block = {
            "status": SOURCE_VERIFICATION_FAILED,
            "provider": sv.get("provider"),
            "connector_status": sv.get("status"),
            "failed_checks": list(sv.get("failed_checks") or []),
            "retrieved_at": sv.get("retrieved_at"),
            "resource": sv.get("resource"),
            "snapshot_sha256": sv.get("snapshot_sha256"),
            "snapshot_hash_matches": bool(file_sha256)
            and sv.get("snapshot_sha256") == file_sha256,
            "reason": None,
        }
        if sv.get("status") != SOURCE_VERIFIED:
            block["reason"] = ("Live-source verification failed: "
                               + ", ".join(block["failed_checks"] or ["unknown"]) + ".")
        elif not block["snapshot_hash_matches"]:
            block["reason"] = ("The input file is not the snapshot that was verified "
                               "(SHA-256 mismatch).")
        elif source_types != {SOURCE_LIVE}:
            block["reason"] = ("Verified snapshots must contain only source_type 'live' rows "
                               f"(found {sorted(source_types) or 'none'}).")
        else:
            block["status"] = SOURCE_VERIFIED
        return block

    def _finish(self, result: dict[str, Any]) -> dict[str, Any]:
        self._result = result
        return self.to_dict()

    def run(self) -> dict[str, Any]:
        """Load, validate and evaluate the CSV. Never raises for data problems."""
        reference_time, mode = self._reference_time()
        loaded = load_observations(self.input_path)
        result = self._base_result(reference_time, mode, loaded["sha256"])
        result["errors"].extend(loaded["file_errors"])
        result["warnings"].extend(loaded["file_warnings"])
        quality = result["data_quality"]
        quality.update(loaded["stats"])
        if loaded["fatal"]:
            return self._finish(result)
        # Row issues are listed only if the row was known at the reference time
        # (rows without a readable time are always listed).
        row_errors, row_warnings = split_issues(
            [issue for moment, issue in reversed(loaded["row_issues"])
             if moment is None or moment <= reference_time],
            "ROW_ISSUES_TRUNCATED", "row issues (most recent first)", "data_quality")
        result["errors"].extend(row_errors)
        result["warnings"].extend(row_warnings)

        # Only use observations that were known at the reference time: the
        # observation must have happened, and (if recorded) been retrieved.
        known, excluded = [], 0
        for obs in loaded["observations"]:
            if obs["observed_at"] > reference_time or (
                    obs["retrieved_at"] is not None and obs["retrieved_at"] > reference_time):
                excluded += 1
            else:
                known.append(obs)
        quality["excluded_after_reference_time"] = excluded
        if excluded:
            result["warnings"].append(_error(
                "EXCLUDED_AFTER_REFERENCE_TIME",
                f"{excluded} observation(s) observed or retrieved after the reference time "
                f"{_iso(reference_time)} were excluded.", "timestamp", severity="warning"))

        # Quality control per station (only on data known at the reference time).
        by_station: dict[str, list[dict[str, Any]]] = {}
        for obs in known:
            by_station.setdefault(obs["station_id"], []).append(obs)
        checked: dict[str, tuple] = {}
        for name in sorted(by_station):
            accepted, rejected, summary = apply_quality_control(
                by_station[name], self.qc.get(name))
            checked[name] = (accepted, rejected, summary)
            latest = accepted[-1] if accepted else None
            result["station_summaries"][name] = {
                "latest_observed_at": _iso(latest["observed_at"]) if latest else None,
                "latest_river_level_m": latest["river_level_m"] if latest else None,
                "latest_source_type": latest["source_type"] if latest else None,
                "valid_observations": len(by_station[name]),
                "qc_accepted_observations": len(accepted),
            }

        stations = sorted(by_station)
        station = self.station_id
        if station is None:
            if len(stations) == 1:
                station = stations[0]
                result["station_id"] = station
            elif len(stations) > 1:
                result["errors"].append(_error(
                    "AMBIGUOUS_STATION",
                    f"File contains several stations ({', '.join(stations)}); "
                    "choose one with station_id.", "station_id"))
                return self._finish(result)
        meta_station = (self.station_metadata or {}).get("station_id")
        if meta_station is not None and meta_station != station:
            result["warnings"].append(_error(
                "STATION_METADATA_MISMATCH",
                f"station_metadata describes {meta_station!r}, not {station!r}.",
                "station_metadata", severity="warning"))

        accepted, rejected, qc_summary = checked.get(station, ([], [], None))
        rows_for_station = by_station.get(station, [])
        quality["other_station_rows"] = len(known) - len(rows_for_station)
        quality["station_observations_used"] = len(accepted)
        quality["qc"] = qc_summary or apply_quality_control([], self.qc.get(station))[2]
        if station not in self.qc:
            result["warnings"].append(_error(
                "QC_NOT_CONFIGURED",
                "No QC range is configured for this station; only negative levels are "
                "rejected, so implausible values may pass.", "qc", severity="warning"))
        # Most recent QC rejections first: they matter most for current evidence.
        qc_errors, qc_warnings = split_issues(
            [_error(o["qc_code"], f"Row {o['row_number']} ({_iso(o['observed_at'])}): "
                    f"{o['qc_message']}", "river_level_m") for o in reversed(rejected)],
            "QC_ISSUES_TRUNCATED", "QC rejections (most recent first)", "data_quality.qc")
        result["errors"].extend(qc_errors)
        result["warnings"].extend(qc_warnings)

        # Recent data quality: accepted vs all kinds of rejected readings.
        rejected_times = [o["observed_at"] for o in rejected] + [
            moment for name, moment in loaded["rejected_points"]
            if name == station and moment <= reference_time]
        recent = assess_recent_quality([o["observed_at"] for o in accepted], rejected_times,
                                       reference_time, self.quality_window_hours,
                                       self.suspect_fraction)
        quality["recent_quality"] = recent
        if recent["status"] == QUALITY_SUSPECT:
            result["warnings"].append(_error(
                "SUSPECT_RECENT_DATA", recent["reason"], "data_quality.recent_quality",
                severity="warning"))

        if not accepted:
            result["errors"].append(_error(
                "NO_VALID_OBSERVATIONS",
                f"No valid observations for station {station!r} at or before "
                f"{_iso(reference_time)}"
                + (" that passed quality control." if rows_for_station else "."),
                "station_id"))
            return self._finish(result)

        rows = accepted
        latest = rows[-1]
        source_types = {o["source_type"] for o in rows}
        quality["source_types_present"] = sorted(source_types)
        quality["evidence_grade"] = evidence_grade(source_types)
        verification = self._verification_block(loaded["sha256"], source_types)
        quality["source_verification"] = verification
        if self.source_verification is not None and verification["status"] != SOURCE_VERIFIED:
            result["warnings"].append(_error(
                "LIVE_SOURCE_NOT_VERIFIED", verification["reason"],
                "data_quality.source_verification", severity="warning"))
        quality["is_simulated"] = SOURCE_SIMULATED in source_types
        if SOURCE_SIMULATED in source_types:
            result["warnings"].append(_error(
                "SIMULATED_DATA",
                "Observations include simulated (fabricated) values; they are not real "
                "river measurements.", "source_type", severity="warning"))
        if SOURCE_LIVE in source_types:
            result["warnings"].append(_error(
                "UNVERIFIED_LIVE_LABEL",
                "Some rows are labelled 'live' in the CSV; unless data_quality."
                "source_verification is VERIFIED they are treated as unverified.",
                "source_type", severity="warning"))
        if len(source_types) > 1:
            result["warnings"].append(_error(
                "MIXED_SOURCE_TYPES",
                f"Station rows mix source types: {', '.join(sorted(source_types))}.",
                "source_type", severity="warning"))

        result["source_type"] = latest["source_type"]
        result["observed_at"] = _iso(latest["observed_at"])
        result["retrieved_at"] = _iso(latest["retrieved_at"])
        result["river_level_m"] = latest["river_level_m"]

        freshness = assess_freshness(latest["observed_at"], reference_time,
                                     self.max_age_hours, latest["retrieved_at"])
        result["freshness"] = {**freshness, "reference_mode": mode}
        stale = freshness["status"] != FRESHNESS_FRESH
        if stale:
            result["warnings"].append(_error(
                "STALE_OBSERVATION", freshness["reason"], "observed_at", severity="warning"))
        if verification["status"] == SOURCE_VERIFIED:
            quality["verification_status"] = "VERIFIED"
            quality["is_verified_live"] = not stale
            quality["evidence_grade"] = (GRADE_VERIFIED_SOURCE_STALE if stale
                                         else GRADE_VERIFIED_LIVE)

        trend = calculate_trend(rows, self.trend_window_hours)
        result["trend"] = trend
        result["level_change_m"] = trend["level_change_m"]
        result["rate_of_change_m_per_hour"] = trend["rate_of_change_m_per_hour"]
        if trend["status"] != SECTION_AVAILABLE:
            result["warnings"].append(_error(
                "TREND_UNAVAILABLE", trend["reason"], "trend", severity="warning"))

        evaluation = evaluate_thresholds(latest["river_level_m"], self.thresholds.get(station),
                                         based_on_stale_data=stale, data_datum=self.data_datum)
        result["threshold_evaluation"] = evaluation
        result["threshold_status"] = evaluation["status"]
        if evaluation["status"] == THRESHOLD_UNKNOWN:
            code = ("THRESHOLD_DATUM_MISMATCH" if evaluation["reason"]
                    and evaluation["reason"].startswith("Datum mismatch")
                    else "THRESHOLDS_NOT_CONFIGURED")
            result["warnings"].append(_error(code, evaluation["reason"], "threshold_status",
                                             severity="warning"))
        elif not evaluation["threshold_is_official"]:
            result["warnings"].append(_error(
                "THRESHOLDS_NOT_OFFICIAL",
                "Configured thresholds are not marked official; treat the threshold status "
                "as indicative only.", "threshold_evaluation", severity="warning"))

        # Rejected rows older than the quality window are listed in `errors` but do
        # not lower the status; rejected rows with no readable time always do.
        complete = (not stale and trend["status"] == SECTION_AVAILABLE
                    and evaluation["status"] != THRESHOLD_UNKNOWN
                    and recent["status"] == QUALITY_ACCEPTABLE
                    and recent["rejected_in_window"] == 0
                    and quality["rejected_rows_without_time"] == 0)
        result["status"] = STATUS_OK if complete else STATUS_PARTIAL
        return self._finish(result)

    def unavailable(self, errors: list[dict[str, Any]]) -> dict[str, Any]:
        """Record and return a DATA_UNAVAILABLE result without reading the input.

        Used when the source itself could not be obtained (e.g. a live API
        outage). Every measurement is null; the errors explain why.
        """
        reference_time, mode = self._reference_time()
        result = self._base_result(reference_time, mode, None)
        result["errors"].extend(copy.deepcopy(errors))
        return self._finish(result)

    def to_dict(self) -> dict[str, Any]:
        """Return a copy of the current structured result."""
        if self._result is None:
            reference_time, mode = self._reference_time()
            result = self._base_result(reference_time, mode, None)
            result["errors"].append(_error(
                "NOT_RUN", "RiverMonitoringAgent.run() has not been called yet."))
            return result
        return copy.deepcopy(self._result)


# --------------------------------------------------------------------------
# Saving and command line
# --------------------------------------------------------------------------

def save_evidence(result: dict[str, Any], path: Path = DEFAULT_OUTPUT_PATH) -> Path:
    """Write the result as JSON. DATA_UNAVAILABLE results are saved too, so an
    older successful file is never left behind to be mistaken for current evidence."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(result, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


def load_json_file(path: str | Path, what: str) -> Any:
    """Read a JSON file. Raises ValueError with a readable message."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise ValueError(f"Cannot read {what} file: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"{what} file is not valid JSON: {exc}") from exc


def load_threshold_file(path: str | Path) -> dict[str, Any]:
    """Read and validate a thresholds JSON file."""
    return validate_thresholds(load_json_file(path, "Thresholds"))


def load_qc_file(path: str | Path) -> dict[str, Any]:
    """Read and validate a QC settings JSON file."""
    return validate_qc_config(load_json_file(path, "QC"))


def _print_summary(result: dict[str, Any]) -> None:
    def show(value: Any, unit: str = "") -> str:
        return "UNAVAILABLE" if value is None else f"{value}{unit}"

    fresh = result["freshness"]
    trend = result["trend"]
    quality = result["data_quality"]
    print(f"Status:          {result['status']}")
    print(f"Station:         {show(result['station_id'])}")
    print(f"Source type:     {show(result['source_type'])} "
          f"(evidence grade {quality['evidence_grade']}, verified live: {quality['is_verified_live']})")
    print(f"Observed at:     {show(result['observed_at'])}")
    print(f"River level:     {show(result['river_level_m'], ' m')}")
    print(f"Freshness:       {fresh['status']} (age {show(fresh['age_hours'], ' h')}, "
          f"max {fresh['max_age_hours']} h, reference {fresh['reference_time']}, "
          f"{fresh['reference_mode']})")
    print(f"Level change:    {show(result['level_change_m'], ' m')} over "
          f"{show(trend['duration_hours'], ' h')}")
    print(f"Rate of change:  {show(result['rate_of_change_m_per_hour'], ' m/h')} "
          f"({show(trend['direction'])})")
    print(f"Thresholds:      {result['threshold_status']}")
    print(f"Rows:            {quality['rows_read']} read, {quality['valid_rows']} valid, "
          f"{quality['rejected_rows']} rejected")
    qc = quality["qc"]
    print(f"QC:              {'configured' if qc['configured'] else 'NOT configured'}; "
          f"{qc['accepted']} of {qc['checked']} accepted (placeholder "
          f"{qc['rejected_placeholder']}, out of range "
          f"{qc['rejected_out_of_range']}, spike {qc['rejected_spike']}, negative "
          f"{qc['rejected_negative']})")
    recent = quality["recent_quality"]
    print(f"Recent quality:  {recent['status']} ({recent['rejected_in_window']} of "
          f"{recent['readings_in_window']} readings rejected in the last "
          f"{recent['window_hours']} h)")
    items = result["warnings"] + result["errors"]
    for item in items[:12]:
        print(f"  [{item['severity']}] {item['code']}: {item['message']}")
    if len(items) > 12:
        print(f"  ... {len(items) - 12} more errors/warnings (use --json to see all)")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the flood-project River Monitoring Agent on a CSV file.")
    parser.add_argument("--input", required=True, help="Path to the river-level CSV file.")
    parser.add_argument("--station", default=None,
                        help="Station ID to evaluate (needed if the file has several).")
    parser.add_argument("--as-of", default=None,
                        help="Reference time for historical replay, e.g. 2026-10-10T12:00:00Z.")
    parser.add_argument("--max-age-hours", type=float, default=DEFAULT_MAX_AGE_HOURS,
                        help=f"Maximum observation age before it is STALE "
                             f"(default {DEFAULT_MAX_AGE_HOURS}).")
    parser.add_argument("--trend-window-hours", type=float, default=DEFAULT_TREND_WINDOW_HOURS,
                        help=f"Trend window in hours (default {DEFAULT_TREND_WINDOW_HOURS}).")
    parser.add_argument("--thresholds", default=None,
                        help="JSON file with station warning/danger thresholds.")
    parser.add_argument("--qc", default=None,
                        help="JSON file with station QC settings (valid range, max jump).")
    parser.add_argument("--station-metadata", default=None,
                        help="Station metadata JSON (e.g. written by src.cwc_adapter).")
    parser.add_argument("--data-datum", default=None, choices=ALLOWED_DATUMS,
                        help="Datum of the levels in the input (checked against thresholds).")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON.")
    parser.add_argument("--save", action="store_true",
                        help=f"Write the result to {DEFAULT_OUTPUT_PATH.relative_to(PROJECT_DIR).as_posix()}.")
    parser.add_argument("--output", default=None, help="Custom output path for --save.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    as_of = None
    if args.as_of is not None:
        as_of = parse_timestamp(args.as_of)
        if as_of is None:
            parser.error("--as-of must be a timezone-aware ISO 8601 time, "
                         "e.g. 2026-10-10T12:00:00Z")
    try:
        thresholds = load_threshold_file(args.thresholds) if args.thresholds else None
        qc = load_qc_file(args.qc) if args.qc else None
        metadata = (load_json_file(args.station_metadata, "Station metadata")
                    if args.station_metadata else None)
        agent = RiverMonitoringAgent(
            args.input, station_id=args.station, thresholds=thresholds,
            max_age_hours=args.max_age_hours, trend_window_hours=args.trend_window_hours,
            as_of=as_of, qc=qc, station_metadata=metadata, data_datum=args.data_datum)
    except ValueError as exc:
        parser.error(str(exc))

    result = agent.run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_summary(result)
    if args.save:
        path = save_evidence(result, Path(args.output) if args.output else DEFAULT_OUTPUT_PATH)
        print(f"Saved river evidence to {path}", file=sys.stderr)
    return 1 if result["status"] == STATUS_UNAVAILABLE else 0


if __name__ == "__main__":
    sys.exit(main())
