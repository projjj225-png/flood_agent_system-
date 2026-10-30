// Frontend tests. Fixtures are real output of the Python adapter
// (src/dashboard_service.py) generated offline; see README.
import { describe, expect, it } from "vitest";
import { renderToStaticMarkup } from "react-dom/server";
import { parseDashboard, parseSequence } from "../api";
import { fmtNum, fmtTime, humanize } from "../format";
import App, { Dashboard } from "../App";
import { TimelineSection } from "../components/sections";
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
