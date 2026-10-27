# Review report: simulation timestamp fix (2026-10-27)

Project: `flood_project_v6_evidence_layer/proj` (v3 dashboard plus the Evidence Layer commit `a5dd7b8`).
Baseline before changes: 679 Python tests passed, 30 frontend tests passed.
After changes: **706 Python tests passed, 31 frontend tests passed, typecheck clean, production build OK.**

## 1. Bug fixed: simulated runs were judged at the wall clock

**Symptom.** The simulated data describe 2026-10-10, and simulated runs replay them at `as_of` = 2026-10-10T12:00Z. The workflow still passed the real clock to the evidence gate (`now=service.clock()`) and to the state manager. With a real clock, two things went wrong:

- **Spurious denial reason.** Every simulated run (CLI, dashboard simulated mode, risk scenarios) added `EVIDENCE_STALE_AT_DECISION` to every real-world denial, so the dashboard showed fresh simulated data as stale.
- **Scenarios collapsed.** `run_simulated_scenario()` defaulted to `as_of=None`, unlike the CLI and `run_risk_scenario`. Called from code, it graded all four scenarios `STALE` and `SIMULATION_RESTRICTED`, so `no-conflict` and `conflict-resolves` could no longer be told apart from the failing scenarios.

The existing tests did not catch this because they inject a fixed clock close to the simulated time. The bug was already present in the original v3 project; it was not introduced by the Evidence Layer.

**Fix** (`src/river_agent_workflow.py`):

- **New helper.** `decision_time(service, mode, as_of)` returns `as_of` only for **simulated or historical replays**; every other case uses the real clock.
- **Where the decision time is used.** The gate (`now`), risk assessment, evidence layer and state evaluation all use this one decision time.
- **Default replay time.** `run_simulated_scenario()` now defaults `as_of` to `SIMULATED_AS_OF`, as the CLI does.
- **Unchanged.** Audit timestamps and reverification `started_at` still use the real clock, because they record when events actually happened.

**Why the freshness checks are not weakened:**

- **Gate untouched.** `capability_control.py` is unchanged.
- **Live and auto runs.** These always use the real clock. The service also refuses `as_of` in those modes, and a test checks this.
- **Replays cannot authorize.** Simulated and historical evidence is never eligible for real-world action (`EVIDENCE_SIMULATED` / `EVIDENCE_NOT_VERIFIED_LIVE`).
- **Stale data stays stale.** Data that is stale at its own replay time are still denied: the `stale-river` scenario and the historical replay test both still report `EVIDENCE_STALE_AT_DECISION`.
- **Ageing after production.** A new test confirms that live evidence which is fresh when produced but 4 h old at decision time is still denied.

## 2. Test results (all actual runs)

### Three reverification scenarios × four disaster states (CLI, real wall clock)

The `no-conflict` baseline was run as well, so the table has four rows.

| Scenario | NORMAL | ALERT | EMERGENCY | RECOVERY |
|---|---|---|---|---|
| no-conflict | NOT_REQUIRED / ALLOWED | NOT_REQUIRED / ALLOWED | NOT_REQUIRED / ALLOWED | NOT_REQUIRED / ALLOWED |
| conflict-resolves | NOT_AUTHORIZED / RESTRICTED | RESOLVED / ALLOWED | RESOLVED / ALLOWED | RESOLVED / ALLOWED |
| conflict-persists | NOT_AUTHORIZED / RESTRICTED | CONFLICT_PERSISTS / RESTRICTED | CONFLICT_PERSISTS / RESTRICTED | CONFLICT_PERSISTS / RESTRICTED |
| reverification-fails | NOT_AUTHORIZED / RESTRICTED | FAILED / RESTRICTED | FAILED / RESTRICTED | FAILED / RESTRICTED |

Each cell shows reverification outcome / simulation permission. `NORMAL` does not permit reverification, so a conflict stays unresolved there, which is the intended behaviour.

