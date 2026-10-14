"""River evidence service: one entry point for the Weather Agent / orchestrator.

    service = RiverEvidenceService.from_file("config/river_agent.json")
    evidence = service.get_evidence(mode="auto")      # dict, JSON-compatible

Modes
- ``live``       fetch + verify a snapshot from the NWDP/CWC API; no fallback.
- ``auto``       try live; if the API is unreachable or verification fails,
                 fall back to the historical CSV, clearly labelled.
- ``historical`` the committed historical CSV (supports ``as_of`` replay).
- ``simulated``  fabricated test data (supports ``as_of``).

The result is the River Agent's evidence dict with extra top-level fields:
``schema_version``, ``evidence_id``, ``run_id``, ``generated_at``,
``mode_requested``, ``source_label``, ``fallback``, ``live_fetch``,
``cross_source_check``, ``decision_support`` and ``audit``. Existing fields
are unchanged, so earlier consumers keep working.

Safety rules: missing, stale, unverified, suspect or conflicting evidence is
never reported as usable current evidence (``decision_support``), and every
decision here is a deterministic rule in Python.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

from src.audit_log import AuditLog
from src.river_monitoring_agent import (
    FRESHNESS_FRESH,
    PROJECT_DIR,
    QUALITY_ACCEPTABLE,
    SOURCE_VERIFIED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    RiverMonitoringAgent,
    _error,
    load_json_file,
    load_observations,
    load_qc_file,
    load_threshold_file,
    parse_timestamp,
    save_evidence,
)
from src.river_sources import LiveSourceError, PROVIDER_NAME, fetch_live_snapshot

SCHEMA_VERSION = "1.0"

MODE_LIVE = "live"
MODE_AUTO = "auto"
MODE_HISTORICAL = "historical"
MODE_SIMULATED = "simulated"
MODES = (MODE_AUTO, MODE_LIVE, MODE_HISTORICAL, MODE_SIMULATED)

LABEL_LIVE_VERIFIED = "LIVE_VERIFIED"
LABEL_LIVE_UNVERIFIED = "LIVE_UNVERIFIED"
LABEL_LIVE_UNAVAILABLE = "LIVE_UNAVAILABLE"
LABEL_HISTORICAL = "HISTORICAL_CSV"
LABEL_FALLBACK = "HISTORICAL_FALLBACK"
LABEL_SIMULATED = "SIMULATED_TEST_DATA"

DEFAULT_CONFIG_PATH = PROJECT_DIR / "config" / "river_agent.json"
VOLATILE_KEYS = ("evidence_id", "run_id", "generated_at", "audit")
MAX_CONFLICT_DETAILS = 1000


# --------------------------------------------------------------------------
# Cross-source conflict check (pure, deterministic)
# --------------------------------------------------------------------------

def cross_check_sources(primary_path: Path, station_id: str, other_paths: list[Path],
                        reference_time: datetime, tolerance_m: float = 0.005,
                        latest_observed_at: str | None = None,
                        quality_window_hours: float = 24.0) -> dict[str, Any]:
    """Compare readings for the same station and timestamp across files.

    Only rows that passed row validation and were observed at or before the
    reference time are compared. A difference above tolerance_m is a conflict.
    """
    block: dict[str, Any] = {
        "status": "NOT_CHECKED", "compared_with": [], "overlapping_timestamps": 0,
        "conflicts": 0, "conflicts_affecting_current_evidence": 0,
        "tolerance_m": tolerance_m, "examples": [],
        # Every conflict (capped) so reverification can re-check each one.
        "conflict_details": [],
    }

    def readings(path: Path) -> dict[datetime, float]:
        loaded = load_observations(path)
        return {o["observed_at"]: o["river_level_m"] for o in loaded["observations"]
                if o["station_id"] == station_id and o["observed_at"] <= reference_time}

    primary = readings(primary_path)
    window_start = reference_time - timedelta(hours=quality_window_hours)
    latest = parse_timestamp(latest_observed_at) if latest_observed_at else None
    for other in other_paths:
        if Path(other).resolve() == Path(primary_path).resolve() or not Path(other).is_file():
            continue
        block["compared_with"].append(Path(other).as_posix())
        secondary = readings(Path(other))
        for moment in sorted(set(primary) & set(secondary)):
            block["overlapping_timestamps"] += 1
            difference = abs(primary[moment] - secondary[moment])
            if difference > tolerance_m:
                block["conflicts"] += 1
                current = moment > window_start or (latest is not None and moment == latest)
                block["conflicts_affecting_current_evidence"] += int(current)
                detail = {
                    "observed_at": moment.isoformat(), "primary_m": primary[moment],
                    "other_m": secondary[moment], "other_source": Path(other).as_posix(),
                    "difference_m": round(difference, 4), "affects_current_evidence": current}
                if len(block["examples"]) < 10:
                    block["examples"].append(detail)
                if len(block["conflict_details"]) < MAX_CONFLICT_DETAILS:
                    block["conflict_details"].append(detail)
    if block["compared_with"]:
        block["status"] = ("CONFLICT" if block["conflicts"] else
                           "CONSISTENT" if block["overlapping_timestamps"] else "NO_OVERLAP")
    return block


def decision_support(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic summary for the orchestrator: may this be used as current evidence?"""
    quality = evidence["data_quality"]
    reasons = []
    if evidence["status"] == STATUS_UNAVAILABLE:
        reasons.append("No usable river reading (DATA_UNAVAILABLE).")
    if not quality.get("is_verified_live"):
        reasons.append(f"Not verified live evidence (grade {quality.get('evidence_grade')}, "
                       f"source {evidence.get('source_label')}).")
    if evidence["freshness"]["status"] != FRESHNESS_FRESH:
        reasons.append(f"Freshness is {evidence['freshness']['status']}.")
    if quality.get("recent_quality", {}).get("status") != QUALITY_ACCEPTABLE:
        reasons.append(f"Recent data quality is "
                       f"{quality.get('recent_quality', {}).get('status')}.")
    if evidence.get("cross_source_check", {}).get("conflicts_affecting_current_evidence"):
        reasons.append("Sources disagree about current readings.")
    if evidence.get("fallback", {}).get("used"):
        reasons.append("Historical fallback in use because live data could not be used.")
    usable = not reasons
    return {
        "current_evidence_usable": usable,
        "reasons_not_usable": reasons,
        "river_level_m": evidence["river_level_m"] if usable else None,
        "last_known_level_m": evidence["river_level_m"],
        "last_known_observed_at": evidence["observed_at"],
        "trend_direction": evidence["trend"]["direction"],
        "rate_of_change_m_per_hour": evidence["rate_of_change_m_per_hour"],
        "threshold_status": evidence["threshold_status"],
        "note": ("When current_evidence_usable is false, river conditions must be treated as "
                 "UNKNOWN, never as safe."),
    }


