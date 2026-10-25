"""Read-only dashboard adapter: one sanitized JSON document per mode.

It runs the EXISTING workflow (src.river_agent_workflow) and reshapes its
output for display. Nothing is recalculated here: risk, evidence eligibility,
state transitions and capability decisions are exactly the workflow's.

Real mode:      live river evidence (mode "live", no historical fallback), live
                Open-Meteo weather, and the persisted REAL state record read
                read-only. If evidence is stale or unavailable, that is what the
                document says; nothing is substituted.
Simulated mode: the existing fabricated scenarios (run_risk_scenario) and the
                flood-cycle sequence (run_state_sequence). Always labelled
                SIMULATED DATA; never touches the real state record.

Safety:
- The dashboard NEVER saves a state record and exposes no write operation.
  Workflow runs append to their own audit log, as every workflow run does.
- Query values are whitelisted; no client input reaches a file path.
- Absolute file paths are removed from the output; audit details pass through
  audit_log.redact.
- Results are cached and computed one at a time, so polling cannot hammer
  the NWDP or Open-Meteo APIs.
"""

from __future__ import annotations

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog, redact
from src.disaster_state_manager import SEQUENCES, STATES, load_state_record, run_state_sequence
from src.risk_assessment import WEATHER_SIMULATED_SOURCE_TYPE
from src.river_agent_workflow import RISK_SCENARIOS, run_risk_scenario, run_river_workflow
from src.river_monitoring_agent import apply_quality_control, load_observations, parse_timestamp
from src.river_service import RiverEvidenceService
from src.weather_agent import LOCATION_DESCRIPTION, LATITUDE, LONGITUDE, TIMEZONE, WeatherAgent

SCHEMA_VERSION = "1.0"
MODES = ("real", "simulated")
DEFAULT_STATE_FILE = "outputs/state/real_state.json"
DEFAULT_AUDIT_PATH = "outputs/audit/dashboard_audit.jsonl"
SERIES_HOURS = 48
MAX_AUDIT_EVENTS = 100
SIMULATED_LABEL = "SIMULATED DATA"
REAL_LABEL = "REAL DATA"

SCENARIO_DESCRIPTIONS = {
    "low-rain-valid-river": "Steady river below the TEST warning level, light rain.",
    "heavy-forecast-stable-river": "Steady low river, 130.8 mm forecast for the next 24 h.",
    "rising-river-increasing-rain": "River rising 0.17 m/h below warning, rain intensifying.",
    "stale-river": "Last river reading 9 h old at assessment time.",
    "missing-weather": "Steady low river, no weather evidence.",
    "conflicting-evidence": "Two sources disagree on the current reading; re-check backs "
                            "the other source.",
    "simulated-high-risk": "River above the TEST danger level and rising, extreme rain.",
    "recovery-after-high-water": "River falling back through the TEST warning level.",
}
SEQUENCE_DESCRIPTIONS = {
    "flood-cycle": "Full fabricated flood cycle, evaluated every 2 h (h30-h118).",
    "short-rise": "The rising limb only (h40-h54).",
}


# --------------------------------------------------------------------------
# Sanitizing
# --------------------------------------------------------------------------

# A drive path (C:\ or C:/, not the "s:/" of "https://") or a POSIX absolute path
# (a "/" not preceded by a word character, ":" or "/", so URLs and units like m/h
# are left alone).
_ABS_PATH = re.compile(r"(?:(?<![A-Za-z])[A-Za-z]:[\\/]|(?<![\w:/.~-])/(?=[A-Za-z]))"
                       r"[^\s,;'\"<>|]*")


def sanitize(value: Any, base_dir: Path) -> Any:
    """Copy of ``value`` with absolute paths made project-relative (or reduced to a
    file name if outside the project) and secrets redacted."""
    base = Path(base_dir).resolve().as_posix().rstrip("/") + "/"

    def fix_path(match: re.Match) -> str:
        text = match.group(0).replace("\\", "/")
        if text.lower().startswith(base.lower()):
            return text[len(base):]
        if text.startswith("/") and "/" not in text[1:]:
            return text  # e.g. "/health": a route, not a file path
        return text.rsplit("/", 1)[-1] or "[path]"

    def walk(v: Any) -> Any:
        if isinstance(v, dict):
            return {k: walk(x) for k, x in v.items()}
        if isinstance(v, (list, tuple)):
            return [walk(x) for x in v]
        if isinstance(v, str):
            return _ABS_PATH.sub(fix_path, v)
        return v
    return walk(redact(value))


