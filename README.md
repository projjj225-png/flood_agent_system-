# State-Aware Trust and Evidence-Driven Capability Control for Multi-LLM Agents in Flood Disaster Management

Prototype location: **Cauvery River near Upper Anicut, Tamil Nadu, India**
(latitude 10.888611, longitude 78.580000, timezone Asia/Kolkata).

**Current progress:** Phase 1, components 1 and 2: the **Weather Agent** and the
**River Monitoring Agent**, with a river evidence service and a local HTTP API. Data sources: the National Water
Data Portal's CWC telemetry API (verified, but its latest Upper Anicut data is months old), real historical
CWC telemetry 2021–2025, and simulated test data.
Nothing else is implemented yet. That includes evidence conflict detection, trust scoring, risk assessment,
disaster-state logic (the disaster state is currently supplied by the caller) and the dashboard.

## Project structure

```
flood_agent_system/
├── src/
│   ├── __init__.py
│   ├── weather_agent.py            # The Weather Agent
│   ├── river_monitoring_agent.py   # The River Monitoring Agent (CSV input, QC, thresholds)
│   ├── cwc_adapter.py              # Converts the raw CWC telemetry export to the agent's schema
│   ├── river_sources.py            # Live NWDP/CWC API connector: retries + verification
│   ├── river_service.py            # Entry point: modes, fallback, conflicts, audit, envelope
│   ├── river_api.py                # Read-only local HTTP API
│   ├── audit_log.py                # Append-only JSONL audit log
│   ├── capability_control.py       # State allowlists + evidence gate (never executes actions)
│   ├── evidence_reverification.py  # Conflict policy + actual re-check of conflicting readings
│   └── river_agent_workflow.py     # Full loop: evidence -> conflicts -> reverification -> permissions
├── tests/
│   ├── test_weather_agent.py            # Offline tests (mocked API, no internet needed)
│   ├── test_river_monitoring_agent.py   # Offline tests (temporary CSV files)
│   ├── test_cwc_adapter.py              # Offline tests for the CWC converter
│   ├── test_river_sources.py            # Connector: verification, retries, failures
│   ├── test_river_service.py            # Service: modes, fallback, conflicts, audit, CLI
│   ├── test_river_api.py                # HTTP API end to end (local sockets)
│   ├── test_river_real_data.py          # Regression tests on the real CWC data
│   ├── test_capability_control.py       # State allowlists, audit-gated execution
│   ├── test_evidence_reverification.py  # Conflict policy
│   ├── test_river_workflow.py           # Integrated workflow: real conflicts, reverification, permissions
│   └── nwdp_fixtures.py                 # API-shaped fixtures (provenance in the file)
├── data/
│   ├── raw/                        # Put rwl_tel_hr_cwc_009_2021_2025.csv here (gitignored, 103 MB)
│   ├── cwc/
│   │   ├── upper_anicut_hourly_2021_2025.csv  # Real CWC telemetry, Upper Anicut, normalized
│   │   └── upper_anicut_station.json          # Station attributes, provenance, assumptions
│   ├── simulated/                  # FABRICATED comparison sources for workflow scenarios
│   └── river_levels_simulated.csv  # FABRICATED test levels, not real measurements
├── config/
│   ├── river_agent.json               # Service config: station, live source, paths
│   ├── river_qc.json                  # Prototype QC settings (data-derived, NOT official)
│   └── river_thresholds.example.json  # TEST thresholds only, not official levels
├── outputs/                  # Weather samples, river_evidence.json; live/ and audit/ (gitignored)
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

> **Status of the live source (checked 2026-10-13):** the agent can fetch CWC telemetry for Upper Anicut from
> the **National Water Data Portal's documented CKAN API** and verify where it came from. However, the provider's
> newest Upper Anicut reading was **03-06-2026 21:00 IST** (more than 4 months old), so live evidence is
> currently graded `VERIFIED_SOURCE_STALE`, **not** current. The connector has been tested offline with responses
> that mirror the real API. Run `python -m src.river_service --probe` on your PC to demonstrate the Python
> connection yourself (see [Live data](#live-data-cwc-via-the-national-water-data-portal)).
>
> Other sources:
> - **Historical:** real CWC hourly telemetry 2021–2025 (`data/cwc/`, see [Real data](#real-data-cwc-telemetry-20212025)).
> - **Simulated:** `data/river_levels_simulated.csv` holds **fabricated** test values. Never present them as real measurements.

## River evidence service (recommended entry point)

`src/river_service.py` wraps the agent for the Weather Agent and the future orchestrator. It handles:
- source selection;
- live fetching and verification;
- a labelled fallback;
- cross-source conflict checks;
- audit logging;
- a stable output envelope.

```python
from src.river_service import RiverEvidenceService

