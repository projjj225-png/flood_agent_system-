"""Continuous demonstration simulation: SIMULATED LIVE river, rain and traffic evidence.

    python -m src.simulation_engine --ticks 45 --scenario FULL_CYCLE

Public live CWC access is unavailable, so the dashboard's demonstration runs on this
engine. Every reading it produces is FABRICATED and labelled SIMULATED LIVE.

One tick (every ``tick_seconds`` of real time, adjustable 1x/2x/5x):
1. The SIMULATION clock advances by ``simulated_minutes_per_tick`` (default 60, the
   cadence of CWC hourly telemetry).
2. DemoRiverGenerator adds one reading: the level relaxes towards the active
   scenario's target level, rate-limited, plus small deterministic noise
   (seeded per session and timestamp). Readings are never independent random
   numbers: each follows from the previous one.
3. One hour of rain is generated the same way (intensity relaxing towards the
   scenario's rain rate), and an Open-Meteo-shaped payload is built from the
   rolling rain history plus the scenario's forecast. It is parsed by the real
   WeatherAgent parser and labelled simulated.
4. The scenario's fabricated TrafficAgent file is re-timed to the simulation time.
5. run_river_workflow evaluates everything exactly as for any other evidence:
   River Agent validation and QC, freshness, trend, Risk Assessment Engine,
   Evidence Layer, Disaster State Manager (persistence, hysteresis, legal
   transitions, traffic check) and state-aware capability control.
6. src.insights turns the result into the dashboard blocks (risk score, trust
   score, agent cards, state view, capability cards).

A scenario sets CONDITIONS (target level, rainfall, traffic), never the disaster
state. Every session starts in NORMAL; the state changes only when the state
manager's rules accept a transition. There is no way to assign a state.

Separation from real data:
- Station UPPER_ANICUT_SIM (twin of UPPER_ANICUT, same QC rules, TEST thresholds).
- Readings go to their own CSV (outputs/simulated_demo/); the river service's audit
  goes to its own file; the workflow audit stays in memory.
- The state record has scope "simulated", lives in memory and is never written to
  the real state file. Reset discards it and starts again from NORMAL.
"""

from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import math
import os
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog, redact
from src.disaster_state_manager import load_state_config, new_state_record
from src.insights import build_insights
from src.risk_assessment import MODE_SIMULATED as SCOPE_SIMULATED
from src.risk_assessment import load_risk_config, simulated_weather_from_doc
from src.risk_score import load_scoring_config
from src.river_agent_workflow import run_river_workflow
from src.river_monitoring_agent import (
    PROJECT_DIR,
    REQUIRED_COLUMNS,
    SOURCE_SIMULATED,
    STATION_ID_PATTERN,
    load_json_file,
    validate_thresholds,
)
from src.river_service import MODE_SIMULATED, RiverEvidenceService
from src.traffic_agent import run_simulated_at as run_simulated_traffic_at

DEFAULT_DEMO_CONFIG_PATH = PROJECT_DIR / "config" / "demo_simulation.json"
WEATHER_TEMPLATE_PATH = PROJECT_DIR / "data" / "simulated" / "weather" / "light_rain.json"
LIVE_LABEL = "SIMULATED LIVE"
SCHEMA_VERSION = "2.0"
SCENARIOS = ("NORMAL", "ALERT", "EMERGENCY", "RECOVERY")
CYCLE = "FULL_CYCLE"
SCENARIO_CHOICES = SCENARIOS + (CYCLE,)
ACTIONS = ("start", "pause", "resume", "reset", "scenario", "speed")
RIVER_AUDIT_PATH = "outputs/audit/simulation_demo_river_audit.jsonl"
WEATHER_PAST_HOURS = 48
READINGS_WINDOW_HOURS = 48  # readings and rain sent to the dashboard (charts show <= 24 h)
WEATHER_FUTURE_HOURS = 48
_SCENARIO_NUMBERS = ("target_level_m", "response_per_hour", "max_rise_m_per_hour",
                     "max_fall_m_per_hour", "noise_m", "rain_mm_per_hour",
                     "rain_response_per_hour", "rain_variability", "forecast_mm_per_hour",
                     "wave_m")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _number(value: Any, name: str, minimum: float | None = 0.0) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) \
            or (minimum is not None and value < minimum):
        raise ValueError(f"Demo simulation config: {name} must be a finite number"
                         + (f" >= {minimum:g}." if minimum is not None else "."))
    return float(value)


def _traffic_plan(value: Any, name: str) -> list[dict[str, Any]]:
    if not isinstance(value, list) or not value:
        raise ValueError(f"Demo simulation config: {name}.traffic must be a non-empty list.")
    plan = []
    for item in value:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str):
            raise ValueError(f"Demo simulation config: {name}.traffic entries need a path.")
        plan.append({"from_hour": _number(item.get("from_hour"), f"{name}.traffic.from_hour"),
                     "path": item["path"]})
    hours = [p["from_hour"] for p in plan]
    if hours != sorted(hours) or len(set(hours)) != len(hours) or hours[0] != 0:
        raise ValueError(f"Demo simulation config: {name}.traffic must start at hour 0 and "
                         "increase.")
    return plan


