// Tests for response validation, formatting and the Forecast & Analytics page.
// Fixtures are real output of the Python backend: the historical CWC analysis
// (analysis_real.json), a synthetic series run through the real forecasting pipeline so
// that horizons publish (analysis_synthetic_published.json), and the historical series
// endpoint on the real export (series_*.json).
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { fetchSeries, parseAnalysis, parseDashboard, parseSeries, parseSequence } from "../api";
import {
  AnalyticsView, ForecastChart, historicalChartData, HorizonCards, ModelComparison, qualityTone, SummaryCards,
} from "../components/analytics";
import { DashboardBody } from "../App";
import {
  drawablePredictions, forecastChartData, horizonCounts, horizonStatus, modelBars, validationChecks,
} from "../forecastModel";
import type { AnalysisDoc, SeriesDoc } from "../types";
import { fmtNum, fmtTime, humanize } from "../format";
import analysisReal from "./fixtures/analysis_real.json";
import analysisPublished from "./fixtures/analysis_synthetic_published.json";
import seriesUA from "./fixtures/series_upper_anicut_7d.json";
import seriesMusiri from "./fixtures/series_musiri_30d.json";
import simEmergency from "./fixtures/simulation_emergency.json";
import { fromSimulation, parseSimulation } from "../model";
import realStale from "./fixtures/real_stale.json";
import realOutage from "./fixtures/real_outage.json";
import simulated from "./fixtures/simulated_high_risk.json";
import sequence from "./fixtures/sequence_flood_cycle.json";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const real = analysisReal as unknown as AnalysisDoc;
const pub = analysisPublished as unknown as AnalysisDoc;
const ua = seriesUA as unknown as SeriesDoc;
const musiri = seriesMusiri as unknown as SeriesDoc;
const noop = () => undefined;
const view = (doc: AnalysisDoc, series: SeriesDoc | null = ua, station = "UPPER_ANICUT", range: SeriesDoc["range"] = "7d") =>
  text(renderToStaticMarkup(<AnalyticsView doc={doc} series={series} seriesError={null} seriesLoading={false}
    station={station} range={range} onStation={noop} onRange={noop} />));

afterEach(() => vi.unstubAllGlobals());

describe("response validation", () => {
  it("accepts real backend documents", () => {
    for (const doc of [realStale, realOutage, simulated]) expect(parseDashboard(doc).ok).toBe(true);
    expect(parseSequence(sequence).ok).toBe(true);
  });

  it.each([
    ["not an object", "nope"],
    ["an array", []],
    ["null", null],
  ])("rejects %s", (_, value) => {
    expect(parseDashboard(value).ok).toBe(false);
  });

  it("surfaces backend errors instead of rendering", () => {
    const r = parseDashboard({ error: "Dashboard could not be assembled (RuntimeError)." });
    expect(r).toEqual({ ok: false, error: "Dashboard could not be assembled (RuntimeError)." });
  });

  it("rejects an unknown schema, missing mode or a malformed section", () => {
    expect(parseDashboard({ ...clone(realStale), schema_version: "9" }).ok).toBe(false);
    expect(parseDashboard({ ...clone(realStale), mode: "demo" }).ok).toBe(false);
    expect(parseDashboard({ ...clone(realStale), river: "stale" }).ok).toBe(false);
    expect(parseDashboard({ ...clone(realStale), generated_at: undefined }).ok).toBe(false);
  });

  it("never mixes simulated data into real mode", () => {
    for (const part of ["weather", "river", "risk"]) {
      const doc = clone(realStale) as Record<string, any>;
      doc[part].is_simulated = true;
      const r = parseDashboard(doc);
      expect(r.ok).toBe(false);
      if (!r.ok) expect(r.error).toMatch(/simulated/);
    }
    expect(parseDashboard({ ...clone(realStale), is_simulated: true }).ok).toBe(false);
    expect(parseDashboard({ ...clone(simulated), is_simulated: false }).ok).toBe(false);
  });

  it("rejects a sequence that is not labelled simulated", () => {
    expect(parseSequence({ ...clone(sequence), is_simulated: false }).ok).toBe(false);
  });
});


