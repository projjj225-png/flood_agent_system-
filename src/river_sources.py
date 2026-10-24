"""Live river-level source: CWC telemetry via the National Water Data Portal.

Provider (documented, public): National Water Data Portal (NWDP), operated by
the National Water Informatics Centre (NWIC). It runs CKAN, whose standard
Action API (``/api/3/action/<action>``) is publicly documented by CKAN. The
CWC dataset "River Water Level (Telemetry - Hourly), Central Water Commission
(CWC)" publishes per-basin CSV resources with an active DataStore, so rows
can be queried with ``datastore_search`` and the resource described with
``resource_show``.

This module:
1. fetches resource metadata and the most recent rows for one station, with
   timeouts and bounded retries (timeouts, connection errors, HTTP 5xx/429);
2. runs deterministic VERIFICATION checks on the responses (endpoint, dataset
   and resource identity, field names and unit, station name, agency,
   coordinates, gauge zero, timestamps);
3. converts the rows into the River Agent's normalized CSV schema with
   source_type ``live`` and a ``retrieved_at`` time, and saves the raw
   responses and the snapshot for audit.

Verification establishes WHERE the data came from and that it describes the
configured station. It says nothing about whether the values are physically
correct (that is quality control) or current (that is freshness); the River
Agent decides those separately. Nothing here invents or repairs values.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests

from src.cwc_adapter import convert_timestamp, normalize_station_id, parse_utc_offset

PROVIDER_NAME = "National Water Data Portal (NWDP/NWIC), CKAN Action API"
PROVIDER_ID = "NWDP_CKAN"

VERIFIED = "VERIFIED"
FAILED = "FAILED"
NOT_APPLICABLE = "NOT_APPLICABLE"

RETRYABLE_STATUS = {429, 500, 502, 503, 504}
DEFAULT_TIMEOUT = (5.0, 30.0)
DEFAULT_BACKOFF_SECONDS = (2.0, 5.0)
# A record more than this far in the future (relative to retrieval) suggests a
# timezone or clock error, so verification fails.
FUTURE_TOLERANCE = timedelta(hours=1)

NORMALIZED_HEADER = ("timestamp", "station_id", "river_level_m", "source_type", "retrieved_at")


class LiveSourceError(Exception):
    """The live source could not be reached or returned unusable data."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def validate_live_config(live: dict[str, Any], station: dict[str, Any]) -> dict[str, Any]:
    """Check the live-source and station settings. Raises ValueError."""
    if not isinstance(live, dict) or not isinstance(station, dict):
        raise ValueError("live and station settings must be objects")
    required_live = ("api_base_url", "allowed_hosts", "dataset_id", "resource_id",
                     "resource_name", "station_field", "time_field", "level_field")
    for key in required_live:
        if not live.get(key):
            raise ValueError(f"live.{key} is required")
    required_station = ("provider_station_name", "agency", "latitude", "longitude",
                        "rl_zero_gauge_m", "timestamp_utc_offset")
    for key in required_station:
        if station.get(key) in (None, ""):
            raise ValueError(f"station.{key} is required for live verification")
    parts = urlsplit(live["api_base_url"])
    if parts.scheme != "https":
        raise ValueError("live.api_base_url must use https")
    if parts.hostname not in live["allowed_hosts"]:
        raise ValueError(f"live.api_base_url host {parts.hostname!r} is not in allowed_hosts")
    parse_utc_offset(station["timestamp_utc_offset"])
    records = live.get("records_to_fetch", 72)
    if not isinstance(records, int) or isinstance(records, bool) or not 1 <= records <= 1000:
        raise ValueError("live.records_to_fetch must be an integer between 1 and 1000")
    attempts = live.get("max_attempts", 3)
    if not isinstance(attempts, int) or isinstance(attempts, bool) or not 1 <= attempts <= 10:
        raise ValueError("live.max_attempts must be an integer between 1 and 10")
    return {
        **live,
        "records_to_fetch": records,
        "max_attempts": attempts,
        "timeout": tuple(live.get("timeout_seconds", DEFAULT_TIMEOUT)),
        "backoff_seconds": tuple(live.get("backoff_seconds", DEFAULT_BACKOFF_SECONDS)),
        "coordinate_tolerance_deg": float(live.get("coordinate_tolerance_deg", 0.001)),
        "rl_zero_tolerance_m": float(live.get("rl_zero_tolerance_m", 0.001)),
    }


# --------------------------------------------------------------------------
# HTTP with retries
# --------------------------------------------------------------------------