class RecordingAuditLog(AuditLog):
    """The normal append-only audit log, also keeping this run's (redacted) events
    in memory so the dashboard can show them without reading the log file."""

    def __init__(self, path: str | Path | None, **kwargs: Any):
        super().__init__(path, **kwargs)
        self.entries: list[dict[str, Any]] = []

    def log(self, run_id: str, event: str, **details: Any) -> bool:
        written = super().log(run_id, event, **details)
        self.entries.append({"at": self.clock().astimezone(timezone.utc).isoformat(),
                             "event": event, "written": written, "details": redact(details)})
        return written


# --------------------------------------------------------------------------
# Blocks
# --------------------------------------------------------------------------

def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def weather_block(weather: Any) -> dict[str, Any]:
    if not isinstance(weather, dict):
        return {"status": "NOT_PROVIDED", "is_simulated": False, "hourly": [],
                "reason": "No weather evidence was available for this assessment."}
    retrieved = parse_timestamp(weather.get("retrieved_at"))
    hourly = _obj(weather.get("hourly_weather"))
    variables = _obj(hourly.get("variables"))
    points = []
    for i, text in enumerate(hourly.get("time") or []):
        moment = parse_timestamp(text)
        if moment is None or retrieved is None:
            continue
        if moment < retrieved - timedelta(hours=24):
            continue  # keep the past 24 h of model estimates and the whole forecast

        def value(name: str) -> Any:
            series = variables.get(name)
            return series[i] if isinstance(series, list) and i < len(series) else None
        points.append({"time": text, "precipitation_mm": value("precipitation"),
                       "rain_mm": value("rain"),
                       "precipitation_probability_pct": value("precipitation_probability"),
                       "temperature_c": value("temperature_2m"),
                       # Open-Meteo hour T covers the hour ENDING at T.
                       "kind": "MODEL_ESTIMATE_PAST" if moment <= retrieved else "FORECAST"})
    windows = {}
    for key, entry in _obj(_obj(weather.get("rainfall_accumulation")).get("windows")).items():
        entry = _obj(entry)
        windows[key] = {"period_start": entry.get("period_start"),
                        "period_end": entry.get("period_end"),
                        "precipitation": _obj(entry.get("precipitation")),
                        "rain": _obj(entry.get("rain"))}
    current = _obj(weather.get("current_weather"))
    return {
        "status": weather.get("status"),
        "source": weather.get("source"),
        "source_type": weather.get("source_type"),
        "is_simulated": weather.get("source_type") == WEATHER_SIMULATED_SOURCE_TYPE,
        "retrieved_at": weather.get("retrieved_at"),
        "timezone": _obj(weather.get("timezone")),
        "requested_location": _obj(weather.get("requested_location")),
        "grid_location": _obj(weather.get("returned_grid_location")),
        "current": {"status": current.get("status"), "time": current.get("time"),
                    "interval_seconds": current.get("interval_seconds"),
                    "values": _obj(current.get("values")),
                    "missing": _obj(current.get("missing"))},
        "units": _obj(weather.get("units")),
        "rainfall": windows,
        "hourly": points,
        "errors": weather.get("errors") or [],
        "data_notes": weather.get("data_notes") or [],
    }


def river_series(evidence: dict[str, Any], service: RiverEvidenceService) -> dict[str, Any]:
    """Accepted and QC-rejected readings for the SERIES_HOURS before the latest
    reading, read from the file the evidence itself came from (server-side path)."""
    out: dict[str, Any] = {"points": [], "rejected": [], "hours": SERIES_HOURS,
                           "status": "UNAVAILABLE", "reason": None}
    live = _obj(evidence.get("live_fetch"))
    raw = live.get("snapshot_path") or _obj(evidence.get("input_file")).get("path")
    if not raw:
        out["reason"] = "The evidence names no readable source file."
        return out
    path = Path(raw).resolve()
    # Only server-configured locations: the project folder or the live snapshot folder.
    roots = [Path(service.base_dir).resolve()]
    snapshot_dir = service.resolve_path(_obj(service.config.get("outputs")).get("snapshot_dir"))
    if snapshot_dir:
        roots.append(snapshot_dir.resolve())
    if not any(path.is_relative_to(root) for root in roots):
        out["reason"] = "Source file is outside the configured data folders; not read."
        return out
    loaded = load_observations(path)
    if loaded["fatal"]:
        out["reason"] = "Source file could not be read."
        return out
    station = evidence.get("station_id")
    reference = parse_timestamp(_obj(evidence.get("freshness")).get("reference_time"))
    obs = [o for o in loaded["observations"] if o["station_id"] == station
           and (reference is None or o["observed_at"] <= reference)]
    accepted, rejected, _ = apply_quality_control(obs, (service.qc or {}).get(station))
    if not accepted:
        out["reason"] = "No QC-accepted readings."
        return out
    end = max(o["observed_at"] for o in accepted)
    start = end - timedelta(hours=SERIES_HOURS)
    out["points"] = [{"time": o["observed_at"].isoformat(), "level_m": o["river_level_m"]}
                     for o in sorted(accepted, key=lambda o: o["observed_at"])
                     if o["observed_at"] >= start]
    out["rejected"] = [{"time": o["observed_at"].isoformat(), "level_m": o["river_level_m"],
                        "qc_code": o.get("qc_code")} for o in rejected
                       if start <= o["observed_at"] <= end]
    out.update(status="AVAILABLE", window_start=start.isoformat(), window_end=end.isoformat())
    return out