service = RiverEvidenceService.from_file("config/river_agent.json")
evidence = service.get_evidence(mode="auto")      # JSON-compatible dict
if not evidence["decision_support"]["current_evidence_usable"]:
    ...  # river conditions are UNKNOWN - never assume safe
```

| Mode | What it does | `source_label` |
|---|---|---|
| `auto` (default) | Tries live. If the API is unreachable or verification fails, it falls back to the historical CSV with a `HISTORICAL_FALLBACK_USED` warning. | `LIVE_VERIFIED`, `LIVE_UNVERIFIED` or `HISTORICAL_FALLBACK` |
| `live` | Live only, no fallback. An outage gives `DATA_UNAVAILABLE`. | `LIVE_VERIFIED`, `LIVE_UNVERIFIED` or `LIVE_UNAVAILABLE` |
| `historical` | Committed real CWC data 2021–2025. Supports `as_of` replay. | `HISTORICAL_CSV` |
| `simulated` | Fabricated test data. Supports `as_of`. | `SIMULATED_TEST_DATA` |

`as_of` is refused for `live`/`auto`, because live data describe the present.

### Command line

```powershell
python -m src.river_service                                   # auto mode, readable summary
python -m src.river_service --probe                           # live connectivity probe with every verification check
python -m src.river_service --mode historical --as-of 2025-12-30T06:00:00+05:30 --json
python -m src.river_service --mode simulated --as-of 2026-10-10T12:00:00Z
python -m src.river_service --mode auto --save                # writes outputs/river_evidence.json
```
Exit codes: `0` = evidence produced (`OK`/`PARTIAL`), `1` = `DATA_UNAVAILABLE`, `2` = configuration error.

### Local HTTP API (read-only)

```powershell
python -m src.river_api --port 8765
```

| Request | Returns |
|---|---|
| `GET /health` | `{"status": "ok", "schema_version": "1.0"}` |
| `GET /river/evidence?mode=auto` | The full evidence JSON |
| `GET /river/evidence?mode=historical&as_of=2025-12-30T06:00:00%2B05:30` | A replay (encode `+` as `%2B`) |
| `GET /river/summary?mode=auto` | IDs plus the `decision_support` block only |

- **Binding:** it listens on `127.0.0.1` only, unless you pass `--host`.
- **Caching:** `live`/`auto` results are cached for `--cache-seconds` (default 300) to protect the provider. The cache age is shown in `cache_age_seconds` and the `X-Evidence-Cache-Age` header.
- **Errors:** bad requests return `400`, POST returns `405`. Internal errors return `500` with `"status": "DATA_UNAVAILABLE"`, never data.

### Evidence envelope (added to the agent's fields; existing fields unchanged)

| Field | Meaning |
|---|---|
| `schema_version` | `"1.0"`. Bump it on breaking changes. |
| `evidence_id` | SHA-256 of the evidence without `run_id`/`generated_at`/`audit`. The same input gives the same ID, which makes replays reproducible. |
| `run_id`, `generated_at` | ID and UTC time of this run (see the audit log). |
| `mode_requested`, `source_label` | What was asked for, and what the data actually are. |
| `fallback` | `{used, reason}`. |
| `live_fetch` | `attempted`, `status` (`FETCHED`/`UNAVAILABLE`/`DISABLED`), `error`, every HTTP `attempts` entry (URL, parameters, status, bytes, SHA-256, retry delay), `verification` (all checks), `snapshot_path`, `snapshot_sha256`, `raw_paths`, `retrieved_at`. |
| `cross_source_check` | Readings at the same station and time compared across sources: `status` (`CONSISTENT`/`CONFLICT`/`NO_OVERLAP`/`NOT_CHECKED`), `conflicts`, `conflicts_affecting_current_evidence`, `examples`. |
| `decision_support` | `current_evidence_usable` (bool), `reasons_not_usable`, `river_level_m` (`null` unless usable), `last_known_level_m`/`_observed_at`, `trend_direction`, `rate_of_change_m_per_hour`, `threshold_status`. |
| `audit` | `log_path`, `run_id`, `events_written`, `write_errors`. |
| `data_quality.source_verification` | `NOT_APPLICABLE` (CSV), `VERIFIED` or `FAILED`, with the reason, `failed_checks`, `snapshot_hash_matches` and resource details. |

**`current_evidence_usable` is true only if all of these hold:**
- a reading exists;
- the source is verified live;
- freshness is `FRESH`;
- recent quality is `ACCEPTABLE`;
- there is no fallback;
- no source conflict affects the current readings.

Otherwise the orchestrator must treat river conditions as **unknown**, never as safe. Every rule is deterministic Python; no LLM is involved.

### Evidence grades

| Grade | Meaning |
|---|---|
| `VERIFIED_LIVE` | Live snapshot, all verification checks passed, the file hash matches the verified snapshot, and the latest reading is fresh. |
| `VERIFIED_SOURCE_STALE` | Same as above, but the latest reading is not fresh. This is the **current real situation** for Upper Anicut. |
| `UNVERIFIED_LIVE` | Rows labelled `live` that did not pass verification, or a CSV that merely claims to be live. |
| `HISTORICAL`, `SIMULATED`, `MIXED_UNVERIFIED`, `UNAVAILABLE` | As before. |

## River Agent workflow: conflicts, reverification, permissions

`src/river_agent_workflow.py` is the complete River Agent loop. Each step is deterministic Python and is recorded in `outputs/audit/river_workflow_audit.jsonl`.

1. **Evidence.** `RiverEvidenceService.get_evidence()` in mode `live`, `auto`, `historical` or `simulated`.
2. **Conflict detection.** The service compares readings at the same station and timestamp across sources (`cross_source_check`). A conflict is a difference above `tolerance_m` (0.005 m). **No conflict is injected in code.**
3. **Conflict policy.** `evidence_reverification.assess_conflict()` decides whether a conflict that affects current evidence requires reverification.
4. **Reverification.** It runs only if the disaster state allows `request_evidence_reverification`, and goes through `capability_control.execute_if_authorized()`, so it is audited and fails closed:
   - **Re-obtain the primary source:** a new live fetch in live mode, or a re-read of the file otherwise.
   - **Check it hasn't changed:** each conflicting reading must have the same value on re-check.
   - **Check an independent source:** the conflicting value is compared with sources that are neither the primary nor the conflicting source (`reverify_conflicts()`).

   | Outcome | When |
   |---|---|
   | `RESOLVED` | For every conflicting reading, the primary is unchanged and an independent source agrees with the primary but not with the conflicting source. |
   | `CONFLICT_PERSISTS` | Any reading where the independent source backs the other value, sources disagree, the primary changed or went missing, or no independent reading exists. |
   | `FAILED` | The primary is unavailable on re-check, or no independent source could be read. |
   | `NOT_AUTHORIZED` | The state's allowlist doesn't include `request_evidence_reverification` (e.g. `NORMAL`), or the decision couldn't be audited. |

   Values are never averaged, picked or corrected. Reverification only decides whether the restriction may be lifted.
5. **Permissions** (`capability_control.authorize_with_evidence`) apply two fail-closed gates to each requested capability:
   - **State gate:** the capability must be on the state's allowlist (your `STATE_CAPABILITIES`, unchanged).
   - **Evidence gate:** applies to *real-world* capabilities (`prepare_advisory`, `propose_evacuation_plan`, `request_human_emergency_authorization`). They need evidence that is not simulated, `LIVE_VERIFIED` with `is_verified_live`, `FRESH`, `ACCEPTABLE` in recent quality, not a fallback, and free of unresolved conflicts. Anything else is denied with reason codes (`EVIDENCE_SIMULATED`, `EVIDENCE_STALE`, `EVIDENCE_NOT_VERIFIED_LIVE`, `EVIDENCE_UNRESOLVED_CONFLICT`, …).
   - **Even when both gates pass,** the decision is `PERMITTED_PENDING_HUMAN_APPROVAL` and `real_world_authorization` is `HUMAN_AUTHORIZATION_REQUIRED`. The system never self-authorizes or executes an action.
   - **Other capabilities:** read-only ones (`read_*`) and internal ones (`request_evidence_reverification`, `prepare_recovery_assessment`) need only the state gate.
   - **Audit failure:** if a decision cannot be written to the audit log, that permission is revoked (`AUDIT_WRITE_FAILED`). An incomplete workflow audit forces `RESTRICTED`.

Key result fields:
- **Identity:** `decision_id` (deterministic), `service_evidence_id`.
- **Comparison and reverification:** `cross_source_check`, `reverification` (outcome, per-conflict statuses, independent sources and errors, `fresh_evidence_id`).
- **Permissions:** `evidence_assessment` (eligible, reasons), `capability_decisions`, `real_world_authorization`.
- **Levels:** `river_level_m`, which is `null` unless the evidence is eligible. For simulated runs: `simulated_river_level_m` and `simulation_permission`.

### Simulated scenarios are data, not code

The scenario only chooses **which fabricated source files** are compared (see `data/simulated/README.md`). The outcome comes from the values in them:

| `--scenario` | Second source (11:00 UTC) | Independent source | Result in `ALERT`/`EMERGENCY` |
|---|---|---|---|
| `no-conflict` | 3.352 m (within tolerance) | – | `CONSISTENT`, `SIMULATION_ALLOWED` |
| `conflict-resolves` | 3.80 m | 3.35 m (backs primary) | `RESOLVED`, `SIMULATION_ALLOWED` |
| `conflict-persists` | 3.80 m | 3.80 m (backs other) | `CONFLICT_PERSISTS`, `SIMULATION_RESTRICTED` |
| `reverification-fails` | 3.80 m | missing file | `FAILED`, `SIMULATION_RESTRICTED` |

In every scenario, `real_world_authorization` is `RESTRICTED` and real-world capabilities are denied with `EVIDENCE_SIMULATED`.

### Commands

```powershell
python -m src.river_agent_workflow --mode simulated --scenario conflict-resolves --state ALERT
python -m src.river_agent_workflow --mode simulated --scenario conflict-persists --state EMERGENCY
python -m src.river_agent_workflow --mode simulated --scenario conflict-resolves --state NORMAL     # reverification NOT_AUTHORIZED
python -m src.river_agent_workflow --mode auto --state EMERGENCY                                   # live if reachable, else labelled fallback
python -m src.river_agent_workflow --mode historical --as-of 2025-12-30T06:00:00+05:30 --state ALERT
python -m src.river_agent_workflow --mode live --state EMERGENCY --capability read_river --capability propose_evacuation_plan
```
Exit codes: `0` = done, `1` = evidence unavailable, `2` = config error or audit write failure.

**Real data today:** the verified NWDP live data for Upper Anicut is months old, so `--mode live`/`auto` always ends `RESTRICTED` (`EVIDENCE_STALE`, …). No independent second source for the real station is configured (`reverification.independent_paths` is empty), so a conflict on real data can never be `RESOLVED`. It stays restricted until a genuine independent source is added.

## Live data: CWC via the National Water Data Portal

**Provider:** National Water Data Portal (NWDP), run by the National Water Informatics Centre (NWIC), Ministry of Jal Shakti. The data producer is CWC.

The portal runs CKAN 2.11, so it has the standard, publicly documented **CKAN Action API**. No credentials are needed or used. The identifiers below were read from the API on 2026-10-13:

| Item | Value |
|---|---|
| API base | `https://nwdp.nwic.gov.in/api/3/action/` |
| Dataset | "River Water Level (Telemetry - Hourly), Central Water Commission (CWC)", id `68600163-c5a0-4327-aa1a-fa7157b86cce`, licence "Other (Open)" |
| Resource | "River Water Level CWC Cauvery (2026 - 2030) Telemetry Hourly", id `d027c5ac-379d-4ac2-8ced-97b02b6edbc0`, DataStore active |
| Station filter | `Station = "UPPER ANICUT"` (1,696 rows on 2026-10-13) |
| Level field / unit | `River Water Level Telemetry Hourly (meter)`, metres |
| Time field | `Data Acquisition Time`, `DD-MM-YYYY HH:MM`, no timezone; **IST assumed** |
| Station attributes | Agency CWC; lat 10.88861111, lon 78.58000000; `RL_of_zeroGauge` 72.6; Tiruchirappalli, Tamil Nadu |

