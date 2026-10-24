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
