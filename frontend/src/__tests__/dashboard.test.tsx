// Frontend tests. Fixtures are real output of the Python adapter
// (src/dashboard_service.py) generated offline; see README.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { parseAnalysis, parseDashboard, parseSequence } from "../api";
import { AnalysisSections, DataSourcesStrip } from "../components/analysis";
import type { AnalysisDoc } from "../types";
import analysisReal from "./fixtures/analysis_real.json";
import { fmtNum, fmtTime, humanize } from "../format";
import App, { Dashboard } from "../App";
import { KpiCards, RiverSection, riverDataClass, TimelineSection } from "../components/sections";
import type { DashboardDoc, SequenceDoc } from "../types";
import realStale from "./fixtures/real_stale.json";
import realOutage from "./fixtures/real_outage.json";
import simulated from "./fixtures/simulated_high_risk.json";
import sequence from "./fixtures/sequence_flood_cycle.json";
import simulatedLive from "./fixtures/simulated_live_recovery.json";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const render = (doc: unknown) => {
  const parsed = parseDashboard(doc);
  if (!parsed.ok) throw new Error(parsed.error);
  return renderToStaticMarkup(<Dashboard doc={parsed.value} />);
};
const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/\s+/g, " ");

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

describe("real mode with stale evidence", () => {
  const html = render(realStale);
  const t = text(html);

  it("says the river reading is not current", () => {
    expect(t).toContain("Last known reading — NOT current");
    expect(t).toMatch(/STALE · [\d.]+ days old/);
    expect(html).toContain("is-stale");
  });

  it("shows the indeterminate risk and why", () => {
    expect(t).toContain("INDETERMINATE");
    expect(t).toContain("Evidence stale");
    expect(t).toContain("No usable river evidence");
  });

  it("explains the state basis and that nothing is saved", () => {
    expect(t).toContain("No valid persisted real-state record");
    expect(t).toContain("NORMAL , the most restrictive state");
    expect(t).toContain("The dashboard never saves a state record");
  });

  it("is not labelled simulated", () => {
    expect(t).not.toContain("SIMULATED DATA");
    expect(t).not.toMatch(/\bSIMULATED\b/);
  });

  it("shows every real-world capability as denied", () => {
    expect(t).toContain("RESTRICTED");
    expect((t.match(/DENIED/g) ?? []).length).toBeGreaterThanOrEqual(3);
    expect(t).not.toContain("PENDING HUMAN");
  });

  it("lists the audit events of the run", () => {
    expect(t).toContain("river_workflow_started");
    expect(t).toContain("river_workflow_completed");
  });
});

describe("real mode during an outage", () => {
  const t = text(render(realOutage));
  it("shows unavailable, never a number", () => {
    expect(t).toContain("No river reading available");
    expect(t).toContain("No river-level series");
    expect(t).toMatch(/River level\s+Unavailable/);
  });
});

describe("simulated mode", () => {
  const t = text(render(simulated));
  it("is visibly marked SIMULATED DATA", () => {
    expect(t).toContain("SIMULATED DATA");
    expect(t).toContain("Simulated reading (fabricated test data)");
    expect(t).toContain("TEST / prototype — not official");
  });
  it("shows the backend's risk and the scenario", () => {
    expect(t).toContain("CRITICAL");
    expect(t).toContain("simulated-high-risk");
  });
  it("judges the scenario at its simulated time, never as stale", () => {
    // Regression: the backend used the wall clock, so every simulated run showed
    // a spurious "stale at decision" denial. Real-world actions stay denied.
    const reasons = simulated.safety.decisions.flatMap((d) => d.evidence_reasons ?? []);
    expect(reasons).toContain("EVIDENCE_SIMULATED");
    expect(reasons).not.toContain("EVIDENCE_STALE_AT_DECISION");
    expect(t).not.toMatch(/stale at decision/i);
    expect(simulated.risk.assessment_timestamp).toBe("2026-10-10T12:00:00+00:00");
    expect(simulated.safety.decisions.filter((d) => d.allowed).map((d) => d.capability_class))
      .not.toContain("REAL_WORLD");
  });
  it("renders the flood-cycle timeline as simulated", () => {
    const parsed = parseSequence(sequence);
    if (!parsed.ok) throw new Error(parsed.error);
    const tl = text(renderToStaticMarkup(<TimelineSection seq={parsed.value as SequenceDoc} error={null} />));
    expect(tl).toContain("SIMULATED");
    expect((tl.match(/→/g) ?? []).length).toBe(4);  // the four transitions
  });
});

