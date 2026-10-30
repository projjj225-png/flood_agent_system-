"""Hydrological analysis of actual CWC observations (read only, deterministic).

    python -m src.hydro_analysis               # summary for the registry's stations

Works only on QC-accepted MEASURED readings from the raw CWC export
(src.cwc_adapter.load_cwc_stations + the River Agent's apply_quality_control).
Nothing is interpolated, filled or corrected: a missing hour is a gap, never a
zero and never an estimate. Every derived number says it is derived.

Reused from the River Agent: assess_freshness (age from the OBSERVATION time,
never the file or download date) and calculate_trend (time-based windows, so
irregular sampling is handled).

This module does not assess risk, change a disaster state or authorize
anything; its output is for display and for src.river_forecast.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.cwc_adapter import DEFAULT_REGISTRY_PATH, load_cwc_stations, load_station_registry
from src.river_monitoring_agent import (
    PROJECT_DIR,
    apply_quality_control,
    assess_freshness,
    calculate_trend,
    load_qc_file,
)

DEFAULT_QC_PATH = PROJECT_DIR / "config" / "river_qc.json"
TREND_WINDOWS_HOURS = (1, 3, 6, 24)
GAP_HOURS = 3.0          # same as the River Agent's freshness limit
EXPECTED_INTERVAL_HOURS = 1.0  # the export is nominally hourly
DEFAULT_MAX_AGE_HOURS = 3.0
MEASURED = "MEASURED"
DERIVED = "DERIVED_FROM_MEASURED"
HISTORICAL_CLASS = "HISTORICAL_CWC_DATA"


def analysis_qc(station_qc: dict[str, Any] | None,
                spec: dict[str, Any]) -> dict[str, Any] | None:
    """The station's QC with the registry's analysis-only stuck-run limit added.
    Only ever stricter: the smaller of the two limits wins."""
    if station_qc is None:
        return None
    qc = dict(station_qc)
    extra = spec.get("analysis_max_constant_run_hours")
    if extra is not None:
        current = qc.get("max_constant_run_hours")
        qc["max_constant_run_hours"] = extra if current is None else min(current, extra)
    return qc


def _round(value: float | None, digits: int = 3) -> float | None:
    return None if value is None else round(value, digits)


def _pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    if sx == 0 or sy == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy)


def find_gaps(times: list[datetime], gap_hours: float = GAP_HOURS) -> dict[str, Any]:
    """Periods longer than ``gap_hours`` with no accepted reading."""
    gaps = [(a, b, (b - a).total_seconds() / 3600) for a, b in zip(times, times[1:])
            if (b - a).total_seconds() / 3600 > gap_hours]
    longest = sorted(gaps, key=lambda g: g[2], reverse=True)[:5]
    return {"threshold_hours": gap_hours, "count": len(gaps),
            "missing_hours": round(sum(h - EXPECTED_INTERVAL_HOURS for _, _, h in gaps), 1),
            "longest": [{"from": a.isoformat(), "to": b.isoformat(), "hours": round(h, 1)}
                        for a, b, h in longest]}


def completeness(times: list[datetime]) -> dict[str, Any]:
    """Accepted readings as a share of the hourly slots they could fill."""
    if not times:
        return {"overall": None, "by_year": {}, "basis": DERIVED}
    span = (times[-1] - times[0]).total_seconds() / 3600 + EXPECTED_INTERVAL_HOURS
    by_year: dict[str, Any] = {}
    for year in range(times[0].year, times[-1].year + 1):
        start = max(times[0], times[0].replace(year=year, month=1, day=1, hour=0, minute=0))
        end = min(times[-1], times[0].replace(year=year, month=12, day=31, hour=23, minute=59))
        count = sum(1 for t in times if t.year == year)
        slots = max(1.0, (end - start).total_seconds() / 3600 + EXPECTED_INTERVAL_HOURS)
        by_year[str(year)] = {"accepted": count, "completeness": round(min(1.0, count / slots), 3)}
    return {"overall": round(min(1.0, len(times) / span), 3), "by_year": by_year,
            "expected_interval_hours": EXPECTED_INTERVAL_HOURS, "basis": DERIVED}


def analyse_series(accepted: list[dict[str, Any]], rejected: list[dict[str, Any]],
                   reference_time: datetime, max_age_hours: float = DEFAULT_MAX_AGE_HOURS,
                   data_class: str = HISTORICAL_CLASS) -> dict[str, Any]:
    """Summary of one station's QC-accepted readings, judged at ``reference_time``."""
    out: dict[str, Any] = {"data_class": data_class, "value_basis": MEASURED,
                           "reference_time": reference_time.isoformat(),
                           "accepted_readings": len(accepted), "rejected_readings": len(rejected),
                           "rejected_by_reason": {}}
    for obs in rejected:
        code = obs.get("qc_code", "REJECTED")
        out["rejected_by_reason"][code] = out["rejected_by_reason"].get(code, 0) + 1
    if not accepted:
        out.update(status="NO_ACCEPTED_READINGS", latest=None,
                   freshness=assess_freshness(None, reference_time, max_age_hours))
        return out
    ordered = sorted(accepted, key=lambda o: o["observed_at"])
    latest = ordered[-1]
    times = [o["observed_at"] for o in ordered]
    levels = [o["river_level_m"] for o in ordered]
    out["status"] = "AVAILABLE"
    out["latest"] = {"level_m": latest["river_level_m"],
                     "observed_at": latest["observed_at"].isoformat(),
                     "basis": MEASURED, "data_class": data_class}
    out["freshness"] = assess_freshness(latest["observed_at"], reference_time, max_age_hours)
    out["is_current"] = out["freshness"]["status"] == "FRESH" and data_class != HISTORICAL_CLASS
    out["trends"] = {}
    for hours in TREND_WINDOWS_HOURS:
        trend = calculate_trend(ordered, window_hours=hours)
        out["trends"][f"{hours}h"] = {k: trend[k] for k in (
            "status", "direction", "level_change_m", "rate_of_change_m_per_hour",
            "baseline_observed_at", "duration_hours", "observations_in_window", "reason")}
        out["trends"][f"{hours}h"]["basis"] = DERIVED
    window = [o["river_level_m"] for o in ordered
              if o["observed_at"] > latest["observed_at"] - timedelta(hours=24)]
    out["rolling_24h"] = {"readings": len(window), "mean_m": _round(statistics.fmean(window)),
                          "std_m": _round(statistics.pstdev(window) if len(window) > 1 else None,
                                          4),
                          "min_m": min(window), "max_m": max(window),
                          "window_end": latest["observed_at"].isoformat(), "basis": DERIVED}
    hi, lo = max(range(len(levels)), key=levels.__getitem__), min(range(len(levels)),
                                                                  key=levels.__getitem__)
    out["record"] = {"max_m": levels[hi], "max_at": times[hi].isoformat(),
                     "min_m": levels[lo], "min_at": times[lo].isoformat(),
                     "period": [times[0].isoformat(), times[-1].isoformat()],
                     "basis": MEASURED,
                     "note": "Extremes of QC-accepted readings in this file only; not "
                             "official record levels."}
    out["completeness"] = completeness(times)
    out["gaps"] = find_gaps(times)
    return out