In all 16 combinations, real-world authorization was `RESTRICTED`, and `EVIDENCE_STALE_AT_DECISION` no longer appears.

### Regression tests and frontend

- **New regression file.** `tests/test_simulation_clock.py` has 27 tests. 21 of them fail on the old code and pass with the fix. The other 6 are safety guards that pass either way: the `decision_time` unit test, live freshness unchanged, live ignores `as_of`, historical replay, stale scenario and flood-cycle sequence.
- **Frontend.** One new assertion in `dashboard.test.tsx`: the simulated view carries no "stale at decision" denial, and real-world actions remain denied.

### Dashboard (running API on port 8766 plus the Vite dev server)

- **Simulated mode.** All 8 risk scenarios × 4 start states returned HTTP 200 with no errors. `EVIDENCE_STALE_AT_DECISION` appears only for `stale-river`, which is correct. No real-world capability was allowed.
- **Flood-cycle sequence.** Exactly 4 transitions: h34 NORMAL→ALERT, h48 ALERT→EMERGENCY, h72 EMERGENCY→RECOVERY, h86 RECOVERY→NORMAL.
- **Real mode.** The NWDP fetch succeeded and was verified, but the data are `VERIFIED_SOURCE_STALE`. The last reading is 71.999 m from 2026-08-31, about 1363 h old, and is shown as `LAST_KNOWN_NOT_CURRENT`. Risk is `INDETERMINATE`, the state is `INSUFFICIENT_EVIDENCE`, and real-world authorization is `RESTRICTED`, all of which is correct.
- **Input handling.** A bad mode returns 400 and POST returns 405. The frontend served the page and its proxy returned the fixed document.

## 3. Feature comparison with the original project

The original (`Disaster_flood_system_complete/.../flood_agent_system-`) is exactly commit `cef96d6` (v3) of this repository, and its working tree has no uncommitted changes. A file-by-file diff shows:

- **Nothing missing.** No file or feature of the original is absent from this project.
- **Removed lines are a refactor.** The only removed code in modified files is the single-attempt reverification, which became the bounded retry loop (1–5 attempts, default 2).
- **Added in this project.** The Evidence Layer (`src/evidence_layer.py`, `config/evidence_layer.json`, 53 tests), its dashboard panel, the reverification retry loop, and now the replay-time fix with its tests.

## 4. Changed files

| File | Change |
|---|---|
| `src/river_agent_workflow.py` | `decision_time()` helper; gate, risk, evidence layer and state use it; `run_simulated_scenario` defaults `as_of` |
| `tests/test_simulation_clock.py` | New file: 27 regression tests |
| `frontend/src/__tests__/fixtures/simulated_high_risk.json` | Regenerated from the fixed adapter with the same test clock (it had captured the bug) |
| `frontend/src/__tests__/dashboard.test.tsx` | One regression test |
| `README.md` | Test count 679→706, new table row, one sentence on replay time |
| `outputs/REVIEW_REPORT_2026-10-27.md` | This report |

The original project was not modified. Nothing was committed or pushed.

## 5. Missing features and remaining issues

- **No current real data.** The live NWDP feed is about 57 days old, so real mode is always `RESTRICTED` / `INDETERMINATE`. This is correct behaviour, not a bug.
- **No official thresholds and no independent real second source.** Real-data conflicts cannot be `RESOLVED`, and river-level-based HIGH/CRITICAL is impossible on real data (already documented).
- **Evidence IDs depend on the install location.** The river evidence hashes the absolute CSV path before sanitising, so the same data get a different `evidence_id` in a different folder. This is not a safety issue, but it makes IDs non-portable. It was not fixed, because changing the hashed content would alter every evidence ID.
- **Redundant denial reason.** `EVIDENCE_ASSESSMENT_REJECTED` accompanies every ineligible decision, because the workflow passes its own ineligible assessment to the gate. This is by design ("it can only deny"), but it adds noise in the UI.
- **Other fixtures not regenerated.** The real-mode frontend fixtures were left unchanged; they correctly show stale data.