def validate_demo_config(config: Any) -> dict[str, Any]:
    """Check the demo config; raises ValueError (a setup mistake, never ignored)."""
    if not isinstance(config, dict):
        raise ValueError("Demo simulation config must be a JSON object.")
    station = config.get("station_id")
    if not isinstance(station, str) or not STATION_ID_PATTERN.match(station):
        raise ValueError("Demo simulation config: station_id must be a valid identifier.")
    if station == config.get("twin_of"):
        raise ValueError("Demo simulation config: the simulated station must not reuse the "
                         "real station's id.")
    if not isinstance(config.get("qc_from_station"), str):
        raise ValueError("Demo simulation config: qc_from_station must name a real station.")
    _number(config.get("tick_seconds"), "tick_seconds", 0.05)
    minutes = config.get("simulated_minutes_per_tick")
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not 30 <= minutes <= 120:
        raise ValueError("Demo simulation config: simulated_minutes_per_tick must be an integer "
                         "from 30 to 120 (the state rules count observations at least 0.5 h "
                         "apart and break runs after 3 h gaps).")
    if 60 % minutes and minutes % 60:
        raise ValueError("Demo simulation config: simulated_minutes_per_tick must divide or be "
                         "a multiple of 60 so rainfall stays on whole hours.")
    per_tick = config.get("readings_per_tick", 1)
    if isinstance(per_tick, bool) or not isinstance(per_tick, int) or not 1 <= per_tick <= 12 \
            or minutes % per_tick:
        raise ValueError("Demo simulation config: readings_per_tick must be an integer 1-12 "
                         "that divides simulated_minutes_per_tick.")
    _number(config.get("wave_period_hours", 12), "wave_period_hours", 1)
    speeds = config.get("speeds")
    if not isinstance(speeds, list) or not speeds or any(
            isinstance(s, bool) or not isinstance(s, (int, float)) or s <= 0 for s in speeds):
        raise ValueError("Demo simulation config: speeds must be a list of positive numbers.")
    for key in ("warmup_hours", "history_limit", "activity_limit"):
        _number(config.get(key), key, 1)
    thresholds = validate_thresholds(config.get("thresholds"))
    if station not in thresholds:
        raise ValueError(f"Demo simulation config: thresholds must include {station}.")
    if thresholds[station].get("official"):
        raise ValueError("Demo simulation config: simulated thresholds can never be official.")
    scenarios = {k: v for k, v in (config.get("scenarios") or {}).items()
                 if not k.startswith("_")}
    if set(scenarios) != set(SCENARIOS):
        raise ValueError(f"Demo simulation config: scenarios must be exactly "
                         f"{', '.join(SCENARIOS)}.")
    for name, spec in scenarios.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Demo simulation config: scenario {name} must be an object.")
        for key in _SCENARIO_NUMBERS:
            _number(spec.get(key), f"{name}.{key}")
        scenarios[name] = {**spec, "traffic": _traffic_plan(spec.get("traffic"), name)}
    cycle = config.get("cycle") or {}
    schedule = cycle.get("schedule")
    if not isinstance(schedule, list) or not schedule:
        raise ValueError("Demo simulation config: cycle.schedule must be a non-empty list.")
    hours = []
    for step in schedule:
        if not isinstance(step, dict) or step.get("scenario") not in SCENARIOS:
            raise ValueError("Demo simulation config: cycle.schedule entries need a scenario.")
        hours.append(_number(step.get("from_hour"), "cycle.schedule.from_hour"))
    if hours != sorted(hours) or hours[0] != 0:
        raise ValueError("Demo simulation config: cycle.schedule must start at hour 0.")
    if config.get("start_scenario", CYCLE) not in SCENARIO_CHOICES:
        raise ValueError("Demo simulation config: start_scenario is not a known scenario.")
    return {**config, "thresholds": thresholds, "scenarios": scenarios}


def load_demo_config(path: str | Path = DEFAULT_DEMO_CONFIG_PATH) -> dict[str, Any]:
    return validate_demo_config(load_json_file(path, "Demo simulation config"))


# --------------------------------------------------------------------------
# Deterministic generators
# --------------------------------------------------------------------------

def noise(seed: str, key: str) -> float:
    """Deterministic pseudo-random value in [-1, 1] for (seed, key)."""
    digest = hashlib.sha256(f"{seed}|{key}".encode()).digest()
    return int.from_bytes(digest[:8], "big") / 2 ** 64 * 2 - 1


def next_level(level: float, spec: dict[str, Any], hours: float, seed: str,
               moment: datetime) -> float:
    """One rate-limited step towards the scenario's target level, plus bounded noise."""
    return next_base(level, spec, hours) + spec["noise_m"] * noise(seed,
                                                                    "level|" + moment.isoformat())


def next_base(level: float, spec: dict[str, Any], hours: float) -> float:
    """The underlying (noise-free) level: one rate-limited step towards the target."""
    step = spec["response_per_hour"] * (spec["target_level_m"] - level) * hours
    return level + max(-spec["max_fall_m_per_hour"] * hours,
                       min(spec["max_rise_m_per_hour"] * hours, step))