def river_block(result: dict[str, Any], evidence: dict[str, Any],
                service: RiverEvidenceService) -> dict[str, Any]:
    quality = _obj(evidence.get("data_quality"))
    ds = _obj(evidence.get("decision_support"))
    simulated = bool(result.get("simulated_river_level_m") is not None
                     or result.get("source_label") == "SIMULATED_TEST_DATA"
                     or quality.get("is_simulated"))
    last_known = ds.get("last_known_level_m")
    if result.get("river_level_m") is not None:
        role, value = "CURRENT_VERIFIED", result["river_level_m"]
    elif simulated and last_known is not None:
        role, value = "SIMULATED", last_known
    elif last_known is not None:
        role, value = "LAST_KNOWN_NOT_CURRENT", last_known
    else:
        role, value = "UNAVAILABLE", None
    live = _obj(evidence.get("live_fetch"))
    verification = _obj(quality.get("source_verification"))
    cross = _obj(evidence.get("cross_source_check"))
    return {
        "station_id": evidence.get("station_id"),
        "status": evidence.get("status"),
        "source_label": result.get("source_label"),
        "source": evidence.get("source"),
        "is_simulated": simulated,
        "evidence_id": evidence.get("evidence_id"),
        "level": {"value_m": value, "role": role, "observed_at": evidence.get("observed_at"),
                  "datum": _obj(evidence.get("threshold_evaluation")).get("data_datum")},
        "freshness": _obj(evidence.get("freshness")),
        "evidence_grade": quality.get("evidence_grade"),
        "is_verified_live": quality.get("is_verified_live") is True,
        "source_verification": {"status": verification.get("status"),
                                "failed_checks": verification.get("failed_checks", []),
                                "reason": verification.get("reason")},
        "recent_quality": _obj(quality.get("recent_quality")),
        "qc": _obj(quality.get("qc")),
        "counts": {k: quality.get(k) for k in ("rows_read", "valid_rows", "rejected_rows",
                                               "duplicate_rows_removed",
                                               "station_observations_used")},
        "trend": _obj(evidence.get("trend")),
        "thresholds": _obj(evidence.get("threshold_evaluation")),
        "cross_source_check": {k: cross.get(k) for k in (
            "status", "compared_with", "conflicts", "conflicts_affecting_current_evidence",
            "tolerance_m", "examples")},
        "reverification": {k: _obj(result.get("reverification")).get(k) for k in (
            "status", "outcome", "reason", "conflicts_remaining")},
        "fallback": _obj(evidence.get("fallback")),
        "live_fetch": {k: live.get(k) for k in ("attempted", "status", "verification_status",
                                                "failed_checks", "error", "retrieved_at")},
        "reasons_not_usable": ds.get("reasons_not_usable", []),
        "warnings": evidence.get("warnings") or [],
        "errors": evidence.get("errors") or [],
        "series": river_series(evidence, service) if evidence else
        {"points": [], "rejected": [], "status": "UNAVAILABLE",
         "reason": result.get("evidence_error") or "No river evidence."},
    }