class CkanClient:
    """Minimal CKAN Action API client with bounded, logged retries."""

    def __init__(self, api_base_url: str, allowed_hosts: list[str], session: Any = None,
                 timeout: tuple[float, float] = DEFAULT_TIMEOUT, max_attempts: int = 3,
                 backoff_seconds: tuple[float, ...] = DEFAULT_BACKOFF_SECONDS,
                 sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] | None = None):
        parts = urlsplit(api_base_url)
        if parts.scheme != "https" or parts.hostname not in allowed_hosts:
            raise ValueError("CKAN base URL must be https and on an allowed host")
        self.api_base_url = api_base_url.rstrip("/") + "/"
        self.session = session if session is not None else requests.Session()
        self.timeout = timeout
        self.max_attempts = max_attempts
        self.backoff_seconds = backoff_seconds
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.attempts: list[dict[str, Any]] = []  # audit trail of every HTTP attempt

    def call(self, action: str, params: dict[str, Any]) -> tuple[dict[str, Any], datetime, bytes]:
        """Call one action. Returns (decoded JSON, retrieved_at, raw bytes).

        Raises LiveSourceError after the last failed attempt.
        """
        url = self.api_base_url + action
        last_error: LiveSourceError | None = None
        for attempt in range(1, self.max_attempts + 1):
            started = self.clock()
            record: dict[str, Any] = {"action": action, "url": url, "params": params,
                                      "attempt": attempt, "started_at": started.isoformat()}
            try:
                response = self.session.get(url, params=params, timeout=self.timeout)
            except requests.exceptions.Timeout as exc:
                last_error = LiveSourceError("NETWORK_TIMEOUT", f"{action}: timed out ({exc})")
            except requests.exceptions.ConnectionError as exc:
                last_error = LiveSourceError("NETWORK_ERROR", f"{action}: connection failed ({exc})")
            except requests.exceptions.RequestException as exc:
                last_error = LiveSourceError("REQUEST_FAILED", f"{action}: request failed ({exc})")
                record.update(outcome="error", error=last_error.message)
                self.attempts.append(record)
                raise last_error from exc
            else:
                retrieved_at = self.clock()
                record["http_status"] = response.status_code
                raw = response.content if isinstance(getattr(response, "content", None), bytes) \
                    else (response.text or "").encode("utf-8")
                record["bytes"] = len(raw)
                if response.status_code == 200:
                    try:
                        payload = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                        record.update(outcome="error", error="invalid JSON")
                        self.attempts.append(record)
                        raise LiveSourceError("INVALID_JSON", f"{action}: response is not "
                                                              f"valid JSON ({exc})") from exc
                    record["outcome"] = "ok"
                    record["sha256"] = hashlib.sha256(raw).hexdigest()
                    self.attempts.append(record)
                    if not isinstance(payload, dict) or payload.get("success") is not True:
                        error = payload.get("error") if isinstance(payload, dict) else None
                        raise LiveSourceError("API_ERROR", f"{action}: CKAN reported failure: "
                                                           f"{error!r}")
                    return payload, retrieved_at, raw
                last_error = LiveSourceError(
                    "HTTP_ERROR", f"{action}: HTTP {response.status_code}")
                if response.status_code not in RETRYABLE_STATUS:
                    record.update(outcome="error", error=last_error.message)
                    self.attempts.append(record)
                    raise last_error
            record.update(outcome="error", error=last_error.message)
            self.attempts.append(record)
            if attempt < self.max_attempts:
                delay = self.backoff_seconds[min(attempt - 1, len(self.backoff_seconds) - 1)] \
                    if self.backoff_seconds else 0
                record["retry_after_seconds"] = delay
                self.sleep(delay)
        assert last_error is not None
        raise LiveSourceError(last_error.code, f"{last_error.message} after "
                                               f"{self.max_attempts} attempt(s)")


# --------------------------------------------------------------------------
# Verification (pure)
# --------------------------------------------------------------------------

def _check(name: str, passed: bool, detail: str) -> dict[str, Any]:
    return {"name": name, "passed": bool(passed), "detail": detail}


def _float(text: Any) -> float | None:
    try:
        value = float(text)
    except (TypeError, ValueError):
        return None
    return value if math.isfinite(value) else None


