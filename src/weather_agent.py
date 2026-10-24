"""Weather Agent for the flood disaster management prototype.

Retrieves model-based current weather and hourly forecasts from the
Open-Meteo Forecast API for the Cauvery River near Upper Anicut, Tamil Nadu,
and returns a validated, structured result.

Design rules:
- Network access (``fetch_raw``) is separate from parsing/validation
  (``parse_response``) and from derived calculations
  (``calculate_rainfall_accumulation``), so each can be tested on its own.
- Missing or invalid data is reported as unavailable (null + reason). Values are
  never invented, and missing data is never treated as "safe weather".
- Every timestamp in the output is timezone-aware ISO 8601.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlencode

import requests

AGENT_NAME = "WeatherAgent"
PROVIDER = "Open-Meteo Forecast API (https://open-meteo.com)"
SOURCE_TYPE = "MODEL_BASED_CURRENT_AND_FORECAST"
BASE_URL = "https://api.open-meteo.com/v1/forecast"

LATITUDE = 10.888611
LONGITUDE = 78.58
TIMEZONE = "Asia/Kolkata"
LOCATION_DESCRIPTION = "Cauvery River near Upper Anicut, Tamil Nadu, India"

HOURLY_VARIABLES = (
    "precipitation",
    "rain",
    "wind_gusts_10m",
    "wind_speed_10m",
    "precipitation_probability",
    "temperature_2m",
    "relative_humidity_2m",
)
CURRENT_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "precipitation",
    "rain",
    "wind_speed_10m",
    "wind_direction_10m",
    "wind_gusts_10m",
)
ACCUMULATION_WINDOWS_HOURS = (3, 6, 24)
ACCUMULATION_VARIABLES = ("precipitation", "rain")

# Open-Meteo `past_days`: 1 adds yesterday's hours so the 24 h rainfall total
# over completed hours can always be computed (without it the hourly series
# starts at 00:00 IST today and the 24 h total is unavailable before midnight).
DEFAULT_PAST_DAYS = 1

# (connect timeout, read timeout) in seconds.
DEFAULT_TIMEOUT = (5, 20)

STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNAVAILABLE = "DATA_UNAVAILABLE"

SECTION_AVAILABLE = "AVAILABLE"
SECTION_PARTIAL = "PARTIAL"
SECTION_UNAVAILABLE = "UNAVAILABLE"

DATA_NOTES = [
    "Values are numerical weather model output (current conditions and "
    "forecasts), not guaranteed physical weather-station observations.",
    "Coordinates are snapped by the provider to the nearest model grid cell; "
    "see returned_grid_location.",
    "Hourly precipitation/rain values are sums over the preceding hour.",
]

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "outputs"


class WeatherFetchError(Exception):
    """Raised when weather data cannot be retrieved from the API."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _error(code: str, message: str, field: str | None = None,
           severity: str = "error") -> dict[str, Any]:
    return {"code": code, "severity": severity, "field": field, "message": message}