describe("formatting", () => {
  it("never turns a missing number into zero", () => {
    expect(fmtNum(null, "mm")).toBe("—");
    expect(fmtNum(undefined, "m")).toBe("—");
    expect(fmtNum(Number.NaN, "m")).toBe("—");
    expect(fmtNum(0, "mm")).toBe("0.0 mm");
  });
  it("shows times in IST", () => {
    expect(fmtTime("2026-10-25T16:18:10Z")).toBe("25 Oct 2026, 21:48 IST");
    expect(fmtTime(null)).toBe("—");
  });
  it("humanizes codes", () => {
    expect(humanize("EVIDENCE_STALE")).toBe("Evidence stale");
    expect(humanize("R13_INDETERMINATE_RIVER_UNUSABLE")).toBe("Indeterminate river unusable");
  });
});


describe("analysis response validation", () => {
  it("accepts the backend's analysis and series documents", () => {
    expect(parseAnalysis(analysisReal).ok).toBe(true);
    expect(parseAnalysis(analysisPublished).ok).toBe(true);
    expect(parseSeries(seriesUA).ok).toBe(true);
    expect(parseSeries(seriesMusiri).ok).toBe(true);
  });

  it("rejects predictions for unpublished horizons, mislabelled or authority-claiming documents", () => {
    const sneaky = clone(analysisReal) as Record<string, any>;
    sneaky.forecast.live[0].predicted_level_m = 74.5;
    expect(parseAnalysis(sneaky).ok).toBe(false);
    expect(parseAnalysis({ ...clone(analysisReal), is_simulated: true }).ok).toBe(false);
    expect(parseAnalysis({ ...clone(analysisReal), data_label: "REAL DATA" }).ok).toBe(false);
    const authority = clone(analysisReal) as Record<string, any>;
    authority.forecast.affects_risk_assessment = true;
    expect(parseAnalysis(authority).ok).toBe(false);
    const staleNumber = clone(analysisReal) as Record<string, any>;
    staleNumber.forecast.evaluation.horizons[0].published = true;
    Object.assign(staleNumber.forecast.live[0], { status: "FORECAST", published: true, predicted_level_m: 74.5 });
    expect(parseAnalysis(staleNumber).ok).toBe(false);
  });

  it("rejects a series that is simulated, mislabelled or not measured", () => {
    expect(parseSeries({ ...clone(seriesUA), is_simulated: true }).ok).toBe(false);
    expect(parseSeries({ ...clone(seriesUA), data_label: "SIMULATED LIVE" }).ok).toBe(false);
    expect(parseSeries({ ...clone(seriesUA), value_basis: "MODEL" }).ok).toBe(false);
    expect(parseSeries({ error: "Historical data could not be loaded (OSError)." })).toEqual(
      { ok: false, error: "Historical data could not be loaded (OSError)." });
  });
});

describe("summary cards", () => {
  it("show the latest level, observation time, forecast availability and freshness from the backend", () => {
    const t = text(renderToStaticMarkup(<SummaryCards doc={real} />));
    const st = real.analysis!.stations!.UPPER_ANICUT.analysis!;
    expect(t).toContain(fmtNum(st.latest!.level_m, "m", 2));
    expect(t).toContain(fmtTime(st.latest!.observed_at));
    expect(t).toContain("HISTORICAL CWC");
    const counts = horizonCounts(real.forecast);
    expect(counts).toEqual({ total: 5, published: 0 });
    expect(t).toContain("0 / 5");
    expect(t).toContain("NOT AVAILABLE");
    expect(real.forecast!.input!.status).toBe("STALE");
    expect(t).toMatch(/Data freshness\s+STALE/);
    expect(t).toContain("304 days old");
  });

  it("report FRESH and AVAILABLE only when the backend says so", () => {
    const t = text(renderToStaticMarkup(<SummaryCards doc={pub} />));
    const counts = horizonCounts(pub.forecast);
    expect(counts.published).toBeGreaterThan(0);
    expect(t).toContain(`${counts.published} / ${counts.total}`);
    expect(t).toMatch(/AVAILABLE/);
    expect(t).toMatch(/Data freshness\s+FRESH/);
  });
});