def verify_live_payloads(resource_payload: dict[str, Any], search_payload: dict[str, Any],
                         live: dict[str, Any], station: dict[str, Any],
                         retrieved_at: datetime) -> dict[str, Any]:
    """Deterministic provenance/identity checks. VERIFIED only if all pass."""
    checks: list[dict[str, Any]] = []
    resource = resource_payload.get("result") if isinstance(resource_payload, dict) else None
    result = search_payload.get("result") if isinstance(search_payload, dict) else None
    resource = resource if isinstance(resource, dict) else {}
    result = result if isinstance(result, dict) else {}

    checks.append(_check(
        "resource_id_matches", resource.get("id") == live["resource_id"],
        f"resource_show id {resource.get('id')!r}, expected {live['resource_id']!r}"))
    checks.append(_check(
        "dataset_id_matches", resource.get("package_id") == live["dataset_id"],
        f"package_id {resource.get('package_id')!r}, expected {live['dataset_id']!r}"))
    checks.append(_check(
        "resource_name_matches", resource.get("name") == live["resource_name"],
        f"resource name {resource.get('name')!r}"))
    checks.append(_check(
        "datastore_resource_matches", result.get("resource_id") == live["resource_id"],
        f"datastore_search resource_id {result.get('resource_id')!r}"))

    field_ids = [f.get("id") for f in result.get("fields", []) if isinstance(f, dict)]
    needed = [live["station_field"], live["time_field"], live["level_field"],
              "Agency", "Latitude", "Longitude", "RL_of_zeroGauge"]
    missing = [f for f in needed if f not in field_ids]
    checks.append(_check("required_fields_present", not missing,
                         f"missing: {missing}" if missing else "all required fields present"))
    unit_ok = live["level_field"].strip().lower().endswith("(meter)")
    checks.append(_check("unit_is_metre", unit_ok,
                         f"level field {live['level_field']!r} declares the unit"))

    records = [r for r in result.get("records", []) if isinstance(r, dict)]
    checks.append(_check("records_present", bool(records), f"{len(records)} record(s)"))

    expected_name = station["provider_station_name"].strip().casefold()
    names = {str(r.get(live["station_field"], "")).strip() for r in records}
    checks.append(_check(
        "station_name_matches",
        bool(records) and all(n.casefold() == expected_name for n in names),
        f"station names in response: {sorted(names)}"))
    agencies = {str(r.get("Agency", "")).strip() for r in records}
    checks.append(_check("agency_matches",
                         bool(records) and agencies == {station["agency"]},
                         f"agencies: {sorted(agencies)}"))

    tol = live["coordinate_tolerance_deg"]
    bad_coords = [r.get("_id") for r in records
                  if _float(r.get("Latitude")) is None or _float(r.get("Longitude")) is None
                  or abs(_float(r.get("Latitude")) - station["latitude"]) > tol
                  or abs(_float(r.get("Longitude")) - station["longitude"]) > tol]
    checks.append(_check("coordinates_match", bool(records) and not bad_coords,
                         f"{len(bad_coords)} record(s) outside {tol} deg of "
                         f"({station['latitude']}, {station['longitude']})"))
    rl_tol = live["rl_zero_tolerance_m"]
    bad_rl = [r.get("_id") for r in records
              if _float(r.get("RL_of_zeroGauge")) is None
              or abs(_float(r.get("RL_of_zeroGauge")) - station["rl_zero_gauge_m"]) > rl_tol]
    checks.append(_check("gauge_zero_matches", bool(records) and not bad_rl,
                         f"{len(bad_rl)} record(s) with RL_of_zeroGauge != "
                         f"{station['rl_zero_gauge_m']}"))

    tz = parse_utc_offset(station["timestamp_utc_offset"])
    parsed = [convert_timestamp(str(r.get(live["time_field"], "")), tz) for r in records]
    parsed_ok = [datetime.fromisoformat(p) for p in parsed if p is not None]
    checks.append(_check("timestamps_parseable", bool(parsed_ok),
                         f"{len(parsed_ok)} of {len(records)} timestamps parsed "
                         f"(offset {station['timestamp_utc_offset']}, "
                         f"{station.get('timestamp_offset_status', 'ASSUMED')})"))
    future = [p for p in parsed_ok if p > retrieved_at + FUTURE_TOLERANCE]
    checks.append(_check("no_future_timestamps", not future,
                         f"{len(future)} timestamp(s) later than retrieval time + 1 h"))

    status = VERIFIED if all(c["passed"] for c in checks) else FAILED
    latest = max(parsed_ok).astimezone(timezone.utc).isoformat() if parsed_ok else None
    return {
        "status": status,
        "provider": PROVIDER_NAME,
        "provider_id": PROVIDER_ID,
        "checks": checks,
        "failed_checks": [c["name"] for c in checks if not c["passed"]],
        "resource": {
            "id": resource.get("id"), "name": resource.get("name"),
            "package_id": resource.get("package_id"),
            "last_modified": resource.get("last_modified"),
            "format": resource.get("format"), "url": resource.get("url"),
        },
        "records_received": len(records),
        "total_records_for_station": result.get("total"),
        "latest_record_observed_at": latest,
        "retrieved_at": retrieved_at.astimezone(timezone.utc).isoformat(),
        "timestamp_interpretation": {
            "utc_offset": station["timestamp_utc_offset"],
            "status": station.get("timestamp_offset_status", "ASSUMED"),
        },
        "note": ("Verification confirms source and station identity only. Values still go "
                 "through quality control, and freshness is assessed separately."),
    }