def state_block(result: dict[str, Any], persisted: dict[str, Any] | None, basis: str,
                record_problem: str | None, state_file: str | None) -> dict[str, Any]:
    se = _obj(result.get("state_evaluation"))
    record = _obj(se.get("record")) or _obj(persisted)
    history = [{k: e.get(k) for k in ("observed_at", "condition", "risk_level",
                                      "evaluated_at", "reasons")}
               for e in record.get("history") or [] if isinstance(e, dict)]
    return {
        "persisted": {
            "available": persisted is not None,
            "state": (persisted or {}).get("state"),
            "scope": (persisted or {}).get("scope"),
            "entered_at": (persisted or {}).get("entered_at"),
            "last_transition": (persisted or {}).get("last_transition"),
            "record_file": Path(state_file).name if state_file else None,
            "problem": record_problem,
        },
        "state_used_for_capability_checks": result.get("disaster_state"),
        "state_basis": basis,
        "evaluation": {k: se.get(k) for k in (
            "outcome", "scope", "current_state", "proposed_state", "resulting_state",
            "condition", "reason", "reasons", "persistence", "transition_rule",
            "supporting_evidence_ids", "evidence_id", "observed_at", "evaluated_at",
            "risk_level", "is_simulated", "state_applies_to", "audit_written",
            "record_changed", "state_decision_id", "authorizes_actions",
            "executes_actions")},
        "history": history[-50:],
        "record_saved_by_dashboard": False,
        "note": ("The dashboard never saves a state record. A proposed or accepted next "
                 "state becomes the persisted state only when the workflow CLI saves it "
                 "(--state-file) after a fully audited run."),
    }


def safety_block(result: dict[str, Any]) -> dict[str, Any]:
    ea = _obj(result.get("evidence_assessment"))
    return {
        "real_world_authorization": result.get("real_world_authorization"),
        "pending_human_approval": result.get("real_world_capabilities_pending_human_approval")
        or [],
        "decisions": [{k: d.get(k) for k in ("capability", "capability_class", "allowed",
                                             "reason", "evidence_reasons",
                                             "human_approval_required", "state")}
                      for d in result.get("capability_decisions") or []],
        "evidence_assessment": {k: ea.get(k) for k in ("eligible", "reasons",
                                                       "input_problems")},
        "reason": result.get("reason"),
        "executes_actions": False,
        "note": ("Read-only view of the workflow's own decisions. Nothing here can execute "
                 "an action; real-world capabilities can at most be put to a human."),
    }


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

