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
