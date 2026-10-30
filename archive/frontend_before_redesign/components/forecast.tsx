// Model predictions: graphical view of the backend's forecast validation
// (/dashboard/analysis -> forecast). Every number comes from the backend response.
// Safety: a predicted value is drawn only when the backend returned it as a published
// FORECAST from FRESH inputs; predictions are never drawn as measurements and never
// joined to the measured line.
import { useState } from "react";
import {
  Area, Bar, BarChart, CartesianGrid, ComposedChart, Legend, Line, ReferenceLine,
  ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { AnalysisDoc, HorizonEvaluation, LiveForecast, ModelMetrics } from "../types";
import { epoch, fmtAge, fmtNum, fmtShort, fmtTime, isNum } from "../format";
import { Badge, Section, Unavailable } from "./common";

type Forecast = NonNullable<AnalysisDoc["forecast"]>;

export const MODEL_NAMES: Record<string, string> = {
  persistence: "Persistence (no change)",
  moving_average_6h: "Moving average (6 h)",
  linear_trend_6h: "Linear trend (6 h)",
  ridge_ar_6h: "Ridge autoregression (6 h)",
};
const MODEL_COLORS: Record<string, string> = {
  persistence: "#64748b", moving_average_6h: "#0e7490", linear_trend_6h: "#d97706", ridge_ar_6h: "#7c3aed",
};
const MEASURED_COLOR = "#0b2545";
const PREDICTED_COLOR = "#2563eb";
const modelName = (m?: string | null) => (m ? MODEL_NAMES[m] ?? m : "—");
const cm = (m?: number | null) => (isNum(m) ? `${(m * 100).toFixed(1)} cm` : "—");
const pct = (x?: number | null) => (isNum(x) ? `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "—");

// ---- pure helpers (tested) ----------------------------------------------------------

/** True only when the backend says the forecast inputs are fresh. */
export function inputsFresh(f?: Forecast): boolean {
  return f?.input?.status === "FRESH";
}

/** Predictions that may be drawn: published FORECAST entries with numbers, from fresh inputs. */
export function drawablePredictions(f?: Forecast): LiveForecast[] {
  if (!inputsFresh(f)) return [];
  return (f?.live ?? []).filter((x) => x.status === "FORECAST" && x.published === true
    && isNum(x.predicted_level_m) && !!x.valid_for);
}

export function forecastChartData(f?: Forecast) {
  const measured = (f?.recent_measurements?.points ?? [])
    .filter((p) => isNum(p.level_m) && epoch(p.time) !== null)
    .map((p) => ({ t: epoch(p.time) as number, measured: p.level_m }));
  const predicted = drawablePredictions(f).map((x) => ({
    t: epoch(x.valid_for) as number, predicted: x.predicted_level_m as number,
    range: x.interval_m && x.interval_m.length === 2 ? [x.interval_m[0], x.interval_m[1]] : null,
    horizon: x.horizon_hours,
  }));
  return { measured, predicted };
}

/** Test MAE of every model per horizon (absolute metres, or % of persistence's MAE). */
export function modelComparisonData(f?: Forecast, relative = false) {
  return (f?.evaluation?.horizons ?? []).filter((h) => h.models).map((h) => {
    const row: Record<string, number | string | null> = { horizon: `+${h.horizon_hours} h` };
    const base = h.models?.persistence?.test?.mae_m;
    for (const name of Object.keys(MODEL_NAMES)) {
      const mae = h.models?.[name]?.test?.mae_m;
      row[name] = !isNum(mae) ? null : relative ? (isNum(base) && base > 0 ? (mae / base) * 100 : null) : mae;
    }
    return row;
  });
}

export type HorizonTone = "ok" | "caution" | "danger";
export type Check = { label: string; value: string; pass: boolean };
export type HorizonState = {
  tone: HorizonTone;
  label: string;        // compact badge text
  status: string;       // large status text
  short: string[];      // short reason summary shown on the card
  details: string[];    // full backend reasons, shown under "Why not published?"
};

// Display mirrors of the backend publishing rule (river_forecast.py); the backend's
// validation_status stays authoritative, these only label which check failed.
const MIN_SKILL = 0.02;
const MIN_COVERAGE = 0.8;

/** The three publishing checks for the selected model, from the backend's metrics. */
export function validationChecks(h: HorizonEvaluation): Check[] {
  const m = h.selected_model ? h.models?.[h.selected_model] : undefined;
  const checks: Check[] = [];
  const vs = m?.validation?.skill_vs_persistence, ts = m?.test?.skill_vs_persistence, cov = m?.interval?.test_coverage;
  if (m?.validation) checks.push({ label: "Validation skill", value: pct(vs), pass: (vs ?? 0) >= MIN_SKILL });
  if (m?.test) checks.push({ label: "Test skill", value: pct(ts), pass: (ts ?? 0) >= MIN_SKILL });
  if (isNum(cov)) checks.push({ label: "Interval coverage", value: `${Math.round(cov * 100)}%`, pass: cov >= MIN_COVERAGE });
  return checks;
}

/** Card status: green published, amber waiting for sufficient validation, red failed or stale. */
export function horizonState(h: HorizonEvaluation, live?: LiveForecast, f?: Forecast): HorizonState {
  const stale = !inputsFresh(f);
  const details = [...new Set([...(h.validation_status !== "PUBLISHED" && h.reason ? [h.reason] : []),
    ...(live?.reason && live.status !== "FORECAST" ? [live.reason] : [])])];
  const failed = validationChecks(h).filter((c) => !c.pass).map((c) =>
    `${c.label} ${c.value} (needs ${c.label === "Interval coverage" ? `≥ ${Math.round(MIN_COVERAGE * 100)}%` : `≥ +${MIN_SKILL * 100}%`})`);
  if (h.validation_status === "PUBLISHED" && live?.status === "FORECAST" && !stale)
    return { tone: "ok", label: "Published", status: "PUBLISHED", short: ["Passed validation; inputs fresh"], details: [] };
  if (h.validation_status === "INSUFFICIENT_DATA")
    return { tone: "caution", label: "Awaiting validation", status: "AWAITING VALIDATION",
      short: ["Not enough data to validate this horizon yet"], details };
  if (h.validation_status === "PUBLISHED")
    return { tone: "danger", label: "Not published", status: "NOT PUBLISHED",
      short: ["Passed validation", stale ? "input data stale (see notice above)" : "input readings incomplete"], details };
  return { tone: "danger", label: "Not published", status: "NOT PUBLISHED",
    short: failed.length ? failed : ["Failed validation"], details };
}

/** Counts for the summary strip, from the backend's evaluation and live entries. */
export function horizonCounts(f?: Forecast) {
  const horizons = f?.evaluation?.horizons ?? [];
  const published = horizons.filter((h) =>
    horizonState(h, f?.live?.find((x) => x.horizon_hours === h.horizon_hours), f).tone === "ok").length;
  const passedValidation = horizons.filter((h) => h.validation_status === "PUBLISHED").length;
  return { total: horizons.length, published, notPublished: horizons.length - published, passedValidation };
}

// ---- charts -------------------------------------------------------------------------

function ForecastChart({ f }: { f: Forecast }) {
  const { measured, predicted } = forecastChartData(f);
  if (!measured.length && !predicted.length) return <Unavailable>No measured readings to plot.</Unavailable>;
  const levels = [...measured.map((d) => d.measured), ...predicted.flatMap((d) => [d.predicted, ...(d.range ?? [])])];
  const lo = Math.min(...levels), hi = Math.max(...levels), pad = Math.max(0.05, (hi - lo) * 0.15);
  const latest = measured.length ? measured[measured.length - 1].t : null;
  return (
    <figure className="chart" aria-label="River level: measured readings and model predictions">
      <ResponsiveContainer width="100%" height={300}>
        <ComposedChart margin={{ top: 8, right: 20, bottom: 28, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} allowDuplicatedCategory={false}
                 tickFormatter={(t: number) => fmtShort(new Date(t).toISOString())} fontSize={11}
                 label={{ value: "Time (IST)", position: "insideBottom", offset: -18, fontSize: 12 }} />
          <YAxis domain={[Math.floor((lo - pad) * 100) / 100, Math.ceil((hi + pad) * 100) / 100]} fontSize={11}
                 tickFormatter={(v: number) => v.toFixed(2)}
                 label={{ value: "River level (m)", angle: -90, position: "insideLeft", fontSize: 12 }} />
          <Tooltip labelFormatter={(t: number) => `${fmtShort(new Date(t).toISOString())} IST`}
                   formatter={(v: number | number[], name: string) =>
                     [Array.isArray(v) ? `${fmtNum(v[0], "", 3)} – ${fmtNum(v[1], "m", 3)}` : fmtNum(v, "m", 3), name]} />
          <Legend verticalAlign="top" height={30} />
          {latest !== null && <ReferenceLine x={latest} stroke="#0f172a" strokeDasharray="4 2"
                                             label={{ value: "Latest measurement", position: "insideTopLeft", fontSize: 11 }} />}
          <Line data={measured} dataKey="measured" name="Measured level (CWC, QC-accepted)" stroke={MEASURED_COLOR}
                strokeWidth={2} dot={{ r: 2 }} connectNulls={false} isAnimationActive={false} />
          {predicted.length > 0 && <Area data={predicted} dataKey="range" name="90% prediction range (model)"
                                         fill={PREDICTED_COLOR} fillOpacity={0.15} stroke="none" isAnimationActive={false} />}
          {predicted.length > 0 && <Line data={predicted} dataKey="predicted" name="MODEL PREDICTION (not a measurement)"
                                         stroke={PREDICTED_COLOR} strokeWidth={2} strokeDasharray="6 4"
                                         dot={{ r: 4, fill: "#fff", strokeWidth: 2 }} isAnimationActive={false} />}
        </ComposedChart>
      </ResponsiveContainer>
      <figcaption>
        {predicted.length
          ? <>Solid line: measured readings. Dashed line and open circles: model predictions at +{predicted.map((d) => d.horizon).join(", +")} h, with the shaded 90% range. Predictions are not joined to the measurements.</>
          : <>Measured readings only ({f.recent_measurements?.window_hours ?? 72} h up to {fmtTime(f.input?.latest_observed_at)}). <strong>No prediction is drawn</strong>: no forecast is available from these inputs.</>}
      </figcaption>
    </figure>
  );
}

function ModelComparisonChart({ f }: { f: Forecast }) {
  const [relative, setRelative] = useState(false);
  const data = modelComparisonData(f, relative);
  if (!data.length) return <Unavailable>No validated models to compare.</Unavailable>;
  return (
    <figure className="chart" aria-label="Test error of each model by forecast horizon">
      <label className="small">View{" "}
        <select value={relative ? "relative" : "absolute"} onChange={(e) => setRelative(e.target.value === "relative")}>
          <option value="absolute">Average error in metres (MAE)</option>
          <option value="relative">Error as % of persistence (100% = same as persistence)</option>
        </select>
      </label>
      <ResponsiveContainer width="100%" height={280}>
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="horizon" fontSize={12} label={{ value: "Forecast horizon", position: "insideBottom", offset: -18, fontSize: 12 }} />
          <YAxis fontSize={11} tickFormatter={(v: number) => (relative ? `${v.toFixed(0)}%` : v.toFixed(2))}
                 label={{ value: relative ? "% of persistence error" : "Test MAE (m) — lower is better", angle: -90, position: "insideLeft", fontSize: 12 }} />
          <Tooltip formatter={(v: number, name: string) => [relative ? `${v.toFixed(1)}% of persistence` : `${fmtNum(v, "m", 4)} (${cm(v)})`, name]} />
          <Legend verticalAlign="top" height={30} />
          {relative && <ReferenceLine y={100} stroke="#0f172a" strokeDasharray="4 2" label={{ value: "persistence", position: "insideTopRight", fontSize: 11 }} />}
          {Object.keys(MODEL_NAMES).map((m) => (
            <Bar key={m} dataKey={m} name={MODEL_NAMES[m]} fill={MODEL_COLORS[m]} isAnimationActive={false} />
          ))}
        </BarChart>
      </ResponsiveContainer>
      <figcaption>Lower bars are better: a model is only useful if its bar is lower than persistence's. Errors are measured on the held-out test period.</figcaption>
    </figure>
  );
}

// ---- cards --------------------------------------------------------------------------

const METRICS: { key: keyof ModelMetrics; name: string; help: string }[] = [
  { key: "mae_m", name: "MAE", help: "Average size of the prediction error, ignoring direction. Lower is better." },
  { key: "rmse_m", name: "RMSE", help: "Like MAE, but large errors count extra. Lower is better." },
  { key: "bias_m", name: "Bias", help: "Average signed error: positive means the model tends to over-predict, negative under-predict. Closer to zero is better." },
  { key: "r2", name: "R²", help: "How much of the variation in measured levels the predictions explain: 1 is perfect, 0 is no better than the average, negative is worse." },
  { key: "skill_vs_persistence", name: "Skill vs persistence", help: "How much smaller the error is than simply assuming the level stays the same. Positive is better; at least +2% is required to publish." },
];

function metricValue(key: keyof ModelMetrics, m?: ModelMetrics): string {
  const v = m?.[key];
  if (!isNum(v)) return "—";
  if (key === "r2") return v.toFixed(3);
  if (key === "skill_vs_persistence") return pct(v);
  return `${key === "bias_m" && v > 0 ? "+" : ""}${cm(v)}`;
}

function metricNote(key: keyof ModelMetrics, m?: ModelMetrics, base?: ModelMetrics): string {
  const v = m?.[key];
  if (key === "bias_m" && isNum(v)) return v > 0 ? "tends to over-predict" : v < 0 ? "tends to under-predict" : "no tendency";
  if (key === "skill_vs_persistence" && isNum(v))
    return `${v >= 0.02 ? "better than" : v > 0 ? "only marginally better than" : "not better than"} persistence on the test period (validation must agree to publish)`;
  const b = base?.[key];
  return isNum(b) && key !== "skill_vs_persistence" ? `persistence: ${metricValue(key, base)}` : "";
}

function AccuracyCards({ f }: { f: Forecast }) {
  const horizons = (f.evaluation?.horizons ?? []).filter((h) => h.selected_model);
  const [chosen, setChosen] = useState(horizons[0]?.horizon_hours ?? 1);
  const h = horizons.find((x) => x.horizon_hours === chosen) ?? horizons[0];
  if (!h) return <Unavailable>No validated models.</Unavailable>;
  const model = h.selected_model ?? "persistence";
  const m = h.models?.[model]?.test;
  const base = h.models?.persistence?.test;
  return (
    <div>
      <label className="small">Horizon{" "}
        <select value={h.horizon_hours} onChange={(e) => setChosen(Number(e.target.value))}>
          {horizons.map((x) => <option key={x.horizon_hours} value={x.horizon_hours}>+{x.horizon_hours} h</option>)}
        </select>
      </label>{" "}
      <span className="small muted">Best model on validation: <strong>{modelName(model)}</strong> · {m?.samples ?? "—"} test samples</span>
      <div className="metric-grid">
        {METRICS.map((x) => (
          <article key={x.key} className="metric-card" title={x.help}>
            <h4>{x.name}</h4>
            <p className="metric-value">{metricValue(x.key, m)}</p>
            <p className="metric-note">{metricNote(x.key, m, base)}</p>
            <p className="metric-help">{x.help}</p>
          </article>
        ))}
      </div>
    </div>
  );
}

const MODEL_HELP = "Selected as the model with the lowest error on the historical validation period. "
  + "This is a validation result, not a live prediction.";
const hoursLabel = (n: number) => `+${n} ${n === 1 ? "Hour" : "Hours"}`;

/** Small ring icon: the filled arc is the horizon's share of a day. */
function HorizonIcon({ hours }: { hours: number }) {
  const r = 9, c = 2 * Math.PI * r, arc = Math.min(1, hours / 24) * c;
  return (
    <svg className="hz-icon" width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
      <circle cx="13" cy="13" r={r} fill="none" stroke="currentColor" strokeOpacity="0.18" strokeWidth="3" />
      <circle cx="13" cy="13" r={r} fill="none" stroke="currentColor" strokeWidth="3" strokeLinecap="round"
              strokeDasharray={`${arc.toFixed(2)} ${c.toFixed(2)}`} transform="rotate(-90 13 13)" />
    </svg>
  );
}

export function HorizonCard({ h, live, f, defaultOpen = false }: {
  h: HorizonEvaluation; live?: LiveForecast; f: Forecast; defaultOpen?: boolean;
}) {
  const [open, setOpen] = useState(defaultOpen);
  const state = horizonState(h, live, f);
  const model = h.selected_model;
  const test = model ? h.models?.[model]?.test : undefined;
  const checks = validationChecks(h);
  const validation = h.validation_status === "PUBLISHED" ? "Passed"
    : h.validation_status === "INSUFFICIENT_DATA" ? "Insufficient data" : "Failed";
  const validationTone = validation === "Passed" ? "ok" : validation === "Failed" ? "danger" : "caution";
  const bodyId = `hz-why-${h.horizon_hours}`;
  return (
    <article className={`hz-card tone-${state.tone}`} aria-label={`Forecast horizon ${hoursLabel(h.horizon_hours)}`}>
      <header className="hz-head">
        <HorizonIcon hours={h.horizon_hours} />
        <h4>{hoursLabel(h.horizon_hours)}</h4>
        <Badge tone={state.tone}>{state.label}</Badge>
      </header>

      <p className="hz-status">{state.status}</p>
      <div className="hz-value">
        <span className="hz-k">Forecast value</span>
        {state.tone === "ok" && isNum(live?.predicted_level_m)
          ? <span><strong>{fmtNum(live?.predicted_level_m, "m", 3)}</strong> <Badge tone="info">MODEL PREDICTION</Badge>
              <span className="small muted"> 90% range {fmtNum(live?.interval_m?.[0], "", 3)}–{fmtNum(live?.interval_m?.[1], "m", 3)} · for {fmtTime(live?.valid_for)}</span></span>
          : <span className="hz-na">Not available</span>}
      </div>

      <div className="hz-model" title={MODEL_HELP}>
        <span className="hz-k">Selected model <span className="hz-help" aria-label={MODEL_HELP}>ⓘ</span></span>
        <span className="model-chip">{modelName(model)}</span>
      </div>

      <dl className="hz-stats">
        <div><dt>Test MAE</dt><dd><strong>{cm(test?.mae_m)}</strong><span className="muted">persistence {cm(h.models?.persistence?.test?.mae_m)}</span></dd></div>
        <div><dt>Validation</dt><dd className={`hz-result tone-${validationTone}`}>{validation}</dd></div>
      </dl>

      {checks.length > 0 && (
        <ul className="hz-checks" aria-label="Publishing checks (historical validation)">
          {checks.map((c) => <li key={c.label} className={c.pass ? "pass" : "fail"}>
            <span aria-hidden="true">{c.pass ? "✓" : "✕"}</span>{c.label} <strong>{c.value}</strong></li>)}
        </ul>
      )}

      {state.tone !== "ok" && <p className="hz-short">{state.short.join(" · ")}</p>}

      {state.details.length > 0 && (
        <div className={`hz-why${open ? " open" : ""}`}>
          <button type="button" className="hz-why-btn" aria-expanded={open} aria-controls={bodyId} onClick={() => setOpen(!open)}>
            Why not published? <span className="hz-chev" aria-hidden="true">▾</span>
          </button>
          <div id={bodyId} className="hz-why-body" aria-hidden={!open}>
            <div><ul>{state.details.map((r, i) => <li key={i}>{r}</li>)}</ul></div>
          </div>
        </div>
      )}
    </article>
  );
}

export function HorizonSummary({ f }: { f: Forecast }) {
  const c = horizonCounts(f);
  const status = (f.input?.status ?? "UNAVAILABLE").toUpperCase();
  const fresh = inputsFresh(f);
  return (
    <>
      <div className="hz-summary" aria-label="Forecast status summary">
        <div><span className="hz-k">Forecast horizons</span><strong>{c.total}</strong></div>
        <div className="tone-ok"><span className="hz-k">Published</span><strong>{c.published}</strong></div>
        <div className={c.notPublished ? "tone-danger" : ""}><span className="hz-k">Not published</span><strong>{c.notPublished}</strong></div>
        <div className={fresh ? "tone-ok" : "tone-danger"}><span className="hz-k">Input freshness</span><strong>{status}</strong>
          <span className="small muted">latest reading {fmtAge(f.input?.age_hours)}</span></div>
      </div>
      {!fresh && (
        <div className="hz-notice" role="note">
          <strong>NO LIVE FORECAST — INPUT DATA ARE {status}</strong>
          <span>The latest measured river reading ({fmtTime(f.input?.latest_observed_at)}) is older than the configured
            freshness limit of {fmtNum(f.input?.max_age_hours, "h", 0)}, so no horizon produces a forecast value.
            The model, error and checks on each card are historical validation results only.</span>
        </div>
      )}
    </>
  );
}

// ---- section ------------------------------------------------------------------------

export function ForecastSection({ doc }: { doc: AnalysisDoc }) {
  const f = doc.forecast;
  if (!f) return <Section id="forecast" title="Model predictions (validated forecasts)"><Unavailable>No forecast in this response.</Unavailable></Section>;
  const horizons = f.evaluation?.horizons ?? [];
  const published = horizons.filter((h) => h.validation_status === "PUBLISHED" || h.published).length;
  const fresh = inputsFresh(f);
  const data = f.evaluation?.data;
  const testPeriod = horizons.find((h) => h.split?.periods?.test)?.split?.periods?.test;
  return (
    <Section id="forecast" title="Model predictions (validated forecasts)"
      subtitle={`${f.station_id ?? "—"} · four models compared against persistence · chronological validation`}
      badges={<><Badge tone="info">MODEL PREDICTION</Badge><Badge tone={published ? "ok" : "neutral"}>{published} of {horizons.length} horizons pass validation</Badge></>}>
      {!fresh && (
        <div className="alert" role="alert">
          <strong>NO LIVE FORECAST — INPUT DATA ARE {(f.input?.status ?? "UNAVAILABLE").toUpperCase()}.</strong>{" "}
          The latest measured reading was made {fmtTime(f.input?.latest_observed_at)} and is {fmtAge(f.input?.age_hours)} (limit {fmtNum(f.input?.max_age_hours, "h", 0)}).
          No prediction is made or drawn from it. Everything below is <strong>historical validation only</strong>.
        </div>
      )}
      <p className="note"><strong>Model predictions are never CWC measurements</strong> and never change the risk level, the disaster state, permissions or emergency authorization.</p>

      <h3>River level: measurements{fresh ? " and forecast" : ""}</h3>
      <ForecastChart f={f} />

      <h3>Which model predicts best? {!fresh && <Badge tone="neutral">HISTORICAL VALIDATION ONLY</Badge>}</h3>
      <ModelComparisonChart f={f} />

      <h3>How accurate is the selected model? {!fresh && <Badge tone="neutral">HISTORICAL VALIDATION ONLY</Badge>}</h3>
      <AccuracyCards f={f} />

      <h3>Forecast horizons</h3>
      <HorizonSummary f={f} />
      <div className="hz-grid">
        {horizons.map((h) => <HorizonCard key={h.horizon_hours} h={h} f={f} live={f.live?.find((x) => x.horizon_hours === h.horizon_hours)} />)}
      </div>

      <div className="info-box">
        <h4>How this forecast works</h4>
        <ul>
          <li>The models learn from <strong>{data?.accepted_readings ?? "—"} valid measured readings</strong> of the local CWC export ({fmtTime(data?.first)} – {fmtTime(data?.last)}); fill values, NaN and out-of-range readings are excluded.</li>
          <li>The data are split by time: models train on the oldest 70%, are tuned on the next 15%, and are tested once on the newest 15% ({fmtShort(testPeriod?.[0])} – {fmtShort(testPeriod?.[1])}). The future is never used to predict the past.</li>
          <li>Every model must beat <strong>persistence</strong> ("the level stays the same") by at least 2% on both the tuning and the test periods, and its 90% range must contain the measured level at least 80% of the time. Otherwise that horizon is not published.</li>
          <li>Even a published model makes no prediction unless the latest measured reading is fresh (at most {fmtNum(f.input?.max_age_hours, "h", 0)} old) and the last 6 hours are complete.</li>
        </ul>
      </div>
    </Section>
  );
}
