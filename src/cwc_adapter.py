"""Convert a CWC river-water-level telemetry export into the River Agent schema.

The raw export (e.g. ``rwl_tel_hr_cwc_009_2021_2025.csv``) has one row per
station-hour with 23 columns, including station metadata, a naive timestamp
``Data Acquisition Time`` (format ``DD-MM-YYYY HH:MM``) and the value column
``River Water Level Telemetry Hourly (meter)``.

This module only CONVERTS the format. It does not judge data quality:
- The level text is copied unchanged (``NaN`` stays ``NaN``), so the River
  Agent can reject and count bad values itself. Nothing becomes zero.
- Unparseable timestamps are copied unchanged so the agent rejects them visibly.
- Timestamps in the file carry no timezone. They are interpreted using an
  explicit, configurable UTC offset (default +05:30, IST). This is an
  ASSUMPTION and is recorded as such in the metadata file.

Output: a normalized CSV (timestamp,station_id,river_level_m,source_type)
with source_type ``historical``, plus a JSON metadata file describing the
station and the provenance of the conversion.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import math
import re
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

COL_STATION = "Station"
COL_TIME = "Data Acquisition Time"
COL_LEVEL = "River Water Level Telemetry Hourly (meter)"
REQUIRED_CWC_COLUMNS = (COL_STATION, COL_TIME, COL_LEVEL)
METADATA_COLUMNS = (
    "Agency", "State", "District", "Tehsil", "River", "Basin", "Tributary",
    "Local River", "Latitude", "Longitude", "Is_DischargeDataAvailable",
    "RL_of_zeroGauge", "MeanSeaLevel",
)
CWC_TIME_FORMAT = "%d-%m-%Y %H:%M"
DEFAULT_UTC_OFFSET = "+05:30"
NORMALIZED_HEADER = ("timestamp", "station_id", "river_level_m", "source_type")
SOURCE_TYPE = "historical"


class CwcConversionError(Exception):
    """Raised when the input cannot be converted at all."""


def normalize_station_id(name: str) -> str:
    """'UPPER ANICUT' -> 'UPPER_ANICUT', 'PALLIPALAYAM/Erode' -> 'PALLIPALAYAM_ERODE'."""
    station_id = re.sub(r"[^A-Za-z0-9]+", "_", name.strip()).strip("_").upper()
    if not station_id:
        raise CwcConversionError(f"Station name {name!r} cannot be turned into an ID.")
    return station_id


def parse_utc_offset(text: str) -> timezone:
    """'+05:30' -> timezone(+5:30). Raises ValueError on bad input."""
    match = re.fullmatch(r"([+-])(\d{2}):(\d{2})", text.strip())
    if not match:
        raise ValueError(f"UTC offset must look like +05:30, got {text!r}")
    sign = 1 if match.group(1) == "+" else -1
    hours, minutes = int(match.group(2)), int(match.group(3))
    if hours > 14 or minutes > 59:
        raise ValueError(f"UTC offset out of range: {text!r}")
    return timezone(sign * timedelta(hours=hours, minutes=minutes))


def convert_timestamp(text: str, tz: timezone) -> str | None:
    """CWC 'DD-MM-YYYY HH:MM' -> ISO 8601 with offset, or None if unparseable."""
    try:
        return datetime.strptime(text.strip(), CWC_TIME_FORMAT).replace(tzinfo=tz).isoformat()
    except ValueError:
        return None


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _open_reader(path: Path):
    if not path.is_file():
        raise CwcConversionError(f"Input file not found: {path}")
    handle = path.open(newline="", encoding="utf-8-sig")
    reader = csv.DictReader(handle)
    header = [h.strip() for h in (reader.fieldnames or [])]
    reader.fieldnames = header
    missing = [c for c in REQUIRED_CWC_COLUMNS if c not in header]
    if missing:
        handle.close()
        raise CwcConversionError(
            f"Not a CWC telemetry export: missing column(s) {', '.join(missing)}.")
    return handle, reader


def list_stations(path: str | Path) -> dict[str, int]:
    """Station name -> number of rows."""
    handle, reader = _open_reader(Path(path))
    counts: dict[str, int] = {}
    with handle:
        for row in reader:
            name = (row.get(COL_STATION) or "").strip()
            counts[name] = counts.get(name, 0) + 1
    return counts


def convert_cwc_file(
    input_path: str | Path,
    station_name: str,
    output_csv: str | Path,
    metadata_path: str | Path | None = None,
    utc_offset: str = DEFAULT_UTC_OFFSET,
) -> dict[str, Any]:
    """Write one station's rows in the normalized schema. Returns the metadata dict.

    Raises CwcConversionError if the file is unusable or the station is absent.
    """
    input_path = Path(input_path)
    tz = parse_utc_offset(utc_offset)
    wanted = station_name.strip().casefold()
    station_id = normalize_station_id(station_name)

    handle, reader = _open_reader(input_path)
    rows_out: list[tuple[str, str, str, str]] = []
    first_row: dict[str, Any] | None = None
    inconsistent: set[str] = set()
    unparsed_times = 0
    with handle:
        for row in reader:
            if (row.get(COL_STATION) or "").strip().casefold() != wanted:
                continue
            if first_row is None:
                first_row = row
            else:
                for col in METADATA_COLUMNS:
                    if (row.get(col) or "").strip() != (first_row.get(col) or "").strip():
                        inconsistent.add(col)
            raw_time = (row.get(COL_TIME) or "").strip()
            iso = convert_timestamp(raw_time, tz)
            if iso is None:
                unparsed_times += 1
            raw_level = (row.get(COL_LEVEL) or "").strip()
            # Unparseable times and level text are passed through unchanged.
            rows_out.append((iso or raw_time, station_id, raw_level, SOURCE_TYPE))

    if first_row is None:
        raise CwcConversionError(
            f"Station {station_name!r} not found in {input_path.name}. "
            "Use --list-stations to see the station names.")

    output_csv = Path(output_csv)
    output_csv.parent.mkdir(parents=True, exist_ok=True)
    with output_csv.open("w", newline="", encoding="utf-8") as out:
        writer = csv.writer(out)
        writer.writerow(NORMALIZED_HEADER)
        writer.writerows(rows_out)

    metadata = {
        "station_id": station_id,
        "station_name": first_row[COL_STATION].strip(),
        "source_description": (
            f"CWC river water level telemetry (hourly), file {input_path.name}, "
            f"station {first_row[COL_STATION].strip()}; converted by src.cwc_adapter"),
        "agency_as_stated_in_file": (first_row.get("Agency") or "").strip() or None,
        "attributes_from_file": {col: (first_row.get(col) or "").strip() or None
                                 for col in METADATA_COLUMNS},
        "attributes_inconsistent_across_rows": sorted(inconsistent),
        "raw_file": {"name": input_path.name, "sha256": _sha256(input_path)},
        "normalized_file": output_csv.name,
        "rows_converted": len(rows_out),
        "unparseable_timestamps_passed_through": unparsed_times,
        "source_type": SOURCE_TYPE,
        "timestamp_interpretation": {
            "raw_format": "DD-MM-YYYY HH:MM (no timezone in file)",
            "assumed_utc_offset": utc_offset,
            "status": "ASSUMED",
            "note": "The export does not state its timezone. Offset applied by assumption; "
                    "verify with the data provider.",
        },
        "level_interpretation": {
            "status": "ASSUMED",
            "note": "Values are copied as reported in metres. The file does not state the "
                    "datum; compare with RL_of_zeroGauge before interpreting them as gauge "
                    "height or reduced level.",
        },
        "notes": [
            "Raw telemetry: values are not quality-controlled by this converter.",
            "The download origin of the file is not verified by the software.",
        ],
    }
    if metadata_path is not None:
        metadata_path = Path(metadata_path)
        metadata_path.parent.mkdir(parents=True, exist_ok=True)
        metadata_path.write_text(json.dumps(metadata, indent=2, ensure_ascii=False) + "\n",
                                 encoding="utf-8")
    return metadata


# --------------------------------------------------------------------------
# Multi-station loading (analysis and forecasting; read only)
# --------------------------------------------------------------------------

STATION_KINDS = ("river_gauge", "reservoir")
DEFAULT_REGISTRY_PATH = Path(__file__).resolve().parents[1] / "config" / "cwc_stations.json"
COORDINATE_TOLERANCE_DEG = 1e-6
RL_TOLERANCE_M = 1e-3


def load_station_registry(path: str | Path = DEFAULT_REGISTRY_PATH) -> dict[str, Any]:
    """Read and check config/cwc_stations.json. Raises ValueError on a setup mistake."""
    try:
        registry = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Station registry {path}: {exc}") from exc
    stations = registry.get("stations") if isinstance(registry, dict) else None
    if not isinstance(stations, dict) or not stations:
        raise ValueError("Station registry needs a non-empty 'stations' object.")
    for station_id, spec in stations.items():
        if not isinstance(spec, dict) or not isinstance(spec.get("source_name"), str) \
                or not spec["source_name"].strip():
            raise ValueError(f"Station {station_id}: source_name is required.")
        if spec.get("kind") not in STATION_KINDS:
            raise ValueError(f"Station {station_id}: kind must be one of {STATION_KINDS}.")
        for key in ("latitude", "longitude", "rl_zero_gauge_m"):
            value = spec.get(key)
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"Station {station_id}: {key} must be a number.")
        hours = spec.get("analysis_max_constant_run_hours")
        if hours is not None and (isinstance(hours, bool) or not isinstance(hours, (int, float))
                                  or hours <= 0):
            raise ValueError(f"Station {station_id}: analysis_max_constant_run_hours must be "
                             "a positive number.")
    for role in ("primary_station", "upstream_candidate"):
        station_id = registry.get(role)
        if station_id is None:
            continue
        if station_id not in stations or stations[station_id]["kind"] != "river_gauge":
            raise ValueError(f"{role} must name a river_gauge station in the registry.")
    parse_utc_offset(registry.get("utc_offset", DEFAULT_UTC_OFFSET))
    return registry


def _identity_mismatches(row: dict[str, Any], spec: dict[str, Any]) -> list[str]:
    """Registry fields that disagree with a row of the file."""
    problems = []
    for key, column in (("latitude", "Latitude"), ("longitude", "Longitude")):
        try:
            if abs(float(row.get(column) or "nan") - spec[key]) > COORDINATE_TOLERANCE_DEG:
                problems.append(f"{column} {row.get(column)} != registry {spec[key]}")
        except ValueError:
            problems.append(f"{column} {row.get(column)!r} is not a number")
    try:
        if abs(float(row.get("RL_of_zeroGauge") or "nan") - spec["rl_zero_gauge_m"]) \
                > RL_TOLERANCE_M:
            problems.append(f"RL_of_zeroGauge {row.get('RL_of_zeroGauge')} != registry "
                            f"{spec['rl_zero_gauge_m']}")
    except ValueError:
        problems.append(f"RL_of_zeroGauge {row.get('RL_of_zeroGauge')!r} is not a number")
    for key, column in (("river", "River"), ("tributary", "Tributary")):
        if key in spec and (row.get(column) or "").strip() != spec[key]:
            problems.append(f"{column} {row.get(column)!r} != registry {spec[key]!r}")
    return problems


def load_cwc_stations(input_path: str | Path, registry: dict[str, Any],
                      station_ids: list[str] | tuple[str, ...]) -> dict[str, dict[str, Any]]:
    """Read several stations from the raw CWC export in ONE pass. The file is
    only read, never modified.

    A row belongs to a station only if its Station field equals the registry's
    source_name exactly (surrounding spaces ignored: the export pads some names).
    Each station's identity (coordinates, zero-gauge RL, river, tributary) is
    checked on every row; disagreements are reported, never silently accepted.

    Per station the result holds observations sorted by time, each
    {"observed_at", "river_level_m", "row_number"}: only rows with a parseable
    timestamp and a finite numeric level. Nothing is filled in: a missing or
    non-numeric level is counted and left out, never read as zero. Duplicate
    timestamps with identical values are kept once; timestamps with conflicting
    values are dropped entirely (which value is right cannot be known).
    """
    input_path = Path(input_path)
    tz = parse_utc_offset(registry.get("utc_offset", DEFAULT_UTC_OFFSET))
    specs = registry["stations"]
    unknown = [s for s in station_ids if s not in specs]
    if unknown:
        raise CwcConversionError(f"Unknown station id(s): {', '.join(unknown)}.")
    by_name = {specs[s]["source_name"].strip(): s for s in station_ids}
    raw: dict[str, list[tuple[datetime, float, int]]] = {s: [] for s in station_ids}
    stats = {s: {"rows_read": 0, "unparseable_timestamps": 0, "non_numeric_levels": 0,
                 "identity_mismatch_rows": 0, "identity_mismatches": set()}
             for s in station_ids}
    handle, reader = _open_reader(input_path)
    with handle:
        for row_number, row in enumerate(reader, start=2):
            station_id = by_name.get((row.get(COL_STATION) or "").strip())
            if station_id is None:
                continue
            st = stats[station_id]
            st["rows_read"] += 1
            mismatches = _identity_mismatches(row, specs[station_id])
            if mismatches:
                st["identity_mismatch_rows"] += 1
                st["identity_mismatches"].update(mismatches)
            try:
                observed = datetime.strptime((row.get(COL_TIME) or "").strip(),
                                             CWC_TIME_FORMAT).replace(tzinfo=tz)
            except ValueError:
                st["unparseable_timestamps"] += 1
                continue
            try:
                level = float((row.get(COL_LEVEL) or "").strip())
            except ValueError:
                level = math.nan
            if not math.isfinite(level):
                st["non_numeric_levels"] += 1
                continue
            raw[station_id].append((observed, level, row_number))

    sha256 = _sha256(input_path)
    out: dict[str, dict[str, Any]] = {}
    for station_id in station_ids:
        groups: dict[datetime, list[tuple[float, int]]] = {}
        for observed, level, row_number in raw[station_id]:
            groups.setdefault(observed, []).append((level, row_number))
        observations, duplicates, conflicts = [], 0, 0
        for observed in sorted(groups):
            entries = groups[observed]
            if len({round(v, 6) for v, _ in entries}) > 1:
                conflicts += 1
                continue
            duplicates += len(entries) - 1
            observations.append({"observed_at": observed, "river_level_m": entries[0][0],
                                 "row_number": entries[0][1]})
        st = stats[station_id]
        spec = specs[station_id]
        out[station_id] = {
            "station_id": station_id,
            "source_name": spec["source_name"],
            "kind": spec["kind"],
            "found": st["rows_read"] > 0,
            "rows_read": st["rows_read"],
            "observations": observations,
            "counts": {"usable_rows": len(observations),
                       "unparseable_timestamps": st["unparseable_timestamps"],
                       "non_numeric_levels": st["non_numeric_levels"],
                       "duplicate_rows_removed": duplicates,
                       "conflicting_timestamps_dropped": conflicts},
            "identity": {"verified": st["rows_read"] > 0 and not st["identity_mismatch_rows"],
                         "mismatch_rows": st["identity_mismatch_rows"],
                         "mismatches": sorted(st["identity_mismatches"])[:10]},
            "first_observed_at": observations[0]["observed_at"].isoformat()
            if observations else None,
            "last_observed_at": observations[-1]["observed_at"].isoformat()
            if observations else None,
            "provenance": {
                "raw_file": input_path.name, "sha256": sha256,
                "time_column": COL_TIME, "level_column": COL_LEVEL,
                "time_basis": "OBSERVATION (Data Acquisition Time; the file has no separate "
                              "acquisition or download time)",
                "assumed_utc_offset": registry.get("utc_offset", DEFAULT_UTC_OFFSET),
                "timestamp_status": "ASSUMED",
                "datum": spec.get("datum"), "datum_status": spec.get("datum_status"),
                "units": "m",
            },
        }
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Convert a CWC telemetry export into the River Agent CSV schema.")
    parser.add_argument("--input", required=True, help="Raw CWC CSV export.")
    parser.add_argument("--station", help='Station name as in the file, e.g. "UPPER ANICUT".')
    parser.add_argument("--output", help="Normalized CSV to write.")
    parser.add_argument("--metadata", help="Station metadata JSON to write.")
    parser.add_argument("--utc-offset", default=DEFAULT_UTC_OFFSET,
                        help=f"Assumed UTC offset of the file's timestamps (default {DEFAULT_UTC_OFFSET}).")
    parser.add_argument("--list-stations", action="store_true",
                        help="List station names and row counts, then exit.")
    args = parser.parse_args(argv)
    try:
        if args.list_stations:
            for name, count in sorted(list_stations(args.input).items()):
                print(f"{count:8d}  {name}")
            return 0
        if not args.station or not args.output:
            parser.error("--station and --output are required unless --list-stations is used")
        meta = convert_cwc_file(args.input, args.station, args.output, args.metadata,
                                args.utc_offset)
    except (CwcConversionError, ValueError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    print(f"Converted {meta['rows_converted']} rows for {meta['station_name']} "
          f"-> {args.output} (station_id {meta['station_id']})")
    if meta["unparseable_timestamps_passed_through"]:
        print(f"Note: {meta['unparseable_timestamps_passed_through']} timestamps could not be "
              "parsed and were passed through unchanged.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