describe("missing fields", () => {
  it("renders without sections and invents no values", () => {
    const doc = clone(realStale) as Record<string, any>;
    delete doc.river; delete doc.weather; delete doc.risk; delete doc.state; delete doc.safety; delete doc.audit;
    const t = text(render(doc));
    expect(t).toContain("Unavailable");
    expect(t).not.toMatch(/\b0\.0 mm\b/);
    expect(t).not.toMatch(/\b0\.00 m\b/);
  });

  it("shows unavailable rainfall windows as a dash, not zero", () => {
    const doc = clone(realStale) as Record<string, any>;
    doc.weather.rainfall["24h"].precipitation = { status: "UNAVAILABLE", total: null, reason: "not covered" };
    const t = text(render(doc));
    expect(t).toMatch(/24h —/);
  });

  it("keeps null hourly values as gaps", () => {
    const doc = clone(simulated) as unknown as DashboardDoc;
    doc.weather!.hourly![0].precipitation_mm = null;
    expect(() => render(doc)).not.toThrow();
  });
});

describe("no real-world actions", () => {
  it("has only read-only controls", () => {
    const html = renderToStaticMarkup(<App />) + render(realStale) + render(simulated) + render(simulatedLive);
    const buttons = [...html.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)].map((m) => text(m[1]).trim());
    // Mode switch and a read-only reload (shown as "Loading…" before the first fetch).
    expect(buttons.sort()).toEqual(["Loading…", "Real CWC Data", "Real-Time River Simulation", "Simulated demo"]);
    expect(html).not.toMatch(/<form|method="post"|type="submit"|<a\s/i);
    // No control is labelled as an action. (The page's own safety notice mentions
    // evacuation and dispatch as things it cannot do, so only labels are checked.)
    for (const label of buttons)
      expect(label).not.toMatch(/evacuat|dispatch|alert|warn|approve|authori[sz]e|execute|send/i);
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

describe("evidence layer", () => {
  const stale = text(render(realStale));
  const outage = text(render(realOutage));
  const sim = text(render(simulated));

  it("shows every safety-critical claim with its status", () => {
    for (const t of [stale, outage, sim]) {
      expect(t).toContain("Evidence and claims");
      expect(t).toContain("C1_RIVER_CURRENT_LEVEL");
      expect(t).toContain("C6_EVIDENCE_SUFFICIENT_FOR_REAL_WORLD_ACTION");
      expect(t).toContain("INSUFFICIENT FOR REAL-WORLD ACTION");
    }
  });

  it("distinguishes live-stale, unavailable, simulated and model evidence", () => {
    expect(stale).toContain("Live stale");
    expect(stale).toContain("describes past conditions, not current conditions");
    expect(stale).toContain("Model estimate");
    expect(stale).toContain("not a rain-gauge or sensor reading");
    expect(outage).toContain("Unavailable");
    expect(outage).toContain("Live source attempt");
    expect(sim).toContain("Simulated only");
    expect(sim).toContain("NOT official CWC thresholds");
  });

  it("never claims sufficiency for stale, unavailable or simulated evidence", () => {
    for (const t of [stale, outage, sim]) expect(t).not.toContain("SUFFICIENT FOR HUMAN REVIEW");
  });

  it("rejects simulated evidence records inside a real-mode response", () => {
    const doc = clone(realStale) as Record<string, any>;
    doc.evidence_layer.records[0].evidence_class = "SIMULATED";
    const r = parseDashboard(doc);
    expect(r.ok).toBe(false);
    if (!r.ok) expect(r.error).toMatch(/simulated evidence/);
    expect(parseDashboard({ ...clone(realStale), evidence_layer: "x" }).ok).toBe(false);
  });

  it("shows a missing layer as unavailable instead of hiding it", () => {
    const doc = clone(realStale) as Record<string, any>;
    delete doc.evidence_layer;
    expect(text(render(doc))).toContain("No evidence layer in this response.");
  });
});

describe("real-time river simulation", () => {
  const html = render(simulatedLive);
  const t = text(html);

  it("accepts the backend's simulated-live document", () => {
    expect(parseDashboard(simulatedLive).ok).toBe(true);
  });

  it("labels every generated reading SIMULATED LIVE and never as CWC data", () => {
    expect(t).toContain("SIMULATED LIVE");
    expect(t).toContain("NOT Central Water Commission observations");
    expect(t).toContain("Not CWC data");
    expect(t).toContain("UPPER_ANICUT_SIM (simulated twin of UPPER_ANICUT)");
    expect(t).not.toContain("Current, verified live reading");
  });

  it("shows level, timestamp, freshness, trend, quality, risk and state from the backend", () => {
    const doc = simulatedLive as Record<string, any>;
    expect(t).toContain(fmtNum(doc.river.level.value_m, "m", 3));
    expect(t).toContain(fmtTime(doc.river.level.observed_at));
    expect(t).toContain(doc.river.freshness.status);
    expect(t).toContain(doc.river.trend.direction);
    expect(t).toContain(doc.river.recent_quality.status);
    expect(t).toContain(doc.risk.risk_level);
    expect(t).toContain("RECOVERY");
    expect(t).toContain("Recent evaluations");
  });

  it("shows emergency capabilities revoked in RECOVERY and nothing authorized", () => {
    expect(t).toContain("Simulated evidence never authorizes real-world actions");
    expect(t).toContain("RESTRICTED");
    expect(t).not.toContain("PERMITTED PENDING HUMAN APPROVAL");
    const doc = simulatedLive as Record<string, any>;
    const denied = doc.safety.decisions.filter((d: any) =>
      ["request_human_emergency_authorization", "propose_evacuation_plan"].includes(d.capability));
    expect(denied.length).toBe(2);
    for (const d of denied) expect(d).toMatchObject({ allowed: false, reason: "CAPABILITY_DENIED", state: "RECOVERY" });
  });

  it("rejects a simulated-live response that is mislabelled or claims authority", () => {
    expect(parseDashboard({ ...clone(simulatedLive), data_label: "REAL DATA" }).ok).toBe(false);
    expect(parseDashboard({ ...clone(simulatedLive), is_simulated: false }).ok).toBe(false);
    expect(parseDashboard({ ...clone(simulatedLive), simulation: undefined }).ok).toBe(false);
    const authority = clone(simulatedLive) as Record<string, any>;
    authority.simulation.authorizes_real_world_actions = true;
    expect(parseDashboard(authority).ok).toBe(false);
    const pending = clone(simulatedLive) as Record<string, any>;
    pending.safety.pending_human_approval = ["prepare_advisory"];
    expect(parseDashboard(pending).ok).toBe(false);
    const granted = clone(simulatedLive) as Record<string, any>;
    granted.safety.real_world_authorization = "HUMAN_AUTHORIZATION_REQUIRED";
    expect(parseDashboard(granted).ok).toBe(false);
  });

  it("is never accepted as real data", () => {
    expect(parseDashboard({ ...clone(simulatedLive), mode: "real" }).ok).toBe(false);
  });
});

describe("first-page River monitoring card", () => {
  const river = (doc: unknown) => {
    const parsed = parseDashboard(doc);
    if (!parsed.ok) throw new Error(parsed.error);
    return text(renderToStaticMarkup(<RiverSection doc={parsed.value} />));
  };
  const live = simulatedLive as Record<string, any>;
  const real = realStale as Record<string, any>;

  it("real mode keeps the CWC evidence display", () => {
    const t = river(realStale);
    expect(t).toContain(real.river.station_id);
    expect(t).toContain(fmtNum(real.river.level.value_m, "m", 3));
    expect(t).toContain("Last known reading — NOT current");
    expect(t).toMatch(/[\d.]+ days old/);
    expect(t).toContain("Recent river level");
    expect(t).not.toContain("SIMULATED LIVE");
  });

  it("simulated-live mode shows the simulation's reading, never the CWC one", () => {
    const t = river(simulatedLive);
    expect(t).toContain("SIMULATED LIVE — NOT CWC DATA");
    expect(t).toContain("fabricated test data");
    expect(t).toContain("not actual Cauvery river measurements");
    expect(t).toContain(live.simulation.station_id);
    expect(t).toContain(fmtNum(live.river.level.value_m, "m", 3));
    expect(t).toContain(fmtTime(live.river.level.observed_at));
    expect(t).toContain(live.river.freshness.status);
    expect(t).toContain(humanize(live.river.trend.direction));
    expect(t).toContain(live.river.recent_quality.status);
    expect(t).toContain(live.risk.risk_level);
    expect(t).toContain(live.state.evaluation.resulting_state);
    expect(t).toContain("Simulated river level");
    expect(t).not.toContain("No simulated river-level series");
    // Nothing from the real CWC evidence.
    expect(t).not.toContain(fmtNum(real.river.level.value_m, "m", 3));
    expect(t).not.toContain("Last known reading — NOT current");
    expect(t).not.toMatch(/days old/);
    expect(t).not.toContain("Live fetch");
  });

  it("charts the simulated series from the response", () => {
    expect(live.river.series.status).toBe("AVAILABLE");
    expect(live.river.series.points.length).toBeGreaterThan(0);
    for (const p of live.river.series.points) expect(Number.isFinite(p.level_m)).toBe(true);
  });

  it("refuses to show real CWC river evidence inside a simulated-live response", () => {
    const mixed = clone(simulatedLive) as Record<string, any>;
    mixed.river = clone(real.river);
    const t = river(mixed);
    expect(t).toContain("SIMULATED LIVE — NOT CWC DATA");
    expect(t).toContain("Real CWC readings are never shown in simulation mode");
    expect(t).not.toContain(fmtNum(real.river.level.value_m, "m", 3));
    expect(t).not.toMatch(/days old/);
  });
});

describe("real-mode River card: data class, staleness and risk", () => {
  const t = text(renderToStaticMarkup(<RiverSection doc={parseDashboard(realStale).ok ? (realStale as unknown as DashboardDoc) : (null as never)} />));

  it("labels an old live-feed reading STALE LIVE FEED, never an actual current observation", () => {
    expect(t).toContain("STALE LIVE FEED");
    expect(t).not.toContain("ACTUAL CWC OBSERVATION");
    expect(t).toContain("NOT CURRENT CONDITIONS");
    expect(t).toContain("latest valid reading in the NWDP live feed was observed");
    expect(t).toContain("no current reading is available and none is substituted");
  });

  it("classifies readings by source and freshness", () => {
    expect(riverDataClass("LIVE_VERIFIED", "FRESH").text).toBe("ACTUAL CWC OBSERVATION");
    expect(riverDataClass("LIVE_VERIFIED", "STALE").text).toBe("STALE LIVE FEED");
    expect(riverDataClass("LIVE_UNVERIFIED", "UNAVAILABLE").text).toBe("STALE LIVE FEED");
    expect(riverDataClass("HISTORICAL_CSV", "STALE").text).toBe("HISTORICAL CWC DATA");
    expect(riverDataClass("LIVE_UNAVAILABLE", null).text).toBe("NO CWC OBSERVATION");
  });

  it("puts the same badge on the River level summary card", () => {
    const kpi = text(renderToStaticMarkup(<KpiCards doc={realStale as unknown as DashboardDoc} />));
    expect(kpi).toContain("STALE LIVE FEED");
    expect(kpi).toContain("Last known reading — NOT current");
  });

  it("summarises repeated issues by code instead of one line per row", () => {
    const doc = clone(realStale) as Record<string, any>;
    doc.river.errors = [
      ...Array.from({ length: 39 }, (_, i) => ({ code: "PLACEHOLDER_VALUE", severity: "error",
        message: `Row ${i + 2} (2026-08-${String(27 + Math.floor(i / 10)).padStart(2, "0")}T0${i % 10}:00:00+00:00): river_level_m 72.0 is a configured placeholder value, not a measurement.` })),
      { code: "NON_NUMERIC_LEVEL", severity: "error", message: "Row 50: river_level_m 'NaN' is not a finite number." },
    ];
    const html = renderToStaticMarkup(<RiverSection doc={doc as unknown as DashboardDoc} />);
    const shown = text(html);
    expect((html.match(/>PLACEHOLDER_VALUE</g) ?? []).length).toBe(1);
    expect(shown).toContain("PLACEHOLDER_VALUE × 39");
    expect(shown).toContain("27 Aug");
    expect(shown).toContain("NON_NUMERIC_LEVEL Row 50: river_level_m");
    expect(shown).toContain("NaN&#x27; is not a finite number.");
    expect(shown).toContain("These readings are excluded from all calculations.");
    expect(shown).not.toMatch(/ 0\.000 m/);  // nothing missing was turned into zero
  });

  it("shows the deterministic risk assessment", () => {
    expect(t).toContain("Risk assessment");
    expect(t).toContain((realStale as Record<string, any>).risk.risk_level);
  });

  it("never shows the CWC class in simulated-live mode", () => {
    const live = text(renderToStaticMarkup(<RiverSection doc={simulatedLive as unknown as DashboardDoc} />));
    expect(live).not.toContain("ACTUAL CWC OBSERVATION");
    expect(live).not.toContain("HISTORICAL CWC DATA");
  });
});

describe("historical analysis and model predictions", () => {
  const parse = (doc: unknown) => {
    const r = parseAnalysis(doc);
    if (!r.ok) throw new Error(r.error);
    return r.value;
  };
  const show = (doc: AnalysisDoc) => text(renderToStaticMarkup(<AnalysisSections doc={doc} />));
  const real = analysisReal as Record<string, any>;
  const t = show(parse(analysisReal));

  it("accepts the backend's analysis of the real export", () => {
    expect(parseAnalysis(analysisReal).ok).toBe(true);
  });

  it("says the data are historical, not real-time, with the observation time and age", () => {
    const ua = real.analysis.stations.UPPER_ANICUT.analysis;
    expect(t).toContain("HISTORICAL DATA — NOT REAL-TIME");
    expect(t).toContain("HISTORICAL CWC DATA");
    expect(t).toContain("STALE");
    expect(t).toContain(fmtTime(ua.latest.observed_at));
    expect(t).toContain(fmtNum(ua.latest.level_m, "m", 3));
    expect(t).toContain("Placeholder value");
  });

  it("shows every model against persistence and publishes nothing that does not beat it", () => {
    expect(t).toContain("MODEL PREDICTION");
    expect(t).toContain("0 of 5 horizons published");
    for (const name of ["persistence", "moving_average_6h", "linear_trend_6h", "ridge_ar_6h"]) expect(t).toContain(name);
    expect(t).toContain("NOT PUBLISHED");
    expect(t).toContain("No live forecast");
    for (const f of real.forecast.live) expect(f.predicted_level_m).toBeNull();
    expect(t).not.toMatch(/MODEL PREDICTION \d+\.\d{3} m/);   // no predicted level anywhere
  });

  it("explains that Musiri's upstream relationship is not verified", () => {
    expect(t).toContain("Upstream relationship NOT verified");
    expect(t).toContain("not used in forecasts or risk");
    expect(t).toContain("Mettur Reservoir (reservoir)");
  });

  it("shows a published horizon's prediction with its interval, labelled", () => {
    const doc = clone(analysisReal) as Record<string, any>;
    doc.forecast.evaluation.horizons[0].published = true;
    Object.assign(doc.forecast.live[0], { status: "FORECAST", published: true, predicted_level_m: 74.512,
      interval_m: [74.49, 74.53], valid_for: "2026-10-30T07:00:00+00:00" });
    const shown = show(parse(doc));
    expect(shown).toContain("MODEL PREDICTION 74.512 m");
    expect(shown).toContain("74.490");
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
    const unlabelled = clone(analysisReal) as Record<string, any>;
    unlabelled.forecast.label = "CWC OBSERVATION";
    expect(parseAnalysis(unlabelled).ok).toBe(false);
    expect(parseAnalysis({ error: "Historical analysis could not be built (OSError)." }).ok).toBe(false);
  });
});

describe("data sources strip and placement", () => {
  const real = realStale as Record<string, any>;
  const hist = (analysisReal as Record<string, any>).analysis.stations.UPPER_ANICUT.analysis;
  const strip = (analysis: AnalysisDoc | null, error: string | null = null) => text(renderToStaticMarkup(
    <DataSourcesStrip doc={realStale as unknown as DashboardDoc} analysis={analysis} analysisError={error} />));

  it("shows the live feed, the historical export and the forecast separately, each with its own date", () => {
    const t = strip(analysisReal as unknown as AnalysisDoc);
    expect(t).toContain("NWDP live feed");
    expect(t).toContain("STALE LIVE FEED");
    expect(t).toContain(fmtNum(real.river.level.value_m, "m", 3));
    expect(t).toContain(fmtTime(real.river.level.observed_at));
    expect(t).toContain("Local CWC export (UPPER_ANICUT)");
    expect(t).toContain("HISTORICAL CWC DATA");
    expect(t).toContain(fmtNum(hist.latest.level_m, "m", 3));
    expect(t).toContain(fmtTime(hist.latest.observed_at));
    expect(t).toContain(`${hist.accepted_readings} valid readings`);
    expect(t).toContain("MODEL PREDICTION");
    expect(t).toContain("No forecast");
    expect(t).toContain("0 of 5 horizons beat persistence");
  });

  it("never fills the historical box with the live value while loading or on error", () => {
    const loading = strip(null);
    expect(loading).toContain("Loading…");
    expect(loading.split(fmtNum(real.river.level.value_m, "m", 3)).length - 1).toBe(1);
    expect(strip(null, "Cannot reach the API")).toContain("Cannot reach the API");
  });

  it("places the historical analysis directly after River monitoring", () => {
    const html = renderToStaticMarkup(<Dashboard doc={realStale as unknown as DashboardDoc}
      afterRiver={<AnalysisSections doc={analysisReal as unknown as AnalysisDoc} />} />);
    const order = ["river", "historical", "forecast", "stations", "risk"]
      .map((id) => html.indexOf(`id="${id}-title"`));
    expect(order.every((i) => i >= 0)).toBe(true);
    expect([...order].sort((a, b) => a - b)).toEqual(order);
  });

  it("shows the observation periods and the number of valid readings", () => {
    const t = text(renderToStaticMarkup(<AnalysisSections doc={analysisReal as unknown as AnalysisDoc} />));
    const st = (analysisReal as Record<string, any>).analysis.stations;
    expect(t).toContain(`Rows in file ${st.UPPER_ANICUT.load.rows_read}`);
    expect(t).toContain(`Valid readings ${st.UPPER_ANICUT.analysis.accepted_readings}`);
    expect(t).toContain(fmtTime(st.MUSIRI.analysis.latest.observed_at));  // Musiri's own latest date
    expect(t).toContain("Validated on 13086 valid measured readings");
  });
});
