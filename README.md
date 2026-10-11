# State-Aware Trust and Evidence-Driven Capability Control for Multi-LLM Agents in Flood Disaster Management

Prototype location: **Cauvery River near Upper Anicut, Tamil Nadu, India**
(latitude 10.888611, longitude 78.580000, timezone Asia/Kolkata).

**Current progress:** Phase 1, components 1 and 2: the **Weather Agent** and the
**River Monitoring Agent** (CSV-based: real historical CWC telemetry for Upper Anicut 2021–2025, plus
simulated test data; no verified live river source yet).
Nothing else is implemented yet. That includes evidence conflict detection, trust scoring, risk assessment,
disaster-state logic, capability control and the dashboard.

## Project structure

```
flood_agent_system/
├── src/
│   ├── __init__.py
│   ├── weather_agent.py            # The Weather Agent
│   ├── river_monitoring_agent.py   # The River Monitoring Agent (CSV input, QC)
│   └── cwc_adapter.py              # Converts the raw CWC telemetry export to the agent's schema
├── tests/
│   ├── test_weather_agent.py            # Offline tests (mocked API, no internet needed)
│   ├── test_river_monitoring_agent.py   # Offline tests (temporary CSV files)
│   └── test_cwc_adapter.py              # Offline tests for the CWC converter
├── data/
│   ├── raw/                        # Put rwl_tel_hr_cwc_009_2021_2025.csv here (gitignored, 103 MB)
│   ├── cwc/
│   │   ├── upper_anicut_hourly_2021_2025.csv  # Real CWC telemetry, Upper Anicut, normalized
│   │   └── upper_anicut_station.json          # Station attributes, provenance, assumptions
│   └── river_levels_simulated.csv  # FABRICATED test levels, not real measurements
├── config/
│   ├── river_qc.json                  # Prototype QC settings (data-derived, NOT official)
│   └── river_thresholds.example.json  # TEST thresholds only, not official levels
├── outputs/                  # Live weather samples + latest river evidence (river_evidence.json)
├── pytest.ini                # Lets pytest find the src package
├── requirements.txt          # requests + pytest (pinned)
├── .gitignore
└── README.md
```

## What the Weather Agent does

1. Calls the Open-Meteo Forecast API for the prototype location (with a 5 s connect and 20 s read timeout).
2. Handles timeouts, connection failures, HTTP errors and invalid JSON without crashing.
3. Checks that the expected sections (`current`, `hourly`, units, timezone offset) and fields are present.
4. Extracts the current weather and the hourly series: precipitation, rain, wind, temperature, humidity and precipitation probability.
5. Keeps the coordinates **you requested** and the **model grid coordinates the API actually returned**.
6. Calculates precipitation and rain totals for the preceding **3, 6 and 24 completed hours**. Future forecast hours are never included.
7. Returns `DATA_UNAVAILABLE` when retrieval fails. **It never invents values.** A missing value stays `null` and comes with a reason. It is never read as "0 mm" or "safe weather".

The code is split into independent steps:

| Step | Function | Network? |
|---|---|---|
| Fetch | `WeatherAgent.fetch_raw()` | yes |
| Parse and validate | `WeatherAgent.parse_response(payload, retrieved_at)` | no |
| Rainfall totals | `calculate_rainfall_accumulation(hourly, reference_time)` | no |
| Full workflow | `WeatherAgent.run()` → result dict | yes |
| Get result | `WeatherAgent.to_dict()` (a copy of the last result) | no |

## Setup on Windows

Open a terminal in the `flood_agent_system` folder.

**PowerShell:**
```powershell
.\.venv\Scripts\Activate.ps1
```
If PowerShell blocks the script, run this once:
`Set-ExecutionPolicy -Scope CurrentUser RemoteSigned`

**Command Prompt (cmd):**
```bat
.venv\Scripts\activate.bat
```

**Git Bash:**
```bash
source .venv/Scripts/activate
```

To create the environment again from scratch (it already exists):
```powershell
py -3.13 -m venv .venv
```

