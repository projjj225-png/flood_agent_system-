# State-Aware Trust and Evidence-Driven Capability Control for Multi-LLM Agents in Flood Disaster Management

Prototype location: **Cauvery River near Upper Anicut, Tamil Nadu, India**
(latitude 10.888611, longitude 78.580000, timezone Asia/Kolkata).

**Current progress:** Phase 1, component 1: the **Weather Agent** only.
Nothing else is implemented yet. That includes the other agents, trust scoring, risk assessment,
disaster-state logic, capability control and the dashboard.

## Project structure

```
flood_agent_system/
├── src/
│   ├── __init__.py
│   └── weather_agent.py      # The Weather Agent
├── tests/
│   └── test_weather_agent.py # Offline tests (mocked API, no internet needed)
├── outputs/                  # Saved samples from real live API calls only
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

`outputs/` contains only results saved from **real, successful** API calls (`--save`).
Each file is named `weather_live_<UTC retrieval time>.json`. It contains a `label` with the retrieval time, the structured `result` and the unmodified `raw_api_response`.
The code refuses to save results with the status `DATA_UNAVAILABLE`.
