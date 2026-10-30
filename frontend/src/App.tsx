// FloodGuard AI dashboard shell: navigation, data source, simulation controls and
// polling. Pages only render backend values (see src/model.ts).
import { useCallback, useEffect, useRef, useState, type ReactNode } from "react";
import { fetchAnalysis, fetchHealth, POLL_SECONDS } from "./api";
import {
  controlSimulation, fetchView, fromSimulation, SCENARIO_TEST_CASES, SIM_POLL_MS,
  type ControlAction, type Source, type View,
} from "./model";
import type { AnalysisDoc } from "./types";
import { fmtTime } from "./format";
import { AnalyticsPage, PAGES, renderPage, type PageId } from "./components/pages";
import { Icon, Tag } from "./components/ui";

const SOURCES: { id: Source; label: string; hint: string }[] = [
  { id: "simulation", label: "Live simulation", hint: "Continuous SIMULATED LIVE demonstration" },
  { id: "scenario", label: "Test cases", hint: "Fixed simulated scenarios (stale, missing, conflicting evidence…)" },
  { id: "real", label: "Real CWC data", hint: "Live CWC / Open-Meteo / TomTom sources" },
];

function pageFromHash(): PageId {
  const id = typeof window === "undefined" ? "" : window.location.hash.replace(/^#\/?/, "");
  return (PAGES.find((p) => p.id === id)?.id ?? "overview") as PageId;
}

/** The page body for a loaded view. Exported for tests. */
export function DashboardBody({ view, page, analysis = null, analysisError = null }: {
  view: View; page: PageId; analysis?: AnalysisDoc | null; analysisError?: string | null;
}) {
  return <>{renderPage(page, view, analysis, analysisError)}</>;
}

export function SourceBanner({ view }: { view: View }) {
  return (
    <div className={`source-banner ${view.simulated ? "is-sim" : "is-real"}`} role="note">
      <Tag kind={view.source === "simulation" ? "sim" : view.simulated ? "test" : "real"}>{view.source === "simulation" ? "SIMULATED LIVE" : view.label}</Tag>
      <span>
        <strong>{view.title}.</strong> {view.subtitle}{" "}
        {view.simulated
          ? "Fabricated evidence and research/test thresholds: never Central Water Commission measurements, never saved to the real state record, never a real-world authorization."
          : "Real data only; missing or stale evidence is shown as such and never replaced."}
      </span>
    </div>
  );
}

function SimControls({ view, busy, onControl }: { view: View; busy: boolean; onControl: (a: ControlAction, extra?: { scenario?: string; speed?: number }) => void }) {
  const sim = view.sim;
  if (!sim) return null;
  return (
    <div className="sim-controls" aria-label="Simulation controls">
      <div className="sim-clock">
        <span className={`live-dot ${sim.running ? "on" : ""}`} aria-hidden />
        <div>
          <span className="sim-k">Simulation time</span>
          <strong>{fmtTime(sim.sim_time)}</strong>
          <span className="sim-k">{sim.running ? (sim.next_tick_in_seconds !== null ? `next reading in ${Math.ceil(sim.next_tick_in_seconds)} s` : "running") : "paused"} · step {sim.tick}</span>
        </div>
      </div>
      <button className="btn primary" disabled={busy} onClick={() => onControl(sim.running ? "pause" : "resume")}>
        <Icon name={sim.running ? "pause" : "play"} size={14} /> {sim.running ? "Pause" : "Start"}
      </button>
      <button className="btn" disabled={busy} onClick={() => onControl("reset")} title="Restart from NORMAL with fresh simulated evidence (the real state is not touched)">
        <Icon name="reset" size={14} /> Reset
      </button>
      <label className="ctl">Conditions
        <select value={sim.selected_scenario} disabled={busy} onChange={(e) => onControl("scenario", { scenario: e.target.value })}>
          {sim.scenarios.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}
        </select>
      </label>
      <label className="ctl">Speed
        <select value={sim.speed} disabled={busy} onChange={(e) => onControl("speed", { speed: Number(e.target.value) })}>
          {sim.speeds.map((s) => <option key={s} value={s}>{s}× ({(sim.tick_seconds / s).toFixed(0)} s/step)</option>)}
        </select>
      </label>
      {sim.last_error && <span className="ctl-error" role="status"><Icon name="alert" size={13} /> Last engine error ({fmtTime(sim.last_error.at)}): {sim.last_error.error}</span>}
      <span className="sim-status" title="Conditions drive the simulated river and rain; the disaster state follows only through the backend rules.">
        <span className="src-badge src-simulated">SIMULATED LIVE</span> Active: <strong>{sim.scenarios.find((x) => x.name === sim.active_conditions)?.label ?? sim.active_conditions}</strong>
      </span>
    </div>
  );
}

type Load = { view: View | null; error: string | null; loading: boolean };

export default function App() {
  const [source, setSource] = useState<Source>("simulation");
  const [scenario, setScenario] = useState(SCENARIO_TEST_CASES[0].name);
  const [page, setPage] = useState<PageId>(pageFromHash);
  const [load, setLoad] = useState<Load>({ view: null, error: null, loading: true });
  const [online, setOnline] = useState<boolean | null>(null);
  const [busy, setBusy] = useState(false);
  const [analysis, setAnalysis] = useState<{ doc: AnalysisDoc | null; error: string | null }>({ doc: null, error: null });
  const inFlight = useRef<AbortController | null>(null);
  const generation = useRef(0);  // responses for an older source/scenario are dropped

  const refresh = useCallback(async () => {
    if (inFlight.current) return;  // never two requests at once
    const controller = new AbortController();
    inFlight.current = controller;
    const gen = generation.current;
    try {
      const result = await fetchView(source, scenario, controller.signal);
      if (gen !== generation.current) return;
      setOnline(result.ok || result.status !== undefined);
      setLoad((l) => result.ok
        ? { view: result.value, error: null, loading: false }
        : { view: l.view, error: result.error, loading: false });
    } catch (err) {
      if ((err as Error).name !== "AbortError" && gen === generation.current)
        setLoad((l) => ({ ...l, loading: false, error: String(err) }));
    } finally {
      if (inFlight.current === controller) inFlight.current = null;
    }
  }, [source, scenario]);

  useEffect(() => {
    generation.current += 1;
    inFlight.current?.abort();
    inFlight.current = null;
    setLoad({ view: null, error: null, loading: true });
    void refresh();
    if (source === "scenario") return;  // a test case is one fixed evaluation
    const id = window.setInterval(() => void refresh(), source === "simulation" ? SIM_POLL_MS : POLL_SECONDS * 1000);
    return () => window.clearInterval(id);
  }, [source, scenario, refresh]);

  useEffect(() => {
    const onHash = () => setPage(pageFromHash());
    window.addEventListener("hashchange", onHash);
    void fetchHealth().then(setOnline).catch(() => setOnline(false));
    return () => window.removeEventListener("hashchange", onHash);
  }, []);

  useEffect(() => {
    if (page !== "analytics" || analysis.doc) return;
    const controller = new AbortController();
    void fetchAnalysis(controller.signal)
      .then((r) => setAnalysis(r.ok ? { doc: r.value, error: null } : { doc: null, error: r.error }))
      .catch(() => undefined);
    return () => controller.abort();
  }, [page, analysis.doc]);

  const onControl = useCallback(async (action: ControlAction, extra: { scenario?: string; speed?: number } = {}) => {
    setBusy(true);
    const r = await controlSimulation(action, extra);
    setBusy(false);
    if (r.ok && source === "simulation") setLoad({ view: fromSimulation(r.value), error: null, loading: false });
    else if (!r.ok) setLoad((l) => ({ ...l, error: r.error }));
  }, [source]);

  const go = (id: PageId) => { window.location.hash = `/${id}`; setPage(id); };
  const view = load.view && load.view.source === source ? load.view : null;
  const state = view?.insights?.state.current;

  let body: ReactNode;
  if (view) body = <DashboardBody view={view} page={page} analysis={analysis.doc} analysisError={analysis.error} />;
  else if (page === "analytics") body = <AnalyticsPage analysis={analysis.doc} error={analysis.error} />;
  else body = <div className="empty big" role="status">{load.loading ? "Loading the latest evaluation…" : load.error ? "Data unavailable." : "No data."}</div>;

  return (
    <div className="shell">
      <aside className="sidebar" aria-label="Sections">
        <div className="brand">
          <span className="brand-mark" aria-hidden><Icon name="river" size={20} /></span>
          <div><strong>FloodGuard AI</strong><span>Flood command centre</span></div>
        </div>
        <nav>
          {PAGES.map((p) => (
            <a key={p.id} href={`#/${p.id}`} className={page === p.id ? "on" : ""} aria-current={page === p.id ? "page" : undefined}
              onClick={(e) => { e.preventDefault(); go(p.id); }}>
              <Icon name={p.icon} size={16} /> <span>{p.label}</span>
            </a>
          ))}
        </nav>
        <p className="sidebar-foot">Cauvery River · Upper Anicut, Tamil Nadu<br />Research prototype · decision support only</p>
      </aside>
      <div className="main">
        <header className="topbar">
          <div className="segmented" role="radiogroup" aria-label="Data source">
            {SOURCES.map((s) => (
              <button key={s.id} role="radio" aria-checked={source === s.id} title={s.hint}
                className={source === s.id ? "on" : ""} onClick={() => setSource(s.id)}>{s.label}</button>
            ))}
          </div>
          {source === "scenario" && (
            <label className="ctl">Test case
              <select value={scenario} onChange={(e) => setScenario(e.target.value)}>
                {SCENARIO_TEST_CASES.map((s) => <option key={s.name} value={s.name}>{s.label}</option>)}
              </select>
            </label>
          )}
          <div className="topbar-status">
            {state && <span className={`state-chip state-${state.toLowerCase()}`}>{state}</span>}
            <span className={`api ${online ? "ok" : online === false ? "down" : ""}`}>
              <i aria-hidden /> API {online === null ? "…" : online ? "connected" : "unreachable"}
            </span>
            <span className="updated">Updated {view ? fmtTime(view.generatedAt) : "—"}</span>
          </div>
        </header>
        {view && page !== "analytics" && <SourceBanner view={view} />}
        {view?.sim && <SimControls view={view} busy={busy} onControl={onControl} />}
        {load.error && (
          <div className="error" role="alert"><Icon name="alert" size={16} />
            <span><strong>{view ? "Latest refresh failed — showing the previous evaluation." : "Data unavailable."}</strong> {load.error}</span>
          </div>
        )}
        <main>{body}</main>
        <footer className="foot">
          Research prototype. Score bands and thresholds are research/test values, not official CWC flood-warning levels.
          The system never executes evacuations, dispatches or public alerts; real-world capabilities can at most be proposed to a human.
        </footer>
      </div>
    </div>
  );
}
