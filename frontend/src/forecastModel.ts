// Pure helpers for the Forecast & Analytics page. They only read the backend's forecast
// validation (/dashboard/analysis -> forecast); the backend's publication decision is
// authoritative and nothing here can turn an unpublished horizon into a forecast.
import type { AnalysisDoc, HorizonEvaluation, LiveForecast } from "./types";
import { epoch, isNum } from "./format";

export type Forecast = NonNullable<AnalysisDoc["forecast"]>;

export const MODEL_NAMES: Record<string, string> = {
  persistence: "Persistence (no change)",
  moving_average_6h: "Moving average (6 h)",
  linear_trend_6h: "Linear trend (6 h)",
  ridge_ar_6h: "Ridge autoregression (6 h)",
};
export const modelName = (m?: string | null) => (m ? MODEL_NAMES[m] ?? m : "—");
const SHORT_NAMES: Record<string, string> = {
  persistence: "Persistence", moving_average_6h: "Moving avg (6 h)", linear_trend_6h: "Linear trend (6 h)", ridge_ar_6h: "Ridge AR (6 h)",
};

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

/** Measured readings and drawable predictions as separate series (never joined). */
export function forecastChartData(f?: Forecast) {
  const measured = (f?.recent_measurements?.points ?? [])
    .filter((p) => isNum(p.level_m) && epoch(p.time) !== null)
    .map((p) => ({ t: epoch(p.time) as number, measured: p.level_m }));
  const predicted = drawablePredictions(f).map((x) => ({
    t: epoch(x.valid_for) as number, predicted: x.predicted_level_m as number,
    range: x.interval_m && x.interval_m.length === 2 ? [x.interval_m[0], x.interval_m[1]] as [number, number] : null,
    horizon: x.horizon_hours,
  }));
  return { measured, predicted };
}

// Display mirrors of the backend publishing rule (river_forecast.py), used only to name
// which check failed; the backend's validation_status stays authoritative.
const MIN_SKILL = 0.02;
const MIN_COVERAGE = 0.8;
const pct = (x?: number | null) => (isNum(x) ? `${x > 0 ? "+" : ""}${(x * 100).toFixed(1)}%` : "—");
export type Check = { label: string; value: string; pass: boolean };

/** The publishing checks for the selected model, from the backend's metrics. */
export function validationChecks(h: HorizonEvaluation): Check[] {
  const m = h.selected_model ? h.models?.[h.selected_model] : undefined;
  const checks: Check[] = [];
  const vs = m?.validation?.skill_vs_persistence, ts = m?.test?.skill_vs_persistence, cov = m?.interval?.test_coverage;
  if (m?.validation) checks.push({ label: "Validation skill", value: pct(vs), pass: (vs ?? 0) >= MIN_SKILL });
  if (m?.test) checks.push({ label: "Test skill", value: pct(ts), pass: (ts ?? 0) >= MIN_SKILL });
  if (isNum(cov)) checks.push({ label: "Interval coverage", value: `${Math.round(cov * 100)}%`, pass: cov >= MIN_COVERAGE });
  return checks;
}

export type HorizonTone = "ok" | "caution" | "danger";
export interface HorizonStatus {
  tone: HorizonTone;
  published: boolean;
  label: string;       // "Published" | "Not published — input stale" | "Validation failed" | ...
  details: string[];   // full backend reasons, for the tooltip / details
}

/** Concise status of one horizon. Published only if the backend validated it AND returned a
 *  FORECAST from fresh inputs. */
export function horizonStatus(h: HorizonEvaluation, live: LiveForecast | undefined, f?: Forecast): HorizonStatus {
  const stale = !inputsFresh(f);
  const details = [...new Set([...(h.validation_status !== "PUBLISHED" && h.reason ? [h.reason] : []),
    ...(live?.reason && live.status !== "FORECAST" ? [live.reason] : [])])];
  if (h.validation_status === "PUBLISHED" && live?.status === "FORECAST" && live.published === true
      && isNum(live.predicted_level_m) && !stale)
    return { tone: "ok", published: true, label: "Published", details: [] };
  if (h.validation_status === "INSUFFICIENT_DATA")
    return { tone: "caution", published: false, label: "Awaiting validation — too little data", details };
  if (h.validation_status === "PUBLISHED")
    return { tone: "danger", published: false, label: stale ? "Not published — input stale" : "Not published — input incomplete", details };
  return { tone: "danger", published: false, label: "Validation failed", details };
}

/** Counts for the summary card, from the backend's evaluation and live entries. */
export function horizonCounts(f?: Forecast) {
  const horizons = f?.evaluation?.horizons ?? [];
  const published = horizons.filter((h) =>
    horizonStatus(h, f?.live?.find((x) => x.horizon_hours === h.horizon_hours), f).published).length;
  return { total: horizons.length, published };
}

export type Metric = "mae_m" | "rmse_m";

/** One bar per model for a horizon: the backend's validation error, in centimetres. The
 *  selected model is marked; it is called "published" only if the backend published it. */
export function modelBars(h: HorizonEvaluation | undefined, metric: Metric) {
  if (!h?.models) return [];
  return Object.keys(MODEL_NAMES).flatMap((name) => {
    const v = h.models?.[name]?.validation?.[metric];
    return isNum(v) ? [{
      model: name, label: SHORT_NAMES[name] ?? name, fullName: MODEL_NAMES[name], cm: Math.round(v * 1000) / 10,
      selected: h.selected_model === name, published: h.selected_model === name && h.validation_status === "PUBLISHED",
      baseline: name === "persistence",
    }] : [];
  });
}