**Each live fetch makes two requests.** Each request uses a 5 s connect / 30 s read timeout and up to 3 attempts. Timeouts, connection errors, 429 and 5xx are retried after 2 s and then 5 s; other 4xx errors are not retried.
1. `resource_show?id=<resource>` to get the dataset, name and `last_modified`.
2. `datastore_search?resource_id=<resource>&filters={"Station":"UPPER ANICUT"}&sort=_id desc&limit=72` to get the newest 72 rows.

**Verification** (`src/river_sources.py`). The data count as verified only if every check passes:
- **Identity:** resource ID, dataset ID and resource name match the config; the DataStore resource ID matches.
- **Format:** required fields are present; the level field declares metres.
- **Content:** at least one record; every record's station name, agency, coordinates (±0.001°) and gauge zero (±0.001 m) match the config.
- **Time:** timestamps parse, and none are more than 1 h later than the retrieval time (this catches timezone errors).

Verification proves where the data came from. QC and freshness then judge the values and how current they are.

**Audit files** (gitignored), written on every fetch:
- `outputs/live/`: the raw `resource_show` and `datastore_search` JSON, plus a normalized snapshot CSV with `retrieved_at`, whose SHA-256 is recorded.
- `outputs/audit/river_audit.jsonl`: one JSON line per event: `run_started`, `live_fetch_ok`/`live_fetch_failed`, `live_verification`, `fallback_used`, `evidence_produced`.

