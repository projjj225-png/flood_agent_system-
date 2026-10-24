# Independent second source for Upper Anicut (template)

Real cross-source conflict detection and reverification need readings from a
source other than the CWC telemetry, for example manual staff-gauge readings
or a Tamil Nadu WRD feed, for the same station and hours.

1. Copy `upper_anicut_manual_readings.template.csv` to
   `upper_anicut_manual_readings.csv` and add rows:
   `2026-10-21T08:00:00+05:30,UPPER_ANICUT,74.91,historical`
   (timezone-aware time, same datum as the telemetry).
2. In `config/river_agent.json` add the file to `cross_check.paths` (to detect
   conflicts) and/or `reverification.independent_paths` (to re-check them).
   Use a source in only one of the two lists, so it is never its own
   tie-breaker.

The template has no data rows, so nothing is invented.