def compute_evidence_id(evidence: dict[str, Any]) -> str:
    """SHA-256 of the evidence without volatile fields (same input -> same id)."""
    stable = {k: v for k, v in evidence.items() if k not in VOLATILE_KEYS}
    canonical = json.dumps(stable, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Service
# --------------------------------------------------------------------------

def load_service_config(path: str | Path) -> dict[str, Any]:
    config = load_json_file(path, "Service config")
    if not isinstance(config, dict):
        raise ValueError("Service config must be a JSON object.")
    for key in ("station", "historical", "outputs"):
        if not isinstance(config.get(key), dict):
            raise ValueError(f"Service config needs a '{key}' object.")
    return config


class RiverEvidenceService:
    def __init__(self, config: dict[str, Any], base_dir: str | Path = PROJECT_DIR,
                 session: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], datetime] | None = None):
        self.config = copy.deepcopy(config)
        self.base_dir = Path(base_dir)
        self.session = session
        self.sleep = sleep
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.station = self.config["station"]
        self.audit = AuditLog(self._path(self.config["outputs"].get("audit_log")),
                              clock=self.clock)
        # Fail early on bad QC/threshold files rather than on first request.
        self.qc = load_qc_file(self._path(self.config["qc_path"])) \
            if self.config.get("qc_path") else None
        self.thresholds = load_threshold_file(self._path(self.config["thresholds_path"])) \
            if self.config.get("thresholds_path") else None

    @classmethod
    def from_file(cls, path: str | Path = DEFAULT_CONFIG_PATH, **kwargs: Any) -> "RiverEvidenceService":
        return cls(load_service_config(path), **kwargs)

    def resolve_path(self, value: str | Path | None) -> Path | None:
        """Resolve a config-style path (relative to the project folder)."""
        return self._path(str(value)) if value else None

    def _path(self, value: str | None) -> Path | None:
        if not value:
            return None
        path = Path(value)
        return path if path.is_absolute() else self.base_dir / path

    def _agent(self, input_path: Path, station_id: str, as_of: datetime | None,
               metadata: dict[str, Any] | None = None,
               verification: dict[str, Any] | None = None,
               real_station: bool = True) -> RiverMonitoringAgent:
        cfg = self.config
        return RiverMonitoringAgent(
            input_path, station_id=station_id, thresholds=self.thresholds, qc=self.qc,
            max_age_hours=cfg.get("max_age_hours", 3.0),
            trend_window_hours=cfg.get("trend_window_hours", 3.0),
            quality_window_hours=cfg.get("quality_window_hours", 24.0),
            suspect_fraction=cfg.get("suspect_fraction", 0.2),
            as_of=as_of, clock=self.clock, station_metadata=metadata,
            source_verification=verification,
            data_datum=self.station.get("data_datum") if real_station else None)

    def _live_metadata(self) -> dict[str, Any]:
        live = self.config.get("live", {})
        return {
            "station_id": self.station["station_id"],
            "station_name": self.station["provider_station_name"],
            "source_description": (
                f"CWC river water level telemetry (hourly) via {PROVIDER_NAME}: "
                f"{live.get('resource_name')} ({live.get('resource_id')})"),
            "agency_as_stated_by_provider": self.station.get("agency"),
            "attributes_expected": {k: self.station.get(k) for k in (
                "river", "latitude", "longitude", "rl_zero_gauge_m")},
            "timestamp_interpretation": {"assumed_utc_offset": self.station.get(
                "timestamp_utc_offset"), "status": self.station.get("timestamp_offset_status")},
            "level_interpretation": {"datum": self.station.get("data_datum"),
                                     "status": self.station.get("data_datum_status")},
        }

    def _historical(self, as_of: datetime | None) -> tuple[dict[str, Any], Path]:
        hist = self.config["historical"]
        metadata_path = self._path(hist.get("station_metadata"))
        metadata = load_json_file(metadata_path, "Station metadata") \
            if metadata_path and metadata_path.is_file() else None
        path = self._path(hist["path"])
        return self._agent(path, self.station["station_id"], as_of, metadata).run(), path

    def get_evidence(self, mode: str = MODE_AUTO, as_of: datetime | None = None,
                     cross_check_paths: list[str | Path] | None = None) -> dict[str, Any]:
        """Produce evidence. ``cross_check_paths`` overrides the configured
        comparison sources for this call (simulated sources for simulated
        mode, real sources otherwise; they are never mixed by default)."""
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if as_of is not None and as_of.tzinfo is None:
            raise ValueError("as_of must be timezone-aware")
        if as_of is not None and mode in (MODE_LIVE, MODE_AUTO):
            raise ValueError("as_of replay is only possible in historical or simulated mode; "
                             "live data describe the present")
        run_id = uuid.uuid4().hex
        self.audit.log(run_id, "run_started", mode=mode,
                       as_of=as_of.isoformat() if as_of else None)
        live_fetch: dict[str, Any] = {"attempted": False}
        fallback: dict[str, Any] = {"used": False, "reason": None}
        primary_path: Path | None = None
        live_cfg = self.config.get("live") or {}

        if mode in (MODE_LIVE, MODE_AUTO):
            live_fetch, evidence, primary_path = self._try_live(run_id, live_cfg)
            if evidence is None or (mode == MODE_AUTO and live_fetch.get("verification_status")
                                    != SOURCE_VERIFIED):
                reason = live_fetch.get("error") or (
                    "Live source verification failed: "
                    + ", ".join(live_fetch.get("failed_checks") or ["unknown"]))
                if mode == MODE_LIVE:
                    if evidence is None:
                        agent = self._agent(self._path(self.config["outputs"]["snapshot_dir"])
                                            / "no-snapshot.csv", self.station["station_id"],
                                            None, self._live_metadata())
                        evidence = agent.unavailable([_error(
                            "LIVE_SOURCE_UNAVAILABLE", reason, "live")])
                    label = (LABEL_LIVE_UNAVAILABLE if primary_path is None
                             else LABEL_LIVE_UNVERIFIED)
                else:
                    fallback = {"used": True, "reason": reason}
                    self.audit.log(run_id, "fallback_used", reason=reason)
                    evidence, primary_path = self._historical(None)
                    evidence["warnings"].append(_error(
                        "HISTORICAL_FALLBACK_USED",
                        f"Live data could not be used ({reason}); historical CSV data are "
                        "shown instead and are not current.", "source_label", severity="warning"))
                    label = LABEL_FALLBACK
            else:
                label = (LABEL_LIVE_VERIFIED if evidence["data_quality"]["source_verification"]
                         ["status"] == SOURCE_VERIFIED else LABEL_LIVE_UNVERIFIED)
        elif mode == MODE_HISTORICAL:
            evidence, primary_path = self._historical(as_of)
            label = LABEL_HISTORICAL
        else:
            sim = self.config.get("simulated") or {}
            primary_path = self._path(sim.get("path"))
            evidence = self._agent(primary_path, sim.get("station_id"), as_of,
                                   real_station=False).run()
            label = LABEL_SIMULATED

        evidence["schema_version"] = SCHEMA_VERSION
        evidence["mode_requested"] = mode
        evidence["source_label"] = label
        evidence["fallback"] = fallback
        evidence["live_fetch"] = live_fetch
        evidence["cross_source_check"] = self._cross_check(evidence, primary_path, label,
                                                           cross_check_paths)
        evidence["decision_support"] = decision_support(evidence)
        evidence["run_id"] = run_id
        evidence["generated_at"] = self.clock().astimezone(timezone.utc).isoformat()
        evidence["evidence_id"] = compute_evidence_id(evidence)
        self.audit.log(run_id, "evidence_produced", evidence_id=evidence["evidence_id"],
                       status=evidence["status"], source_label=label,
                       evidence_grade=evidence["data_quality"]["evidence_grade"],
                       observed_at=evidence["observed_at"],
                       river_level_m=evidence["river_level_m"],
                       freshness=evidence["freshness"]["status"],
                       current_evidence_usable=evidence["decision_support"]
                       ["current_evidence_usable"])
        evidence["audit"] = {
            "log_path": self.audit.path.as_posix() if self.audit.path else None,
            "run_id": run_id, "events_written": self.audit.events_written,
            "write_errors": list(self.audit.write_errors),
        }
        if self.audit.write_errors:
            evidence["warnings"].append(_error(
                "AUDIT_LOG_INCOMPLETE", "; ".join(self.audit.write_errors), "audit",
                severity="warning"))
        return evidence

    def _try_live(self, run_id: str, live_cfg: dict[str, Any]):
        """Returns (live_fetch block, evidence or None, snapshot path or None)."""
        block: dict[str, Any] = {"attempted": True, "provider": PROVIDER_NAME,
                                 "status": "UNAVAILABLE", "error": None, "attempts": []}
        if not live_cfg.get("enabled"):
            block.update(attempted=False, status="DISABLED",
                         error="Live source is disabled in the configuration.")
            return block, None, None
        snapshot_dir = self._path(self.config["outputs"]["snapshot_dir"])
        try:
            snap = fetch_live_snapshot(live_cfg, self.station, snapshot_dir,
                                       session=self.session, sleep=self.sleep, clock=self.clock)
        except LiveSourceError as exc:
            block.update(error=f"{exc.code}: {exc.message}",
                         attempts=getattr(exc, "attempts", []))
            self.audit.log(run_id, "live_fetch_failed", code=exc.code, message=exc.message,
                           attempts=block["attempts"])
            return block, None, None
        except ValueError as exc:  # invalid live configuration
            block.update(error=f"LIVE_CONFIG_INVALID: {exc}")
            self.audit.log(run_id, "live_config_invalid", message=str(exc))
            return block, None, None
        verification = snap["verification"]
        block.update(status="FETCHED", attempts=snap["attempts"],
                     verification_status=verification["status"],
                     failed_checks=verification["failed_checks"],
                     verification=verification,
                     snapshot_path=Path(snap["snapshot_path"]).as_posix(),
                     snapshot_sha256=snap["snapshot_sha256"], raw_paths=snap["raw_paths"],
                     retrieved_at=verification["retrieved_at"])
        self.audit.log(run_id, "live_fetch_ok", attempts=snap["attempts"],
                       snapshot=block["snapshot_path"], snapshot_sha256=snap["snapshot_sha256"],
                       raw_paths=snap["raw_paths"])
        self.audit.log(run_id, "live_verification", status=verification["status"],
                       failed_checks=verification["failed_checks"],
                       resource=verification["resource"],
                       latest_record_observed_at=verification["latest_record_observed_at"])
        evidence = self._agent(Path(snap["snapshot_path"]), snap["station_id"], None,
                               self._live_metadata(), verification).run()
        return block, evidence, Path(snap["snapshot_path"])

    def _cross_check(self, evidence: dict[str, Any], primary_path: Path | None,
                     label: str, override_paths: list[str | Path] | None = None
                     ) -> dict[str, Any]:
        cfg = self.config.get("cross_check") or {}
        if override_paths is not None:
            paths = list(override_paths)
        elif label == LABEL_SIMULATED:
            # Simulated evidence is only ever compared with simulated sources.
            paths = (self.config.get("simulated") or {}).get("cross_check_paths", [])
        else:
            paths = cfg.get("paths", []) if cfg.get("enabled") else []
        if not paths or primary_path is None or evidence["status"] == STATUS_UNAVAILABLE:
            return {"status": "NOT_CHECKED"}
        reference = parse_timestamp(evidence["freshness"]["reference_time"])
        block = cross_check_sources(
            primary_path, evidence["station_id"],
            [self._path(str(p)) for p in paths], reference,
            tolerance_m=cfg.get("tolerance_m", 0.005),
            latest_observed_at=evidence["observed_at"],
            quality_window_hours=self.config.get("quality_window_hours", 24.0))
        if block["conflicts"]:
            evidence["warnings"].append(_error(
                "CONFLICTING_SOURCES",
                f"{block['conflicts']} reading(s) differ between sources by more than "
                f"{block['tolerance_m']} m ({block['conflicts_affecting_current_evidence']} "
                "affect current evidence).", "cross_source_check", severity="warning"))
            if block["conflicts_affecting_current_evidence"] and evidence["status"] == STATUS_OK:
                evidence["status"] = STATUS_PARTIAL
        return block

    def save(self, evidence: dict[str, Any], path: str | Path | None = None) -> Path:
        target = self._path(str(path)) if path else self._path(
            self.config["outputs"]["evidence"])
        return save_evidence(evidence, target)