**What was observed on 2026-10-13:**
- **Freshness:** the resource's `last_modified` is updated daily, but its newest rows for every station are from late May / early June 2026, and the newest Upper Anicut row is 03-06-2026 21:00 IST.
- **Corruption:** many values are still corrupted (151–350 m), alternating with about 71.7–71.9 m.
- **Result:** the agent reports `VERIFIED_SOURCE_STALE`, `STALE` freshness and `SUSPECT` recent quality, with `current_evidence_usable: false`. This is the correct and safe outcome.

**Limitations:**
- **Demonstration:** this development environment could not reach `nwdp.nwic.gov.in`; its proxy refused the connection. That exercised the outage path (`LIVE_UNAVAILABLE`/fallback), but the Python connector itself has not been shown to connect from here. Run `--probe` on a machine with internet access.
- **Terms:** the licence is "Other (Open)". Confirm the portal's terms and acceptable request rates with NWIC (helpdesk-nwic@gov.in) before polling regularly.
- **Not real-time:** the portal publishes data with a lag of months, so it is not a real-time warning feed. CWC's operational flood-forecasting feed has no documented public API.

## Service configuration (`config/river_agent.json`)

| Section | Keys |
|---|---|
| `station` | `station_id`, `provider_station_name`, `agency`, `river`, `latitude`, `longitude`, `rl_zero_gauge_m`, `timestamp_utc_offset` + `timestamp_offset_status` (`ASSUMED`), `data_datum` (`reduced_level`/`gauge_height`) + `data_datum_status` |
| `live` | `enabled`, `api_base_url` (https only), `allowed_hosts`, `dataset_id`, `resource_id`, `resource_name`, `station_field`, `time_field`, `level_field`, `records_to_fetch` (1–1000), `timeout_seconds` [connect, read], `max_attempts` (1–10), `backoff_seconds`, `coordinate_tolerance_deg`, `rl_zero_tolerance_m` |
| `historical` / `simulated` | Data file paths, plus station metadata or station ID |
| top level | `qc_path`, `thresholds_path` (`null` = none configured), `max_age_hours`, `trend_window_hours`, `quality_window_hours`, `suspect_fraction`, `cross_check` {`enabled`, `paths`, `tolerance_m`} |
| `outputs` | `snapshot_dir`, `audit_log`, `evidence` |

