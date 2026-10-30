"""Real-Time River Simulation: fabricated UPPER_ANICUT-like readings every 5 minutes.

    python -m src.river_simulation --scenario rapidly-rising --start-state NORMAL

Every reading is FABRICATED and labelled SIMULATED LIVE. It is not a CWC observation,
and it can never authorize a real-world action.

How it reuses the existing system (nothing here judges evidence, risk or state):
1. SyntheticRiverGenerator writes one reading per 5-minute slot, timestamped at
   that slot of the current (UTC) clock, in the River Agent's own CSV schema with
   source_type "simulated".
2. The existing RiverEvidenceService (simulated mode) and RiverMonitoringAgent read
   that file: row validation, QC (range, the 72.0 m placeholder, spikes), freshness,
   trend and recent quality are computed exactly as for any other file.
3. run_river_workflow then applies the evidence gate, reverification policy,
   risk engine, evidence layer and Disaster State Manager.

Separation from real data:
- The station is UPPER_ANICUT_SIM, a twin of UPPER_ANICUT that uses the real
  station's QC rules (config "qc_from_station") but has its own id and TEST
  thresholds, so a fabricated reading can never be attributed to the real station.
- Each session carries its own state record with scope "simulated"; it lives in
  memory only and is never written to the real state file.
- source_label stays SIMULATED_TEST_DATA, so every existing gate denies real-world
  capabilities with EVIDENCE_SIMULATED.

Weather: the existing fabricated weather files, re-timed so their profile is
centred on the present, parsed by the real WeatherAgent and labelled simulated.
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
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog
from src.disaster_state_manager import STATES, new_state_record
from src.risk_assessment import MODE_SIMULATED as SCOPE_SIMULATED
from src.risk_assessment import simulated_weather_from_doc
from src.river_agent_workflow import run_river_workflow
from src.river_monitoring_agent import (
    PROJECT_DIR,
    REQUIRED_COLUMNS,
    SOURCE_SIMULATED,
    STATION_ID_PATTERN,
    load_json_file,
    parse_timestamp,
    validate_thresholds,
)
from src.river_service import MODE_SIMULATED, RiverEvidenceService

DEFAULT_SIM_CONFIG_PATH = PROJECT_DIR / "config" / "river_simulation.json"
LIVE_LABEL = "SIMULATED LIVE"
SCENARIOS = ("stable", "slowly-rising", "rapidly-rising", "falling")
STATE_SPACING_MINUTES = 30  # catch-up evaluations: the state rules' 0.5 h min_spacing
MAX_TIMELINE = 120
_NUMERIC = ("base_m", "amplitude_m", "rate_m_per_hour", "wave_m", "wave_period_hours",
            "noise_m")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _number(value: Any, name: str, positive: bool = False) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or \
            not math.isfinite(value) or (positive and value <= 0):
        raise ValueError(f"Simulation config: {name} must be a finite"
                         f"{' positive' if positive else ''} number.")
    return float(value)


def validate_simulation_config(config: Any) -> dict[str, Any]:
    """Check the simulation config; raises ValueError (a setup mistake, never ignored)."""
    if not isinstance(config, dict):
        raise ValueError("Simulation config must be a JSON object.")
    station = config.get("station_id")
    if not isinstance(station, str) or not STATION_ID_PATTERN.match(station):
        raise ValueError("Simulation config: station_id must be a valid identifier.")
    if station == config.get("twin_of"):
        raise ValueError("Simulation config: the simulated station must not reuse the real "
                         "station's id.")
    interval = config.get("interval_minutes")
    if isinstance(interval, bool) or not isinstance(interval, int) or \
            not 1 <= interval <= 60 or 60 % interval:
        raise ValueError("Simulation config: interval_minutes must be an integer that "
                         "divides 60.")
    for key in ("history_hours", "retention_hours", "catch_up_hours"):
        _number(config.get(key), key, positive=True)
    if config["retention_hours"] < config["history_hours"]:
        raise ValueError("Simulation config: retention_hours must be >= history_hours.")
    if not isinstance(config.get("qc_from_station"), str):
        raise ValueError("Simulation config: qc_from_station must name the real station whose "
                         "QC rules the simulated readings must pass.")
    thresholds = validate_thresholds(config.get("thresholds"))
    if station not in thresholds:
        raise ValueError(f"Simulation config: thresholds must include {station}.")
    scenarios = {k: v for k, v in (config.get("scenarios") or {}).items()
                 if not k.startswith("_")}
    if set(scenarios) != set(SCENARIOS):
        raise ValueError(f"Simulation config: scenarios must be exactly {', '.join(SCENARIOS)}.")
    for name, spec in scenarios.items():
        if not isinstance(spec, dict):
            raise ValueError(f"Simulation config: scenario {name} must be an object.")
        for key in _NUMERIC:
            _number(spec.get(key), f"{name}.{key}",
                    positive=key in ("amplitude_m", "wave_period_hours"))
        if spec["noise_m"] < 0 or spec["wave_m"] < 0:
            raise ValueError(f"Simulation config: {name} noise_m and wave_m must be >= 0.")
        if not isinstance(spec.get("weather"), str):
            raise ValueError(f"Simulation config: {name}.weather must be a file path.")
    return {**config, "thresholds": thresholds, "scenarios": scenarios}


def load_simulation_config(path: str | Path = DEFAULT_SIM_CONFIG_PATH) -> dict[str, Any]:
    return validate_simulation_config(load_json_file(path, "Simulation config"))


def level_bounds(spec: dict[str, Any]) -> tuple[float, float]:
    """The lowest and highest level a scenario curve can produce (incl. wave and noise)."""
    spread = spec["amplitude_m"] + spec["wave_m"] + spec["noise_m"] + 0.01
    return spec["base_m"] - spread, spec["base_m"] + spread


# --------------------------------------------------------------------------
# Generator (pure apart from the CSV write)
# --------------------------------------------------------------------------

def floor_slot(moment: datetime, interval_minutes: int) -> datetime:
    """The start of the interval slot containing ``moment`` (UTC)."""
    if moment.tzinfo is None:
        raise ValueError("moment must be timezone-aware")
    utc = moment.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return utc - timedelta(minutes=utc.minute % interval_minutes)


def _noise(seed: str, moment: datetime, scale: float) -> float:
    """Deterministic pseudo-random value in [-scale, scale] for (seed, timestamp)."""
    digest = hashlib.sha256(f"{seed}|{moment.isoformat()}".encode()).digest()
    return (int.from_bytes(digest[:8], "big") / 2 ** 64 * 2 - 1) * scale


def scenario_level(spec: dict[str, Any], hours: float, seed: str, moment: datetime) -> float:
    """Fabricated level ``hours`` after the session started (negative: history)."""
    trend = spec["amplitude_m"] * math.tanh(spec["rate_m_per_hour"] * hours / spec["amplitude_m"])
    wave = spec["wave_m"] * math.sin(2 * math.pi * hours / spec["wave_period_hours"])
    return spec["base_m"] + trend + wave + _noise(seed, moment, spec["noise_m"])


class SyntheticRiverGenerator:
    """Appends one fabricated reading per slot up to the current clock.

    Readings are never rewritten: each call only adds the slots that have begun
    since the last one. Every reading is checked before it is kept (finite, inside
    the QC range, not a placeholder, later than the previous one, different from
    the previous value). These checks keep the generator honest; the readings still
    go through the River Agent's own validation and QC afterwards.
    """

    def __init__(self, scenario: str, spec: dict[str, Any], station_id: str,
                 started_at: datetime, seed: str, interval_minutes: int = 5,
                 history_hours: float = 6.0, retention_hours: float = 30.0,
                 qc: dict[str, Any] | None = None):
        if started_at.tzinfo is None:
            raise ValueError("started_at must be timezone-aware")
        if not qc:
            raise ValueError(f"No QC rules for the simulated station: generated readings must "
                             f"face the real station's validation ({scenario}).")
        low, high = level_bounds(spec)
        if low < qc["valid_min_m"] or high > qc["valid_max_m"]:
            raise ValueError(f"Scenario {scenario} can leave the QC range "
                             f"{qc['valid_min_m']}-{qc['valid_max_m']} m.")
        self.scenario = scenario
        self.spec = spec
        self.station_id = station_id
        self.started_at = started_at.astimezone(timezone.utc)
        self.seed = seed
        self.interval = timedelta(minutes=interval_minutes)
        self.interval_minutes = interval_minutes
        self.history = timedelta(hours=history_hours)
        self.retention = timedelta(hours=retention_hours)
        self.qc = qc
        self.placeholders = {round(float(v), 3) for v in qc.get("invalid_values_m") or []}
        self.readings: list[dict[str, Any]] = []
        self.generated_total = 0

    def _level(self, moment: datetime) -> float:
        hours = (moment - self.started_at).total_seconds() / 3600
        level = round(scenario_level(self.spec, hours, self.seed, moment), 3)
        previous = self.readings[-1]["river_level_m"] if self.readings else None
        step = 0.001 if self.spec["rate_m_per_hour"] >= 0 else -0.001
        # Never emit a configured placeholder (e.g. 72.0 m) or repeat the last value.
        while level in self.placeholders or level == previous:
            level = round(level + step, 3)
        return level

    def extend(self, now: datetime) -> list[datetime]:
        """Generate every slot that has started by ``now``; returns the new slot times."""
        end = floor_slot(now, self.interval_minutes)
        if self.readings:
            moment = self.readings[-1]["observed_at"] + self.interval
        else:
            moment = floor_slot(self.started_at - self.history, self.interval_minutes)
        new = []
        while moment <= end:
            level = self._level(moment)
            self._check(moment, level)
            self.readings.append({"observed_at": moment, "river_level_m": level})
            self.generated_total += 1
            new.append(moment)
            moment += self.interval
        cutoff = end - self.retention
        self.readings = [r for r in self.readings if r["observed_at"] >= cutoff]
        return new

    def _check(self, moment: datetime, level: float) -> None:
        """Refuse to emit anything the brief forbids; this is a bug, never data."""
        problems = []
        if not math.isfinite(level):
            problems.append("not finite")
        if round(level, 3) in self.placeholders:
            problems.append("placeholder value")
        if not self.qc["valid_min_m"] <= level <= self.qc["valid_max_m"]:
            problems.append("outside the QC range")
        if self.readings and moment <= self.readings[-1]["observed_at"]:
            problems.append("timestamp not after the previous reading")
        if problems:
            raise RuntimeError(f"Generator produced an invalid reading at {moment.isoformat()} "
                               f"({level}): {', '.join(problems)}")

    def rows(self) -> list[dict[str, str]]:
        return [{"timestamp": r["observed_at"].isoformat(), "station_id": self.station_id,
                 "river_level_m": f"{r['river_level_m']:.3f}", "source_type": SOURCE_SIMULATED}
                for r in self.readings]

    def write_csv(self, path: Path) -> Path:
        """Atomic write, so the River Agent never reads a half-written file."""
        path.parent.mkdir(parents=True, exist_ok=True)
        handle, temp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
        try:
            with os.fdopen(handle, "w", newline="", encoding="utf-8") as f:
                writer = csv.DictWriter(f, fieldnames=list(REQUIRED_COLUMNS))
                writer.writeheader()
                writer.writerows(self.rows())
            os.replace(temp, path)
        except BaseException:
            Path(temp).unlink(missing_ok=True)
            raise
        return path


# --------------------------------------------------------------------------
# Weather: existing fabricated files, re-timed to the present
# --------------------------------------------------------------------------

def realtime_weather(path: Path, now: datetime) -> dict[str, Any]:
    """A fabricated weather file shifted by whole hours so that its retrieval time
    falls on ``now``; parsed by the real WeatherAgent and labelled simulated."""
    doc = load_json_file(path, "Simulated weather")
    if not isinstance(doc, dict) or doc.get("simulated") is not True:
        raise ValueError(f"{path}: simulated weather files must declare \"simulated\": true.")
    file_time = parse_timestamp(doc.get("retrieved_at"))
    if file_time is None or not isinstance(doc.get("payload"), dict):
        raise ValueError(f"{path}: needs retrieved_at and an Open-Meteo-shaped payload.")
    shift = timedelta(hours=math.floor((now - file_time).total_seconds() / 3600))
    payload = copy.deepcopy(doc["payload"])
    local = timezone(timedelta(seconds=payload.get("utc_offset_seconds") or 0))

    def moved(text: str) -> str:
        return (datetime.fromisoformat(text) + shift).strftime("%Y-%m-%dT%H:%M")
    hourly = payload.get("hourly") or {}
    hourly["time"] = [moved(t) for t in hourly.get("time") or []]
    current = payload.get("current")
    if isinstance(current, dict):
        interval = int(current.get("interval") or 900)
        local_now = now.astimezone(local).replace(tzinfo=None, second=0, microsecond=0)
        seconds = (local_now.minute * 60) % interval
        current["time"] = (local_now - timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M")
    retimed = {**doc, "payload": payload, "retrieved_at": now.isoformat()}
    return simulated_weather_from_doc(
        retimed, f"SIMULATED LIVE weather (fabricated; {Path(path).name} re-timed to the "
                 "present)", now, str(path))


# --------------------------------------------------------------------------
# Real-time session
# --------------------------------------------------------------------------

class RealtimeRiverSimulation:
    """One running simulation: a generator, a simulated-scope state record carried
    from tick to tick, and the latest workflow result."""

    def __init__(self, service: RiverEvidenceService, scenario: str,
                 start_state: str = "NORMAL", config: dict[str, Any] | None = None,
                 clock: Callable[[], datetime] | None = None, seed: str | None = None):
        if scenario not in SCENARIOS:
            raise ValueError(f"scenario must be one of {', '.join(SCENARIOS)}")
        if start_state not in STATES:
            raise ValueError(f"state must be one of {', '.join(STATES)}")
        self.config = config if config is not None else load_simulation_config()
        self.scenario = scenario
        self.spec = self.config["scenarios"][scenario]
        self.start_state = start_state
        self.clock = clock or service.clock
        self.started_at = self.clock().astimezone(timezone.utc)
        self.session_id = uuid.uuid4().hex[:12]
        self.seed = seed or self.session_id
        self.station_id = self.config["station_id"]
        real_qc = (service.qc or {}).get(self.config["qc_from_station"])
        self.csv_path = service.resolve_path(
            f"{self.config['output_dir']}/upper_anicut_sim_{scenario}_{start_state.lower()}.csv")
        self.weather_path = service.resolve_path(self.spec["weather"])
        sim_config = json.loads(json.dumps(service.config))
        sim_config["simulated"] = {"path": str(self.csv_path), "station_id": self.station_id,
                                   "cross_check_paths": []}
        sim_config["thresholds_path"] = None
        self.service = RiverEvidenceService(sim_config, base_dir=service.base_dir,
                                            session=service.session, sleep=service.sleep,
                                            clock=self.clock)
        # Same QC as the real station (never weaker); TEST thresholds for the twin only.
        self.service.qc = {**(service.qc or {}), self.station_id: real_qc} if real_qc else None
        self.service.thresholds = {self.station_id: self.config["thresholds"][self.station_id]}
        self.generator = SyntheticRiverGenerator(
            scenario, self.spec, self.station_id, self.started_at, self.seed,
            self.config["interval_minutes"], self.config["history_hours"],
            self.config["retention_hours"], real_qc)
        self.record = new_state_record(start_state, SCOPE_SIMULATED)
        self.result: dict[str, Any] | None = None
        self.timeline: list[dict[str, Any]] = []
        self.evaluations = 0
        self.lock = threading.Lock()

    def tick(self, audit_log: AuditLog) -> tuple[dict[str, Any], bool]:
        """Add the readings due by now and, if there are any, run the workflow.

        Returns (latest result, whether it was recomputed). Slots missed while nobody
        asked (e.g. the page was closed) are evaluated at their own times, one per
        30 minutes for the last ``catch_up_hours``, so the state rules see them.
        """
        with self.lock:
            now = self.clock()
            new = self.generator.extend(now)
            if not new and self.result is not None:
                return self.result, False
            self.generator.write_csv(self.csv_path)
            if self.result is not None and len(new) > 1:
                for slot in self._catch_up_slots(new[:-1], now):
                    self._run(audit_log, as_of=slot)
            self._run(audit_log, as_of=None)
            return self.result, True

    def _catch_up_slots(self, slots: list[datetime], now: datetime) -> list[datetime]:
        earliest = now - timedelta(hours=self.config["catch_up_hours"])
        chosen: list[datetime] = []
        for slot in reversed(slots):  # newest first, spaced 30 min apart
            if slot < earliest:
                break
            if not chosen or chosen[-1] - slot >= timedelta(minutes=STATE_SPACING_MINUTES):
                chosen.append(slot)
        return sorted(chosen)

    def _run(self, audit_log: AuditLog, as_of: datetime | None) -> dict[str, Any]:
        at = as_of or self.clock()
        weather = realtime_weather(self.weather_path, at)
        result = run_river_workflow(
            self.service, audit_log, mode=MODE_SIMULATED, disaster_state=self.record["state"],
            as_of=as_of, cross_check_paths=[], independent_paths=[], weather=weather,
            state_record=self.record, scenario=f"realtime-simulation:{self.scenario}",
            include_evidence=True)
        state = result["state_evaluation"]
        if state.get("record") is not None and state["outcome"] != "EVALUATION_FAILED" \
                and not result["workflow_audit_write_errors"]:
            self.record = state["record"]
        evidence = result.get("evidence") or {}
        self.evaluations += 1
        self.timeline.append({
            "evaluated_at": at.astimezone(timezone.utc).isoformat(),
            "catch_up": as_of is not None,
            "observed_at": result.get("observed_at"),
            "river_level_m": (evidence.get("decision_support") or {}).get("last_known_level_m"),
            "freshness": result.get("evidence_freshness"),
            "trend": (evidence.get("trend") or {}).get("direction"),
            "risk_level": result["risk_assessment"]["risk_level"],
            "condition": state.get("condition"), "outcome": state.get("outcome"),
            "state_before": state.get("current_state"),
            "state_after": state.get("resulting_state"),
            "real_world_authorization": result.get("real_world_authorization"),
        })
        del self.timeline[:-MAX_TIMELINE]
        self.result = result
        return result

    def info(self) -> dict[str, Any]:
        latest = self.generator.readings[-1] if self.generator.readings else None
        return {
            "label": LIVE_LABEL,
            "session_id": self.session_id,
            "scenario": self.scenario,
            "scenario_description": self.spec.get("description"),
            "start_state": self.start_state,
            "current_state": self.record["state"],
            "started_at": self.started_at.isoformat(),
            "station_id": self.station_id,
            "twin_of": self.config.get("twin_of"),
            "qc_rules_from": self.config["qc_from_station"],
            "interval_minutes": self.config["interval_minutes"],
            "readings_held": len(self.generator.readings),
            "readings_generated": self.generator.generated_total,
            "latest_generated_at": latest["observed_at"].isoformat() if latest else None,
            "next_reading_due": ((latest["observed_at"] + self.generator.interval).isoformat()
                                 if latest else None),
            "evaluations": self.evaluations,
            "thresholds": {**self.config["thresholds"][self.station_id], "official": False},
            "timeline": list(self.timeline),
            "state_record_scope": self.record.get("scope"),
            "state_record_persisted": False,
            "authorizes_real_world_actions": False,
            "disclaimer": ("SIMULATED LIVE: fabricated readings generated by this system every "
                           f"{self.config['interval_minutes']} minutes. They are NOT Central Water "
                           "Commission observations, use TEST thresholds, and can never "
                           "authorize a real-world action."),
        }


class SimulationRegistry:
    """At most one session per (scenario, start state); created on first use."""

    def __init__(self, service: RiverEvidenceService, config: dict[str, Any] | None = None,
                 clock: Callable[[], datetime] | None = None):
        self.service = service
        self.config = config if config is not None else load_simulation_config()
        self.clock = clock
        self._sessions: dict[tuple[str, str], RealtimeRiverSimulation] = {}
        self._lock = threading.Lock()

    def session(self, scenario: str, start_state: str) -> RealtimeRiverSimulation:
        with self._lock:
            key = (scenario, start_state)
            if key not in self._sessions:
                self._sessions[key] = RealtimeRiverSimulation(
                    self.service, scenario, start_state, self.config, self.clock)
            return self._sessions[key]


# --------------------------------------------------------------------------
# Command line (offline demonstration with an accelerated clock)
# --------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run the Real-Time River Simulation for N ticks. With --accelerate the "
                    "clock jumps one interval per tick (offline demonstration).")
    parser.add_argument("--scenario", choices=SCENARIOS, default="rapidly-rising")
    parser.add_argument("--start-state", choices=STATES, default="NORMAL")
    parser.add_argument("--ticks", type=int, default=1)
    parser.add_argument("--accelerate", action="store_true")
    parser.add_argument("--audit", default="outputs/audit/simulated_live_audit.jsonl")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    service = RiverEvidenceService.from_file()
    config = load_simulation_config()
    moment = [datetime.now(timezone.utc)]
    clock = (lambda: moment[0]) if args.accelerate else None
    sim = RealtimeRiverSimulation(service, args.scenario, args.start_state, config, clock)
    audit = AuditLog(service.resolve_path(args.audit), component="realtime_simulation")
    print(f"{LIVE_LABEL}: fabricated {sim.station_id} readings (twin of "
          f"{config.get('twin_of')}), scenario {args.scenario}; NOT CWC observations.")
    for _ in range(max(1, args.ticks)):
        result, _ = sim.tick(audit)
        row = sim.timeline[-1]
        print(f"{row['observed_at']}  {row['river_level_m']!s:>7} m  {row['freshness']:<6} "
              f"trend {row['trend'] or '-':<9} risk {row['risk_level']:<13} "
              f"{row['state_before']} -> {row['state_after']}  "
              f"real-world: {result['real_world_authorization']}")
        if args.accelerate:
            moment[0] += timedelta(minutes=config["interval_minutes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