### Install dependencies
```powershell
python -m pip install -r requirements.txt
```

## Running the agent

```powershell
python -m src.weather_agent            # readable summary
python -m src.weather_agent --json     # full structured result as JSON
python -m src.weather_agent --save     # also save to outputs/ (only if retrieval succeeded)
python -m src.weather_agent --past-days 1   # adds yesterday's hours so the 24 h total can be computed
```

From Python:
```python
from src.weather_agent import WeatherAgent

agent = WeatherAgent()
result = agent.run()
print(result["status"])
print(agent.calculate_rainfall_accumulation()["windows"]["6h"])
```

The command exits with code `1` when the status is `DATA_UNAVAILABLE` and `0` otherwise.

## Running the tests

```powershell
python -m pytest -v
```
The tests use fake API responses, so they do **not** need internet access.

## Output fields

| Field | Meaning |
|---|---|
| `agent_name` | Always `WeatherAgent`. |
| `status` | `OK`: all expected data present. `PARTIAL`: some fields missing or null (see `errors`). `DATA_UNAVAILABLE`: no usable data; every value is null. |
| `source` | Data provider (Open-Meteo Forecast API). |
| `source_url` | The exact URL that was called. |
| `source_type` | Always `MODEL_BASED_CURRENT_AND_FORECAST`. These are model values, not station observations. |
| `data_notes` | Short notes on how to read the data. |
| `requested_location` | The coordinates we asked for. |
| `returned_grid_location` | The model grid-cell coordinates and elevation the API used. These usually differ slightly from the requested point. |
| `retrieved_at` | When the response was received (timezone-aware, UTC). |
| `timezone` | Requested and returned timezone, its abbreviation and UTC offset. |
| `current_weather` | `time` (model time of the "current" values, in IST), `interval_seconds` (usually 900 = 15 min), `values` per variable, `missing` (variable → reason) and a section `status`. |
| `hourly_weather` | `time` list (hourly, IST) and `variables` (variable → list aligned with `time`). Hours before `retrieved_at` are model estimates; later hours are forecasts. Also contains `missing`, `null_counts` and `status`. |
| `rainfall_accumulation` | `reference_time`, `unit` and `windows` (`3h`, `6h`, `24h`). Each window has `period_start`, `period_end` and, for `precipitation` and `rain`, a `status`, a `total` and the `reason` it is unavailable. |
| `units` | Units returned by the API for the current and hourly variables (e.g. `mm`, `°C`, `km/h`, `%`). |
| `errors` | List of `{code, severity, field, message}`. `severity` is `error` (data missing or unusable) or `warning` (e.g. null hourly values, timezone mismatch). |

### How the rainfall totals are calculated

Open-Meteo's hourly precipitation at time *T* is the total for the hour **ending** at *T*.
An hour counts as completed once *T* is at or before the reference time (by default, the retrieval time).
For example, at 14:20 IST the last completed hour is the one ending at 14:00. The 3 h total therefore uses the hours ending at 12:00, 13:00 and 14:00 (the period 11:00–14:00).
If any required hour is missing from the series or is null, that total is `UNAVAILABLE`. It is never partly summed.

**Note on the 24 h total:** the configured endpoint returns hourly data starting at 00:00 today (IST).
Before midnight, fewer than 24 completed hours are available, so the 24 h total is normally `UNAVAILABLE` with an explanation.
To make it available, run with `--past-days 1`. This adds `past_days=1` to the request.

## Limitations of Open-Meteo model-based data

- **Not observations.** The values come from numerical weather models. They are not rain-gauge or weather-station measurements, and they may differ from what actually happened on the ground.
- **Grid resolution.** The point is snapped to a model grid cell (the live call returned about 10.861 N, 78.611 E, about 4.5 km away). Very local convective rainfall can be missed or displaced.
- **"Past" hours are still modelled.** Hourly values before the retrieval time are model analyses or short-range forecasts, not measured accumulations.
- **"Current" is a 15-minute model value**, not a live sensor reading. Current `precipitation`/`rain` covers the preceding 15 minutes.
- **Forecast uncertainty** increases with lead time. `precipitation_probability` is a model probability, not a guarantee.
- **No river information.** Rainfall alone does not give river level or discharge at Upper Anicut. Upstream rainfall and dam releases (e.g. Mettur) matter more for flooding there.
- **Service dependency.** Network problems, rate limits or API changes produce `DATA_UNAVAILABLE`. Callers must treat that as "unknown", never as "safe".

