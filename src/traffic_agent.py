"""Traffic Agent for the flood disaster management prototype.

Monitors road segments on the evacuation corridors between Upper Anicut
(Mukkombu) and Tiruchirappalli, classifies congestion and road closures, and
reports whether each configured route is usable.

Data comes from one of two sources with the same payload shape:
- live: the TomTom Traffic Flow Segment Data API (needs an API key in the
  environment variable named in the config, TOMTOM_API_KEY by default);
- simulated: a FABRICATED scenario file in data/simulated/traffic/ holding
  TomTom-shaped responses. It is parsed by the same code and labelled
  SIMULATED_TEST_DATA so it can never pass for real traffic.

Design rules:
- Fetching (``LiveTrafficSource`` / ``SimulatedTrafficSource``) is separate
  from parsing (``parse_segment``) and from route assessment
  (``assess_routes``), so each can be tested on its own.
- A segment without usable data is UNKNOWN, never "free flow". A route with an
  UNKNOWN segment is UNKNOWN unless another segment is known to be closed.
- Recommendations are advisory only. The agent never authorizes or executes
  an action; that is the job of capability control and risk assessment.
- The API key never appears in the output: URLs and error messages are redacted.
"""

from __future__ import annotations

import argparse
import copy
import json
import math
import os
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlencode

import requests

from src.audit_log import redact

AGENT_NAME = "TrafficAgent"
LIVE_PROVIDER = "TomTom Traffic Flow Segment Data API (https://developer.tomtom.com)"
BASE_URL = "https://api.tomtom.com/traffic/services/4/flowSegmentData/absolute/{zoom}/json"
SOURCE_TYPE_LIVE = "REAL_TIME_PROBE_TRAFFIC"
SOURCE_TYPE_SIMULATED = "SIMULATED_TEST_DATA"
MODE_LIVE = "live"
MODE_SIMULATED = "simulated"

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "traffic_agent.json"
SIMULATED_DIR = PROJECT_ROOT / "data" / "simulated" / "traffic"
OUTPUT_DIR = PROJECT_ROOT / "outputs"

# (connect timeout, read timeout) in seconds.
DEFAULT_TIMEOUT = (5, 20)

STATUS_OK = "OK"
STATUS_PARTIAL = "PARTIAL"
STATUS_UNAVAILABLE = "DATA_UNAVAILABLE"

SECTION_AVAILABLE = "AVAILABLE"
SECTION_UNAVAILABLE = "UNAVAILABLE"

# Segment conditions, least to most severe. UNKNOWN is kept outside the order.
FREE_FLOW = "FREE_FLOW"
MODERATE = "MODERATE"
HEAVY = "HEAVY"
SEVERE = "SEVERE"
CLOSED = "CLOSED"
UNKNOWN = "UNKNOWN"
CONDITION_ORDER = (FREE_FLOW, MODERATE, HEAVY, SEVERE, CLOSED)

# Route conditions: the worst segment condition, BLOCKED when a segment is
# closed, UNKNOWN when a segment has no data and none is closed.
BLOCKED = "BLOCKED"
USABLE_ROUTE_CONDITIONS = (FREE_FLOW, MODERATE)

# A returned road farther than this from the configured point is a different road.
DEFAULT_MAX_MATCH_DISTANCE_M = 100
EARTH_RADIUS_M = 6_371_000

# Errors after which further requests would fail the same way (and use quota).
STOP_ON_HTTP_STATUS = (401, 403, 429)

DATA_NOTES = [
    "Speeds come from vehicle probe data processed by the provider; they describe the "
    "road segment nearest to each configured point (see returned_road).",
    "confidence (0-1) is the provider's confidence that the value is real-time; low values "
    "lean on historical patterns and are flagged low_confidence.",
    "A segment without usable data is UNKNOWN, never treated as free-flowing.",
    "Congestion breakpoints and routes are prototype settings, not official standards.",
]