def _hourly_changes(readings: dict[datetime, float], hours: int) -> dict[datetime, float]:
    """Change over exactly ``hours`` between two measured readings (no interpolation)."""
    step = timedelta(hours=hours)
    return {t: v - readings[t - step] for t, v in readings.items() if t - step in readings}


def upstream_relationship(upstream: list[dict[str, Any]], primary: list[dict[str, Any]],
                          upstream_spec: dict[str, Any], primary_spec: dict[str, Any],
                          rules: dict[str, Any]) -> dict[str, Any]:
    """Does the upstream candidate's level change lead the primary station's?

    Changes, not levels, are compared, so differing datums cannot create a
    relationship. Only exactly aligned measured readings are used.
    """
    max_lag = int(rules.get("max_lag_hours", 24))
    min_pairs = int(rules.get("min_pairs", 500))
    min_corr = float(rules.get("min_abs_correlation", 0.3))
    up = {o["observed_at"]: o["river_level_m"] for o in upstream}
    down = {o["observed_at"]: o["river_level_m"] for o in primary}
    results = []
    for step in (1, 6):
        up_change, down_change = _hourly_changes(up, step), _hourly_changes(down, step)
        for lag in range(0, max_lag + 1):
            shift = timedelta(hours=lag)
            pairs = [(up_change[t - shift], c) for t, c in down_change.items()
                     if t - shift in up_change]
            r = _pearson([a for a, _ in pairs], [b for _, b in pairs])
            results.append({"change_hours": step, "lag_hours": lag, "pairs": len(pairs),
                            "correlation": None if r is None else round(r, 3)})
    eligible = [r for r in results if r["pairs"] >= min_pairs and r["correlation"] is not None]
    best = max(eligible, key=lambda r: abs(r["correlation"]), default=None)
    if best is None:
        verdict = "INSUFFICIENT_DATA"
        reason = f"No lag has {min_pairs} aligned measured pairs."
    elif abs(best["correlation"]) >= min_corr:
        verdict = "VERIFIED"
        reason = (f"Level changes correlate (r={best['correlation']}) at a {best['lag_hours']} h "
                  f"lag over {best['pairs']} pairs.")
    else:
        verdict = "NOT_VERIFIED"
        reason = (f"No measurable relationship: the strongest correlation of level changes at "
                  f"lags 0-{max_lag} h is r={best['correlation']} (lag {best['lag_hours']} h, "
                  f"{best['pairs']} pairs), below the {min_corr} acceptance level.")
    lat1, lon1 = math.radians(upstream_spec["latitude"]), math.radians(upstream_spec["longitude"])
    lat2, lon2 = math.radians(primary_spec["latitude"]), math.radians(primary_spec["longitude"])
    distance = 2 * 6371.0 * math.asin(math.sqrt(
        math.sin((lat2 - lat1) / 2) ** 2
        + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2))
    return {
        "verdict": verdict, "reason": reason,
        "used_in_forecast": verdict == "VERIFIED",
        "used_in_risk_assessment": False,
        "estimated_lag_hours": best["lag_hours"] if verdict == "VERIFIED" else None,
        "best": best, "by_lag": results,
        "acceptance_rule": {"max_lag_hours": max_lag, "min_pairs": min_pairs,
                            "min_abs_correlation": min_corr},
        "metadata": {
            "same_river": upstream_spec.get("river") == primary_spec.get("river"),
            "both_main_stem": upstream_spec.get("tributary") == "-" ==
            primary_spec.get("tributary"),
            "straight_line_km": round(distance, 1),
            "zero_gauge_rl_m": {"upstream": upstream_spec["rl_zero_gauge_m"],
                                "primary": primary_spec["rl_zero_gauge_m"]},
            "zero_gauge_falls_downstream": upstream_spec["rl_zero_gauge_m"]
            > primary_spec["rl_zero_gauge_m"],
            "datums": {"upstream": [upstream_spec.get("datum"), upstream_spec.get("datum_status")],
                       "primary": [primary_spec.get("datum"), primary_spec.get("datum_status")]},
        },
        "note": ("Station order and geography alone do not prove a measurable hydraulic link. "
                 "Upper Anicut is a regulated weir pool, which can mask upstream changes."),
    }


