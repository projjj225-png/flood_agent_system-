import { useCallback, useEffect, useRef, useState } from "react";
import { fetchDashboard, fetchHealth, fetchScenarios, fetchSequence, POLL_SECONDS } from "./api";
import type { DashboardDoc, Mode, ScenarioList, SequenceDoc } from "./types";
import { fmtTime } from "./format";
import { Badge, SimulatedBanner, Unavailable } from "./components/common";
import {
  AuditSection, EvidenceSection, KpiCards, RiskSection, RiverSection, SafetySection, StateSection,
  TimelineSection, WeatherSection,
} from "./components/sections";

type Load = { doc: DashboardDoc | null; error: string | null; loading: boolean; failedAt: string | null };

export function Header({ online, doc, mode }: { online: boolean | null; doc: DashboardDoc | null; mode: Mode }) {
  return (
    <header className="topbar">
      <div className="brand">
        <h1>Cauvery Flood Disaster Management System</h1>
        <p>Upper Anicut, Tamil Nadu · research prototype · decision support only</p>
      </div>
      <div className="status">
        <Badge tone={online === null ? "neutral" : online ? "ok" : "danger"}>
          API {online === null ? "checking…" : online ? "connected" : "unreachable"}
        </Badge>
        <Badge tone={mode === "simulated" ? "sim" : "info"}>{mode === "simulated" ? "SIMULATED DATA" : "REAL DATA"}</Badge>
        <span className="updated">
          Last update: {doc ? fmtTime(doc.generated_at) : "—"}
          {doc && typeof doc.cache_age_seconds === "number" && doc.cache_age_seconds > 0 && ` (cached ${Math.round(doc.cache_age_seconds)} s)`}
          <br />Timezone: Asia/Kolkata (IST, UTC+05:30)
        </span>
      </div>
    </header>
  );
}

export function Dashboard({ doc }: { doc: DashboardDoc }) {
  return (
    <>
      {doc.is_simulated && (
        <SimulatedBanner text={`Scenario "${doc.scenario}" (${doc.scenario_description ?? ""}) starting in ${doc.start_state}. Fabricated river and weather data, TEST thresholds; never real conditions, never saved to the real state record.`} />
      )}
      <KpiCards doc={doc} />
      <div className="layout">
        <WeatherSection doc={doc} />
        <RiverSection doc={doc} />
        <RiskSection doc={doc} />
        <StateSection doc={doc} />
        <SafetySection doc={doc} />
        <EvidenceSection doc={doc} />
        <AuditSection doc={doc} />
      </div>
    </>
  );
}

const STATES = ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"];

/** Optional starting view from the URL (?mode=simulated&scenario=...&state=...), for
 *  shareable demo links. Real mode stays the default; the backend validates names. */
function initialView() {
  const q = typeof window === "undefined" ? new URLSearchParams() : new URLSearchParams(window.location.search);
  const scenario = q.get("scenario") ?? "";
  return {
    mode: (q.get("mode") === "simulated" ? "simulated" : "real") as Mode,
    scenario: /^[a-z0-9-]{1,64}$/.test(scenario) ? scenario : "rising-river-increasing-rain",
    state: STATES.includes(q.get("state") ?? "") ? (q.get("state") as string) : "NORMAL",
  };
}