def _as_number(value: Any) -> float | None:
    """Return a finite float, or None if the value is not a usable number."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if not math.isfinite(value):
        return None
    return float(value)


def _parse_api_time(text: Any, tz: timezone) -> datetime | None:
    """Parse an Open-Meteo local time string (e.g. '2026-10-10T14:00')."""
    if not isinstance(text, str):
        return None
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=tz)
    return parsed


def _floor_to_hour(moment: datetime) -> datetime:
    return moment.replace(minute=0, second=0, microsecond=0)


# --------------------------------------------------------------------------
# Rainfall accumulation (pure function, no network)
# --------------------------------------------------------------------------

def calculate_rainfall_accumulation(
    hourly_weather: dict[str, Any] | None,
    reference_time: datetime | None,
    windows: Iterable[int] = ACCUMULATION_WINDOWS_HOURS,
    variables: Iterable[str] = ACCUMULATION_VARIABLES,
    unit: str | None = None,
) -> dict[str, Any]:
    """Sum hourly precipitation/rain over the preceding N *completed* hours.

    Open-Meteo hourly precipitation at timestamp T is the total for the hour
    ending at T. An hour is "completed" when T <= reference_time. For a window
    of N hours we need every hourly timestamp from (last_end - (N-1)h) to
    last_end, where last_end is reference_time floored to the hour in the
    data's own timezone. Future forecast hours are never included. If any
    required hour is missing or null, that total is reported as unavailable.
    """
    windows = tuple(windows)
    variables = tuple(variables)
    result: dict[str, Any] = {
        "reference_time": None,
        "method": (
            "Sum of hourly values whose hour ended at or before reference_time. "
            "Each window requires every hour to be present and non-null; "
            "otherwise the total is UNAVAILABLE. Future forecast hours are excluded."
        ),
        "unit": unit,
        "windows": {},
    }

    def all_unavailable(reason: str) -> dict[str, Any]:
        for hours in windows:
            result["windows"][f"{hours}h"] = {
                "hours": hours,
                "period_start": None,
                "period_end": None,
                **{var: {"status": SECTION_UNAVAILABLE, "total": None, "reason": reason}
                   for var in variables},
            }
        return result

    if reference_time is None or reference_time.tzinfo is None:
        return all_unavailable("No timezone-aware reference time was provided.")
    result["reference_time"] = reference_time.isoformat()

    if not hourly_weather or hourly_weather.get("status") == SECTION_UNAVAILABLE:
        return all_unavailable("Hourly weather data is unavailable.")

    raw_times = hourly_weather.get("time") or []
    times: list[datetime] = []
    for text in raw_times:
        try:
            parsed = datetime.fromisoformat(text)
        except (TypeError, ValueError):
            return all_unavailable("Hourly timestamps could not be parsed.")
        if parsed.tzinfo is None:
            return all_unavailable("Hourly timestamps are not timezone-aware.")
        times.append(parsed)
    if not times:
        return all_unavailable("Hourly series contains no timestamps.")

    index_by_time = {moment: i for i, moment in enumerate(times)}
    # Floor in the data's timezone (Asia/Kolkata is UTC+05:30, so flooring in
    # UTC would misalign every hour by 30 minutes).
    last_end = _floor_to_hour(reference_time.astimezone(times[0].tzinfo))
    series = hourly_weather.get("variables") or {}

    for hours in windows:
        needed = [last_end - timedelta(hours=k) for k in range(hours - 1, -1, -1)]
        entry: dict[str, Any] = {
            "hours": hours,
            "period_start": (last_end - timedelta(hours=hours)).isoformat(),
            "period_end": last_end.isoformat(),
        }
        missing_hours = [t for t in needed if t not in index_by_time]
        for var in variables:
            if missing_hours:
                entry[var] = {
                    "status": SECTION_UNAVAILABLE,
                    "total": None,
                    "reason": (
                        f"Hourly series ({times[0].isoformat()} to {times[-1].isoformat()}) "
                        f"does not cover {len(missing_hours)} of the {hours} required "
                        f"completed hours."
                    ),
                }
                continue
            values = series.get(var)
            if values is None:
                entry[var] = {"status": SECTION_UNAVAILABLE, "total": None,
                              "reason": f"Hourly '{var}' data is unavailable."}
                continue
            window_values = [values[index_by_time[t]] for t in needed]
            null_count = sum(v is None for v in window_values)
            if null_count:
                entry[var] = {"status": SECTION_UNAVAILABLE, "total": None,
                              "reason": f"{null_count} of {hours} hourly '{var}' values "
                                        f"in this window are null."}
                continue
            entry[var] = {"status": SECTION_AVAILABLE,
                          "total": round(sum(window_values), 3),
                          "reason": None}
        result["windows"][f"{hours}h"] = entry
    return result


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------

class WeatherAgent:
    """Retrieves and validates Open-Meteo weather data for one location."""

    def __init__(
        self,
        latitude: float = LATITUDE,
        longitude: float = LONGITUDE,
        timezone_name: str = TIMEZONE,
        timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
        past_days: int | None = DEFAULT_PAST_DAYS,
        session: Any = None,
    ):
        self.latitude = latitude
        self.longitude = longitude
        self.timezone_name = timezone_name
        self.timeout = timeout
        # Open-Meteo `past_days` adds earlier hours to the hourly series.
        # Default 1 (see DEFAULT_PAST_DAYS); None omits the parameter.
        self.past_days = past_days
        # Anything with a requests-compatible .get() (injectable for tests).
        self.session = session if session is not None else requests
        self.raw_payload: Any = None
        self._result: dict[str, Any] | None = None

    # ---- configuration -------------------------------------------------

    @property
    def source_url(self) -> str:
        params: dict[str, Any] = {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "hourly": ",".join(HOURLY_VARIABLES),
            "current": ",".join(CURRENT_VARIABLES),
            "timezone": self.timezone_name,
        }
        if self.past_days is not None:
            params["past_days"] = self.past_days
        return f"{BASE_URL}?{urlencode(params, safe=',')}"

    @property
    def requested_location(self) -> dict[str, Any]:
        return {
            "latitude": self.latitude,
            "longitude": self.longitude,
            "description": LOCATION_DESCRIPTION,
        }

    # ---- network -------------------------------------------------------

    def fetch_raw(self) -> Any:
        """Call the API and return decoded JSON. Raises WeatherFetchError."""
        try:
            response = self.session.get(self.source_url, timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise WeatherFetchError("NETWORK_TIMEOUT", f"Request timed out: {exc}") from exc
        except requests.exceptions.ConnectionError as exc:
            raise WeatherFetchError("NETWORK_ERROR", f"Connection failed: {exc}") from exc
        except requests.exceptions.RequestException as exc:
            raise WeatherFetchError("REQUEST_FAILED", f"Request failed: {exc}") from exc

        if response.status_code != 200:
            reason = None
            try:
                body = response.json()
                if isinstance(body, dict):
                    reason = body.get("reason")
            except ValueError:
                pass
            detail = f": {reason}" if reason else ""
            raise WeatherFetchError(
                "HTTP_ERROR", f"API returned HTTP {response.status_code}{detail}")

        try:
            return response.json()
        except ValueError as exc:
            raise WeatherFetchError("INVALID_JSON", f"Response is not valid JSON: {exc}") from exc

    # ---- parsing and validation ----------------------------------------

    def unavailable_result(self, retrieved_at: datetime,
                           errors: list[dict[str, Any]]) -> dict[str, Any]:
        """Build a DATA_UNAVAILABLE result. All measurements are null."""
        reason = "; ".join(e["message"] for e in errors if e["severity"] == "error") \
            or "No data retrieved."
        return {
            "agent_name": AGENT_NAME,
            "status": STATUS_UNAVAILABLE,
            "source": PROVIDER,
            "source_url": self.source_url,
            "source_type": SOURCE_TYPE,
            "data_notes": list(DATA_NOTES),
            "requested_location": self.requested_location,
            "returned_grid_location": {
                "latitude": None, "longitude": None, "elevation_m": None,
                "reason": "Not available: " + reason,
            },
            "retrieved_at": retrieved_at.isoformat(),
            "timezone": {
                "requested": self.timezone_name, "returned": None,
                "abbreviation": None, "utc_offset_seconds": None,
            },
            "current_weather": {
                "status": SECTION_UNAVAILABLE, "time": None, "interval_seconds": None,
                "values": {var: None for var in CURRENT_VARIABLES},
                "missing": {var: reason for var in CURRENT_VARIABLES},
            },
            "hourly_weather": {
                "status": SECTION_UNAVAILABLE, "time": [],
                "variables": {var: None for var in HOURLY_VARIABLES},
                "missing": {var: reason for var in HOURLY_VARIABLES},
                "null_counts": {},
            },
            "rainfall_accumulation": calculate_rainfall_accumulation(None, retrieved_at),
            "units": {"current": {var: None for var in CURRENT_VARIABLES},
                      "hourly": {var: None for var in HOURLY_VARIABLES}},
            "errors": errors,
        }

    def parse_response(self, payload: Any, retrieved_at: datetime) -> dict[str, Any]:
        """Validate a decoded API payload and convert it to the output format."""
        if retrieved_at.tzinfo is None:
            raise ValueError("retrieved_at must be timezone-aware")

        if not isinstance(payload, dict):
            return self.unavailable_result(retrieved_at, [_error(
                "INVALID_PAYLOAD", "API response is not a JSON object.")])
        if payload.get("error"):
            return self.unavailable_result(retrieved_at, [_error(
                "API_ERROR", f"API reported an error: {payload.get('reason', 'no reason given')}")])

        errors: list[dict[str, Any]] = []

        # Returned grid location (preserved exactly as returned).
        grid: dict[str, Any] = {"latitude": None, "longitude": None, "elevation_m": None,
                                "reason": None}
        missing_location = []
        for key, out_key in (("latitude", "latitude"), ("longitude", "longitude"),
                             ("elevation", "elevation_m")):
            value = _as_number(payload.get(key))
            grid[out_key] = value
            if value is None and key != "elevation":
                missing_location.append(key)
        if missing_location:
            grid["reason"] = f"Missing from API response: {', '.join(missing_location)}."
            errors.append(_error("MISSING_FIELD", grid["reason"], "returned_grid_location"))

        # Timezone: needed to interpret the API's local timestamps.
        offset = payload.get("utc_offset_seconds")
        tz_info = {
            "requested": self.timezone_name,
            "returned": payload.get("timezone"),
            "abbreviation": payload.get("timezone_abbreviation"),
            "utc_offset_seconds": offset if isinstance(offset, int) and not isinstance(offset, bool) else None,
        }
        if tz_info["utc_offset_seconds"] is None:
            errors.append(_error(
                "MISSING_FIELD",
                "utc_offset_seconds is missing or invalid; returned timestamps cannot be "
                "interpreted safely.", "utc_offset_seconds"))
            result = self.unavailable_result(retrieved_at, errors)
            result["returned_grid_location"] = grid
            result["timezone"] = tz_info
            return result
        if tz_info["returned"] != self.timezone_name:
            errors.append(_error(
                "TIMEZONE_MISMATCH",
                f"Requested timezone {self.timezone_name!r} but API returned "
                f"{tz_info['returned']!r}.", "timezone", severity="warning"))
        tz = timezone(timedelta(seconds=tz_info["utc_offset_seconds"]))

        units = {
            "current": self._parse_units(payload, "current_units", CURRENT_VARIABLES, errors),
            "hourly": self._parse_units(payload, "hourly_units", HOURLY_VARIABLES, errors),
        }
        current = self._parse_current(payload, tz, errors)
        hourly = self._parse_hourly(payload, tz, errors)

        if current["status"] == SECTION_UNAVAILABLE and hourly["status"] == SECTION_UNAVAILABLE:
            status = STATUS_UNAVAILABLE
        elif (current["status"] == SECTION_AVAILABLE and hourly["status"] == SECTION_AVAILABLE
              and not missing_location):
            status = STATUS_OK
        else:
            status = STATUS_PARTIAL

        return {
            "agent_name": AGENT_NAME,
            "status": status,
            "source": PROVIDER,
            "source_url": self.source_url,
            "source_type": SOURCE_TYPE,
            "data_notes": list(DATA_NOTES),
            "requested_location": self.requested_location,
            "returned_grid_location": grid,
            "retrieved_at": retrieved_at.isoformat(),
            "timezone": tz_info,
            "current_weather": current,
            "hourly_weather": hourly,
            "rainfall_accumulation": calculate_rainfall_accumulation(
                hourly, retrieved_at, unit=units["hourly"].get("precipitation")),
            "units": units,
            "errors": errors,
        }

    @staticmethod
    def _parse_units(payload: dict, key: str, variables: Iterable[str],
                     errors: list) -> dict[str, str | None]:
        section = payload.get(key)
        if not isinstance(section, dict):
            errors.append(_error("MISSING_SECTION", f"'{key}' section is missing.", key,
                                 severity="warning"))
            return {var: None for var in variables}
        units = {}
        for var in variables:
            unit = section.get(var)
            units[var] = unit if isinstance(unit, str) else None
            if units[var] is None:
                errors.append(_error("MISSING_FIELD", f"Unit for '{var}' is missing.",
                                     f"{key}.{var}", severity="warning"))
        return units

    @staticmethod
    def _parse_current(payload: dict, tz: timezone, errors: list) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": SECTION_UNAVAILABLE, "time": None, "interval_seconds": None,
            "values": {var: None for var in CURRENT_VARIABLES}, "missing": {},
        }
        section = payload.get("current")
        if not isinstance(section, dict):
            reason = "'current' section is missing from API response."
            errors.append(_error("MISSING_SECTION", reason, "current"))
            out["missing"] = {var: reason for var in CURRENT_VARIABLES}
            return out

        moment = _parse_api_time(section.get("time"), tz)
        if moment is None:
            # A value without a valid timestamp cannot be called "current".
            reason = "current.time is missing or invalid; current values not used."
            errors.append(_error("MISSING_FIELD", reason, "current.time"))
            out["missing"] = {var: reason for var in CURRENT_VARIABLES}
            return out
        out["time"] = moment.isoformat()
        interval = section.get("interval")
        out["interval_seconds"] = interval if isinstance(interval, int) else None

        for var in CURRENT_VARIABLES:
            if var not in section:
                out["missing"][var] = "Field absent from API response."
            elif section[var] is None:
                out["missing"][var] = "API returned null."
            else:
                value = _as_number(section[var])
                if value is None:
                    out["missing"][var] = f"Non-numeric value: {section[var]!r}."
                out["values"][var] = value
            if var in out["missing"]:
                errors.append(_error("MISSING_VALUE", out["missing"][var], f"current.{var}"))

        if not out["missing"]:
            out["status"] = SECTION_AVAILABLE
        elif len(out["missing"]) < len(CURRENT_VARIABLES):
            out["status"] = SECTION_PARTIAL
        return out

    @staticmethod
    def _parse_hourly(payload: dict, tz: timezone, errors: list) -> dict[str, Any]:
        out: dict[str, Any] = {
            "status": SECTION_UNAVAILABLE, "time": [],
            "variables": {var: None for var in HOURLY_VARIABLES},
            "missing": {}, "null_counts": {},
        }

        def section_unavailable(reason: str, field: str) -> dict[str, Any]:
            errors.append(_error("MISSING_SECTION" if field == "hourly" else "INVALID_FIELD",
                                 reason, field))
            out["missing"] = {var: reason for var in HOURLY_VARIABLES}
            return out

        section = payload.get("hourly")
        if not isinstance(section, dict):
            return section_unavailable("'hourly' section is missing from API response.", "hourly")
        raw_times = section.get("time")
        if not isinstance(raw_times, list) or not raw_times:
            return section_unavailable("hourly.time is missing or empty.", "hourly.time")
        times = [_parse_api_time(t, tz) for t in raw_times]
        if any(t is None for t in times):
            return section_unavailable("hourly.time contains invalid timestamps.", "hourly.time")
        if any(later <= earlier for earlier, later in zip(times, times[1:])):
            return section_unavailable("hourly.time is not strictly increasing.", "hourly.time")
        out["time"] = [t.isoformat() for t in times]

        for var in HOURLY_VARIABLES:
            values = section.get(var)
            if values is None:
                out["missing"][var] = "Field absent from API response."
            elif not isinstance(values, list) or len(values) != len(times):
                out["missing"][var] = (
                    f"Length does not match hourly.time ({len(times)} timestamps); "
                    "values cannot be aligned.")
            else:
                cleaned = [_as_number(v) for v in values]
                out["variables"][var] = cleaned
                nulls = sum(v is None for v in cleaned)
                out["null_counts"][var] = nulls
                if nulls:
                    errors.append(_error(
                        "NULL_VALUES", f"{nulls} of {len(cleaned)} hourly '{var}' values "
                        "are null or non-numeric and are kept as null.", f"hourly.{var}",
                        severity="warning"))
            if var in out["missing"]:
                errors.append(_error("MISSING_FIELD", out["missing"][var], f"hourly.{var}"))

        if len(out["missing"]) == len(HOURLY_VARIABLES):
            out["status"] = SECTION_UNAVAILABLE
        elif out["missing"] or any(out["null_counts"].values()):
            out["status"] = SECTION_PARTIAL
        else:
            out["status"] = SECTION_AVAILABLE
        return out

    # ---- public workflow -----------------------------------------------

    def run(self) -> dict[str, Any]:
        """Fetch, validate and store the result. Never raises for data problems."""
        try:
            payload = self.fetch_raw()
        except WeatherFetchError as exc:
            self.raw_payload = None
            self._result = self.unavailable_result(
                datetime.now(timezone.utc), [_error(exc.code, exc.message)])
            return self.to_dict()
        retrieved_at = datetime.now(timezone.utc)
        self.raw_payload = payload
        self._result = self.parse_response(payload, retrieved_at)
        return self.to_dict()

    def calculate_rainfall_accumulation(
        self, reference_time: datetime | None = None,
        windows: Iterable[int] = ACCUMULATION_WINDOWS_HOURS,
    ) -> dict[str, Any]:
        """Rainfall totals over completed hours, from the last run() result.

        reference_time defaults to the retrieval time of the last run.
        """
        if self._result is None:
            return calculate_rainfall_accumulation(None, reference_time, windows)
        if reference_time is None:
            reference_time = datetime.fromisoformat(self._result["retrieved_at"])
        return calculate_rainfall_accumulation(
            self._result["hourly_weather"], reference_time, windows,
            unit=self._result["units"]["hourly"].get("precipitation"))

    def to_dict(self) -> dict[str, Any]:
        """Return a copy of the current structured result."""
        if self._result is None:
            return self.unavailable_result(datetime.now(timezone.utc), [_error(
                "NOT_RUN", "WeatherAgent.run() has not been called yet.")])
        return copy.deepcopy(self._result)


# --------------------------------------------------------------------------
# Saving live samples
# --------------------------------------------------------------------------

def save_live_sample(result: dict[str, Any], raw_payload: Any,
                     output_dir: Path = OUTPUT_DIR) -> Path:
    """Save a result from a real API response, labelled with its retrieval time.

    Refuses to save DATA_UNAVAILABLE results or results without a raw payload,
    so a saved sample is always backed by an actual API response.
    """
    if result.get("status") == STATUS_UNAVAILABLE or raw_payload is None:
        raise ValueError("Only results from a successful live API response can be saved.")
    retrieved_at = datetime.fromisoformat(result["retrieved_at"]).astimezone(timezone.utc)
    stamp = retrieved_at.strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"weather_live_{stamp}.json"
    document = {
        "label": f"Live Open-Meteo response retrieved at {result['retrieved_at']}",
        "retrieved_at": result["retrieved_at"],
        "result": result,
        "raw_api_response": raw_payload,
    }
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _print_summary(result: dict[str, Any]) -> None:
    print(f"Status:        {result['status']}")
    print(f"Retrieved at:  {result['retrieved_at']}")
    grid = result["returned_grid_location"]
    print(f"Grid location: {grid['latitude']}, {grid['longitude']} "
          f"(requested {LATITUDE}, {LONGITUDE})")
    current = result["current_weather"]
    print(f"Current ({current['time']}):")
    for var, value in current["values"].items():
        unit = result["units"]["current"].get(var) or ""
        shown = "UNAVAILABLE" if value is None else f"{value} {unit}"
        print(f"  {var:22s} {shown}")
    acc = result["rainfall_accumulation"]
    print(f"Rainfall accumulation (completed hours up to {acc['reference_time']}):")
    for name, window in acc["windows"].items():
        for var in ACCUMULATION_VARIABLES:
            item = window[var]
            shown = (f"{item['total']} {acc['unit'] or ''}" if item["status"] == SECTION_AVAILABLE
                     else f"UNAVAILABLE - {item['reason']}")
            print(f"  {name:4s} {var:14s} {shown}")
    for err in result["errors"]:
        print(f"  [{err['severity']}] {err['code']}: {err['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the flood-project Weather Agent once.")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON.")
    parser.add_argument("--save", action="store_true",
                        help="Save the result to outputs/ (only if retrieval succeeded).")
    parser.add_argument("--past-days", type=int, default=DEFAULT_PAST_DAYS,
                        help=f"Open-Meteo past_days (default {DEFAULT_PAST_DAYS}, needed for the "
                             "24 h rainfall total; 0 = today only).")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")  # Windows consoles default to cp1252 ("°")

    agent = WeatherAgent(past_days=args.past_days)
    result = agent.run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_summary(result)

    if args.save:
        try:
            path = save_live_sample(result, agent.raw_payload)
            print(f"Saved live sample to {path}")
        except ValueError as exc:
            print(f"Not saved: {exc}", file=sys.stderr)
    return 1 if result["status"] == STATUS_UNAVAILABLE else 0


if __name__ == "__main__":
    sys.exit(main())
