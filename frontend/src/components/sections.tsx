// Dashboard sections. Every value shown comes from the backend; nothing here
// computes risk, evidence eligibility, state transitions or permissions.
import type { DashboardDoc, Issue, RainWindow, SequenceDoc } from "../types";
import {
  fmtAge, fmtNum, fmtShort, fmtTime, humanize, isNum, RISK_TONE, STATE_TONE,
} from "../format";
import { Badge, CodeList, Field, Section, SimulatedBanner, Unavailable } from "./common";
import { RainfallChart, RiverChart, StateTimelineChart } from "./charts";

const LEVEL_ROLE_TEXT: Record<string, string> = {
  CURRENT_VERIFIED: "Current, verified live reading",
  SIMULATED: "Simulated reading (fabricated test data)",
  LAST_KNOWN_NOT_CURRENT: "Last known reading — NOT current",
  UNAVAILABLE: "No river reading available",
};

function rainTotal(w?: RainWindow) {
  const p = w?.precipitation;
  return p?.status === "AVAILABLE" && isNum(p.total) ? p.total : null;
}

// ---- KPI cards ------------------------------------------------------------------

export function KpiCards({ doc }: { doc: DashboardDoc }) {
  const { river, weather, risk, state } = doc;
  const level = river?.level;
  const stale = river?.freshness?.status && river.freshness.status !== "FRESH";
  const rain = weather?.rainfall ?? {};
  const temp = weather?.current?.values?.temperature_2m;
  const persisted = state?.persisted;
  const shownState = persisted?.available ? persisted.state : state?.state_used_for_capability_checks;
  return (
    <div className="kpis" role="list">
      <article className="kpi" role="listitem">
        <h3>River level</h3>
        {level?.role === "UNAVAILABLE" || !isNum(level?.value_m) ? (
          <p className="kpi-value muted">Unavailable</p>
        ) : (
          <p className={`kpi-value ${level?.role === "LAST_KNOWN_NOT_CURRENT" ? "is-stale" : ""}`}>
            {/* 3 decimals as reported: rounding 71.999 to "72.00" would look like the
                72.0 m fill value that QC rejects. */}
            {fmtNum(level?.value_m, "m", 3)}
          </p>
        )}
        <p className="kpi-note">
          {LEVEL_ROLE_TEXT[level?.role ?? "UNAVAILABLE"] ?? humanize(level?.role)}
          {level?.observed_at && <> · {fmtShort(level.observed_at)} IST</>}
        </p>
        <div className="kpi-badges">
          {river?.is_simulated && <Badge tone="sim">SIMULATED</Badge>}
          {!river?.is_simulated && river?.source_label && (() => {
            const c = riverDataClass(river.source_label, river.freshness?.status);
            return <Badge tone={c.tone}>{c.text}</Badge>;
          })()}
          {stale && <Badge tone="warn">{river?.freshness?.status} · {fmtAge(river?.freshness?.age_hours)}</Badge>}
        </div>
      </article>

      <article className="kpi" role="listitem">
        <h3>Temperature</h3>
        <p className="kpi-value">{isNum(temp) ? fmtNum(temp, "°C") : <span className="muted">Unavailable</span>}</p>
        <p className="kpi-note">
          {weather?.current?.time ? `Model value for ${fmtShort(weather.current.time)} IST` : weather?.reason ?? "No weather data"}
        </p>
        {weather?.is_simulated && <div className="kpi-badges"><Badge tone="sim">SIMULATED</Badge></div>}
      </article>

      <article className="kpi" role="listitem">
        <h3>Rainfall (completed hours)</h3>
        <div className="kpi-rain">
          {(["3h", "6h", "24h"] as const).map((k) => (
            <div key={k} title={rain[k]?.precipitation?.reason ?? undefined}>
              <span className="kpi-rain-label">{k}</span>
              <span className="kpi-rain-value">{rainTotal(rain[k]) === null ? "—" : fmtNum(rainTotal(rain[k]), "mm")}</span>
            </div>
          ))}
        </div>
        <p className="kpi-note">Model precipitation (Open-Meteo), not gauge totals</p>
      </article>

      <article className="kpi" role="listitem">
        <h3>Risk</h3>
        <p className="kpi-value"><Badge tone={RISK_TONE[risk?.risk_level ?? "INDETERMINATE"] ?? "unknown"}>{risk?.risk_level ?? "UNKNOWN"}</Badge></p>
        <p className="kpi-note">Evidence confidence: <strong>{risk?.evidence_quality?.confidence ?? "—"}</strong></p>
      </article>

      <article className="kpi" role="listitem">
        <h3>Disaster state</h3>
        <p className="kpi-value"><Badge tone={STATE_TONE[shownState ?? ""] ?? "unknown"}>{shownState ?? "UNKNOWN"}</Badge></p>
        <p className="kpi-note">
          {persisted?.available ? "Persisted state" :
            state?.state_basis === "SCENARIO_START_STATE" ? "Scenario start state (simulated)" :
            state?.state_basis === "SIMULATION_SESSION_RECORD" ? "Simulation session state (in memory, never saved)" :
              "No persisted record — NORMAL assumed for checks"}
          {state?.evaluation?.proposed_state && state.evaluation.proposed_state !== state.evaluation.current_state &&
            <> · proposed next: <strong>{state.evaluation.proposed_state}</strong></>}
        </p>
      </article>
    </div>
  );
}

// ---- Weather ---------------------------------------------------------------------