# --------------------------------------------------------------------------
# Conversion and snapshot
# --------------------------------------------------------------------------

def records_to_normalized_csv(records: list[dict[str, Any]], live: dict[str, Any],
                              station: dict[str, Any], station_id: str,
                              retrieved_at: datetime) -> str:
    """Normalized CSV text. Level text and unparseable times are passed through."""
    tz = parse_utc_offset(station["timestamp_utc_offset"])
    rows = []
    for record in records:
        raw_time = str(record.get(live["time_field"], "")).strip()
        iso = convert_timestamp(raw_time, tz) or raw_time
        level = record.get(live["level_field"])
        level_text = "" if level is None else str(level).strip()
        rows.append((record.get("_id") if isinstance(record.get("_id"), int) else 0,
                     (iso, station_id, level_text, "live",
                      retrieved_at.astimezone(timezone.utc).isoformat())))
    rows.sort(key=lambda item: item[0])  # provider insertion order (_id), oldest first
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(NORMALIZED_HEADER)
    writer.writerows(row for _, row in rows)
    return buffer.getvalue()


def fetch_live_snapshot(live: dict[str, Any], station: dict[str, Any], snapshot_dir: Path,
                        session: Any = None, sleep: Callable[[float], None] = time.sleep,
                        clock: Callable[[], datetime] | None = None) -> dict[str, Any]:
    """Fetch, verify and save one live snapshot.

    Returns {"verification", "snapshot_path", "snapshot_sha256", "raw_paths",
    "attempts", "station_id"}. Raises LiveSourceError if the source cannot be
    reached or answers with an error; a reachable source whose data fails
    verification is returned with verification status FAILED.
    """
    live = validate_live_config(live, station)
    client = CkanClient(live["api_base_url"], live["allowed_hosts"], session=session,
                        timeout=live["timeout"], max_attempts=live["max_attempts"],
                        backoff_seconds=live["backoff_seconds"], sleep=sleep, clock=clock)
    station_id = station.get("station_id") or normalize_station_id(
        station["provider_station_name"])
    try:
        resource_payload, _, resource_raw = client.call(
            "resource_show", {"id": live["resource_id"]})
        search_payload, retrieved_at, search_raw = client.call("datastore_search", {
            "resource_id": live["resource_id"],
            "filters": json.dumps({live["station_field"]: station["provider_station_name"]}),
            "sort": "_id desc",
            "limit": live["records_to_fetch"],
        })
    except LiveSourceError as exc:
        exc.attempts = client.attempts  # type: ignore[attr-defined]
        raise
    verification = verify_live_payloads(resource_payload, search_payload, live, station,
                                        retrieved_at)
    records = search_payload.get("result", {}).get("records", []) \
        if isinstance(search_payload.get("result"), dict) else []
    csv_text = records_to_normalized_csv(
        [r for r in records if isinstance(r, dict)], live, station, station_id, retrieved_at)

    stamp = retrieved_at.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    snapshot_dir = Path(snapshot_dir)
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    snapshot_path = snapshot_dir / f"{station_id.lower()}_live_{stamp}.csv"
    snapshot_path.write_bytes(csv_text.encode("utf-8"))
    raw_paths = {}
    for name, raw in (("resource_show", resource_raw), ("datastore_search", search_raw)):
        path = snapshot_dir / f"{station_id.lower()}_live_{stamp}_{name}.json"
        path.write_bytes(raw)
        raw_paths[name] = path.as_posix()
    snapshot_sha = hashlib.sha256(csv_text.encode("utf-8")).hexdigest()
    verification["snapshot_sha256"] = snapshot_sha
    return {
        "verification": verification,
        "snapshot_path": snapshot_path,
        "snapshot_sha256": snapshot_sha,
        "raw_paths": raw_paths,
        "attempts": client.attempts,
        "station_id": station_id,
        "retrieved_at": retrieved_at,
    }