class DashboardService:
    def __init__(self, service: RiverEvidenceService,
                 state_file: str | Path | None = DEFAULT_STATE_FILE,
                 audit_path: str | Path | None = DEFAULT_AUDIT_PATH,
                 weather_fetcher: Callable[[], dict[str, Any]] | None = None,
                 cache_seconds: float = 300.0,
                 monotonic: Callable[[], float] = time.monotonic):
        self.service = service
        self.state_file = service.resolve_path(state_file) if state_file else None
        self.audit_path = service.resolve_path(audit_path) if audit_path else None
        self.weather_fetcher = weather_fetcher or (lambda: WeatherAgent(past_days=1).run())
        self.cache_seconds = cache_seconds
        self.monotonic = monotonic
        self._cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()  # one computation at a time: no duplicate fetches

    # ---- public ------------------------------------------------------------

    @staticmethod
    def scenarios() -> dict[str, Any]:
        return {"modes": list(MODES), "default_mode": "real",
                "scenarios": [{"name": k, "description": SCENARIO_DESCRIPTIONS.get(k, "")}
                              for k in RISK_SCENARIOS],
                "sequences": [{"name": k, "description": SEQUENCE_DESCRIPTIONS.get(k, "")}
                              for k in SEQUENCES],
                "start_states": list(STATES), "data_label": SIMULATED_LABEL}

    def get(self, mode: str, scenario: str | None = None,
            state: str | None = None) -> tuple[dict[str, Any], float]:
        """Validated, cached dashboard document. Raises ValueError for bad input."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if mode == "real" and (scenario or state):
            raise ValueError("scenario and state apply to simulated mode only")
        if mode == "simulated":
            scenario = scenario or "rising-river-increasing-rain"
            if scenario not in RISK_SCENARIOS:
                raise ValueError(f"scenario must be one of {', '.join(RISK_SCENARIOS)}")
            state = state or "NORMAL"
            if state not in STATES:
                raise ValueError(f"state must be one of {', '.join(STATES)}")
        return self._cached(("dashboard", mode, scenario, state),
                            lambda: self._real() if mode == "real"
                            else self._simulated(scenario, state))

    def sequence(self, name: str) -> tuple[dict[str, Any], float]:
        if name not in SEQUENCES:
            raise ValueError(f"sequence must be one of {', '.join(SEQUENCES)}")
        return self._cached(("sequence", name), lambda: self._sequence(name), forever=True)

    # ---- internals ------------------------------------------------------------

    def _cached(self, key: tuple, compute: Callable[[], dict[str, Any]],
                forever: bool = False) -> tuple[dict[str, Any], float]:
        with self._lock:
            hit = self._cache.get(key)
            if hit and (forever or self.monotonic() - hit[0] < self.cache_seconds):
                return hit[1], self.monotonic() - hit[0]
            doc = compute()
            if not doc.get("error"):
                self._cache[key] = (self.monotonic(), doc)
            return doc, 0.0

    def _audit(self) -> RecordingAuditLog:
        return RecordingAuditLog(self.audit_path, clock=self.service.clock,
                                 component="dashboard")

    def _base(self, mode: str, simulated: bool) -> dict[str, Any]:
        return {"schema_version": SCHEMA_VERSION,
                "generated_at": self.service.clock().astimezone(timezone.utc).isoformat(),
                "mode": mode, "is_simulated": simulated,
                "data_label": SIMULATED_LABEL if simulated else REAL_LABEL,
                "timezone": TIMEZONE,
                "location": {"description": LOCATION_DESCRIPTION, "latitude": LATITUDE,
                             "longitude": LONGITUDE},
                "cache_seconds": self.cache_seconds}

    def _real(self) -> dict[str, Any]:
        doc = self._base("real", False)
        try:
            persisted, problem = None, None
            if self.state_file and self.state_file.is_file():
                try:
                    persisted = load_state_record(self.state_file)
                except ValueError as exc:
                    problem = str(exc)
                if persisted is not None and persisted.get("scope") != "real":
                    problem = (f"Persisted record has scope {persisted.get('scope')!r}, not "
                               "'real'; it is ignored.")
                    persisted = None
            # Without a valid real record, authorization uses NORMAL: the most restrictive
            # allowlist. The dashboard says so instead of inventing a state.
            state = persisted["state"] if persisted else "NORMAL"
            basis = ("PERSISTED_RECORD" if persisted else "INVALID_RECORD_NORMAL_ASSUMED"
                     if problem else "NO_RECORD_NORMAL_ASSUMED")
            try:
                weather = self.weather_fetcher()
                weather_error = None
            except Exception as exc:  # weather failure is shown, never hidden
                weather, weather_error = None, f"{type(exc).__name__}: {exc}"
            audit = self._audit()
            result = run_river_workflow(self.service, audit, mode="live",
                                        disaster_state=state, weather=weather,
                                        state_record=persisted, include_evidence=True)
            doc.update(self._assemble(result, audit, persisted, basis, problem))
            if weather_error:
                doc["weather"]["fetch_error"] = weather_error
        except Exception as exc:  # never crash the server; never invent data
            doc["error"] = f"Dashboard could not be assembled ({type(exc).__name__})."
        return sanitize(doc, self.service.base_dir)

    def _simulated(self, scenario: str, state: str) -> dict[str, Any]:
        doc = self._base("simulated", True)
        doc.update(scenario=scenario, scenario_description=SCENARIO_DESCRIPTIONS.get(scenario),
                   start_state=state)
        try:
            audit = self._audit()
            result = run_risk_scenario(scenario, self.service, audit, disaster_state=state,
                                       include_evidence=True)
            doc.update(self._assemble(result, audit, None, "SCENARIO_START_STATE", None))
        except Exception as exc:
            doc["error"] = f"Simulation could not be run ({type(exc).__name__})."
        return sanitize(doc, self.service.base_dir)

    def _sequence(self, name: str) -> dict[str, Any]:
        doc = self._base("simulated", True)
        doc.update(sequence=name, description=SEQUENCE_DESCRIPTIONS.get(name))
        try:
            doc["steps"] = run_state_sequence(name, self.service, self._audit())
        except Exception as exc:
            doc["error"] = f"Sequence could not be run ({type(exc).__name__})."
        return sanitize(doc, self.service.base_dir)

    def _assemble(self, result: dict[str, Any], audit: RecordingAuditLog,
                  persisted: dict[str, Any] | None, basis: str,
                  record_problem: str | None) -> dict[str, Any]:
        evidence = _obj(result.get("evidence"))
        return {
            "run_id": result.get("run_id"),
            "decision_id": result.get("decision_id"),
            "weather": weather_block(result.get("weather_evidence")),
            "river": river_block(result, evidence, self.service),
            "risk": result.get("risk_assessment"),
            "state": state_block(result, persisted, basis, record_problem,
                                 str(self.state_file) if self.state_file else None),
            "safety": safety_block(result),
            "audit": {"events": audit.entries[-MAX_AUDIT_EVENTS:],
                      "write_errors": result.get("workflow_audit_write_errors") or [],
                      "log_file": self.audit_path.name if self.audit_path else None},
        }