const CURRENT_LABELS: [string, string][] = [
  ["temperature_2m", "Temperature"], ["relative_humidity_2m", "Relative humidity"],
  ["precipitation", "Precipitation (15 min)"], ["rain", "Rain (15 min)"],
  ["wind_speed_10m", "Wind speed"], ["wind_direction_10m", "Wind direction"],
  ["wind_gusts_10m", "Wind gusts"],
];

export function WeatherSection({ doc }: { doc: DashboardDoc }) {
  const w = doc.weather;
  const unavailable = !w || w.status === "NOT_PROVIDED" || w.status === "DATA_UNAVAILABLE";
  return (
    <Section id="weather" title="Weather"
      subtitle={w?.source ? `${w.source} · ${humanize(w.source_type)}` : "No weather source"}
      badges={<>
        {w?.is_simulated && <Badge tone="sim">SIMULATED</Badge>}
        <Badge tone={unavailable ? "danger" : w?.status === "OK" ? "ok" : "warn"}>{w?.status ?? "UNKNOWN"}</Badge>
      </>}>
      {unavailable ? (
        <Unavailable>
          Weather evidence is unavailable{w?.reason ? `: ${w.reason}` : "."}
          {w?.fetch_error && <> Fetch error: <code>{w.fetch_error}</code></>}
        </Unavailable>
      ) : (
        <>
          <p className="note">
            All values are <strong>numerical model output</strong> for a grid cell
            {isNum(w?.grid_location?.latitude) && ` (${w?.grid_location?.latitude?.toFixed(3)}, ${w?.grid_location?.longitude?.toFixed(3)})`},
            not rain-gauge observations. Retrieved {fmtTime(w?.retrieved_at)}. Times in {w?.timezone?.returned ?? "Asia/Kolkata"}.
          </p>
          <div className="grid-2">
            <div>
              <h3>Current conditions <span className="muted">({fmtShort(w?.current?.time)} IST, model)</span></h3>
              <dl className="fields">
                {CURRENT_LABELS.map(([key, label]) => (
                  <Field key={key} label={label}>
                    {isNum(w?.current?.values?.[key])
                      ? fmtNum(w?.current?.values?.[key], w?.units?.current?.[key] ?? "", key.includes("direction") ? 0 : 1)
                      : <span className="muted">missing{w?.current?.missing?.[key] ? ` (${w.current.missing[key]})` : ""}</span>}
                  </Field>
                ))}
              </dl>
            </div>
            <div>
              <h3>Rainfall over completed hours</h3>
              <table className="table">
                <thead><tr><th>Window</th><th>Period (IST)</th><th>Precipitation</th><th>Rain</th></tr></thead>
                <tbody>
                  {(["3h", "6h", "24h"] as const).map((k) => {
                    const r = w?.rainfall?.[k];
                    return (
                      <tr key={k}>
                        <td>{k}</td>
                        <td>{r?.period_start ? `${fmtShort(r.period_start)} – ${fmtShort(r.period_end)}` : "—"}</td>
                        <td>{rainTotal(r) === null ? <span className="muted" title={r?.precipitation?.reason ?? ""}>unavailable</span> : fmtNum(rainTotal(r), "mm")}</td>
                        <td>{r?.rain?.status === "AVAILABLE" && isNum(r.rain.total) ? fmtNum(r.rain.total, "mm") : <span className="muted">unavailable</span>}</td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
              <p className="muted small">"Precipitation" includes showers; "rain" excludes them (Open-Meteo definitions).</p>
            </div>
          </div>
          <h3>Hourly precipitation</h3>
          {w?.hourly?.length ? <RainfallChart points={w.hourly} /> : <Unavailable>No hourly series.</Unavailable>}
          {!!w?.errors?.length && (
            <div className="issues"><h4>Weather data warnings</h4>
              <ul>{w.errors.map((e, i) => <li key={i}><Badge tone={e.severity === "error" ? "danger" : "warn"}>{e.code}</Badge> {e.message}</li>)}</ul>
            </div>
          )}
        </>
      )}
    </Section>
  );
}

// ---- River -------------------------------------------------------------------------

// Simulated-live mode: the card shows only the simulation session's river evidence.
// A river block that is not the session's simulated station is never shown here.
function LiveRiverSection({ doc }: { doc: DashboardDoc }) {
  const r = doc.river;
  const sim = doc.simulation;
  const series = r?.series;
  const station = sim?.station_id;
  const state = doc.state?.evaluation?.resulting_state ?? sim?.current_state;
  const risk = doc.risk?.risk_level;
  const own = r?.is_simulated === true && !!station && r?.station_id === station && r?.level?.role === "SIMULATED";
  return (
    <Section id="river" title="River monitoring"
      subtitle={`${own ? station : "No simulated station"} · Real-Time River Simulation`}
      badges={<>
        <Badge tone="sim">SIMULATED LIVE — NOT CWC DATA</Badge>
        {own && <Badge tone={r?.freshness?.status === "FRESH" ? "ok" : "warn"}>Freshness: {r?.freshness?.status ?? "UNKNOWN"}</Badge>}
      </>}>
      <SimulatedBanner label="SIMULATED LIVE — NOT CWC DATA"
        text={`These readings are fabricated test data generated by this system${station ? ` for station ${station}` : ""}. They are not actual Cauvery river measurements and not Central Water Commission observations.`} />
      {!own ? (
        <Unavailable>This response does not contain the simulation's own river evidence, so no reading is shown. Real CWC readings are never shown in simulation mode.</Unavailable>
      ) : (
        <>
          <div className="grid-2">
            <dl className="fields">
              <Field label="Station">{station}</Field>
              <Field label="Latest simulated reading">
                {isNum(r?.level?.value_m) ? <strong>{fmtNum(r?.level?.value_m, "m", 3)}</strong> : <span className="muted">unavailable</span>}
                {" "}<span className="muted">({LEVEL_ROLE_TEXT.SIMULATED})</span>
              </Field>
              <Field label="Reading timestamp">{fmtTime(r?.level?.observed_at)}</Field>
              <Field label="Evidence age">{r?.freshness?.status ?? "UNKNOWN"} · {fmtAge(r?.freshness?.age_hours)} (limit {fmtNum(r?.freshness?.max_age_hours, "h", 0)})</Field>
            </dl>
            <dl className="fields">
              <Field label="Trend">{r?.trend?.status === "AVAILABLE" ? `${humanize(r.trend.direction)} ${fmtNum(r.trend.rate_of_change_m_per_hour, "m/h", 3)} over ${fmtNum(r.trend.window_hours, "h", 0)}` : <span className="muted">unavailable{r?.trend?.reason ? ` (${r.trend.reason})` : ""}</span>}</Field>
              <Field label="Data quality">{r?.recent_quality?.status ?? "—"}{isNum(r?.recent_quality?.rejected_fraction) ? ` · ${(100 * (r?.recent_quality?.rejected_fraction ?? 0)).toFixed(0)}% rejected` : ""}</Field>
              <Field label="Risk"><Badge tone={RISK_TONE[risk ?? "INDETERMINATE"] ?? "unknown"}>{risk ?? "UNKNOWN"}</Badge> <span className="muted">(simulation only)</span></Field>
              <Field label="Disaster state"><Badge tone={STATE_TONE[state ?? ""] ?? "unknown"}>{state ?? "UNKNOWN"}</Badge> <span className="muted">(simulated scope, never saved)</span></Field>
            </dl>
          </div>
          <h3>Simulated river level <span className="muted">(SIMULATED LIVE{series?.window_start ? ` · ${fmtShort(series.window_start)} – ${fmtShort(series.window_end)} IST` : ""})</span></h3>
          {series?.status === "AVAILABLE" && series.points?.length
            ? <RiverChart points={series.points} rejected={series.rejected ?? []}
                          warning={r?.thresholds?.warning_level_m} danger={r?.thresholds?.danger_level_m}
                          official={r?.thresholds?.threshold_is_official} />
            : <Unavailable>No simulated river-level series: {series?.reason ?? "unavailable"}</Unavailable>}
        </>
      )}
    </Section>
  );
}

/** What kind of river reading the evidence holds, from the backend's source label and
 *  freshness: a live-feed reading is an actual current observation only while FRESH. */
export function riverDataClass(label?: string | null, freshness?: string | null): { text: string; tone: string } {
  const live = label === "LIVE_VERIFIED" || label === "LIVE_UNVERIFIED";
  if (live && freshness !== "FRESH") return { text: "STALE LIVE FEED", tone: "warn" };
  if (label === "LIVE_VERIFIED") return { text: "ACTUAL CWC OBSERVATION", tone: "ok" };
  if (label === "LIVE_UNVERIFIED") return { text: "ACTUAL CWC OBSERVATION (UNVERIFIED)", tone: "warn" };
  if (label === "HISTORICAL_CSV" || label === "HISTORICAL_FALLBACK") return { text: "HISTORICAL CWC DATA", tone: "neutral" };
  if (label === "SIMULATED_TEST_DATA") return { text: "SIMULATED DATA", tone: "sim" };
  return { text: "NO CWC OBSERVATION", tone: "danger" };
}

/** Where a river reading came from, in words, from the backend's source label. */
function riverSourceName(label?: string | null): string {
  if (label === "LIVE_VERIFIED" || label === "LIVE_UNVERIFIED") return "NWDP live feed";
  if (label === "HISTORICAL_CSV" || label === "HISTORICAL_FALLBACK") return "historical CWC file";
  return "river source";
}

// Issue codes that describe a row the River Agent rejected: such rows are left out of
// every calculation (trend, freshness, quality, risk), never corrected or set to zero.
const REJECTED_ROW_CODES = new Set([
  "PLACEHOLDER_VALUE", "OUT_OF_RANGE", "SPIKE", "STUCK_VALUE", "NEGATIVE_LEVEL",
  "NON_NUMERIC_LEVEL", "MISSING_VALUE", "INVALID_TIMESTAMP", "INVALID_STATION_ID",
  "INVALID_SOURCE_TYPE", "MALFORMED_ROW", "CONFLICTING_DUPLICATE",
]);
const ROW_TIME = /\((\d{4}-\d{2}-\d{2}T[^)]+)\)/;

/** Issues grouped by code: one line per code with a count and time span, not one per row. */
export function IssueSummary({ title, issues, tone }: { title: string; issues?: Issue[]; tone: string }) {
  const groups = new Map<string, Issue[]>();
  for (const issue of issues ?? []) {
    const code = issue.code ?? "UNKNOWN";
    groups.set(code, [...(groups.get(code) ?? []), issue]);
  }
  if (!groups.size) return null;
  return (
    <div className="issues"><h4>{title}</h4>
      <ul>
        {[...groups.entries()].map(([code, list]) => {
          const times = list.map((i) => ROW_TIME.exec(i.message ?? "")?.[1]).filter((t): t is string => !!t).sort();
          return (
            <li key={code}>
              <Badge tone={tone}>{code}</Badge>{list.length > 1 && <strong> × {list.length}</strong>}{" "}
              {list.length === 1 ? list[0].message
                : <>{times.length ? `${fmtShort(times[0])} – ${fmtShort(times[times.length - 1])} IST. ` : ""}e.g. {list[0].message}</>}
              {REJECTED_ROW_CODES.has(code) && <span className="muted"> These readings are excluded from all calculations.</span>}
            </li>
          );
        })}
      </ul>
    </div>
  );
}

export function RiverSection({ doc }: { doc: DashboardDoc }) {
  if (doc.mode === "simulated_live") return <LiveRiverSection doc={doc} />;
  const r = doc.river;
  const fresh = r?.freshness?.status;
  const dataClass = riverDataClass(r?.source_label, fresh);
  const risk = doc.risk?.risk_level;
  const series = r?.series;
  const conflicts = r?.cross_source_check?.conflicts_affecting_current_evidence ?? 0;
  return (
    <Section id="river" title="River monitoring"
      subtitle={`${r?.station_id ?? "Unknown station"} · ${humanize(r?.source_label)}`}
      badges={<>
        {r?.is_simulated && <Badge tone="sim">SIMULATED</Badge>}
        {!r?.is_simulated && <Badge tone={dataClass.tone}>{dataClass.text}</Badge>}
        <Badge tone={fresh === "FRESH" ? "ok" : "warn"}>Freshness: {fresh ?? "UNKNOWN"}</Badge>
        <Badge tone={r?.is_verified_live ? "ok" : "neutral"}>{r?.evidence_grade ?? "No grade"}</Badge>
      </>}>
      {r?.is_simulated && <SimulatedBanner text={`These river readings are fabricated test values for station ${r.station_id ?? "UPPER_ANICUT_TEST"}. They are not measurements of the Cauvery.`} />}
      {!r?.is_simulated && fresh && fresh !== "FRESH" && isNum(r?.level?.value_m) && (
        <div className="alert" role="alert">
          <strong>{fresh}: NOT CURRENT CONDITIONS.</strong> The latest valid reading in the {riverSourceName(r?.source_label)} was observed {fmtTime(r?.level?.observed_at)} and is {fmtAge(r?.freshness?.age_hours)}. It is shown for reference only; no current reading is available and none is substituted.
        </div>
      )}
      <div className="grid-2">
        <dl className="fields">
          <Field label="Latest reading">
            {isNum(r?.level?.value_m) ? <strong className={r?.level?.role === "LAST_KNOWN_NOT_CURRENT" ? "is-stale" : ""}>{fmtNum(r?.level?.value_m, "m", 3)}</strong> : <span className="muted">unavailable</span>}
            {" "}<span className="muted">({LEVEL_ROLE_TEXT[r?.level?.role ?? "UNAVAILABLE"]})</span>
          </Field>
          <Field label="Measured at">{fmtTime(r?.level?.observed_at)}</Field>
          <Field label="Evidence age">{fmtAge(r?.freshness?.age_hours)} (limit {fmtNum(r?.freshness?.max_age_hours, "h", 0)})</Field>
          <Field label="Datum">{r?.level?.datum ? humanize(r.level.datum) : "—"}</Field>
          <Field label="Source">{r?.source ?? "—"}</Field>
          <Field label="Risk assessment"><Badge tone={RISK_TONE[risk ?? "INDETERMINATE"] ?? "unknown"}>{risk ?? "UNKNOWN"}</Badge> <span className="muted">confidence {doc.risk?.evidence_quality?.confidence ?? "—"} (deterministic rules)</span></Field>
          <Field label="Live fetch">{r?.live_fetch?.attempted ? `${r.live_fetch.status ?? "—"} · verification ${r.live_fetch.verification_status ?? "—"}${r.live_fetch.error ? ` · ${r.live_fetch.error}` : ""}` : "Not attempted"}</Field>
          <Field label="Source verification">{r?.source_verification?.status ?? "—"}{r?.source_verification?.failed_checks?.length ? ` (failed: ${r.source_verification.failed_checks.join(", ")})` : ""}</Field>
        </dl>
        <dl className="fields">
          <Field label="Trend">{r?.trend?.status === "AVAILABLE" ? `${humanize(r.trend.direction)} ${fmtNum(r.trend.rate_of_change_m_per_hour, "m/h", 3)} over ${fmtNum(r.trend.window_hours, "h", 0)}` : <span className="muted">unavailable{r?.trend?.reason ? ` (${r.trend.reason})` : ""}</span>}</Field>
          <Field label="Recent data quality">{r?.recent_quality?.status ?? "—"}{isNum(r?.recent_quality?.rejected_fraction) ? ` · ${(100 * (r?.recent_quality?.rejected_fraction ?? 0)).toFixed(0)}% rejected` : ""}</Field>
          <Field label="QC (this window)">{isNum(r?.qc?.checked as number) ? `${r?.qc?.accepted} of ${r?.qc?.checked} accepted · ${r?.qc?.rejected_placeholder} fill · ${r?.qc?.rejected_out_of_range} out of range · ${r?.qc?.rejected_spike} spikes` : "—"}</Field>
          <Field label="Thresholds">
            {r?.thresholds?.status && r.thresholds.status !== "UNKNOWN"
              ? <>{humanize(r.thresholds.status)} · warning {fmtNum(r.thresholds.warning_level_m, "m", 2)}, danger {fmtNum(r.thresholds.danger_level_m, "m", 2)} <Badge tone={r.thresholds.threshold_is_official ? "ok" : "warn"}>{r.thresholds.threshold_is_official ? "official" : "TEST / prototype — not official"}</Badge></>
              : <span className="muted">No warning/danger levels configured; none are assumed.</span>}
          </Field>
          <Field label="Cross-source check">
            {r?.cross_source_check?.status ?? "—"}
            {conflicts > 0 && <> · <Badge tone="danger">{conflicts} conflicting current reading(s)</Badge></>}
          </Field>
          <Field label="Reverification">{r?.reverification?.outcome ?? "—"}{r?.reverification?.reason ? ` · ${r.reverification.reason}` : ""}</Field>
          <Field label="Historical fallback">{r?.fallback?.used ? <Badge tone="warn">USED: {r.fallback.reason}</Badge> : "Not used"}</Field>
        </dl>
      </div>
      {!!r?.reasons_not_usable?.length && (
        <div className="issues"><h4>Why this evidence is not usable as current</h4>
          <ul>{r.reasons_not_usable.map((x, i) => <li key={i}>{x}</li>)}</ul></div>
      )}
      <h3>Recent river level {series?.window_start && <span className="muted">({fmtShort(series.window_start)} – {fmtShort(series.window_end)} IST)</span>}</h3>
      {fresh && fresh !== "FRESH" && series?.points?.length ? <p className="note warn-note">These readings are {fmtAge(r?.freshness?.age_hours)}: they show past conditions, not the river now.</p> : null}
      {series?.status === "AVAILABLE" && series.points?.length
        ? <RiverChart points={series.points} rejected={series.rejected ?? []}
                      warning={r?.thresholds?.warning_level_m} danger={r?.thresholds?.danger_level_m}
                      official={r?.thresholds?.threshold_is_official} />
        : <Unavailable>No river-level series: {series?.reason ?? "unavailable"}</Unavailable>}
      {!!r?.warnings?.length && (
        <IssueSummary title="Evidence warnings" issues={r.warnings} tone="warn" />
      )}
      {!!r?.errors?.length && (
        <IssueSummary title="Evidence errors" issues={r.errors} tone="danger" />
      )}
    </Section>
  );
}

// ---- Risk -------------------------------------------------------------------------

export function RiskSection({ doc }: { doc: DashboardDoc }) {
  const k = doc.risk;
  if (!k) return <Section id="risk" title="Risk assessment"><Unavailable>No risk assessment.</Unavailable></Section>;
  const q = k.evidence_quality;
  return (
    <Section id="risk" title="Risk assessment"
      subtitle={`Deterministic rule-based assessment · mode ${k.assessment_mode ?? "—"} · reference ${fmtTime(k.reference_time)}`}
      badges={<>{k.is_simulated && <Badge tone="sim">SIMULATED</Badge>}</>}>
      <div className="risk-head">
        <div>
          <span className="eyebrow">Risk level</span>
          <Badge tone={RISK_TONE[k.risk_level ?? ""] ?? "unknown"}>{k.risk_level ?? "UNKNOWN"}</Badge>
          {k.risk_level_meaning && <p className="small">{k.risk_level_meaning}</p>}
        </div>
        <div>
          <span className="eyebrow">Evidence confidence</span>
          <Badge tone={q?.confidence === "NONE" ? "unknown" : "neutral"}>{q?.confidence ?? "—"}</Badge>
          <p className="small">Applies to: {humanize(q?.confidence_applies_to)}</p>
        </div>
        <div>
          <span className="eyebrow">Hazard indicated</span>
          <Badge tone={RISK_TONE[k.hazard_indicated ?? ""] ?? "neutral"}>{k.hazard_indicated ?? "none"}</Badge>
          <p className="small">Highest hazard shown by usable evidence</p>
        </div>
        <div>
          <span className="eyebrow">Human review</span>
          <Badge tone={k.requires_human_review ? "warn" : "ok"}>{k.requires_human_review ? "REQUIRED" : "not required"}</Badge>
          <CodeList items={k.human_review_reasons} empty="" />
        </div>
      </div>
      <h3>Rules that produced this result</h3>
      {k.risk_factors?.length ? (
        <table className="table">
          <thead><tr><th>Rule</th><th>Hazard</th><th>Source</th><th>Reason</th></tr></thead>
          <tbody>{k.risk_factors.map((f, i) => (
            <tr key={i}>
              <td><code>{f.rule_id}</code></td>
              <td>{f.hazard_level ? <Badge tone={RISK_TONE[f.hazard_level] ?? "neutral"}>{f.hazard_level}</Badge> : <span className="muted">info</span>}</td>
              <td>{f.source}</td><td>{f.description}</td>
            </tr>))}</tbody>
        </table>
      ) : <Unavailable>No rule fired.</Unavailable>}
      <div className="grid-2">
        <div>
          <h3>Evidence problems</h3>
          <h4>River evidence blocking an assessment</h4>
          <CodeList items={q?.river_blocking_reasons} tone="danger" />
          <h4>Weather not used because</h4>
          <CodeList items={q?.weather_not_used_reasons} tone="warn" />
          <h4>Confidence reduced by</h4>
          <CodeList items={q?.confidence_downgrades} tone="neutral" />
        </div>
        <div>
          <h3>Missing data</h3>
          <ul className="plain">{(k.missing_data ?? []).map((m, i) => <li key={i}><strong>{humanize(m.item)}:</strong> {m.reason}</li>)}</ul>
        </div>
      </div>
      <h3>Uncertainties</h3>
      <ul className="plain">{(k.uncertainties ?? []).map((u, i) => <li key={i}><Badge tone="neutral">{humanize(u.code)}</Badge> {u.description}</li>)}</ul>
      <h3>Recommended monitoring (information only)</h3>
      <ul className="plain">{(k.recommended_monitoring_actions ?? []).map((a, i) => <li key={i}><strong>{humanize(a.code)}</strong> — {a.description}</li>)}</ul>
      <p className="disclaimer">
        {k.disclaimer} Thresholds and breakpoints are prototype configuration, not official Cauvery warning levels.
        Calibrated flood probability: {k.calibrated_probability === null || k.calibrated_probability === undefined ? "not produced (no validated model)" : k.calibrated_probability}.
      </p>
    </Section>
  );
}

// ---- Disaster state -------------------------------------------------------------------

export function StateSection({ doc }: { doc: DashboardDoc }) {
  const s = doc.state;
  const e = s?.evaluation;
  const p = s?.persisted;
  const accepted = e?.outcome === "TRANSITION_ACCEPTED";
  const history = [...(s?.history ?? [])].reverse();
  return (
    <Section id="state" title="Disaster state"
      subtitle="Classification only — it never authorizes or executes an action."
      badges={<>{e?.is_simulated && <Badge tone="sim">SIMULATED</Badge>}</>}>
      <div className="grid-2">
        <div className="card">
          <span className="eyebrow">Persisted state (operative)</span>
          {p?.available
            ? <><Badge tone={STATE_TONE[p.state ?? ""] ?? "unknown"}>{p.state}</Badge>
                <dl className="fields compact">
                  <Field label="Scope">{p.scope}</Field>
                  <Field label="Entered">{fmtTime(p.entered_at)}</Field>
                  <Field label="Last transition">{p.last_transition ? `${p.last_transition.from} → ${p.last_transition.to} (${p.last_transition.kind}) at ${fmtShort(p.last_transition.at)}` : "—"}</Field>
                  <Field label="Record">{p.record_file ?? "—"}</Field>
                </dl></>
            : <Unavailable>
                {s?.state_basis === "SCENARIO_START_STATE"
                  ? <>Simulated scenario: no persisted record. It starts from <strong>{s?.state_used_for_capability_checks}</strong>.</>
                  : s?.state_basis === "SIMULATION_SESSION_RECORD"
                  ? <>Real-time simulation: the session carries its own simulated-scope record in memory. It is never written to the real state record. This tick was checked in <strong>{s?.state_used_for_capability_checks}</strong>.</>
                  : <>No valid persisted real-state record{p?.problem ? <> ({p.problem})</> : ""}. Capability checks use <strong>NORMAL</strong>, the most restrictive state.</>}
              </Unavailable>}
          <p className="small">State used for this run's capability checks: <strong>{s?.state_used_for_capability_checks ?? "—"}</strong> ({humanize(s?.state_basis)})</p>
        </div>
        <div className={`card ${accepted ? "card-accent" : ""}`}>
          <span className="eyebrow">This evaluation (proposed, not saved)</span>
          <p className="transition">
            <Badge tone={STATE_TONE[e?.current_state ?? ""] ?? "unknown"}>{e?.current_state ?? "—"}</Badge>
            <span aria-hidden="true">→</span>
            <Badge tone={STATE_TONE[e?.resulting_state ?? ""] ?? "unknown"}>{e?.resulting_state ?? "—"}</Badge>
            <Badge tone={accepted ? "ok" : e?.outcome === "INSUFFICIENT_EVIDENCE" || e?.outcome === "EVALUATION_FAILED" ? "danger" : "neutral"}>{humanize(e?.outcome)}</Badge>
          </p>
          <dl className="fields compact">
            <Field label="Condition">{e?.condition ?? "—"}</Field>
            <Field label="Proposed next">{e?.proposed_state ?? "none"}</Field>
            <Field label="Why">{e?.reason ?? "—"}</Field>
            {e?.persistence && <Field label="Persistence">{e.persistence.observations ?? 0} of {e.persistence.required_observations} observation(s), {fmtNum(e.persistence.span_hours, "h", 1)} of {fmtNum(e.persistence.required_hours, "h", 1)}{isNum(e.persistence.run_broken_by_gap_hours) ? ` · run broken by a ${e.persistence.run_broken_by_gap_hours} h gap` : ""}</Field>}
            <Field label="Observed / evaluated">{fmtShort(e?.observed_at)} / {fmtShort(e?.evaluated_at)} IST</Field>
            <Field label="Applies to">{humanize(e?.state_applies_to)}</Field>
          </dl>
          <CodeList items={e?.reasons} tone={accepted ? "neutral" : "warn"} empty="" />
        </div>
      </div>
      <p className="note">{s?.note}</p>
      <h3>Evaluation history {history.length ? `(${history.length})` : ""}</h3>
      {history.length ? (
        <table className="table">
          <thead><tr><th>Observed (IST)</th><th>Condition</th><th>Risk</th><th>Evaluated</th><th>Reasons</th></tr></thead>
          <tbody>{history.slice(0, 20).map((h, i) => (
            <tr key={i}><td>{fmtShort(h.observed_at)}</td><td>{h.condition}</td><td>{h.risk_level ?? "—"}</td>
              <td>{fmtShort(h.evaluated_at)}</td><td className="small">{(h.reasons ?? []).map(humanize).join(", ")}</td></tr>))}</tbody>
        </table>
      ) : <Unavailable>No evaluation history in the record.</Unavailable>}
    </Section>
  );
}

// ---- Safety & audit ----------------------------------------------------------------------

export function SafetySection({ doc }: { doc: DashboardDoc }) {
  const f = doc.safety;
  const restricted = f?.real_world_authorization !== "HUMAN_AUTHORIZATION_REQUIRED";
  return (
    <Section id="safety" title="Safety and authorization"
      subtitle="Read-only view of the backend's capability decisions."
      badges={<Badge tone={restricted ? "danger" : "warn"}>{f?.real_world_authorization ?? "UNKNOWN"}</Badge>}>
      <p className="note strong-note">
        This dashboard has no controls that run evacuations, dispatch, public alerts or any other
        real-world action. At most, the backend marks a capability as <em>pending human approval</em>.
      </p>
      <table className="table">
        <thead><tr><th>Capability</th><th>Class</th><th>Decision</th><th>Reason</th><th>Evidence reasons</th><th>Human approval</th></tr></thead>
        <tbody>{(f?.decisions ?? []).map((d, i) => (
          <tr key={i}>
            <td><code>{d.capability}</code></td><td>{humanize(d.capability_class)}</td>
            <td><Badge tone={d.allowed ? (d.capability_class === "REAL_WORLD" ? "warn" : "ok") : "danger"}>{d.allowed ? (d.capability_class === "REAL_WORLD" ? "PENDING HUMAN" : "PERMITTED") : "DENIED"}</Badge></td>
            <td>{humanize(d.reason)}</td>
            <td><CodeList items={d.evidence_reasons} tone="danger" empty="—" /></td>
            <td>{d.capability_class === "REAL_WORLD" ? (d.human_approval_required ? "Required" : "Not reached (denied)") : "Not applicable"}</td>
          </tr>))}</tbody>
      </table>
      <h3>Evidence eligibility for real-world capabilities</h3>
      <p><Badge tone={f?.evidence_assessment?.eligible ? "ok" : "danger"}>{f?.evidence_assessment?.eligible ? "ELIGIBLE" : "NOT ELIGIBLE"}</Badge></p>
      <CodeList items={f?.evidence_assessment?.reasons} tone="danger" empty="No blocking reasons" />
      {!!f?.evidence_assessment?.input_problems?.length && <CodeList items={f.evidence_assessment.input_problems} tone="danger" />}
      <p className="small">{f?.reason}</p>
    </Section>
  );
}

// ---- Evidence layer --------------------------------------------------------------------------

export const EVIDENCE_CLASS_TONE: Record<string, string> = {
  LIVE_VERIFIED: "ok", LIVE_STALE: "warn", LIVE_UNVERIFIED: "danger",
  HISTORICAL_NOT_CURRENT: "caution", SIMULATED: "sim", MODEL_ESTIMATE: "info",
  MODEL_STALE: "warn", UNAVAILABLE: "unknown", RECHECK: "neutral",
};
export const CLAIM_TONE: Record<string, string> = {
  ACCEPTED: "ok", REJECTED: "danger", INSUFFICIENT: "unknown",
  UNRESOLVED_CONFLICT: "danger", SIMULATED_ONLY: "sim",
};
const shortId = (id: string) => id.slice(0, 11);

export function EvidenceSection({ doc }: { doc: DashboardDoc }) {
  const e = doc.evidence_layer;
  if (!e || !e.available) {
    return (
      <Section id="evidence" title="Evidence and claims" subtitle="Evidence layer">
        <Unavailable>{e?.reason ?? "No evidence layer in this response."}</Unavailable>
      </Section>
    );
  }
  const records = e.records ?? [];
  const byId = new Map(records.map((r) => [r.evidence_id, r]));
  const ref = (id: string) => {
    const r = byId.get(id);
    return <Badge key={id} tone={EVIDENCE_CLASS_TONE[r?.evidence_class ?? ""] ?? "neutral"}
      title={id}>{shortId(id)}{r ? ` · ${humanize(r.kind)}` : ""}</Badge>;
  };
  const counts = e.summary?.records_by_class ?? {};
  return (
    <Section id="evidence" title="Evidence and claims"
      subtitle={`Every safety-critical claim linked to its evidence · reference time ${fmtTime(e.reference_time)}`}
      badges={<>
        <Badge tone={e.real_world_evidence_sufficient ? "ok" : "danger"}>
          {e.real_world_evidence_sufficient ? "SUFFICIENT FOR HUMAN REVIEW" : "INSUFFICIENT FOR REAL-WORLD ACTION"}
        </Badge>
        <Badge tone={e.audit_complete ? "ok" : "danger"}>{e.audit_complete ? "AUDITED" : "AUDIT INCOMPLETE"}</Badge>
      </>}>
      {e.error && <Unavailable>Evidence layer failed: {e.error}. Real-world capabilities are denied.</Unavailable>}
      <ul className="chips" aria-label="Evidence by class">
        {Object.entries(counts).map(([k, n]) => (
          <li key={k}><Badge tone={EVIDENCE_CLASS_TONE[k] ?? "neutral"}
            title={e.class_descriptions?.[k]}>{humanize(k)} × {n}</Badge></li>))}
      </ul>
      {!!e.deny_reasons?.length && <p>Deny reasons: <CodeList items={e.deny_reasons} tone="danger" /></p>}

      <h3>Claims</h3>
      <table className="table">
        <thead><tr><th>Claim</th><th>Status</th><th>Supporting</th><th>Contradicting</th><th>Why</th></tr></thead>
        <tbody>{(e.claims ?? []).map((c) => (
          <tr key={c.claim_id}>
            <td><code>{c.claim_id}</code><div className="small">{c.statement}</div></td>
            <td><Badge tone={CLAIM_TONE[c.status] ?? "neutral"}>{humanize(c.status)}</Badge></td>
            <td>{c.supporting_evidence_ids?.length ? c.supporting_evidence_ids.map(ref) : <span className="muted">none</span>}</td>
            <td>{c.contradicting_evidence_ids?.length ? c.contradicting_evidence_ids.map(ref) : <span className="muted">none</span>}</td>
            <td className="small">{(c.explanation ?? []).join(" ")}</td>
          </tr>))}</tbody>
      </table>

      <h3>Evidence records</h3>
      <table className="table">
        <thead><tr><th>Evidence</th><th>Class</th><th>Source</th><th>Observed</th><th>Retrieved</th><th>Freshness</th><th>Validation</th></tr></thead>
        <tbody>{records.map((r) => (
          <tr key={r.evidence_id}>
            <td><code title={r.evidence_id}>{shortId(r.evidence_id)}</code><div className="small">{humanize(r.kind)}</div>
              {r.measurement_basis && <div className="small muted">{r.measurement_basis}</div>}</td>
            <td><Badge tone={EVIDENCE_CLASS_TONE[r.evidence_class] ?? "neutral"}
              title={e.class_descriptions?.[r.evidence_class]}>{humanize(r.evidence_class)}</Badge></td>
            <td className="small">{r.source?.name ?? r.source?.label ?? "—"}
              {r.source?.independence && <div className="muted">{humanize(r.source.independence)}</div>}</td>
            <td>{fmtShort(r.observed_at)}</td>
            <td>{fmtShort(r.retrieved_at)}</td>
            <td>{r.freshness?.status ? humanize(r.freshness.status) : "—"}
              {isNum(r.freshness?.age_hours) && <div className="small muted">{fmtAge(r.freshness?.age_hours)}</div>}</td>
            <td><Badge tone={r.validation?.status === "VALID" ? "ok" : r.validation?.status === "DEGRADED" ? "caution" : "danger"}>
              {r.validation?.status ?? "UNKNOWN"}</Badge>
              {(r.validation?.findings ?? []).filter((f) => f.severity !== "info").map((f, i) => (
                <div key={i} className="small" title={f.code}><strong>{humanize(f.code)}:</strong> {f.detail}</div>))}
            </td>
          </tr>))}</tbody>
      </table>

      {!!e.reverification_attempts?.length && (<>
        <h3>Reverification attempts</h3>
        <ol className="audit">{e.reverification_attempts.map((a, i) => (
          <li key={i}><time>{fmtShort(a.started_at)}</time> <code>attempt {a.attempt}</code>{" "}
            <Badge tone={a.outcome === "RESOLVED" ? "ok" : "danger"}>{humanize(a.outcome)}</Badge>
            <span className="small muted"> conflicts remaining: {a.conflicts_remaining ?? "unknown"}{a.error ? ` · ${a.error}` : ""}</span>
          </li>))}</ol>
      </>)}
      {e.disclaimer && <p className="small note">{e.disclaimer}</p>}
    </Section>
  );
}

const AUDIT_KEYS = ["capability", "allowed", "reason", "outcome", "risk_level", "resulting_state",
  "real_world_authorization", "mode", "disaster_state", "evidence_class", "status", "claim_id",
  "attempt", "deny_reasons", "real_world_evidence_sufficient", "error"];

export function AuditSection({ doc }: { doc: DashboardDoc }) {
  const a = doc.audit;
  return (
    <Section id="audit" title="Audit trail (this run)"
      subtitle={`Append-only log ${a?.log_file ?? ""} · details redacted by the backend`}
      badges={<Badge tone={a?.write_errors?.length ? "danger" : "ok"}>{a?.write_errors?.length ? "AUDIT INCOMPLETE" : "COMPLETE"}</Badge>}>
      {a?.write_errors?.length ? <Unavailable>Audit write errors: {a.write_errors.join("; ")}. Permissions and state changes were revoked.</Unavailable> : null}
      {a?.events?.length ? (
        <ol className="audit">
          {a.events.map((e, i) => (
            <li key={i}>
              <time>{fmtShort(e.at)}</time>
              <code>{e.event}</code>
              {!e.written && <Badge tone="danger">not written</Badge>}
              <span className="small muted">
                {AUDIT_KEYS.filter((k) => e.details && e.details[k] !== undefined && e.details[k] !== null)
                  .map((k) => `${k}=${String(e.details?.[k])}`).join(" · ")}
              </span>
            </li>))}
        </ol>
      ) : <Unavailable>No audit events for this run.</Unavailable>}
    </Section>
  );
}

// ---- Simulated flood-cycle timeline --------------------------------------------------------

export function TimelineSection({ seq, error }: { seq: SequenceDoc | null; error: string | null }) {
  const changes = (seq?.steps ?? []).filter((s) => s.state_before !== s.state_after);
  return (
    <Section id="timeline" title="State timeline — simulated flood cycle"
      subtitle={seq?.description ?? "Fabricated river and weather series, TEST thresholds"}
      badges={<Badge tone="sim">SIMULATED</Badge>}>
      <SimulatedBanner text="A fabricated flood cycle run through the real workflow, step by step. It shows how the state manager behaves; it is not a forecast." />
      {error ? <Unavailable>Timeline unavailable: {error}</Unavailable> : !seq ? <Unavailable>Loading…</Unavailable> : (
        <>
          <StateTimelineChart steps={seq.steps} />
          <table className="table">
            <thead><tr><th>Hour</th><th>Level</th><th>Risk</th><th>Transition</th><th>Reason</th></tr></thead>
            <tbody>{changes.map((s) => (
              <tr key={s.hour}><td>{s.hour}</td><td>{fmtNum(s.river_level_m, "m", 2)}</td><td>{s.risk_level}</td>
                <td><Badge tone={STATE_TONE[s.state_before ?? ""] ?? "unknown"}>{s.state_before}</Badge> → <Badge tone={STATE_TONE[s.state_after ?? ""] ?? "unknown"}>{s.state_after}</Badge></td>
                <td className="small">{s.reason}</td></tr>))}</tbody>
          </table>
        </>
      )}
    </Section>
  );
}
