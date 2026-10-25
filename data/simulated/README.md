# Simulated comparison sources (FABRICATED)

All files here are fabricated test data for the station `UPPER_ANICUT_TEST`
(every row has `source_type` = `simulated`). They are not measurements of the
Cauvery River. They give the workflow real data to compare against the
primary simulated series `data/river_levels_simulated.csv`
(08:00–11:00 UTC on 2026-10-10: 2.80, 2.95, 3.10, 3.35 m). No conflict is
injected in code; every conflict comes from comparing these values.

| File | Role | 11:00 UTC value | Effect |
|---|---|---|---|
| `secondary_agrees.csv` | second source | 3.352 m | differs by 0.002 m, within the 0.005 m tolerance, so no conflict |
| `secondary_conflicts.csv` | second source | 3.80 m | 0.45 m from the primary, a conflict on the current reading |
| `independent_supports_primary.csv` | independent re-check source | 3.35 m | backs the primary value, so the conflict is RESOLVED |
| `independent_supports_secondary.csv` | independent re-check source | 3.80 m | backs the other source, so the CONFLICT PERSISTS |

The `reverification-fails` scenario points at an independent source that
does not exist, so the re-check cannot be completed and the result is FAILED.

## Risk-scenario files (FABRICATED)

Used by `python -m src.river_agent_workflow --mode simulated --risk-scenario <name>`
(see the README's Risk Assessment section). River series are hourly at
08:00–11:00 UTC on 2026-10-10 for `UPPER_ANICUT_TEST`, assessed at 12:00 UTC
against the TEST thresholds in `config/river_thresholds.example.json`
(3.0 / 4.0 m, not official).

| File | Values | Used by |
|---|---|---|
| `river_stable_low.csv` | 2.05, 2.05, 2.06, 2.05 m | low-rain, heavy-forecast, stale, missing-weather |
| `river_rising_below_warning.csv` | 2.20 → 2.70 m (+0.17 m/h) | rising-river-increasing-rain |
| `river_above_danger.csv` | 3.70 → 4.40 m | simulated-high-risk |
| `river_recession.csv` | 3.40 → 2.60 m (falling through the warning level) | recovery-after-high-water |

`weather/*.json` files are Open-Meteo-shaped payloads (96 hourly steps from
2026-10-09 00:00 IST, simulated retrieval 2026-10-10T12:00Z), parsed by the
real `WeatherAgent.parse_response` and relabelled `SIMULATED_TEST_DATA`.
Each must declare `"simulated": true` or it is refused.

| File | Past 24 h | Next 24 h |
|---|---|---|
| `weather/light_rain.json` | 2.4 mm | 2.4 mm |
| `weather/heavy_forecast.json` | 1.0 mm | 130.8 mm |
| `weather/heavy_rain_intensifying.json` | 84 mm (48 mm in the last 6 h) | 90 mm |
| `weather/extreme_rain.json` | 156 mm | 228 mm |
| `weather/easing_after_event.json` | 12 mm (heavy rain before that) | 0 mm |

## Flood-cycle files (FABRICATED)

Used by `python -m src.disaster_state_manager --sequence flood-cycle` and by
`tests/test_disaster_state_manager.py`. Station `UPPER_ANICUT_TEST`, TEST
thresholds 3.0 / 4.0 m (not official).

| File | Content |
|---|---|
| `flood_cycle/river.csv` | Hourly from 2026-10-10T00:00Z for 120 h: 2.00 m to h35; rise 0.10 m/h, then 0.20 m/h to a 4.50 m peak (h55-56); recession 0.12 m/h, below 3.0 m from h70; 2.25 m from h84 |
| `flood_cycle/weather.json` | Open-Meteo-shaped, 192 hourly steps from 2026-10-09 00:00 IST: 8 mm/h from about river hour 20 to 50, 1 mm/h to 54, dry afterwards. Each step overrides `retrieved_at` with its replay time |
