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
    const html = renderToStaticMarkup(<App />) + render(realStale) + render(simulated);
    const buttons = [...html.matchAll(/<button[^>]*>([\s\S]*?)<\/button>/g)].map((m) => text(m[1]).trim());
    // Mode switch and a read-only reload (shown as "Loading…" before the first fetch).
    expect(buttons.sort()).toEqual(["Loading…", "Real data", "Simulated demo"]);
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
