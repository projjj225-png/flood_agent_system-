// River level + rainfall panel. Fixtures are real snapshots of the simulation engine
// (scripts/make_frontend_fixtures.py): 15-minute readings, TEST thresholds 75.0 / 75.6 m,
// recovery level 74.8 m. Every assertion compares the panel with the backend's readings.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import {
  levelDomain, RiverRainPanel, thresholdCrossings, windowPoints, windowStats, type RangeHours,
} from "../components/charts";
import { DashboardBody } from "../App";
import { fromDashboard, fromSimulation, parseSimulation, type SimulationDoc, type View } from "../model";
import { parseDashboard } from "../api";
import type { DashboardDoc } from "../types";
import { fmtNum } from "../format";
import simStart from "./fixtures/simulation_start.json";
import simEmergency from "./fixtures/simulation_emergency.json";
import simRecovery from "./fixtures/simulation_recovery.json";
import highRisk from "./fixtures/scenario_simulated_high_risk.json";

const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const sim = (doc: unknown): View => {
  const r = parseSimulation(doc);
  if (!r.ok) throw new Error(r.error);
  return fromSimulation(r.value);
};
const raw = (doc: unknown) => doc as SimulationDoc;
const panel = (view: View, range: RangeHours) => text(renderToStaticMarkup(
  <RiverRainPanel river={view.river} rain={view.rain} rainLabel="Rain per hour (SIMULATED LIVE scenario)"
    range={range} onRange={() => undefined} />));

describe("readings come from the backend", () => {
  it("plots every backend reading with its own timestamp, 15 minutes apart", () => {
    for (const doc of [simStart, simEmergency, simRecovery]) {
      const view = sim(doc);
      expect(view.river.points.map((p) => p.v)).toEqual(raw(doc).readings.map((r) => r.level_m));
      const steps = view.river.points.slice(1).map((p, i) => p.t - view.river.points[i].t);
      expect(new Set(steps)).toEqual(new Set([15 * 60_000]));
    }
  });

  it("receives changing readings as the simulation advances", () => {
    const a = sim(simStart), b = sim(simEmergency), c = sim(simRecovery);
    const last = (v: View) => v.river.points[v.river.points.length - 1];
    expect(last(b).t).toBeGreaterThan(last(a).t);
    expect(last(c).t).toBeGreaterThan(last(b).t);
    expect(last(b).v).toBeGreaterThan(75.6);           // EMERGENCY: above the TEST danger level
    expect(last(c).v).toBeLessThan(74.8);              // RECOVERY: back below the recovery level
    expect(Math.max(...a.river.points.map((p) => p.v)) - Math.min(...a.river.points.map((p) => p.v)))
      .toBeGreaterThan(0.05);                            // NORMAL fluctuates: not a flat line
  });

  it("uses simulated rain for the simulation and Open-Meteo/scenario rain otherwise", () => {
    const s = sim(simEmergency);
    expect(s.rain.map((r) => r.v)).toEqual((raw(simEmergency).rainfall ?? []).map((r) => r.mm));
    const r = parseDashboard(highRisk);
    if (!r.ok) throw new Error(r.error);
    const scenario = fromDashboard((r as { ok: true; value: DashboardDoc }).value, "scenario");
    const hourly = ((highRisk as unknown as DashboardDoc).weather?.hourly ?? []).filter((h) => typeof h.precipitation_mm === "number");
    expect(scenario.rain).toHaveLength(hourly.length);
  });
});

describe("time ranges and statistics", () => {
  const view = sim(simEmergency);
  it.each([[1, 5], [6, 25], [12, 49], [24, 97]] as const)("the %i h range holds %i readings", (h, n) => {
    expect(windowPoints(view.river.points, h)).toHaveLength(n);
  });

  it("shows latest, minimum, maximum and change computed from the shown readings", () => {
    const pts = windowPoints(view.river.points, 6);
    const st = windowStats(pts)!;
    expect(st.latest).toBe(pts[pts.length - 1].v);
    expect(st.min).toBe(Math.min(...pts.map((p) => p.v)));
    const t = panel(view, 6);
    expect(t).toContain(`Latest ${fmtNum(st.latest, "m", 2)}`);
    expect(t).toContain(`Min ${fmtNum(st.min, "m", 2)}`);
    expect(t).toContain(`Max ${fmtNum(st.max, "m", 2)}`);
    expect(t).toContain(`Change ${st.change >= 0 ? "+" : "−"}${Math.abs(st.change).toFixed(2)} m`);
  });
});

describe("thresholds and crossings", () => {
  it("marks the warning and danger crossings of the actual readings", () => {
    const view = sim(simEmergency);
    const crossings = thresholdCrossings(windowPoints(view.river.points, 24), view.river.warning, view.river.danger);
    expect(crossings.map((c) => `${c.direction}:${c.threshold}`)).toEqual(expect.arrayContaining(["up:warning", "up:danger"]));
    for (const c of crossings) {
      const i = view.river.points.findIndex((p) => p.t === c.t);
      const level = c.threshold === "danger" ? view.river.danger! : view.river.warning!;
      expect((view.river.points[i - 1].v < level) !== (view.river.points[i].v < level)).toBe(true);
    }
    const t = panel(view, 24);
    expect(t).toContain("Warning 75.00 m (TEST)");
    expect(t).toContain("Danger 75.60 m (TEST)");
    expect(t).toContain(`Crossings ${crossings.length}`);
  });

  it("zooms to the data and names thresholds that are out of view instead of flattening the line", () => {
    const view = sim(simStart);
    const pts = windowPoints(view.river.points, 6);
    const { domain, shown } = levelDomain(pts, [view.river.warning, view.river.danger, view.river.recovery]);
    expect(domain[1] - domain[0]).toBeLessThan(0.6);
    expect(shown).toEqual([]);
    expect(panel(view, 6)).toMatch(/Danger 75\.60 m is \d+\.\d\d m above the highest reading in view/);
  });

  it("shows the recovery level once the river is near it", () => {
    const view = sim(simRecovery);
    expect(view.river.recovery).toBe(74.8);
    expect(panel(view, 12)).toContain("Recovery 74.80 m (TEST)");
  });
});

describe("pages", () => {
  it("shows the combined panel on Overview and Live Monitoring, and threshold details on Disaster State", () => {
    const view = sim(simRecovery);
    for (const page of ["overview", "monitoring"] as const) {
      const t = text(renderToStaticMarkup(<DashboardBody view={view} page={page} />));
      expect(t).toContain("River level and rainfall");
      expect(t).toContain("Rain per hour (SIMULATED LIVE scenario)");
    }
    const st = text(renderToStaticMarkup(<DashboardBody view={view} page="state" />));
    expect(st).toContain("River vs thresholds");
    expect(st).toContain("recovery 74.80 m (TEST / RESEARCH)");
    const tr = raw(simRecovery).transitions;
    expect(tr.map((x) => x.threshold?.name)).toEqual(["warning", "danger", "recovery"]);
    expect(st).toContain("Threshold: danger 75.60 m (TEST / RESEARCH)");
  });
});