## Outputs folder

For the Weather Agent, `outputs/` contains only results saved from **real, successful** API calls (`--save`).
(The River Agent also writes `outputs/river_evidence.json`; see the River section below.)
Each file is named `weather_live_<UTC retrieval time>.json`. It contains a `label` with the retrieval time, the structured `result` and the unmodified `raw_api_response`.
The code refuses to save results with the status `DATA_UNAVAILABLE`.

---

# River Monitoring Agent

> **Important:** no verified **live** river-level source is connected. The agent reads river levels from a CSV file:
> - **Historical:** real CWC hourly telemetry for the `UPPER ANICUT` station, 2021–2025, converted from
>   `rwl_tel_hr_cwc_009_2021_2025.csv` (see [Real data](#real-data-cwc-telemetry-20212025)).
> - **Simulated:** `data/river_levels_simulated.csv` holds **fabricated** test values. Never present them as real measurements.

## Architecture

| Step | Function | Module |
|---|---|---|
| Convert raw CWC export to the normalized schema | `convert_cwc_file(...)`, `list_stations(...)` | `src/cwc_adapter.py` |
| Load and validate rows | `load_observations(path)` / `validate_row(row, n)` | `src/river_monitoring_agent.py` |
| Quality control (placeholder, range, spike) | `validate_qc_config(cfg)` / `apply_quality_control(obs, qc)` | same |
| Recent data quality | `assess_recent_quality(accepted, rejected, reference_time)` | same |
| Freshness | `assess_freshness(observed_at, reference_time, max_age_hours, retrieved_at)` | same |
| Trend | `calculate_trend(observations, window_hours)` | same |
| Thresholds | `validate_thresholds(cfg)` / `evaluate_thresholds(level, thresholds)` | same |
| Full workflow | `RiverMonitoringAgent(...).run()` / `.to_dict()` | same |
| Save | `save_evidence(result, path)` | same |

None of these steps use the network. The agent reuses the Weather Agent's status values (`OK` / `PARTIAL` / `DATA_UNAVAILABLE`), its location constants and its `{code, severity, field, message}` error format. It uses the Python standard library only, so there are no new dependencies.

Processing order inside `run()`:
1. Read and validate rows; handle duplicates.
2. Drop anything observed or retrieved after the reference time.
3. Apply quality control per station.
4. Measure recent data quality.
5. Take the latest accepted reading, then compute freshness, trend and threshold status.

**The agent reports evidence only.** It never assigns `NORMAL`/`ALERT`/`EMERGENCY`/`RECOVERY` and never declares an emergency from a river reading.

## Input CSV schema (normalized)

```csv
timestamp,station_id,river_level_m,source_type
2025-12-30T05:00:00+05:30,UPPER_ANICUT,74.9,historical
```

| Column | Required | Rules |
|---|---|---|
| `timestamp` | yes | When the level was **observed**. ISO 8601 **with** a timezone (`Z` or `+05:30`), converted to UTC. Naive times are rejected. |
| `station_id` | yes | 1–64 characters: letters, digits, `_ . -`. |
| `river_level_m` | yes | Finite number. Empty, non-numeric, `NaN` and `inf` values are rejected. **They are never replaced with 0.** The valid range is decided by quality control (below). |
| `source_type` | yes | `simulated`, `historical` or `live`. Anything else is rejected. |
| `retrieved_at` | no | When the value was **retrieved** (timezone-aware). It must not be earlier than `timestamp`. |

Other columns are ignored with an `EXTRA_COLUMNS` warning. Exact duplicates are reduced to one row with a warning. **Conflicting duplicates** (same station and time, different values) are all rejected. Out-of-order rows are sorted and flagged.

## Real data: CWC telemetry (2021–2025)

### The file

`rwl_tel_hr_cwc_009_2021_2025.csv` (103.7 MB, 618,724 rows) contains hourly *River Water Level Telemetry* for 31 stations in the Cauvery basin, with Agency = CWC. The `UPPER ANICUT` station sits at **10.88861 N, 78.58000 E**, which is the project's study point. It has 25,453 rows from 01-01-2021 10:30 to 30-12-2025 05:00, and its `RL_of_zeroGauge` is 72.6 m.

The raw file is **not committed**, because it is above GitHub's 100 MB file limit. Put it in `data/raw/`, which is gitignored. The converted Upper Anicut file (about 1.4 MB) **is** committed in `data/cwc/`.

### Converting

```powershell
# See which stations the export contains
python -m src.cwc_adapter --input data/raw/rwl_tel_hr_cwc_009_2021_2025.csv --list-stations

# Convert one station (about 3 s)
python -m src.cwc_adapter --input data/raw/rwl_tel_hr_cwc_009_2021_2025.csv --station "UPPER ANICUT" --output data/cwc/upper_anicut_hourly_2021_2025.csv --metadata data/cwc/upper_anicut_station.json
```

The adapter only changes the format:
- `"UPPER ANICUT"` becomes the station ID `UPPER_ANICUT`.
- `DD-MM-YYYY HH:MM` becomes ISO 8601 with an offset.
- `source_type` is set to `historical`.
- **The level text is copied unchanged** (`NaN` stays `NaN`).
- The metadata JSON records the station attributes from the file, the raw file's SHA-256 hash and the assumptions below.

**Assumptions** (recorded as `"status": "ASSUMED"` in the metadata):
- **Timezone:** the export's timestamps have no timezone. They are interpreted as **IST (+05:30)**; use `--utc-offset` to change this. Verify it with the data provider.
- **Datum:** values are copied in metres as reported. Readings around 71–75 m with `RL_of_zeroGauge` = 72.6 m suggest the values are **reduced levels**, not gauge heights, but the file does not say so. The agent does not convert them.

### What the data looks like: telemetry quality findings

These findings come from inspecting the Upper Anicut series:

| Finding | Evidence | Handling |
|---|---|---|
| 79 `NaN` values | text `NaN` in the value column | rejected as `NON_NUMERIC_LEVEL`; never 0 |
| Values of 1–3 m in Jan–Mar 2021 | alternating ~1.0 / ~2.98, far below the gauge zero of 72.6 m | rejected by the range check |
| Values of 76–1070 m | 7,459 readings outside 70–80 m; the decimal part stays the same while the whole-metre part jumps (e.g. 102.859, 608.87, 1029.859) | rejected by the range check |
| Exactly **72.0 m**, 4,059 times | runs of identical 72.000 m readings up to 1,084 h long, plus single 72.0 readings among 73.0x m values | treated as a **fill value** (`invalid_values_m`) |
| In-range jumps by whole metres | e.g. 71.98 ↔ 89.87 within an hour (Jun 2024) | spike check, `max_jump_m` 1.0 |
| No credible high-water readings | every smooth run (6+ hourly readings changing < 0.3 m/h) stays within 71.0–75.2 m; Aug–Oct 2022 is almost entirely corrupted | high levels in this file **cannot** be verified |
| Gaps | 72 gaps longer than 24 h, the longest Sep 2021 → May 2022 | freshness and trend become unavailable |

Result over the whole record: **13,220 of 25,374 numeric readings (52%) pass QC.** The rest are rejected and counted, never corrected.

### Quality control (`config/river_qc.json`)

```json
"UPPER_ANICUT": {
  "valid_min_m": 70.0, "valid_max_m": 80.0, "max_jump_m": 1.0,
  "invalid_values_m": [72.0],
  "reference": "Prototype QC derived from ... (not official) ..."
}
```

The rules run in order, for each station, on data known at the reference time:
1. **Placeholder:** a value listed in `invalid_values_m` (compared at 3 decimals) is rejected (`PLACEHOLDER_VALUE`).
2. **Range:** a value outside `valid_min_m`–`valid_max_m` is rejected (`OUT_OF_RANGE`).
3. **Spike** (only if `max_jump_m` is set): the value is compared with the **median of up to 5 earlier in-range readings from the preceding 6 h**. If it differs by more than `max_jump_m`, it is rejected (`SPIKE`).
   - Only past readings are used, so replay stays causal.
   - The references are raw readings, not just accepted ones, so the check cannot lock itself out. A genuine sustained step is held back for about 3 readings, then accepted.
   - With fewer than 3 references (for example after a gap), the check is skipped and counted in `spike_check_skipped`.

Without QC settings for a station, only negative levels are rejected, and a `QC_NOT_CONFIGURED` warning is added. A negative `valid_min_m` is allowed for gauges that read below their zero.

**These bounds are prototype choices derived from this one file. They are not official, physical or hydrological limits.**
- The 80 m upper bound leaves room above anything credibly observed, but a real flood above 80 m would be **rejected**. Review the bounds with a hydrologist or with CWC.
- A genuinely fast rise (more than 1 m above the recent median) is also delayed by about 3 readings.
- In both cases the rejections make the recent data quality `SUSPECT`, so the evidence is flagged and is **never** reported as normal or safe.

### Recent data quality

`data_quality.recent_quality` counts readings in the **24 h before the reference time**: accepted readings, and rejected ones (`NaN`, conflicting duplicates, QC rejections).
- If more than **20%** were rejected, the status is `SUSPECT`, a `SUSPECT_RECENT_DATA` warning is added and the overall status cannot be `OK`. If the sensor is producing garbage, even the readings that passed QC are doubtful.
- With no readings in the window, the status is `UNAVAILABLE`.
- Both limits are configurable in Python (`quality_window_hours`, `suspect_fraction`).
- Hours with no row at all are not counted. Compare `readings_in_window` with the window length.

### Running on the real data

```powershell
python -m src.river_monitoring_agent --input data/cwc/upper_anicut_hourly_2021_2025.csv --station UPPER_ANICUT --qc config/river_qc.json --station-metadata data/cwc/upper_anicut_station.json --as-of 2025-12-30T06:00:00+05:30
```

| `--as-of` (IST) | Result |
|---|---|
| 2025-12-30 06:00 | 74.9 m at 05:00 IST, `FRESH`, change −0.004 m over 3 h, recent quality `ACCEPTABLE` (0 of 17 rejected). `PARTIAL` only because no thresholds are configured. |
| 2024-06-06 12:00 | 71.982 m, but recent quality `SUSPECT` (12 of 24 rejected), so `PARTIAL`. |
| 2022-10-05 12:00 (flood season) | Latest accepted reading is from 8 days earlier (`STALE`); all 22 readings in the last 24 h were rejected (`SUSPECT`). |
| (no `--as-of`) | `STALE`, because the file ends on 30-12-2025. |

## Running the River Agent

```powershell
# Simulated test data (historical replay at 12:00 UTC)
python -m src.river_monitoring_agent --input data/river_levels_simulated.csv --as-of 2026-10-10T12:00:00Z

# Simulated data with TEST thresholds, full JSON
python -m src.river_monitoring_agent --input data/river_levels_simulated.csv --as-of 2026-10-10T12:00:00Z --thresholds config/river_thresholds.example.json --json

# Real CWC data, saved to outputs/river_evidence.json
python -m src.river_monitoring_agent --input data/cwc/upper_anicut_hourly_2021_2025.csv --station UPPER_ANICUT --qc config/river_qc.json --station-metadata data/cwc/upper_anicut_station.json --as-of 2025-12-30T06:00:00+05:30 --save
```

Options:
- `--station ID`: required when the file has several stations.
- `--qc FILE`: QC settings.
- `--station-metadata FILE`: embedded in the output as provenance.
- `--thresholds FILE`: warning/danger thresholds.
- `--max-age-hours N`, `--trend-window-hours N`.
- `--json`; `--save` with optional `--output PATH`.

Without `--as-of`, the reference time is the current clock. The readable summary shows at most 12 errors and warnings; use `--json` to see all of them. The command exits with code `1` for `DATA_UNAVAILABLE` and `0` otherwise.

From Python:
```python
import json
from datetime import datetime, timedelta, timezone
from src.river_monitoring_agent import RiverMonitoringAgent, load_qc_file

ist = timezone(timedelta(hours=5, minutes=30))
agent = RiverMonitoringAgent(
    "data/cwc/upper_anicut_hourly_2021_2025.csv",
    station_id="UPPER_ANICUT",
    qc=load_qc_file("config/river_qc.json"),
    station_metadata=json.load(open("data/cwc/upper_anicut_station.json", encoding="utf-8")),
    as_of=datetime(2025, 12, 30, 6, 0, tzinfo=ist),
)
result = agent.run()
print(result["status"], result["river_level_m"], result["data_quality"]["recent_quality"]["status"])
```

## Running the tests

```powershell
python -m pytest tests/test_river_monitoring_agent.py tests/test_cwc_adapter.py -v   # River + adapter
python -m pytest -v                                                                 # whole project
```
The tests write small temporary CSV files and need neither internet access nor the 103 MB raw file.

## Evidence output schema

| Field | Category | Meaning |
|---|---|---|
| `agent_name` | – | Always `RiverMonitoringAgent`. |
| `status` | – | `OK`: fresh latest reading, trend available, thresholds evaluated, recent quality `ACCEPTABLE` with **no** rejected readings in the quality window, and no rejected rows with an unknown time. `PARTIAL`: a usable reading exists but at least one of these fails (see `warnings`). `DATA_UNAVAILABLE`: no usable reading; every measurement is `null`. **`OK` describes completeness, not reliability.** Check `data_quality.evidence_grade`. |
| `location` | – | Study site (Upper Anicut coordinates, timezone). |
| `station_id` | observed | Station evaluated. |
| `source` / `input_file` | provenance | Source description (taken from the station metadata if given), CSV path and SHA-256. |
| `station_metadata` | provenance | Contents of `--station-metadata` (station attributes, raw file hash, timezone/datum assumptions), or `null`. |
| `source_type` | observed | `source_type` of the latest accepted observation. |
| `observed_at` | observed | Time of the latest accepted observation (UTC). |
| `retrieved_at` | observed | Its retrieval time if the CSV has one, otherwise `null`. |
| `river_level_m` | observed | Latest accepted level. |
| `level_change_m`, `rate_of_change_m_per_hour` | derived | Copied from `trend`. `null` if unavailable. |
| `trend` | derived | `status`, `method`, `baseline_*`, `latest_*`, `duration_hours`, `direction`, `observations_in_window`, `reason`. Uses accepted readings only. |
| `freshness` | derived | `status`, `reference_time`, `reference_mode`, `max_age_hours`, `age_minutes`/`age_hours`, retrieval info, `reason`. |
| `data_quality` | – | Whole-file row counts (`rows_read`, `valid_rows`, `rejected_rows`, `rejected_rows_without_time`, duplicates, conflicts, out of order); `excluded_after_reference_time`; `station_observations_used` (accepted by QC); `qc` (settings and counts per rule); `recent_quality`; `source_types_present`, `evidence_grade`, `is_simulated`, `is_verified_live` (always `false`), `verification_status` (always `UNVERIFIED`). |
| `threshold_status` | interpretation | `BELOW_WARNING`, `AT_OR_ABOVE_WARNING`, `AT_OR_ABOVE_DANGER` or `UNKNOWN`. |
| `threshold_evaluation` | interpretation | Thresholds used, their reference, the level compared and `based_on_stale_data`. |
| `station_summaries` | observed | Latest QC-accepted reading for **every** station in the file. |
| `field_categories` | – | Which fields are observed, derived or interpretation. |
| `errors` / `warnings` | – | `{code, severity, field, message}`. Rejected rows and QC rejections known at the reference time, **most recent first**, at most 50 of each (a `*_TRUNCATED` warning gives the total). Older rejections are listed for transparency but only those inside the quality window affect `status`. |
| `data_notes` | – | Provenance and limitation notes. |

`evidence_grade` is one of `SIMULATED`, `HISTORICAL`, `UNVERIFIED_LIVE`, `MIXED_UNVERIFIED` or `UNAVAILABLE`. `VERIFIED_LIVE` is reserved for a future verified source and is **never** produced now.

## Freshness policy

- Age = reference time − **observation** timestamp.
- An observation is `FRESH` if its age is ≤ `max_age_hours` (default **3 h**, a prototype choice); otherwise it is `STALE`. With no accepted reading, the status is `UNAVAILABLE`.
- Stale readings are still reported, with a `STALE_OBSERVATION` warning, `status` set to `PARTIAL` and the threshold comparison marked `based_on_stale_data`.
- Missing, rejected or unavailable evidence is never treated as safe.

## Trend method

The latest accepted reading is compared with the **earliest accepted** reading inside the preceding window (default 3 h). With fewer than 2 readings in the window, or a zero-duration interval, the trend is `UNAVAILABLE`. It is never reported as 0.
Simulated example: (3.35 − 2.80) m over 3 h = 0.1833 m/h. Real example (as of 30-12-2025 06:00 IST): −0.004 m over 3 h.

## Threshold configuration

No official Upper Anicut warning or danger levels are built in, and **none are included for the real station**. Supply them as JSON with a citable `reference`:

```json
{ "UPPER_ANICUT": { "warning_level_m": 0.0, "danger_level_m": 0.0, "reference": "authority, document, date" } }
```
(The zeros are placeholders; fill in authoritative values.) Both levels are required and the warning level must be below the danger level. A malformed configuration stops the program. A station with no entry gets `UNKNOWN`.
Thresholds must be on the same datum as the data. These readings appear to be reduced levels (around 72–75 m), so thresholds expressed as gauge heights would need `RL_of_zeroGauge` (72.6 m) added. Confirm this with the provider.
`config/river_thresholds.example.json` holds **made-up TEST values** for the simulated station only.

## Historical replay

`--as-of` fixes the reference time. Readings **observed or retrieved after** it are excluded **before** quality control, so they cannot influence QC, recent quality, the error list or the result. With `as_of`, repeated runs give identical JSON.

## Output file

`--save` writes `outputs/river_evidence.json` (or `--output PATH`). It is **always overwritten, even with `DATA_UNAVAILABLE`**, so an older result can never be mistaken for current evidence. The committed sample comes from the real CWC data replayed at 30-12-2025 06:00 IST.

## Data limitations

- **Historical, not live:** the latest real reading is from 30-12-2025. Without `--as-of`, the evidence is `STALE`.
- **Raw telemetry:** about half the Upper Anicut record fails QC, and some corrupted readings can still pass QC (for example right after a gap, when the spike check is skipped). The `SUSPECT` flag reports this; it does not remove it.
- **Assumed timezone and datum** (see above).
- **No high-water validation:** the file contains no credible readings above about 75.2 m, so the agent's behaviour in a real flood has **not** been tested on real data.
- **Simulated data** (`UPPER_ANICUT_TEST`) only tests the software. That station and its thresholds are invented.

## What is still needed for a verified live river-level source

1. **An authoritative live provider** for Upper Anicut (CWC or the Tamil Nadu Water Resources Department), with documented access terms. No endpoint is assumed here.
2. **Confirmation from the provider** of the timezone, datum and the meaning of 72.0 m, and of any QC flags the provider itself applies.
3. **Official warning and danger levels** for the station, on the same datum, with a citable reference.
4. **A fetcher** that writes the normalized schema with `retrieved_at`, tested offline with mocked responses.
5. **A verification rule** (trusted endpoint, authentication, response checks). Only that rule should ever set `is_verified_live` / `VERIFIED_LIVE`.
6. **QC bounds reviewed by a hydrologist**, and a `max_age_hours` justified by the source's reporting interval.