def load_qc_accepted(registry: dict[str, Any], qc_config: dict[str, Any],
                     loaded: dict[str, Any], station_id: str
                     ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, Any] | None]:
    """QC-accepted and rejected readings of one loaded station (analysis QC)."""
    spec = registry["stations"][station_id]
    qc = analysis_qc(qc_config.get(station_id), spec)
    if qc is None:  # no QC rules: refuse to analyse rather than trust raw telemetry
        return [], [{**o, "qc_code": "NO_QC_CONFIGURED"} for o in
                    loaded[station_id]["observations"]], None
    accepted, rejected, summary = apply_quality_control(loaded[station_id]["observations"], qc)
    return accepted, rejected, summary


def load_analysis_inputs(registry_path: str | Path = DEFAULT_REGISTRY_PATH,
                         qc_path: str | Path = DEFAULT_QC_PATH,
                         raw_path: str | Path | None = None,
                         base_dir: Path = PROJECT_DIR) -> dict[str, Any]:
    """The slow part, done once: read the raw export and apply QC per station."""
    registry = load_station_registry(registry_path)
    qc_config = load_qc_file(qc_path)
    raw = Path(raw_path) if raw_path else base_dir / registry["raw_file"]
    primary_id, upstream_id = registry["primary_station"], registry.get("upstream_candidate")
    river_ids = [s for s in (primary_id, upstream_id) if s]
    reservoirs = [s for s, spec in registry["stations"].items() if spec["kind"] == "reservoir"]
    loaded = load_cwc_stations(raw, registry, river_ids + reservoirs)
    qc = {s: load_qc_accepted(registry, qc_config, loaded, s) for s in river_ids}
    return {"registry": registry, "raw": raw, "loaded": loaded, "qc": qc,
            "primary": primary_id, "upstream": upstream_id, "river_ids": river_ids,
            "reservoirs": reservoirs}