# --------------------------------------------------------------------------
# Command line
# --------------------------------------------------------------------------

def _print_summary(evidence: dict[str, Any]) -> None:
    ds = evidence["decision_support"]
    quality = evidence["data_quality"]
    print(f"Source label:     {evidence['source_label']} (mode {evidence['mode_requested']})")
    print(f"Status:           {evidence['status']}")
    print(f"Evidence grade:   {quality['evidence_grade']} "
          f"(verified live: {quality['is_verified_live']})")
    print("Last reading:     " + (f"{ds['last_known_level_m']} m at {ds['last_known_observed_at']}"
                                   if ds["last_known_level_m"] is not None else "UNAVAILABLE"))
    print(f"Freshness:        {evidence['freshness']['status']} "
          f"(age {evidence['freshness']['age_hours']} h)")
    print(f"Recent quality:   {quality['recent_quality']['status']}")
    print(f"Threshold status: {evidence['threshold_status']}")
    print(f"Usable as current evidence: {ds['current_evidence_usable']}")
    for reason in ds["reasons_not_usable"]:
        print(f"  - {reason}")
    live = evidence["live_fetch"]
    if live.get("attempted"):
        print(f"Live fetch:       {live['status']}"
              + (f", verification {live.get('verification_status')}"
                 if live.get("verification_status") else "")
              + (f", error: {live['error']}" if live.get("error") else ""))
        for check in (live.get("verification") or {}).get("checks", []):
            print(f"  [{'PASS' if check['passed'] else 'FAIL'}] {check['name']}: {check['detail']}")
    if evidence["fallback"]["used"]:
        print(f"Fallback:         {evidence['fallback']['reason']}")
    print(f"Evidence id:      {evidence['evidence_id']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="River evidence service (live/historical/"
                                                 "simulated) for the flood project.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--mode", default=MODE_AUTO, choices=MODES)
    parser.add_argument("--as-of", default=None,
                        help="Replay time (historical/simulated only), e.g. 2025-12-30T06:00:00+05:30")
    parser.add_argument("--probe", action="store_true",
                        help="Live connectivity probe: same as --mode live, prints verification.")
    parser.add_argument("--json", action="store_true", help="Print the full evidence JSON.")
    parser.add_argument("--save", action="store_true", help="Write the evidence JSON file.")
    parser.add_argument("--output", default=None, help="Custom path for --save.")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    as_of = None
    if args.as_of:
        as_of = parse_timestamp(args.as_of)
        if as_of is None:
            parser.error("--as-of must be a timezone-aware ISO 8601 time")
    mode = MODE_LIVE if args.probe else args.mode
    try:
        service = RiverEvidenceService.from_file(args.config)
        evidence = service.get_evidence(mode=mode, as_of=as_of)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(evidence, indent=2, ensure_ascii=False))
    else:
        _print_summary(evidence)
    if args.save:
        path = service.save(evidence, args.output)
        print(f"Saved evidence to {path}", file=sys.stderr)
    return 1 if evidence["status"] == STATUS_UNAVAILABLE else 0


if __name__ == "__main__":
    sys.exit(main())