def reading_level(base: float, spec: dict[str, Any], seed: str, moment: datetime,
                  started: datetime, period_hours: float) -> float:
    """What the gauge reads: the underlying level plus a smooth natural fluctuation (one
    sine wave of the scenario's amplitude, continuous in time) and small bounded noise.
    Deterministic for (seed, timestamp)."""
    hours = (moment - started).total_seconds() / 3600
    return (base + spec["wave_m"] * math.sin(2 * math.pi * hours / period_hours)
            + spec["noise_m"] * noise(seed, "level|" + moment.isoformat()))


def next_intensity(intensity: float, spec: dict[str, Any], hours: float) -> float:
    """Rain intensity (mm/h) relaxing towards the scenario's rate: gradual, never a jump."""
    alpha = min(1.0, spec["rain_response_per_hour"] * hours)
    return intensity + alpha * (spec["rain_mm_per_hour"] - intensity)


def hourly_rain(intensity: float, spec: dict[str, Any], seed: str, hour_end: datetime) -> float:
    value = intensity * (1 + spec["rain_variability"] * noise(seed, "rain|" + hour_end.isoformat()))
    return round(max(0.0, value), 2)


def forecast_rain(spec: dict[str, Any], seed: str, issued: datetime, hour_end: datetime) -> float:
    value = spec["forecast_mm_per_hour"] * (1 + 0.2 * noise(seed, f"fc|{issued.isoformat()}|"
                                                                   f"{hour_end.isoformat()}"))
    return round(max(0.0, value), 2)


class MemoryAuditLog(AuditLog):
    """Audit log for the simulation: every event is kept (redacted) in a bounded
    in-memory buffer and, if a path is configured, also appended to that file.
    It never touches the real workflow audit log."""

    def __init__(self, path: str | Path | None, clock: Callable[[], datetime],
                 limit: int = 400):
        super().__init__(path, clock=clock, component="simulation_demo")
        self.entries: deque[dict[str, Any]] = deque(maxlen=limit)
        self.total = 0

    def log(self, run_id: str, event: str, **details: Any) -> bool:
        if self.path is not None and not super().log(run_id, event, **details):
            return False
        if self.path is None:
            self.events_written += 1
        self.total += 1
        self.entries.append({"at": self.clock().astimezone(timezone.utc).isoformat(),
                             "run_id": run_id, "event": event, "details": redact(details)})
        return True


LOCAL_OFFSET = timedelta(hours=5, minutes=30)  # IST: CWC telemetry and Open-Meteo use local hours