class TrafficFetchError(Exception):
    """Raised when traffic data for a segment cannot be retrieved."""

    def __init__(self, code: str, message: str, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


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


def _redact_text(text: str) -> str:
    return redact(text) if isinstance(text, str) else text


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def load_config(path: str | Path = DEFAULT_CONFIG_PATH) -> dict[str, Any]:
    """Load and validate the traffic configuration. Raises ValueError."""
    try:
        config = json.loads(Path(path).read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Traffic config not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Traffic config is not valid JSON: {path}: {exc}") from exc
    validate_config(config)
    return config


def validate_config(config: Any) -> None:
    if not isinstance(config, dict):
        raise ValueError("Traffic config must be a JSON object.")
    thresholds = config.get("congestion_thresholds")
    if not isinstance(thresholds, dict):
        raise ValueError("congestion_thresholds is missing.")
    ratios = [_as_number(thresholds.get(k)) for k in
              ("free_flow_min_ratio", "moderate_min_ratio", "heavy_min_ratio")]
    if any(r is None for r in ratios) or not (1 >= ratios[0] > ratios[1] > ratios[2] > 0):
        raise ValueError("congestion_thresholds must satisfy "
                         "1 >= free_flow_min_ratio > moderate_min_ratio > heavy_min_ratio > 0.")
    min_conf = _as_number(config.get("min_confidence"))
    if min_conf is None or not 0 <= min_conf <= 1:
        raise ValueError("min_confidence must be a number between 0 and 1.")
    if "max_match_distance_m" in config:
        limit = _as_number(config["max_match_distance_m"])
        if limit is None or limit <= 0:
            raise ValueError("max_match_distance_m must be a positive number.")

    segments = config.get("segments")
    if not isinstance(segments, list) or not segments:
        raise ValueError("segments must be a non-empty list.")
    ids = set()
    for seg in segments:
        if not isinstance(seg, dict) or not isinstance(seg.get("id"), str):
            raise ValueError("Each segment needs a string id.")
        if seg["id"] in ids:
            raise ValueError(f"Duplicate segment id {seg['id']!r}.")
        ids.add(seg["id"])
        lat, lon = _as_number(seg.get("latitude")), _as_number(seg.get("longitude"))
        if lat is None or lon is None or not -90 <= lat <= 90 or not -180 <= lon <= 180:
            raise ValueError(f"Segment {seg['id']!r} has invalid coordinates.")

    routes = config.get("routes")
    if not isinstance(routes, list):
        raise ValueError("routes must be a list.")
    route_ids = set()
    for route in routes:
        if not isinstance(route, dict) or not isinstance(route.get("id"), str):
            raise ValueError("Each route needs a string id.")
        if route["id"] in route_ids:
            raise ValueError(f"Duplicate route id {route['id']!r}.")
        route_ids.add(route["id"])
        if route.get("role") not in ("primary", "alternate"):
            raise ValueError(f"Route {route['id']!r}: role must be 'primary' or 'alternate'.")
        members = route.get("segments")
        if not isinstance(members, list) or not members:
            raise ValueError(f"Route {route['id']!r} needs at least one segment.")
        unknown = [m for m in members if m not in ids]
        if unknown:
            raise ValueError(f"Route {route['id']!r} uses unknown segments: {unknown}.")


# --------------------------------------------------------------------------
# Data sources
# --------------------------------------------------------------------------

class LiveTrafficSource:
    """Fetches one segment at a time from the TomTom Flow Segment Data API."""

    mode = MODE_LIVE
    source = LIVE_PROVIDER
    source_type = SOURCE_TYPE_LIVE

    def __init__(self, api_key: str | None, zoom: int = 10,
                 timeout: float | tuple[float, float] = DEFAULT_TIMEOUT,
                 session: Any = None):
        self.api_key = api_key
        self.zoom = zoom
        self.timeout = timeout
        # Anything with a requests-compatible .get() (injectable for tests).
        self.session = session if session is not None else requests.Session()
        self.retrieved_at: datetime | None = None

    def url_for(self, segment: dict[str, Any], redacted: bool = True) -> str:
        params = {
            "point": f"{segment['latitude']},{segment['longitude']}",
            "unit": "KMPH",
            "key": "[REDACTED]" if redacted else (self.api_key or ""),
        }
        return f"{BASE_URL.format(zoom=self.zoom)}?{urlencode(params, safe=',[]')}"

    def fetch(self, segment: dict[str, Any]) -> Any:
        """Return the decoded JSON for one segment. Raises TrafficFetchError."""
        if not self.api_key:
            raise TrafficFetchError(
                "MISSING_API_KEY", "No TomTom API key: set the TOMTOM_API_KEY environment "
                "variable, or run with --mode simulated.")
        try:
            response = self.session.get(self.url_for(segment, redacted=False),
                                        timeout=self.timeout)
        except requests.exceptions.Timeout as exc:
            raise TrafficFetchError("NETWORK_TIMEOUT",
                                    _redact_text(f"Request timed out: {exc}")) from exc
        except requests.exceptions.ConnectionError as exc:
            raise TrafficFetchError("NETWORK_ERROR",
                                    _redact_text(f"Connection failed: {exc}")) from exc
        except requests.exceptions.RequestException as exc:
            raise TrafficFetchError("REQUEST_FAILED",
                                    _redact_text(f"Request failed: {exc}")) from exc

        if response.status_code != 200:
            raise _http_error(response.status_code, _safe_json(response))
        try:
            return response.json()
        except ValueError as exc:
            raise TrafficFetchError("INVALID_JSON",
                                    f"Response is not valid JSON: {exc}") from exc


class SimulatedTrafficSource:
    """Serves FABRICATED TomTom-shaped responses from a scenario file."""

    mode = MODE_SIMULATED
    source_type = SOURCE_TYPE_SIMULATED

    def __init__(self, path: str | Path):
        self.path = Path(path)
        try:
            doc = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise ValueError(f"Simulated traffic scenario not found: {self.path}") from exc
        except json.JSONDecodeError as exc:
            raise ValueError(f"{self.path}: not valid JSON: {exc}") from exc
        if not isinstance(doc, dict) or doc.get("simulated") is not True:
            raise ValueError(f"{self.path}: simulated traffic files must declare "
                             "\"simulated\": true.")
        try:
            moment = datetime.fromisoformat(doc.get("retrieved_at", ""))
        except (TypeError, ValueError):
            moment = None
        if moment is None or moment.tzinfo is None:
            raise ValueError(f"{self.path}: retrieved_at must be a timezone-aware "
                             "ISO 8601 time.")
        if not isinstance(doc.get("responses"), dict):
            raise ValueError(f"{self.path}: 'responses' must map segment ids to responses.")
        self.retrieved_at = moment
        self.summary = doc.get("summary")
        self.responses = doc["responses"]
        self.source = f"SIMULATED traffic file (fabricated): {self.path.name}"

    def url_for(self, segment: dict[str, Any], redacted: bool = True) -> None:
        return None

    def fetch(self, segment: dict[str, Any]) -> Any:
        entry = self.responses.get(segment["id"])
        if entry is None:
            raise TrafficFetchError("NO_RESPONSE",
                                    "Scenario has no response for this segment.")
        status = entry.get("http_status", 200)
        if status != 200:
            raise _http_error(status, entry.get("body"))
        return entry.get("body")


def _safe_json(response: Any) -> Any:
    try:
        return response.json()
    except ValueError:
        return None


def _http_error(status: int, body: Any) -> TrafficFetchError:
    detail = None
    if isinstance(body, dict):
        detail = body.get("error") or (body.get("detailedError") or {}).get("message")
    hint = {401: " (API key missing or invalid)", 403: " (API key rejected or not "
            "enabled for Traffic)", 429: " (rate limit or daily quota exceeded)"}.get(status, "")
    suffix = f": {detail}" if detail else ""
    return TrafficFetchError("HTTP_ERROR",
                             _redact_text(f"API returned HTTP {status}{hint}{suffix}"),
                             http_status=status)


# --------------------------------------------------------------------------
# Parsing (pure, no network)
# --------------------------------------------------------------------------

def classify_speed_ratio(ratio: float | None, thresholds: dict[str, Any]) -> str:
    if ratio is None:
        return UNKNOWN
    if ratio >= thresholds["free_flow_min_ratio"]:
        return FREE_FLOW
    if ratio >= thresholds["moderate_min_ratio"]:
        return MODERATE
    if ratio >= thresholds["heavy_min_ratio"]:
        return HEAVY
    return SEVERE


def unavailable_segment(segment: dict[str, Any], reason: str) -> dict[str, Any]:
    return {
        "id": segment["id"],
        "name": segment.get("name"),
        "requested_point": {"latitude": segment["latitude"], "longitude": segment["longitude"]},
        "status": SECTION_UNAVAILABLE,
        "condition": UNKNOWN,
        "road_class": None,
        "returned_road": None,
        "current_speed_kmph": None,
        "free_flow_speed_kmph": None,
        "speed_ratio": None,
        "current_travel_time_s": None,
        "free_flow_travel_time_s": None,
        "delay_s": None,
        "road_closure": None,
        "confidence": None,
        "low_confidence": None,
        "duplicate_of": None,
        "reason": reason,
    }


def _parse_points(raw: Any) -> list[tuple[float, float]]:
    """Valid (lat, lon) pairs from a TomTom coordinate list; [] if any is invalid."""
    if not isinstance(raw, list):
        return []
    points = []
    for item in raw:
        lat = _as_number(item.get("latitude")) if isinstance(item, dict) else None
        lon = _as_number(item.get("longitude")) if isinstance(item, dict) else None
        if lat is None or lon is None:
            return []
        points.append((lat, lon))
    return points


def _distance_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    # Equirectangular approximation: accurate to well under 1% over a few km.
    mean_lat = math.radians((a[0] + b[0]) / 2)
    dx = math.radians(b[1] - a[1]) * math.cos(mean_lat)
    dy = math.radians(b[0] - a[0])
    return EARTH_RADIUS_M * math.hypot(dx, dy)


def distance_to_polyline_m(lat: float, lon: float,
                           points: list[tuple[float, float]]) -> float:
    """Shortest distance in metres from a point to a polyline."""
    scale = math.cos(math.radians(lat))

    def xy(p: tuple[float, float]) -> tuple[float, float]:
        return (math.radians(p[1] - lon) * scale * EARTH_RADIUS_M,
                math.radians(p[0] - lat) * EARTH_RADIUS_M)

    if len(points) == 1:
        return math.hypot(*xy(points[0]))
    best = math.inf
    for a, b in zip(points, points[1:]):
        (ax, ay), (bx, by) = xy(a), xy(b)
        dx, dy = bx - ax, by - ay
        length_sq = dx * dx + dy * dy
        t = 0.0 if length_sq == 0 else max(0.0, min(1.0, -(ax * dx + ay * dy) / length_sq))
        best = min(best, math.hypot(ax + t * dx, ay + t * dy))
    return best


def mark_duplicate_segments(segments: dict[str, dict[str, Any]],
                            errors: list[dict[str, Any]]) -> None:
    """Flag segments whose provider data is the same road segment as an earlier one.

    Two configured points can fall on one long provider segment; their readings
    are then one measurement, not two independent pieces of evidence.
    """
    seen: dict[tuple, str] = {}
    for seg in segments.values():
        road = seg["returned_road"]
        if seg["status"] != SECTION_AVAILABLE or road is None:
            continue
        key = (tuple(road["first_point"].values()), tuple(road["last_point"].values()),
               road["point_count"], seg["current_speed_kmph"], seg["free_flow_speed_kmph"])
        if key in seen:
            seg["duplicate_of"] = seen[key]
            errors.append(_error("DUPLICATE_PROVIDER_SEGMENT",
                                 f"Same provider road segment as {seen[key]}; reported once "
                                 "in claims.", f"segments.{seg['id']}", severity="warning"))
        else:
            seen[key] = seg["id"]


def parse_segment(segment: dict[str, Any], payload: Any, config: dict[str, Any],
                  errors: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate one Flow Segment Data payload and classify the segment."""
    field = f"segments.{segment['id']}"

    def fail(code: str, reason: str) -> dict[str, Any]:
        errors.append(_error(code, reason, field))
        return unavailable_segment(segment, reason)

    if not isinstance(payload, dict) or not isinstance(payload.get("flowSegmentData"), dict):
        return fail("INVALID_PAYLOAD", "Response has no 'flowSegmentData' object.")
    data = payload["flowSegmentData"]
    out = unavailable_segment(segment, None)

    closure = data.get("roadClosure")
    out["road_closure"] = closure if isinstance(closure, bool) else None
    current = _as_number(data.get("currentSpeed"))
    free = _as_number(data.get("freeFlowSpeed"))
    if current is not None and current < 0:
        current = None
    if free is not None and free <= 0:
        free = None

    if out["road_closure"] is not True and (current is None or free is None):
        missing = [name for name, value in (("currentSpeed", current),
                                            ("freeFlowSpeed", free)) if value is None]
        return fail("MISSING_VALUE", f"{', '.join(missing)} missing or invalid; "
                                     "congestion cannot be assessed.")

    out["status"] = SECTION_AVAILABLE
    out["current_speed_kmph"] = current
    out["free_flow_speed_kmph"] = free
    if current is not None and free is not None:
        out["speed_ratio"] = round(min(current / free, 1.0), 3)
    out["road_class"] = data.get("frc") if isinstance(data.get("frc"), str) else None

    cur_tt = _as_number(data.get("currentTravelTime"))
    free_tt = _as_number(data.get("freeFlowTravelTime"))
    out["current_travel_time_s"] = cur_tt
    out["free_flow_travel_time_s"] = free_tt
    if cur_tt is not None and free_tt is not None:
        out["delay_s"] = max(cur_tt - free_tt, 0.0)

    points = _parse_points((data.get("coordinates") or {}).get("coordinate"))
    if points:
        match = distance_to_polyline_m(segment["latitude"], segment["longitude"], points)
        out["returned_road"] = {
            "first_point": {"latitude": points[0][0], "longitude": points[0][1]},
            "last_point": {"latitude": points[-1][0], "longitude": points[-1][1]},
            "point_count": len(points),
            "length_m": round(sum(_distance_m(a, b) for a, b in zip(points, points[1:]))),
            "match_distance_m": round(match, 1),
        }
        limit = config.get("max_match_distance_m", DEFAULT_MAX_MATCH_DISTANCE_M)
        if match > limit:
            # The provider snapped to a different road (e.g. no coverage for a minor
            # road); its speeds say nothing about the configured segment.
            failed = fail("ROAD_MISMATCH",
                          f"Provider returned a road {match:.0f} m from the configured "
                          f"point (limit {limit} m); its data describes a different road.")
            failed["returned_road"] = out["returned_road"]
            failed["road_class"] = out["road_class"]
            return failed
    else:
        errors.append(_error("NO_GEOMETRY", "Response has no road geometry; the matched "
                             "road cannot be verified.", f"{field}.coordinates",
                             severity="warning"))

    conf = _as_number(data.get("confidence"))
    if conf is not None and not 0 <= conf <= 1:
        errors.append(_error("INVALID_FIELD", f"confidence {conf} is outside 0-1; ignored.",
                             f"{field}.confidence", severity="warning"))
        conf = None
    out["confidence"] = conf
    out["low_confidence"] = conf is None or conf < config["min_confidence"]
    if out["low_confidence"]:
        shown = "missing" if conf is None else f"{conf}"
        errors.append(_error("LOW_CONFIDENCE",
                             f"Provider confidence is {shown} (minimum "
                             f"{config['min_confidence']}); the value may rely on "
                             "historical patterns rather than live probes.",
                             f"{field}.confidence", severity="warning"))

    if out["road_closure"] is True:
        out["condition"] = CLOSED
    else:
        out["condition"] = classify_speed_ratio(out["speed_ratio"],
                                                config["congestion_thresholds"])
        if out["road_closure"] is None:
            errors.append(_error("MISSING_FIELD", "roadClosure missing; closure status "
                                 "assumed unknown, condition taken from speeds only.",
                                 f"{field}.roadClosure", severity="warning"))
    return out


# --------------------------------------------------------------------------
# Route assessment and advisories (pure)
# --------------------------------------------------------------------------

def assess_route(route: dict[str, Any], segments: dict[str, dict[str, Any]]) -> dict[str, Any]:
    members = [segments[sid] for sid in route["segments"]]
    closed = [s["id"] for s in members if s["condition"] == CLOSED]
    unknown = [s["id"] for s in members if s["condition"] == UNKNOWN]
    known = [s for s in members if s["condition"] != UNKNOWN]
    out: dict[str, Any] = {
        "id": route["id"],
        "name": route.get("name"),
        "role": route["role"],
        "segments": list(route["segments"]),
        "condition": UNKNOWN,
        "passable": None,
        "worst_segment": None,
        "closed_segments": closed,
        "unknown_segments": unknown,
        "low_confidence_segments": [s["id"] for s in known if s["low_confidence"]],
        "total_delay_s": None,
        "reason": None,
    }
    if closed:
        out["condition"], out["passable"] = BLOCKED, False
        out["worst_segment"] = closed[0]
        out["reason"] = f"Road closure reported on {', '.join(closed)}."
        return out
    if unknown:
        out["reason"] = (f"No usable data for {', '.join(unknown)}; the route is not "
                         "assumed passable.")
        return out
    worst = max(known, key=lambda s: CONDITION_ORDER.index(s["condition"]))
    out["condition"], out["passable"] = worst["condition"], True
    out["worst_segment"] = worst["id"]
    delays = [s["delay_s"] for s in known]
    if all(d is not None for d in delays):
        out["total_delay_s"] = sum(delays)
    return out


def build_advisories(routes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Advisory suggestions only; never an authorization to act."""
    advisories = []
    alternates = [r for r in routes.values() if r["role"] == "alternate"]
    usable_alternates = [r for r in alternates if r["condition"] in USABLE_ROUTE_CONDITIONS
                         and not r["low_confidence_segments"]]
    for route in routes.values():
        if route["role"] != "primary":
            continue
        base = {"route": route["id"], "advisory_only": True,
                "authorization": "NOT_AUTHORIZED: must pass capability control, risk "
                                 "assessment and (where required) human approval."}
        if route["condition"] == UNKNOWN:
            advisories.append({**base, "type": "ROUTE_STATUS_UNKNOWN",
                               "message": f"{route['id']}: {route['reason']} Verify before "
                                          "directing traffic onto it."})
        elif route["condition"] in (BLOCKED, SEVERE, HEAVY):
            if usable_alternates:
                best = min(usable_alternates,
                           key=lambda r: CONDITION_ORDER.index(r["condition"]))
                advisories.append({**base, "type": "CONSIDER_ALTERNATE_ROUTE",
                                   "alternate_route": best["id"],
                                   "message": f"{route['id']} is {route['condition']}; "
                                              f"{best['id']} is {best['condition']}."})
            else:
                advisories.append({**base, "type": "NO_CLEAR_ROUTE",
                                   "message": f"{route['id']} is {route['condition']} and no "
                                              "alternate route is confirmed usable."})
    return advisories


def build_claims(segments: dict[str, dict[str, Any]], routes: dict[str, dict[str, Any]],
                 observed_at: str, is_simulated: bool) -> list[dict[str, Any]]:
    """Claims for the Evidence Verification Engine. Unknown items make no claim."""
    claims = []
    for seg in segments.values():
        if seg["condition"] == UNKNOWN or seg["duplicate_of"]:
            continue
        claims.append({
            "claim_id": f"traffic:segment:{seg['id']}",
            "agent_name": AGENT_NAME,
            "claim_type": "ROAD_SEGMENT_CONDITION",
            "subject": seg["id"],
            "value": seg["condition"],
            "observed_at": observed_at,
            "confidence": seg["confidence"],
            "is_simulated": is_simulated,
            "evidence": {k: seg[k] for k in ("current_speed_kmph", "free_flow_speed_kmph",
                                             "speed_ratio", "road_closure", "delay_s")},
        })
    for route in routes.values():
        if route["condition"] == UNKNOWN:
            continue
        confs = [segments[sid]["confidence"] for sid in route["segments"]
                 if segments[sid]["condition"] != UNKNOWN]
        claims.append({
            "claim_id": f"traffic:route:{route['id']}",
            "agent_name": AGENT_NAME,
            "claim_type": "ROUTE_CONDITION",
            "subject": route["id"],
            "value": route["condition"],
            "observed_at": observed_at,
            # A route is only as certain as its least certain segment.
            "confidence": None if any(c is None for c in confs) else min(confs),
            "is_simulated": is_simulated,
            "evidence": {"segments": route["segments"], "worst_segment": route["worst_segment"],
                         "closed_segments": route["closed_segments"]},
        })
    return claims


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------

class TrafficAgent:
    """Collects, validates and assesses traffic for the configured segments and routes."""

    def __init__(self, source: LiveTrafficSource | SimulatedTrafficSource,
                 config: dict[str, Any] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.config = config if config is not None else load_config()
        validate_config(self.config)
        self.source = source
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.raw_responses: dict[str, Any] = {}
        self._result: dict[str, Any] | None = None

    def run(self) -> dict[str, Any]:
        """Fetch every segment, assess routes and store the result. Never raises
        for data problems."""
        errors: list[dict[str, Any]] = []
        segments: dict[str, dict[str, Any]] = {}
        self.raw_responses = {}
        stop: TrafficFetchError | None = None

        for seg in self.config["segments"]:
            if stop is not None:
                segments[seg["id"]] = unavailable_segment(
                    seg, f"Not requested after earlier failure: {stop.message}")
                continue
            try:
                payload = self.source.fetch(seg)
            except TrafficFetchError as exc:
                errors.append(_error(exc.code, exc.message, f"segments.{seg['id']}"))
                segments[seg["id"]] = unavailable_segment(seg, exc.message)
                if exc.code == "MISSING_API_KEY" or exc.http_status in STOP_ON_HTTP_STATUS:
                    stop = exc
                continue
            self.raw_responses[seg["id"]] = payload
            segments[seg["id"]] = parse_segment(seg, payload, self.config, errors)
        mark_duplicate_segments(segments, errors)

        retrieved_at = getattr(self.source, "retrieved_at", None) or self.clock()
        self._result = self._build_result(segments, errors, retrieved_at)
        return self.to_dict()

    def _build_result(self, segments: dict[str, dict[str, Any]],
                      errors: list[dict[str, Any]], retrieved_at: datetime) -> dict[str, Any]:
        is_simulated = self.source.mode == MODE_SIMULATED
        routes = {r["id"]: assess_route(r, segments) for r in self.config["routes"]}
        available = sum(s["status"] == SECTION_AVAILABLE for s in segments.values())
        if available == 0:
            status = STATUS_UNAVAILABLE
        elif available == len(segments):
            status = STATUS_OK
        else:
            status = STATUS_PARTIAL

        notes = list(DATA_NOTES)
        if is_simulated:
            notes.insert(0, "FABRICATED test traffic; not an observation of any real road.")
        observed_at = retrieved_at.isoformat()
        return {
            "agent_name": AGENT_NAME,
            "status": status,
            "mode": self.source.mode,
            "is_simulated": is_simulated,
            "scenario_summary": getattr(self.source, "summary", None),
            "source": self.source.source,
            "source_type": self.source.source_type,
            "source_urls": {s["id"]: self.source.url_for(s) for s in self.config["segments"]},
            "data_notes": notes,
            "retrieved_at": observed_at,
            "summary": {
                "segments_total": len(segments),
                "segments_available": available,
                "routes": {rid: r["condition"] for rid, r in routes.items()},
            },
            "segments": segments,
            "routes": routes,
            "advisories": build_advisories(routes),
            "claims": build_claims(segments, routes, observed_at, is_simulated),
            "errors": errors,
        }

    def to_dict(self) -> dict[str, Any]:
        """Return a copy of the current structured result."""
        if self._result is None:
            segments = {s["id"]: unavailable_segment(s, "TrafficAgent.run() has not been "
                                                        "called yet.")
                        for s in self.config["segments"]}
            return self._build_result(segments, [_error(
                "NOT_RUN", "TrafficAgent.run() has not been called yet.")], self.clock())
        return copy.deepcopy(self._result)


# --------------------------------------------------------------------------
# Construction helpers, saving and CLI
# --------------------------------------------------------------------------

def list_scenarios(directory: Path = SIMULATED_DIR) -> list[str]:
    return sorted(p.stem for p in directory.glob("*.json"))


def make_source(mode: str, config: dict[str, Any], scenario: str | None = None,
                session: Any = None) -> LiveTrafficSource | SimulatedTrafficSource:
    if mode == MODE_SIMULATED:
        if not scenario:
            raise ValueError("Simulated mode needs a scenario name; available: "
                             f"{', '.join(list_scenarios())}.")
        path = Path(scenario)
        if not path.suffix:
            path = SIMULATED_DIR / f"{scenario}.json"
        return SimulatedTrafficSource(path)
    provider = config.get("provider") or {}
    key = os.environ.get(provider.get("api_key_env", "TOMTOM_API_KEY"), "").strip() or None
    return LiveTrafficSource(key, zoom=provider.get("zoom", 10), session=session)


def run_simulated_at(scenario: str | Path, at: datetime,
                     config: dict[str, Any] | None = None) -> dict[str, Any]:
    """Run a FABRICATED traffic scenario as if retrieved at ``at`` (simulations only).

    The responses are parsed by the same code as live data; the result stays
    SIMULATED_TEST_DATA and its source says it was re-timed.
    """
    config = config if config is not None else load_config()
    source = make_source(MODE_SIMULATED, config, str(scenario))
    source.retrieved_at = at
    source.source = f"{source.source} (re-timed to {at.isoformat()})"
    return TrafficAgent(source, config).run()


def save_result(result: dict[str, Any], raw_responses: dict[str, Any],
                output_dir: Path = OUTPUT_DIR) -> Path:
    """Save a result, labelled with its mode and retrieval time.

    Refuses DATA_UNAVAILABLE results so a saved file always holds real responses
    (or, for simulated mode, responses from a labelled fabricated scenario).
    """
    if result.get("status") == STATUS_UNAVAILABLE:
        raise ValueError("DATA_UNAVAILABLE results are not saved.")
    stamp = datetime.fromisoformat(result["retrieved_at"]).astimezone(
        timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    output_dir.mkdir(parents=True, exist_ok=True)
    path = output_dir / f"traffic_{result['mode']}_{stamp}.json"
    label = ("SIMULATED (fabricated) traffic scenario" if result["is_simulated"]
             else "Live TomTom traffic responses")
    document = {"label": f"{label} retrieved at {result['retrieved_at']}",
                "result": result, "raw_api_responses": raw_responses}
    path.write_text(json.dumps(document, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


def _print_summary(result: dict[str, Any]) -> None:
    def show(value: Any, unit: str = "") -> str:
        return "-" if value is None else f"{value:g}{unit}"

    banner = " [SIMULATED - FABRICATED DATA]" if result["is_simulated"] else ""
    print(f"Status:        {result['status']}{banner}")
    print(f"Mode:          {result['mode']}  ({result['source']})")
    if result["scenario_summary"]:
        print(f"Scenario:      {result['scenario_summary']}")
    print(f"Retrieved at:  {result['retrieved_at']}")
    print("Segments:")
    for seg in result["segments"].values():
        flag = " (low confidence)" if seg["low_confidence"] else ""
        if seg["duplicate_of"]:
            flag += f" (same provider segment as {seg['duplicate_of']})"
        detail = (f"{show(seg['current_speed_kmph'])}/{show(seg['free_flow_speed_kmph'])} km/h, "
                  f"ratio {show(seg['speed_ratio'])}, conf {show(seg['confidence'])}"
                  if seg["status"] == SECTION_AVAILABLE else seg["reason"])
        print(f"  {seg['id']:28s} {seg['condition']:10s} {detail}{flag}")
    print("Routes:")
    for route in result["routes"].values():
        extra = f" - {route['reason']}" if route["reason"] else ""
        print(f"  {route['id']:28s} [{route['role']}] {route['condition']}{extra}")
    if result["advisories"]:
        print("Advisories (advisory only, NOT authorized actions):")
        for adv in result["advisories"]:
            print(f"  {adv['type']}: {adv['message']}")
    for err in result["errors"]:
        print(f"  [{err['severity']}] {err['code']} {err['field'] or ''}: {err['message']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the flood-project Traffic Agent once.")
    parser.add_argument("--mode", choices=(MODE_LIVE, MODE_SIMULATED), default=MODE_LIVE,
                        help="live (TomTom, needs TOMTOM_API_KEY) or simulated (scenario file).")
    parser.add_argument("--scenario", help="Simulated scenario name (or path to a JSON file).")
    parser.add_argument("--list-scenarios", action="store_true",
                        help="List the simulated scenarios and exit.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH),
                        help="Path to the traffic config JSON.")
    parser.add_argument("--json", action="store_true", help="Print the full result as JSON.")
    parser.add_argument("--save", action="store_true",
                        help="Save the result to outputs/ (not for DATA_UNAVAILABLE).")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    if args.list_scenarios:
        for name in list_scenarios():
            print(name)
        return 0
    try:
        config = load_config(args.config)
        source = make_source(args.mode, config, args.scenario)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    agent = TrafficAgent(source, config)
    result = agent.run()
    if args.json:
        print(json.dumps(result, indent=2, ensure_ascii=False))
    else:
        _print_summary(result)

    if args.save:
        try:
            path = save_result(result, agent.raw_responses)
            print(f"Saved result to {path}")
        except ValueError as exc:
            print(f"Not saved: {exc}", file=sys.stderr)
    return 1 if result["status"] == STATUS_UNAVAILABLE else 0


if __name__ == "__main__":
    sys.exit(main())
