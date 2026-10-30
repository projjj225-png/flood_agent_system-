// Tests for the FloodGuard AI dashboard. Fixtures are real output of the Python
// backend (scripts/make_frontend_fixtures.py): the continuous simulation engine and
// the scenario test cases. Every assertion compares what the page shows with the
// value the backend computed, so the cards cannot drift from the backend.
import { afterEach, describe, expect, it, vi } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import App, { DashboardBody, SourceBanner } from "../App";
import { PAGES, type PageId } from "../components/pages";
import {
  controlSimulation, fetchView, fromDashboard, fromSimulation, parseSimulation,
  type SimulationDoc, type View,
} from "../model";
import { parseDashboard } from "../api";
import type { DashboardDoc } from "../types";
import { fmtNum } from "../format";
import simStart from "./fixtures/simulation_start.json";
import simEmergency from "./fixtures/simulation_emergency.json";
import simRecovery from "./fixtures/simulation_recovery.json";
import highRisk from "./fixtures/scenario_simulated_high_risk.json";
import staleRiver from "./fixtures/scenario_stale_river.json";
import conflicting from "./fixtures/scenario_conflicting_evidence.json";
import missingWeather from "./fixtures/scenario_missing_weather.json";
import realStale from "./fixtures/real_stale.json";

const clone = <T,>(x: T): T => JSON.parse(JSON.stringify(x));
const text = (html: string) => html.replace(/<[^>]+>/g, " ").replace(/&#x27;/g, "'").replace(/&gt;/g, ">").replace(/&lt;/g, "<").replace(/&amp;/g, "&").replace(/\s+/g, " ");
const sim = (doc: unknown): View => {
  const r = parseSimulation(doc);
  if (!r.ok) throw new Error(r.error);
  return fromSimulation(r.value);
};
const scenario = (doc: unknown): View => {
  const r = parseDashboard(doc);
  if (!r.ok) throw new Error(r.error);
  return fromDashboard(r.value, "scenario");
};
const page = (view: View, id: PageId) => text(renderToStaticMarkup(<DashboardBody view={view} page={id} />));
const raw = (doc: unknown) => doc as SimulationDoc;

afterEach(() => vi.unstubAllGlobals());

describe("simulation response validation", () => {
  it("accepts real simulation snapshots", () => {
    for (const doc of [simStart, simEmergency, simRecovery]) expect(parseSimulation(doc).ok).toBe(true);
  });

  it.each([
    ["not labelled SIMULATED LIVE", (d: any) => { d.data_label = "REAL DATA"; }],
    ["claiming real-world authority", (d: any) => { d.simulation.authorizes_real_world_actions = true; }],
    ["claiming to change the real state", (d: any) => { d.simulation.real_state_affected = true; }],
    ["granting a real-world authorization", (d: any) => { d.insights.real_world_authorization = "HUMAN_AUTHORIZATION_REQUIRED"; }],
    ["without insights", (d: any) => { delete d.insights; }],
    ["with a malformed history", (d: any) => { d.series = "lots"; }],
  ])("rejects a response %s", (_, mutate) => {
    const doc = clone(simEmergency) as any;
    mutate(doc);
    expect(parseSimulation(doc).ok).toBe(false);
  });

  it("surfaces backend errors", () => {
    expect(parseSimulation({ error: "internal error: RuntimeError" })).toEqual({ ok: false, error: "internal error: RuntimeError" });
  });
});

describe("overview shows backend-derived values (within five seconds)", () => {
  const doc = raw(simEmergency);
  const ins = doc.insights;
  const t = page(sim(simEmergency), "overview");

  it("shows the flood risk score and category", () => {
    expect(t).toContain(`${ins.risk_score.category} RISK`);
    expect(t).toContain(String(ins.risk_score.score));
    expect(t).toContain(ins.risk_score.summary);
  });

  it("shows the evidence trust score, separately from risk", () => {
    expect(t).toContain(ins.trust.label);
    expect(t).toContain(String(ins.trust.score));
    expect(t).toContain("not how dangerous the flood is");
  });

  it("shows the current state and why it was selected", () => {
    expect(t).toContain(ins.state.current);
    expect(t).toContain(ins.state.why);
  });

  it("shows which agents supplied evidence", () => {
    for (const card of Object.values(ins.agents)) {
      expect(t).toContain(card.agent);
      expect(t).toContain(card.status);
      expect(t).toContain(card.tells);
    }
  });

  it("shows which capabilities are allowed or denied", () => {
    for (const c of ins.capabilities) expect(t).toMatch(new RegExp(`${c.name}\\s+${c.status}`));
    expect(new Set(ins.capabilities.map((c) => c.status))).toEqual(new Set(["ALLOWED", "RESTRICTED"]));
    expect(t).toContain(ins.headline);
  });
});

describe("risk assessment page", () => {
  it("explains WHY THIS RISK LEVEL from the backend's own lines", () => {
    const ins = raw(simEmergency).insights;
    const t = page(sim(simEmergency), "risk");
    expect(t).toContain("Why this risk level?");
    for (const line of ins.risk_explanation.lines) {
      expect(t).toContain(line.label);
      expect(t).toContain(line.value);
    }
    expect(t).toContain(`Combined assessment: ${ins.risk_explanation.combined}`);
    for (const f of ins.risk_score.factors) expect(t).toContain(f.detail);
    expect(t).toContain("not official CWC flood-warning levels");
  });

  it("never shows a number for an indeterminate assessment and says what is missing", () => {
    for (const fixture of [staleRiver, conflicting, missingWeather]) {
      const view = scenario(fixture);
      const ins = view.insights!;
      expect(ins.risk_score.score).toBeNull();
      const t = page(view, "risk");
      expect(t).toContain("RISK: INDETERMINATE");
      expect(t).toContain("no defensible score");
      expect(t).toContain("What is missing for a defensible assessment");
      for (const m of ins.risk_explanation.missing) expect(t).toContain(m.reason);
      expect(t).not.toMatch(/INDETERMINATE[^.]*\b0\s*\/\s*100/);
    }
  });
});

describe("evidence intelligence page", () => {
  it("shows three agent cards with status, acceptance and reason", () => {
    const view = scenario(conflicting);
    const ins = view.insights!;
    const t = page(view, "evidence");
    for (const card of Object.values(ins.agents)) {
      expect(t).toContain(card.agent);
      expect(t).toContain(card.why);
      expect(t).toContain(card.accepted ? "Accepted." : "Not accepted.");
    }
    expect(ins.agents.river.status).toBe("CONFLICTING");
    expect(t).toContain("CONFLICTING");
  });

  it("shows the evidence summary counts and trust breakdown", () => {
    const view = scenario(staleRiver);
    const s = view.insights!.evidence_summary;
    const t = page(view, "evidence");
    expect(s.stale).toBe(1);
    expect(t).toMatch(new RegExp(`${s.accepted} Accepted`));
    expect(t).toMatch(new RegExp(`${s.stale} Stale`));
    for (const f of view.insights!.trust.factors) expect(t).toContain(f.label);
    for (const d of view.insights!.trust.deductions) expect(t).toContain(d.reason);
    expect(t).toContain("Capped at");
  });

  it("keeps simulated evidence labelled as such, never as verified", () => {
    const t = page(sim(simEmergency), "evidence");
    expect(t).toContain("NOT verified real-world evidence");
    expect(t).not.toMatch(/CWC verified/i);
  });

  it("keeps technical IDs in an expandable section", () => {
    const html = renderToStaticMarkup(<DashboardBody view={sim(simEmergency)} page="evidence" />);
    const id = raw(simEmergency).evidence_records[0].evidence_id!;
    const details = html.slice(html.indexOf("<details"));
    expect(details).toContain(id.slice(0, 12));
  });
});

describe("disaster state page", () => {
  it("highlights the current and previous state on the timeline", () => {
    const html = renderToStaticMarkup(<DashboardBody view={sim(simRecovery)} page="state" />);
    const st = raw(simRecovery).insights.state;
    expect(html).toMatch(new RegExp(`is-current[^>]*aria-current="step"[\\s\\S]{0,200}${st.current}`));
    expect(text(html)).toContain(`${st.last_transition!.from} → ${st.last_transition!.to}`);
    expect(text(html)).toContain("It cannot be set from this page");
  });

  it("lists every automatic transition with its reason", () => {
    const doc = raw(simRecovery);
    const t = page(sim(simRecovery), "state");
    expect(doc.transitions.map((x) => `${x.from}>${x.to}`)).toEqual(["NORMAL>ALERT", "ALERT>EMERGENCY", "EMERGENCY>RECOVERY"]);
    for (const tr of doc.transitions) expect(t).toContain(tr.reason!);
    expect(t).toContain("Simulation history only");
  });
});

describe("capability control page", () => {
  it("shows every backend decision and revocation in RECOVERY", () => {
    const ins = raw(simRecovery).insights;
    const t = page(sim(simRecovery), "capabilities");
    for (const c of ins.capabilities) {
      expect(t).toContain(c.name);
      expect(t).toContain(c.reason);
    }
    const evac = ins.capabilities.find((c) => c.capability === "propose_evacuation_plan")!;
    expect(evac.status).toBe("DENIED");
    expect(evac.change).toBe("REVOKED");
    expect(t).toContain("Revoked by the latest transition");
    expect(t).toContain("never executes evacuations, dispatches or public alerts");
  });

  it("changes with the state: emergency authorization only in EMERGENCY", () => {
    const status = (doc: unknown) => raw(doc).insights.capabilities.find((c) => c.capability === "request_human_emergency_authorization")!.status;
    expect(status(simStart)).toBe("DENIED");
    expect(status(simEmergency)).toBe("RESTRICTED");
    expect(status(simRecovery)).toBe("DENIED");
  });
});

describe("live monitoring page and charts", () => {
  it("derives every chart point from the backend history (nothing invented)", () => {
    const doc = raw(simEmergency);
    const view = sim(simEmergency);
    expect(view.river.points).toHaveLength(doc.readings.length);
    expect(view.risk).toHaveLength(doc.series.length);
    expect(view.trust.map((p) => p.v)).toEqual(doc.series.map((e) => e.trust_score));
    expect(view.states.map((s) => s.state)).toEqual(doc.series.map((e) => e.state));
    expect(view.river.warning).toBe(doc.simulation.thresholds.warning_level_m);
  });

  it("shows the latest reading with units and the simulation time", () => {
    const doc = raw(simEmergency);
    const t = page(sim(simEmergency), "monitoring");
    expect(t).toContain(fmtNum(doc.insights.agents.river.metrics.level_m as number, "m", 3));
    expect(t).toContain("Simulation time");
    expect(t).toContain("SIMULATED LIVE");
  });

  it("does not plot history for a single evaluation", () => {
    const view = scenario(highRisk);
    expect(view.risk).toEqual([]);
    expect(page(view, "monitoring")).toContain("has no history");
  });
});

describe("activity history and labelling", () => {
  it("shows simulation activity separately from the real history", () => {
    const doc = raw(simEmergency);
    const t = page(sim(simEmergency), "history");
    expect(t).toContain(doc.activity[0].text);
    expect(t).toContain("never the real audit log");
  });

  it("labels simulated and real sources", () => {
    expect(text(renderToStaticMarkup(<SourceBanner view={sim(simStart)} />))).toContain("never Central Water Commission measurements");
    const r = parseDashboard(realStale);
    expect(r.ok).toBe(true);
    const real = fromDashboard((r as { ok: true; value: DashboardDoc }).value, "real");
    expect(real.simulated).toBe(false);
    expect(text(renderToStaticMarkup(<SourceBanner view={real} />))).toContain("Real data only");
  });

  it("renders every page for every fixture, and degrades gracefully without insights", () => {
    const views = [sim(simStart), sim(simEmergency), sim(simRecovery), scenario(highRisk), scenario(staleRiver),
      scenario(conflicting), scenario(missingWeather)];
    for (const v of views) for (const p of PAGES) expect(() => page(v, p.id)).not.toThrow();
    const old = fromDashboard((parseDashboard(realStale) as { ok: true; value: DashboardDoc }).value, "real");
    expect(old.insights).toBeNull();
    expect(page(old, "overview")).toContain("Assessment details are unavailable");
  });

  it("renders the shell without any way to set a disaster state", () => {
    const html = renderToStaticMarkup(<App />);
    expect(html).toContain("FloodGuard AI");
    for (const p of PAGES) expect(html).toContain(p.label.replace("&", "&amp;"));
    expect(html).not.toMatch(/name="state"|Set state|Assign state/i);
  });
});

describe("API calls", () => {
  it("sends only simulation controls, never a disaster state", async () => {
    const fetchMock = vi.fn(async () => new Response(JSON.stringify(simEmergency), { status: 200 }));
    vi.stubGlobal("fetch", fetchMock);
    const r = await controlSimulation("scenario", { scenario: "ALERT" });
    expect(r.ok).toBe(true);
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toMatch(/\/simulation\/control$/);
    expect(init.method).toBe("POST");
    expect(Object.keys(JSON.parse(String(init.body))).sort()).toEqual(["action", "scenario"]);
  });

  it("loads the simulation view and reports backend errors", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify(simStart), { status: 200 })));
    const ok = await fetchView("simulation", "");
    expect(ok.ok && ok.value.source).toBe("simulation");
    vi.stubGlobal("fetch", vi.fn(async () => new Response(JSON.stringify({ error: "scenario must be one of X" }), { status: 400 })));
    expect(await controlSimulation("scenario", { scenario: "nope" })).toEqual({ ok: false, error: "scenario must be one of X", status: 400 });
  });
});