When the 2026–2030 resource is superseded (for example, a 2031–2035 file), update `resource_id` and `resource_name` from the portal. Verification will fail until you do, so a silent mismatch is impossible.

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
python -m pytest -v                                   # everything (267 tests)
python -m pytest tests/test_river_workflow.py -v      # workflow: conflicts, reverification, permissions
python -m pytest tests/test_river_sources.py -v       # live connector: verification, retries, failures
python -m pytest tests/test_river_service.py -v       # service: modes, fallback, conflicts, audit, CLI
python -m pytest tests/test_river_api.py -v           # HTTP API end to end
python -m pytest tests/test_river_real_data.py -v     # regression on real CWC data
```

| File | Tests | Covers |
|---|---|---|
| `test_weather_agent.py` | 34 | Weather Agent (unchanged) |
| `test_river_monitoring_agent.py` | 89 | Validation, freshness, trend, thresholds, QC, replay |
| `test_cwc_adapter.py` | 14 | CWC export converter |
| `test_river_sources.py` | 34 | Unit and failure-mode tests: each verification check, retries (timeout, 429, 5xx), no retry on 4xx, invalid JSON, CKAN failure, config validation, conversion |
| `test_river_service.py` | 30 | Integration tests: verified fresh/stale live data, verification failure, outage (live vs auto), fallback, tampered snapshot, conflict downgrade, deterministic `evidence_id`, audit trail and audit failure, official thresholds/datum, CLI end to end |
| `test_river_api.py` | 10 | HTTP end to end: health, evidence, summary, caching, 400/404/405/500 |
| `test_river_real_data.py` | 6 | Real-data regression tests (counts, replays, why QC is needed) |
| `test_capability_control.py` | 13 | State allowlists, unknown state, audit-gated execution |
| `test_evidence_reverification.py` | 9 | Conflict policy, audited reverification request |
| `test_river_workflow.py` | 28 | No injected conflicts; value-driven conflicts; all four scenarios; `NORMAL` cannot reverify; live verified/stale/outage; live conflict resolved by re-fetch plus independent source; primary changed; outage during re-check; conflicting source can't be its own tie-breaker; audit failure revokes permissions; capability classification; CLI |

All tests are offline. Live API responses are replaced by fixtures shaped like the real ones; their provenance is described in `tests/nwdp_fixtures.py`.

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
{ "UPPER_ANICUT": { "warning_level_m": 0.0, "danger_level_m": 0.0, "official": true,
                    "datum": "reduced_level", "reference": "authority, document, date" } }
```
(The zeros are placeholders; fill in authoritative values.)
- **Validation:** both levels are required, and the warning level must be below the danger level. A malformed configuration stops the program.
- **`official`:** `true` requires a `reference`. Thresholds not marked official produce a `THRESHOLDS_NOT_OFFICIAL` warning.
- **`datum`:** `gauge_height` or `reduced_level`. If it differs from the data's datum (`station.data_datum` in the service config, or `--data-datum`), the result is `UNKNOWN` with a `THRESHOLD_DATUM_MISMATCH` warning, and no comparison is made.
- **Missing entry:** a station with no entry gets `UNKNOWN`.
- **Using it:** set `thresholds_path` in `config/river_agent.json` to the file.
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

## What is still needed

1. **An independent second source for Upper Anicut**, e.g. manual gauge readings or a state WRD feed, added to `reverification.independent_paths`. Without one, conflicts on real data cannot be resolved.
2. **A fresher live feed.** NWDP's API is genuine and verified, but its Upper Anicut data currently lag by months. For real-time use, ask CWC or NWIC (helpdesk-nwic@gov.in) for operational access, or for confirmation of the update schedule. Then point `live` at that source; verification will check it.
3. **Confirmation from the provider** of the timezone (IST assumed), the datum (reduced level assumed), the meaning of 72.0 m, and any QC flags the provider itself applies.
4. **Official warning and danger levels** for the station, with datum and reference. None are configured, because none were found published.
5. **QC bounds reviewed by a hydrologist**, and a `max_age_hours` justified by the feed's real reporting interval.
6. **Live connectivity has been demonstrated:** your PC's runs on 2026-10-13 fetched and `VERIFIED` real NWDP data (snapshots in `outputs/live/`).