export default function App() {
  const [initial] = useState(initialView);
  const [mode, setMode] = useState<Mode>(initial.mode);  // real unless asked; never switched automatically
  const [scenario, setScenario] = useState(initial.scenario);
  const [startState, setStartState] = useState(initial.state);
  const [scenarios, setScenarios] = useState<ScenarioList | null>(null);
  const [online, setOnline] = useState<boolean | null>(null);
  const [load, setLoad] = useState<Load>({ doc: null, error: null, loading: true, failedAt: null });
  const [seq, setSeq] = useState<{ doc: SequenceDoc | null; error: string | null }>({ doc: null, error: null });
  const inFlight = useRef<AbortController | null>(null);

  const refresh = useCallback(async () => {
    if (inFlight.current) return;  // no duplicate requests
    const controller = new AbortController();
    inFlight.current = controller;
    setLoad((l) => ({ ...l, loading: true }));
    try {
      const [health, result] = await Promise.all([
        fetchHealth(controller.signal).catch(() => false),
        fetchDashboard(mode, scenario, startState, controller.signal),
      ]);
      setOnline(health);
      setLoad((l) => result.ok
        ? { doc: result.value, error: null, loading: false, failedAt: null }
        // Keep the last good document but say clearly that it is old.
        : { doc: l.doc?.mode === mode ? l.doc : null, error: result.error, loading: false, failedAt: new Date().toISOString() });
    } catch (err) {
      if ((err as Error).name !== "AbortError") setLoad((l) => ({ ...l, loading: false, error: String(err) }));
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
    }
  }, [mode, scenario, startState]);

  // Changing mode or scenario: drop the old document so modes never mix on screen.
  useEffect(() => {
    inFlight.current?.abort();
    inFlight.current = null;
    setLoad({ doc: null, error: null, loading: true, failedAt: null });
    void refresh();
    if (mode !== "real") return;  // simulated documents are fixed; no polling
    const id = window.setInterval(() => void refresh(), POLL_SECONDS * 1000);
    return () => window.clearInterval(id);
  }, [mode, refresh]);

  useEffect(() => {
    if (mode !== "simulated") return;
    const controller = new AbortController();
    if (!scenarios) void fetchScenarios(controller.signal).then((r) => r.ok && setScenarios(r.value)).catch(() => undefined);
    if (!seq.doc) void fetchSequence("flood-cycle", controller.signal)
      .then((r) => setSeq(r.ok ? { doc: r.value, error: null } : { doc: null, error: r.error }))
      .catch(() => undefined);
    return () => controller.abort();
  }, [mode, scenarios, seq.doc]);

  const doc = load.doc && load.doc.mode === mode ? load.doc : null;
  return (
    <div className={`app ${mode === "simulated" ? "is-sim" : ""}`}>
      <Header online={online} doc={doc} mode={mode} />
      <nav className="modebar" aria-label="Data mode">
        <div className="segmented" role="radiogroup" aria-label="Mode">
          <button role="radio" aria-checked={mode === "real"} className={mode === "real" ? "on" : ""} onClick={() => setMode("real")}>Real data</button>
          <button role="radio" aria-checked={mode === "simulated"} className={mode === "simulated" ? "on sim" : ""} onClick={() => setMode("simulated")}>Simulated demo</button>
        </div>
        {mode === "simulated" && (
          <>
            <label>Scenario
              <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
                {(scenarios?.scenarios ?? [{ name: scenario, description: "" }]).map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              </select>
            </label>
            <label>Start state
              <select value={startState} onChange={(e) => setStartState(e.target.value)}>
                {(scenarios?.start_states ?? ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"]).map((s) => <option key={s}>{s}</option>)}
              </select>
            </label>
          </>
        )}
        <button className="ghost" onClick={() => void refresh()} disabled={load.loading} title="Reload the read-only dashboard data">
          {load.loading ? "Loading…" : "Reload data"}
        </button>
        <span className="small muted">{mode === "real" ? `Refreshes every ${POLL_SECONDS} s (backend caches 5 min)` : "Simulated results are fixed; no polling"}</span>
      </nav>

      <main>
        {load.error && (
          <div className="alert" role="alert">
            <strong>{doc ? "Latest refresh failed — showing the previous data." : "Data unavailable."}</strong> {load.error}
            {load.failedAt && <> ({fmtTime(load.failedAt)})</>}
          </div>
        )}
        {!doc && load.loading && <Unavailable>Loading {mode} data…</Unavailable>}
        {!doc && !load.loading && !load.error && <Unavailable>No data.</Unavailable>}
        {doc && <Dashboard doc={doc} />}
        {mode === "simulated" && <TimelineSection seq={seq.doc} error={seq.error} />}
      </main>
      <footer className="foot">
        Research prototype. Prototype thresholds are not official flood-warning levels. No real-world
        action can be taken from this page; every real-world capability needs human approval in the backend.
      </footer>
    </div>
  );
}