def analysis_document(inputs: dict[str, Any], reference_time: datetime,
                      max_age_hours: float = DEFAULT_MAX_AGE_HOURS) -> dict[str, Any]:
    """Primary station, upstream candidate and their relationship at ``reference_time``."""
    registry, loaded = inputs["registry"], inputs["loaded"]
    stations: dict[str, Any] = {}
    for station_id in inputs["river_ids"]:
        spec = registry["stations"][station_id]
        accepted, rejected, summary = inputs["qc"][station_id]
        stations[station_id] = {
            "station_id": station_id, "source_name": spec["source_name"],
            "role": "primary" if station_id == inputs["primary"] else "upstream_candidate",
            "kind": spec["kind"],
            "location": {k: spec[k] for k in ("latitude", "longitude")},
            "rl_zero_gauge_m": spec["rl_zero_gauge_m"],
            "load": {k: loaded[station_id][k] for k in ("found", "rows_read", "counts",
                                                       "identity", "first_observed_at",
                                                       "last_observed_at", "provenance")},
            "qc": summary,
            "analysis": analyse_series(accepted, rejected, reference_time, max_age_hours),
        }
    excluded = [{"station_id": s, "source_name": registry["stations"][s]["source_name"],
                 "kind": "reservoir", "last_observed_at": loaded[s]["last_observed_at"],
                 "reason": "Reservoir level: not a river gauge and not a release; never compared "
                           "with or used as a river level."} for s in inputs["reservoirs"]]
    relationship = None
    if inputs["upstream"]:  # independent of the clock: computed once per inputs
        if "relationship" not in inputs:
            upstream_id, primary_id = inputs["upstream"], inputs["primary"]
            inputs["relationship"] = upstream_relationship(
                inputs["qc"][upstream_id][0], inputs["qc"][primary_id][0],
                registry["stations"][upstream_id], registry["stations"][primary_id],
                registry.get("upstream_validation") or {})
        relationship = inputs["relationship"]
    return {
        "generated_for": reference_time.isoformat(),
        "data_class": HISTORICAL_CLASS,
        "source": {"raw_file": inputs["raw"].name,
                   "note": "A historical CWC export (2021-2025). Not a live source: nothing here "
                           "describes current conditions."},
        "primary_station": inputs["primary"], "upstream_candidate": inputs["upstream"],
        "stations": stations, "excluded_stations": excluded,
        "upstream_relationship": relationship,
        "main_stem_order": registry.get("main_stem_order"),
        "affects_risk_assessment": False, "authorizes_actions": False,
    }


def build_station_analysis(reference_time: datetime, **kwargs: Any
                           ) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]]]:
    """analysis_document plus the QC-accepted readings per river station."""
    inputs = load_analysis_inputs(**kwargs)
    return analysis_document(inputs, reference_time),         {s: inputs["qc"][s][0] for s in inputs["river_ids"]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Analyse the historical CWC export.")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    doc, _ = build_station_analysis(datetime.now(timezone.utc))
    if args.json:
        print(json.dumps(doc, indent=2, ensure_ascii=False, default=str))
        return 0
    for station_id, st in doc["stations"].items():
        a = st["analysis"]
        print(f"{station_id} ({st['role']}): {a['accepted_readings']} accepted, "
              f"{a['rejected_readings']} rejected {a['rejected_by_reason']}")
        if a.get("latest"):
            print(f"  latest {a['latest']['level_m']} m at {a['latest']['observed_at']} "
                  f"({a['data_class']}, {a['freshness']['status']}, "
                  f"{a['freshness']['age_hours']} h old)")
            print(f"  completeness {a['completeness']['overall']}, gaps>3h {a['gaps']['count']}")
    rel = doc["upstream_relationship"]
    if rel:
        print(f"Upstream relationship: {rel['verdict']} - {rel['reason']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
