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

## Fabricated flood event: `flood_event_upper_anicut_test.csv`

84 hourly readings (2026-10-11 00:00 UTC to 2026-10-14 11:00 UTC) for the
test station `UPPER_ANICUT_TEST`. The level starts at 2.50 m for 12 h, rises
0.12 m/h to a 4.66 m peak, holds for 4 h, then falls 0.06 m/h back to 2.50 m.
It is used with the TEST thresholds in `config/river_thresholds.example.json`
(warning 3.0 m, danger 4.0 m, not official) by
`python -m src.flood_orchestrator --simulate-event` to show the whole
NORMAL -> ALERT -> EMERGENCY -> RECOVERY -> NORMAL cycle. It is not real
Cauvery data.