describe("forecast horizons", () => {
  it("never shows a predicted level while inputs are stale, with a concise reason per horizon", () => {
    const t = text(renderToStaticMarkup(<HorizonCards f={real.forecast!} />));
    for (const h of [1, 3, 6, 12, 24]) expect(t).toContain(`+${h} h`);
    expect(t).not.toMatch(/PUBLISHED(?!.)/);
    expect((t.match(/NOT PUBLISHED/g) ?? [])).toHaveLength(5);
    for (const h of real.forecast!.evaluation!.horizons!) {
      const s = horizonStatus(h, real.forecast!.live!.find((x) => x.horizon_hours === h.horizon_hours), real.forecast);
      expect(s.published).toBe(false);
      expect(t).toContain(s.label);
      expect(["Validation failed", "Not published — input stale", "Awaiting validation — too little data"]).toContain(s.label);
    }
    expect(drawablePredictions(real.forecast)).toEqual([]);
  });

  it("shows the predicted level and interval for published horizons only", () => {
    const f = pub.forecast!;
    const t = text(renderToStaticMarkup(<HorizonCards f={f} />));
    const drawn = drawablePredictions(f);
    expect(drawn.length).toBeGreaterThan(0);
    for (const x of drawn) {
      expect(t).toContain(fmtNum(x.predicted_level_m, "m", 2));
      expect(t).toContain(`${fmtNum(x.interval_m![0], "", 2)}–${fmtNum(x.interval_m![1], "m", 2)}`);
    }
    const unpublished = f.evaluation!.horizons!.filter((h) => !horizonStatus(h, f.live!.find((x) => x.horizon_hours === h.horizon_hours), f).published);
    expect((t.match(/NOT PUBLISHED/g) ?? [])).toHaveLength(unpublished.length);
  });

  it("does not publish a validated horizon when inputs are stale", () => {
    const f = clone(pub.forecast!);
    f.input!.status = "STALE";
    const h = f.evaluation!.horizons!.find((x) => x.validation_status === "PUBLISHED")!;
    expect(horizonStatus(h, f.live!.find((x) => x.horizon_hours === h.horizon_hours), f)).toMatchObject(
      { published: false, label: "Not published — input stale" });
  });
});

describe("observed vs forecast chart data", () => {
  it("uses the backend's measured readings and draws no prediction while stale", () => {
    const { measured, predicted } = forecastChartData(real.forecast);
    expect(measured).toHaveLength(real.forecast!.recent_measurements!.points!.length);
    expect(predicted).toEqual([]);
    expect(text(renderToStaticMarkup(<ForecastChart f={real.forecast!} />))).toContain("No published forecast to draw");
  });

  it("keeps published predictions and their bands as a separate series", () => {
    const { predicted } = forecastChartData(pub.forecast);
    expect(predicted.length).toBe(drawablePredictions(pub.forecast).length);
    expect(predicted.every((p) => p.range === null || p.range[0] <= p.predicted && p.predicted <= p.range[1])).toBe(true);
  });
});

describe("model comparison", () => {
  it("plots the backend's validation errors and never calls a failed model published", () => {
    const h = real.forecast!.evaluation!.horizons![0];
    const bars = modelBars(h, "mae_m");
    expect(bars.map((b) => b.cm)).toEqual(Object.keys(h.models!).map((m) => Math.round(h.models![m].validation!.mae_m! * 1000) / 10));
    expect(bars.find((b) => b.selected)?.model).toBe(h.selected_model);
    expect(bars.some((b) => b.published)).toBe(false);
    const t = text(renderToStaticMarkup(<ModelComparison h={h} metric="mae_m" />));
    expect(t).toContain("NOT PUBLISHED");
    expect(t).toContain("did not pass validation");
    expect(t).not.toContain("passed every publishing check");
    expect(validationChecks(h).some((c) => !c.pass)).toBe(true);
  });

  it("highlights the selected model as published only when the backend published it", () => {
    const h = pub.forecast!.evaluation!.horizons!.find((x) => x.validation_status === "PUBLISHED")!;
    const bars = modelBars(h, "rmse_m");
    expect(bars.filter((b) => b.published).map((b) => b.model)).toEqual([h.selected_model]);
    expect(text(renderToStaticMarkup(<ModelComparison h={h} metric="rmse_m" />))).toContain("passed every publishing check");
  });

  it("keeps technical metrics inside 'Model performance details'", () => {
    const html = renderToStaticMarkup(<AnalyticsView doc={real} series={ua} seriesError={null} seriesLoading={false}
      station="UPPER_ANICUT" range="7d" onStation={noop} onRange={noop} />);
    const details = html.indexOf("Model performance details");
    expect(details).toBeGreaterThan(0);
    for (const label of ["RMSE", "Bias", "R²", "Skill vs persistence", "Interval coverage"]) {
      expect(html.indexOf(`<th>${label}</th>`)).toBeGreaterThan(details);
    }
  });
});