def replace_with_retry(source: str | Path, target: Path, attempts: int = 10) -> None:
    """os.replace, retried briefly: on Windows another process (search indexer, antivirus,
    an editor's file watcher) can hold the target for a moment ("Access is denied")."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.05 * (attempt + 1))


def _floor_hour(moment: datetime) -> datetime:
    """Start of the whole LOCAL (IST) hour containing ``moment``, in UTC."""
    local = (moment.astimezone(timezone.utc) + LOCAL_OFFSET).replace(minute=0, second=0,
                                                                      microsecond=0)
    return local - LOCAL_OFFSET


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class DemoSimulation:
    """The continuous demonstration: generator state, an in-memory simulated-scope
    state record, rolling histories, and an optional background ticker."""

    def __init__(self, service: RiverEvidenceService, config: dict[str, Any] | None = None,
                 wall_clock: Callable[[], datetime] | None = None,
                 monotonic: Callable[[], float] = time.monotonic,
                 scoring: dict[str, Any] | None = None,
                 risk_config: dict[str, Any] | None = None,
                 state_config: dict[str, Any] | None = None,
                 seed: str | None = None):
        self.base_service = service
        self.config = config if config is not None else load_demo_config()
        self.wall_clock = wall_clock or (lambda: datetime.now(timezone.utc))
        self.monotonic = monotonic
        self.scoring = scoring or load_scoring_config()
        self.risk_config = risk_config or load_risk_config()
        self.state_config = state_config or load_state_config()
        self.fixed_seed = seed
        self.station_id = self.config["station_id"]
        self.step = timedelta(minutes=self.config["simulated_minutes_per_tick"])
        self.readings_per_tick = int(self.config.get("readings_per_tick", 1))
        self.sub_step = self.step / self.readings_per_tick
        self.base = 0.0
        self.qc = (service.qc or {}).get(self.config["qc_from_station"])
        if not self.qc:
            raise ValueError("No QC rules for the simulated station: generated readings must "
                             "face the real station's validation.")
        self.csv_path = service.resolve_path(self.config["csv_path"])
        self.weather_template = load_json_file(WEATHER_TEMPLATE_PATH, "Weather template")
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.running = False
        self.last_error: dict[str, str] | None = None  # survives reset, shown in the snapshot
        self.speed = float(self.config["speeds"][0])
        self.next_due: float | None = None
        self.reset(self.config.get("start_scenario", CYCLE), log=False)

    # ---- setup ---------------------------------------------------------------

    def _make_service(self) -> RiverEvidenceService:
        sim_config = json.loads(json.dumps(self.base_service.config))
        sim_config["simulated"] = {"path": str(self.csv_path), "station_id": self.station_id,
                                   "cross_check_paths": []}
        sim_config["thresholds_path"] = None
        sim_config.setdefault("outputs", {})["audit_log"] = self.config.get(
            "river_audit_path", RIVER_AUDIT_PATH)
        svc = RiverEvidenceService(sim_config, base_dir=self.base_service.base_dir,
                                   session=self.base_service.session,
                                   sleep=self.base_service.sleep, clock=lambda: self.sim_time)
        svc.qc = {**(self.base_service.qc or {}), self.station_id: self.qc}
        svc.thresholds = {self.station_id: self.config["thresholds"][self.station_id]}
        return svc

    def reset(self, scenario: str | None = None, log: bool = True) -> None:
        """Start again from NORMAL with fresh simulated evidence. Never touches real data."""
        with self._lock:
            scenario = scenario or self.config.get("start_scenario", CYCLE)
            if scenario not in SCENARIO_CHOICES:
                raise ValueError(f"scenario must be one of {', '.join(SCENARIO_CHOICES)}")
            self.session_id = uuid.uuid4().hex[:10]
            self.seed = self.fixed_seed or self.session_id
            self.started_sim_time = _floor_hour(self.wall_clock())
            self.sim_time = self.started_sim_time
            self.selected = scenario
            self.scenario_since = self.sim_time
            self.tick_count = 0
            self.sequence = 0
            self.audit = MemoryAuditLog(self.base_service.resolve_path(
                self.config["audit_path"]) if self.config.get("audit_path") else None,
                clock=lambda: self.sim_time)
            self.service = self._make_service()
            self.record = new_state_record("NORMAL", SCOPE_SIMULATED)
            self.readings: list[dict[str, Any]] = []
            self.rain: dict[datetime, float] = {}
            self.history: deque[dict[str, Any]] = deque(maxlen=int(self.config["history_limit"]))
            self.transitions: list[dict[str, Any]] = []
            self.activity: deque[dict[str, Any]] = deque(maxlen=int(self.config["activity_limit"]))
            self.snapshot_doc: dict[str, Any] | None = None
            # The history before the session always shows normal conditions; the selected
            # scenario drives the river and rain only from the first step onwards, so a
            # session never starts already above a threshold.
            baseline = self.config["scenarios"]["NORMAL"]
            self.intensity = baseline["rain_mm_per_hour"]
            self._warm_up(baseline)
            if log:
                self._event("control", f"Simulation reset: new session {self.session_id}, "
                                       f"state NORMAL, {self._scenario_label(scenario)}.")
            else:
                self._event("control", f"Simulation session {self.session_id} created in "
                                       f"NORMAL with {self._scenario_label(scenario)}.")
            self._evaluate()
            if self.running:
                self.next_due = self.monotonic() + self.interval_seconds()

    def _warm_up(self, spec: dict[str, Any]) -> None:
        """Simulated readings and rain before the first evaluation, so the River Agent
        has a trend window and the Weather Agent a 24 h rain total. Labelled warm-up."""
        hours = float(self.config["warmup_hours"])
        moment = self.sim_time - timedelta(hours=hours)
        self.base = spec["target_level_m"]
        while moment <= self.sim_time:
            self.base = next_base(self.base, spec, self.sub_step.total_seconds() / 3600)
            self._add_reading(moment, self._reading(moment, spec), warmup=True)
            moment += self.sub_step
        hour = self.sim_time - timedelta(hours=WEATHER_PAST_HOURS)
        while hour <= self.sim_time:
            self.rain[hour] = hourly_rain(spec["rain_mm_per_hour"], spec, self.seed, hour)
            hour += timedelta(hours=1)

    # ---- scenarios -------------------------------------------------------------

    def _scenario_label(self, name: str) -> str:
        if name == CYCLE:
            return self.config["cycle"].get("label", "full flood cycle") + " conditions"
        return f"{self.config['scenarios'][name].get('label', name)} conditions"

    def active_scenario(self) -> str:
        """The physical conditions in force now (for the cycle: from its schedule)."""
        if self.selected != CYCLE:
            return self.selected
        hours = (self.sim_time - self.scenario_since).total_seconds() / 3600
        active = self.config["cycle"]["schedule"][0]["scenario"]
        for step in self.config["cycle"]["schedule"]:
            if step["from_hour"] <= hours:
                active = step["scenario"]
        return active

    def _active_since(self) -> datetime:
        if self.selected != CYCLE:
            return self.scenario_since
        hours = (self.sim_time - self.scenario_since).total_seconds() / 3600
        start = 0.0
        for step in self.config["cycle"]["schedule"]:
            if step["from_hour"] <= hours:
                start = step["from_hour"]
        return self.scenario_since + timedelta(hours=start)

    def conditions(self) -> dict[str, Any]:
        return self.config["scenarios"][self.active_scenario()]

    def set_scenario(self, scenario: str) -> None:
        """Change the simulated CONDITIONS. The disaster state is not touched."""
        if scenario not in SCENARIO_CHOICES:
            raise ValueError(f"scenario must be one of {', '.join(SCENARIO_CHOICES)}")
        with self._lock:
            if scenario == self.selected:
                return
            self.selected = scenario
            self.scenario_since = self.sim_time
            self._event("scenario", f"Conditions changed to {self._scenario_label(scenario)}. "
                                    "The disaster state changes only if the evidence satisfies "
                                    "the transition rules.")

    # ---- ticking ---------------------------------------------------------------

    def interval_seconds(self) -> float:
        return float(self.config["tick_seconds"]) / self.speed

    def set_speed(self, speed: Any) -> None:
        if speed not in self.config["speeds"]:
            raise ValueError(f"speed must be one of {self.config['speeds']}")
        with self._lock:
            self.speed = float(speed)
            if self.running:
                self.next_due = self.monotonic() + self.interval_seconds()
            self._event("control", f"Speed set to {speed:g}x (one simulated "
                                   f"{self.config['simulated_minutes_per_tick']} min step every "
                                   f"{self.interval_seconds():g} s).")

    def _reading(self, moment: datetime, spec: dict[str, Any]) -> float:
        return reading_level(self.base, spec, self.seed, moment, self.started_sim_time,
                             float(self.config.get("wave_period_hours", 12)))

    def tick(self, inject_level: float | None = None,
             withhold_readings: bool = False) -> dict[str, Any]:
        """Advance the simulation clock by one step, generate evidence, evaluate.

        Test hooks (not exposed over HTTP): ``inject_level`` replaces the step's last
        reading (e.g. one noisy spike), and ``withhold_readings`` simulates a sensor
        outage (the clock advances, no reading arrives). Either way the evidence goes
        through the same validation, risk and state pipeline.
        """
        with self._lock:
            start = self.sim_time
            self.sim_time += self.step
            self.tick_count += 1
            spec = self.conditions()
            if not withhold_readings:
                moment = start + self.sub_step
                while moment <= self.sim_time:
                    self.base = next_base(self.base, spec, self.sub_step.total_seconds() / 3600)
                    level = (inject_level if inject_level is not None and moment == self.sim_time
                             else self._reading(moment, spec))
                    self._add_reading(moment, level, warmup=False)
                    moment += self.sub_step
            else:
                self._event("outage", "No river reading arrived in this step (sensor outage "
                                      "test): the state manager must not treat this as safe.")
            hour = _floor_hour(self.sim_time - self.step) + timedelta(hours=1)
            while hour <= self.sim_time:
                self.intensity = next_intensity(self.intensity, spec, 1.0)
                self.rain[hour] = hourly_rain(self.intensity, spec, self.seed, hour)
                hour += timedelta(hours=1)
            cutoff = self.sim_time - timedelta(hours=WEATHER_PAST_HOURS + 1)
            self.rain = {h: v for h, v in self.rain.items() if h >= cutoff}
            return self._evaluate()

    def _add_reading(self, moment: datetime, level: float, warmup: bool) -> None:
        level = round(level, 3)
        placeholders = {round(float(v), 3) for v in self.qc.get("invalid_values_m") or []}
        previous = self.readings[-1]["level_m"] if self.readings else None
        while level in placeholders or level == previous:
            level = round(level + 0.001, 3)
        if not self.qc["valid_min_m"] <= level <= self.qc["valid_max_m"]:
            raise RuntimeError(f"Generator produced {level} m, outside the QC range: a bug, "
                               "never data.")
        self.sequence += 1
        self.readings.append({
            "observation_id": f"SIMOBS-{self.session_id}-{self.sequence:05d}",
            "observed_at": moment, "level_m": level, "warmup": warmup,
            "scenario": self.active_scenario(),
            "generated_at": self.wall_clock().astimezone(timezone.utc).isoformat(),
        })
        keep = (int(self.config["history_limit"]) + int(self.config["warmup_hours"]) + 4) \
            * self.readings_per_tick
        del self.readings[:-keep]

    def _write_csv(self) -> None:
        """Atomic write, so the River Agent never reads a half-written file."""
        path = self.csv_path
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(REQUIRED_COLUMNS))
                writer.writeheader()
                for r in self.readings:
                    writer.writerow({"timestamp": r["observed_at"].isoformat(),
                                     "station_id": self.station_id,
                                     "river_level_m": f"{r['level_m']:.3f}",
                                     "source_type": SOURCE_SIMULATED})
            replace_with_retry(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise

    def weather(self) -> dict[str, Any]:
        """Open-Meteo-shaped payload from the rain history and the scenario forecast,
        parsed by the real WeatherAgent parser and labelled simulated."""
        doc = copy.deepcopy(self.weather_template)
        payload = doc["payload"]
        offset = timedelta(seconds=payload.get("utc_offset_seconds") or 0)
        spec = self.conditions()
        now = self.sim_time
        times, rain = [], []
        hour = now - timedelta(hours=WEATHER_PAST_HOURS - 1)
        while hour <= now + timedelta(hours=WEATHER_FUTURE_HOURS):
            times.append((hour + offset).strftime("%Y-%m-%dT%H:%M"))
            rain.append(self.rain.get(hour, 0.0) if hour <= now
                        else forecast_rain(spec, self.seed, now, hour))
            hour += timedelta(hours=1)
        hourly = payload["hourly"]
        n = len(times)
        hourly["time"] = times
        hourly["precipitation"] = list(rain)
        hourly["rain"] = list(rain)
        hourly["precipitation_probability"] = [min(100.0, round(20 + 15 * r)) for r in rain]
        hourly["temperature_2m"] = [round(28.0 - min(4.0, 0.6 * r), 1) for r in rain]
        hourly["relative_humidity_2m"] = [min(100.0, round(70 + 5 * r)) for r in rain]
        hourly["wind_speed_10m"] = [round(8 + 1.5 * r, 1) for r in rain]
        hourly["wind_gusts_10m"] = [round(16 + 3 * r, 1) for r in rain]
        for key in list(hourly):
            if key != "time" and isinstance(hourly[key], list) and len(hourly[key]) != n:
                hourly[key] = [hourly[key][0]] * n
        last = self.rain.get(now, 0.0)
        current = payload["current"]
        current.update(time=(now + offset).strftime("%Y-%m-%dT%H:%M"),
                       precipitation=round(last / 4, 3), rain=round(last / 4, 3),
                       temperature_2m=round(28.0 - min(4.0, 0.6 * last), 1),
                       relative_humidity_2m=min(100.0, round(70 + 5 * last)))
        doc["retrieved_at"] = now.isoformat()
        return simulated_weather_from_doc(
            doc, f"SIMULATED LIVE weather (generated by the demonstration engine, "
                 f"{self._scenario_label(self.active_scenario())})", now, "Demo weather")

    def traffic_path(self) -> Path:
        hours = (self.sim_time - self._active_since()).total_seconds() / 3600
        plan = self.conditions()["traffic"]
        chosen = plan[0]["path"]
        for entry in plan:
            if entry["from_hour"] <= hours:
                chosen = entry["path"]
        return self.base_service.resolve_path(chosen)

    def _evaluate(self) -> dict[str, Any]:
        self._write_csv()
        weather = self.weather()
        traffic = run_simulated_traffic_at(self.traffic_path(), self.sim_time)
        before = self.record["state"]
        result = run_river_workflow(
            self.service, self.audit, mode=MODE_SIMULATED, disaster_state=before,
            cross_check_paths=[], independent_paths=[], weather=weather,
            state_record=self.record, scenario=f"demo:{self.active_scenario()}",
            include_evidence=True, traffic=traffic, risk_config=self.risk_config,
            state_config=self.state_config)
        se = result["state_evaluation"]
        if se.get("record") is not None and se["outcome"] != "EVALUATION_FAILED" \
                and not result["workflow_audit_write_errors"]:
            if se["record"].get("scope") != SCOPE_SIMULATED:  # defensive: never a real record
                raise RuntimeError("Simulation produced a non-simulated state record.")
            self.record = se["record"]
        insights = build_insights(result, self.scoring, self.risk_config, self.state_config)
        self._record_history(result, insights, before)
        self.snapshot_doc = self._build_snapshot(result, insights)
        return self.snapshot_doc

    def _record_history(self, result: dict[str, Any], insights: dict[str, Any],
                        before: str) -> None:
        se = result["state_evaluation"]
        reading = self.readings[-1]
        river = insights["agents"]["river"]["metrics"]
        weather = insights["agents"]["weather"]["metrics"]
        score = insights["risk_score"]
        entry = {
            "t": self.sim_time.isoformat(), "tick": self.tick_count,
            "observation_id": reading["observation_id"], "level_m": reading["level_m"],
            "rate_m_per_hour": river.get("rate_m_per_hour"),
            "rain_mm": self.rain.get(self.sim_time),
            "rain_24h_mm": weather.get("rain_24h_mm"), "forecast_mm": weather.get("forecast_mm"),
            "risk_score": score["score"], "risk_category": score["category"],
            "trust_score": insights["trust"]["score"], "trust_band": insights["trust"]["band"],
            "state": self.record["state"], "condition": se.get("condition"),
            "outcome": se.get("outcome"), "scenario": self.active_scenario(),
            "freshness": river.get("freshness"),
        }
        self.history.append(entry)
        if se.get("outcome") == "TRANSITION_ACCEPTED":
            changes = result.get("capability_changes") or {}
            persistence = se.get("persistence") or {}
            last = self.record.get("last_transition") or {}
            self.transitions.append({
                "river_level_m": last.get("river_level_m"),
                "threshold": last.get("threshold"),
                "detail": last.get("reason"),
                "at": se.get("observed_at"), "evaluated_at": se.get("evaluated_at"),
                "tick": self.tick_count, "from": before, "to": se.get("resulting_state"),
                "kind": (se.get("transition_rule") or {}).get("kind"),
                "condition": se.get("condition"), "reason": se.get("reason"),
                "risk_category": score["category"], "risk_score": score["score"],
                "trust_score": insights["trust"]["score"],
                "evidence_id": se.get("evidence_id"),
                "supporting_evidence_ids": list(se.get("supporting_evidence_ids") or []),
                "persistence": {"observations": persistence.get("observations"),
                                "span_hours": persistence.get("span_hours")},
                "capabilities_granted": list(changes.get("allowlist_added") or []),
                "capabilities_revoked": list(changes.get("allowlist_revoked") or []),
                "scenario": self.active_scenario(),
            })
            self._event("transition", f"{before} → {se.get('resulting_state')}: "
                                      f"{se.get('reason')}")
        rate = river.get("rate_m_per_hour")
        if se.get("outcome") == "INSUFFICIENT_EVIDENCE":
            self._event("insufficient", f"State {self.record['state']} kept: evidence is "
                                        f"insufficient ({', '.join(se.get('reasons') or [])}).")
        self._event("reading", f"Reading {reading['observation_id']}: {reading['level_m']:.3f} m"
                               + (f" ({rate:+.3f} m/h)" if rate is not None else "")
                               + f"; risk {score['category']}"
                               + (f" {score['score']}" if score["score"] is not None else "")
                               + f"; trust {insights['trust']['score']}; state "
                               f"{self.record['state']} ({str(se.get('outcome')).replace('_', ' ').lower()}).")

    def _event(self, kind: str, text: str) -> None:
        self.activity.append({"sim_time": self.sim_time.isoformat(),
                              "wall_time": self.wall_clock().astimezone(timezone.utc).isoformat(),
                              "kind": kind, "text": text})

    # ---- snapshot ----------------------------------------------------------------

    def _build_snapshot(self, result: dict[str, Any], insights: dict[str, Any]) -> dict[str, Any]:
        from src.dashboard_service import sanitize  # local import: no cycle at load time
        reading = self.readings[-1]
        layer = result.get("evidence_layer") or {}
        doc = {
            "schema_version": SCHEMA_VERSION, "kind": "simulation", "mode": "simulation",
            "is_simulated": True, "data_label": LIVE_LABEL,
            "insights": insights,
            "latest_observation": {
                "observation_id": reading["observation_id"],
                "observed_at": reading["observed_at"].isoformat(),
                "level_m": reading["level_m"], "station_id": self.station_id,
                "rain_mm_last_hour": self.rain.get(self.sim_time),
                "provenance": {"generator": "DemoRiverGenerator (src/simulation_engine.py)",
                               "session_id": self.session_id, "sequence": self.sequence,
                               "scenario": reading["scenario"],
                               "generated_at_wall_clock": reading["generated_at"],
                               "deterministic_seed": hashlib.sha256(
                                   self.seed.encode()).hexdigest()[:12]},
                "label": LIVE_LABEL,
            },
            "evidence_records": [
                {"evidence_id": r.get("evidence_id"), "kind": r.get("kind"),
                 "evidence_class": r.get("evidence_class"), "observed_at": r.get("observed_at"),
                 "validation": (r.get("validation") or {}).get("status"),
                 "findings": [f.get("code") for f in (r.get("validation") or {}).get("findings")
                              or [] if isinstance(f, dict)]}
                for r in layer.get("records") or [] if isinstance(r, dict)],
            "claims": [{"claim_id": c.get("claim_id"), "status": c.get("status"),
                        "statement": c.get("statement")}
                       for c in layer.get("claims") or [] if isinstance(c, dict)],
            "run": {"run_id": result.get("run_id"), "decision_id": result.get("decision_id"),
                    "risk_assessment_id": result.get("risk_assessment_id"),
                    "state_decision_id": result.get("state_decision_id")},
        }
        return sanitize(doc, self.base_service.base_dir)

    def snapshot(self) -> dict[str, Any]:
        """The latest evaluation plus histories and controls (cheap: nothing recomputed)."""
        with self._lock:
            doc = dict(self.snapshot_doc or {})
            due = None
            if self.running and self.next_due is not None:
                due = max(0.0, round(self.next_due - self.monotonic(), 1))
            doc["generated_at"] = self.wall_clock().astimezone(timezone.utc).isoformat()
            doc["simulation"] = {
                "label": LIVE_LABEL, "session_id": self.session_id,
                "running": self.running, "speed": self.speed, "speeds": self.config["speeds"],
                "tick_seconds": float(self.config["tick_seconds"]),
                "interval_seconds": self.interval_seconds(),
                "simulated_minutes_per_tick": self.config["simulated_minutes_per_tick"],
                "next_tick_in_seconds": due, "tick": self.tick_count,
                "sim_time": self.sim_time.isoformat(),
                "started_sim_time": self.started_sim_time.isoformat(),
                "selected_scenario": self.selected, "active_conditions": self.active_scenario(),
                "scenarios": [{"name": k, "label": v.get("label", k),
                               "description": v.get("description", "")}
                              for k, v in self.config["scenarios"].items()]
                + [{"name": CYCLE, "label": self.config["cycle"].get("label", CYCLE),
                    "description": self.config["cycle"].get("description", "")}],
                "cycle_schedule": self.config["cycle"]["schedule"],
                "station_id": self.station_id, "twin_of": self.config.get("twin_of"),
                "thresholds": {**self.config["thresholds"][self.station_id], "official": False},
                "state_record_scope": self.record.get("scope"),
                "state_record_persisted": False,
                "real_state_affected": False,
                "last_error": self.last_error,
                "authorizes_real_world_actions": False,
                "disclaimer": ("SIMULATED LIVE: fabricated readings generated by this system. "
                               "They are NOT Central Water Commission observations, use TEST "
                               "thresholds, and can never authorize a real-world action. "
                               f"Each tick advances simulated time by "
                               f"{self.config['simulated_minutes_per_tick']} minutes."),
            }
            doc["series"] = list(self.history)
            window = self.sim_time - timedelta(hours=READINGS_WINDOW_HOURS)
            doc["readings"] = [{"observation_id": r["observation_id"],
                                "t": r["observed_at"].isoformat(), "level_m": r["level_m"],
                                "warmup": r["warmup"]} for r in self.readings
                               if r["observed_at"] >= window]
            doc["rainfall"] = [{"t": h.isoformat(), "mm": v, "kind": "SIMULATED"}
                               for h, v in sorted(self.rain.items()) if h >= window]
            doc["transitions"] = list(self.transitions)
            doc["activity"] = list(reversed(self.activity))
            doc["audit"] = {"total_events": self.audit.total,
                            "recent": [{"at": e["at"], "event": e["event"]}
                                       for e in list(self.audit.entries)[-40:]][::-1],
                            "storage": ("memory + file" if self.audit.path else "memory only"),
                            "separate_from_real_audit": True}
            return doc

    # ---- background loop -------------------------------------------------------------

    def control(self, action: str, scenario: Any = None, speed: Any = None) -> dict[str, Any]:
        """The only write operations, all on the simulation. Never a disaster state."""
        if action not in ACTIONS:
            raise ValueError(f"action must be one of {', '.join(ACTIONS)}")
        if action in ("start", "resume"):
            self.start()
        elif action == "pause":
            self.pause()
        elif action == "reset":
            self.reset(scenario if scenario is not None else self.selected)
        elif action == "scenario":
            self.set_scenario(scenario)
        elif action == "speed":
            self.set_speed(speed)
        return self.snapshot()

    def start(self) -> None:
        with self._lock:
            if not self.running:
                self.running = True
                self.next_due = self.monotonic() + self.interval_seconds()
                self._event("control", "Simulation running.")
            if self._thread is None or not self._thread.is_alive():
                self._stop.clear()
                self._thread = threading.Thread(target=self._loop, name="demo-simulation",
                                                daemon=True)
                self._thread.start()

    def pause(self) -> None:
        with self._lock:
            if self.running:
                self.running = False
                self.next_due = None
                self._event("control", "Simulation paused.")

    def stop(self) -> None:
        self.pause()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _loop(self) -> None:
        while not self._stop.wait(0.2):
            with self._lock:
                due = self.running and self.next_due is not None and \
                    self.monotonic() >= self.next_due
                if due:
                    try:
                        self.tick()
                    except OSError as exc:  # transient file problem: report, retry next tick
                        message = f"{type(exc).__name__}: {exc}"
                        self.last_error = {"at": self.wall_clock().astimezone(
                            timezone.utc).isoformat(), "error": message}
                        print(f"simulation_engine: tick failed, retrying: {message}",
                              file=sys.stderr)
                        self._event("error", f"A simulation step failed and will be retried "
                                             f"at the next tick: {message}")
                    except Exception as exc:  # never kill the server; report and pause
                        self.running = False
                        message = f"{type(exc).__name__}: {exc}"
                        self.last_error = {"at": self.wall_clock().astimezone(
                            timezone.utc).isoformat(), "error": message}
                        print(f"simulation_engine: paused after an error: {message}",
                              file=sys.stderr)
                        self._event("error", f"Simulation paused after an error: {message}")
                    if self.running:
                        self.next_due = self.monotonic() + self.interval_seconds()


# --------------------------------------------------------------------------
# Command line (offline: no background thread, one line per tick)
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run the demonstration simulation for N ticks "
                                                 "and print one line per tick.")
    parser.add_argument("--scenario", choices=SCENARIO_CHOICES, default=CYCLE)
    parser.add_argument("--ticks", type=int, default=45)
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    sim = DemoSimulation(RiverEvidenceService.from_file())
    sim.reset(args.scenario, log=False)
    print(f"{LIVE_LABEL}: fabricated {sim.station_id} readings; NOT CWC observations.")
    for _ in range(max(1, args.ticks)):
        sim.tick()
        h = sim.history[-1]
        print(f"t+{h['tick']:>3}h {h['scenario']:<9} {h['level_m']:7.3f} m "
              f"{(h['rate_m_per_hour'] or 0):+.3f} m/h  rain24 {h['rain_24h_mm']!s:>6}  "
              f"risk {h['risk_category']:<13} {h['risk_score']!s:>4}  trust {h['trust_score']:>3}"
              f"  {h['condition']:<12} -> {h['state']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