describe("historical river levels", () => {
  it("shows the backend's latest, minimum, maximum, average and data quality", () => {
    const t = view(real, ua);
    expect(t).toContain(`Latest ${fmtNum(ua.latest!.level_m, "m", 2)}`);
    expect(t).toContain(`Minimum ${fmtNum(ua.stats!.min_m, "m", 2)}`);
    expect(t).toContain(`Maximum ${fmtNum(ua.stats!.max_m, "m", 2)}`);
    expect(t).toContain(`Average ${fmtNum(ua.stats!.mean_m, "m", 2)}`);
    expect(t).toContain(`${ua.quality!.accepted_pct}% passed QC`);
    expect(t).toContain(qualityTone(ua.quality!.accepted_pct).label);
  });

  it("switches station and range with the selectors and shows poor data quality honestly", () => {
    const t = view(real, musiri, "MUSIRI", "30d");
    expect(t).toMatch(/Upper Anicut\s+Musiri/);
    expect(t).toContain(`${musiri.stats!.readings} readings`);
    expect(qualityTone(musiri.quality!.accepted_pct).label).toBe("POOR");
    expect(t).toContain("POOR");
    // A response for another station or range is never shown under the wrong selection.
    expect(view(real, ua, "MUSIRI", "7d")).toContain("No readings loaded");
    expect(view(real, ua, "UPPER_ANICUT", "30d")).toContain("No readings loaded");
  });

  it("breaks the line at gaps instead of joining distant readings", () => {
    const pts = historicalChartData(ua);
    const nulls = pts.filter((p) => p.v === null).length;
    expect(pts.length).toBe(ua.points.length + nulls);
    expect(pts.filter((p) => p.v !== null).map((p) => p.v)).toEqual(ua.points.map((p) => p.level_m));
  });

  it("keeps metadata in a collapsed 'Data details' section", () => {
    const html = renderToStaticMarkup(<AnalyticsView doc={real} series={ua} seriesError={null} seriesLoading={false}
      station="UPPER_ANICUT" range="7d" onStation={noop} onRange={noop} />);
    const details = html.indexOf("Data details");
    expect(details).toBeGreaterThan(0);
    expect(html.slice(details)).toContain("rwl_tel_hr_cwc_009_2021_2025.csv");
    expect(html.slice(details - 200, details)).toContain("<details");
    expect(html).not.toMatch(/<details[^>]* open/);
  });

  it("requests the selected station and range from the backend", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(seriesMusiri), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const r = await fetchSeries("MUSIRI", "30d");
    expect(r.ok).toBe(true);
    expect(String((fetchMock.mock.calls[0] as unknown[])[0])).toMatch(/\/dashboard\/analysis\/series\?station=MUSIRI&range=30d$/);
  });
});

describe("page simplification and labelling", () => {
  it("has no station comparison section and no repeated disclaimers", () => {
    const t = view(real);
    expect(t).not.toMatch(/Station comparison|correlation|straight-line|upstream relationship/i);
    expect(t).not.toMatch(/not a live source|never CWC measurements|not official/i);
    expect((t.match(/HISTORICAL CWC/g) ?? []).length).toBeLessThanOrEqual(4);
  });

  it("uses compact source labels and never calls historical or model values telemetry", () => {
    const t = view(pub);
    expect(t).toContain("HISTORICAL CWC");
    expect(t).toContain("MODEL PREDICTION");
    expect(t).not.toContain("CWC TELEMETRY");
    expect(t).not.toContain("SIMULATED LIVE");
  });

  it("renders the page shell with one source-label tooltip, without the long source banner", () => {
    const r = parseSimulation(simEmergency);
    if (!r.ok) throw new Error(r.error);
    const html = renderToStaticMarkup(<DashboardBody view={fromSimulation(r.value)} page="analytics" analysis={real} analysisError={null} />);
    expect((html.match(/role="tooltip"/g) ?? [])).toHaveLength(1);
    expect(text(html)).toContain("Forecast & Analytics");
    expect(text(html)).not.toContain("never Central Water Commission measurements");
  });
});
