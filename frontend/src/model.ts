// Data model for the FloodGuard AI dashboard.
//
// Every value shown comes from the Python backend: the risk score, trust score,
// agent cards, state view and capability cards are built by src/insights.py, and
// the histories by src/simulation_engine.py. This module only validates responses
// and reshapes them into one `View` the pages render. Nothing is calculated here
// and nothing missing is filled in.
import { API_BASE, fetchDashboard, type Result } from "./api";
import type { DashboardDoc } from "./types";
import { epoch, isNum } from "./format";

export type Source = "simulation" | "scenario" | "real";
export const SIM_LABEL = "SIMULATED LIVE";
export const SIM_POLL_MS = 2000;

// ---- backend shapes (src/insights.py) ------------------------------------------------

export interface RiskFactor {
  key: string; label: string; index: number | null; level: string | null;
  value_text: string; detail: string; rule_id: string | null; source: string;
  direction: "raises" | "lowers" | "unknown"; counts_toward_score: boolean; context_only?: boolean;
}
export interface RiskScore {
  available: boolean; score: number | null; category: string; band: [number, number] | null;
  bands: Record<string, [number, number]>; confidence?: string | null; is_simulated: boolean;
  assessed_at?: string | null; method: string; factors: RiskFactor[]; adjustments: string[];
  missing: { item: string; reason: string }[]; summary: string; dominant_factor?: string | null;
  official_thresholds: boolean; rules_fired?: string[]; blocking_reasons?: string[];
}
export interface TrustFactor { key: string; label: string; score: number; weight: number; points: number; deduction: number; detail: string }
export interface TrustAgent {
  agent: string; present: boolean; score: number | null; evidence_class: string; accepted: boolean;
  notes: string[]; authenticity: number | null; freshness: number | null; quality: number | null;
  timestamp_validity: number | null; provenance: number | null;
}
export interface Trust {
  score: number; raw_score: number; band: string; label: string; is_simulated: boolean;
  simulated_note: string | null; factors: TrustFactor[]; agents: Record<string, TrustAgent>;
  deductions: { factor: string; points: number; reason: string }[]; caps: { cap: number; reason: string }[];
  bands: Record<string, [number, number]>; method: string;
  source_reliability: number; freshness: number; completeness: number; consistency: number;
}
export type AgentStatus = "AVAILABLE" | "STALE" | "MISSING" | "CONFLICTING";
export interface Route { id: string; name?: string | null; role?: string | null; condition?: string | null; passable?: boolean | null; reason?: string | null }
export interface AgentCard {
  agent: string; status: AgentStatus; accepted: boolean; is_simulated: boolean; tells: string; why: string;
  evidence_class: string; metrics: Record<string, unknown>; source: string; trust_score: number | null;
  evidence_id: string | null; reasons: string[];
}
export interface StateView {
  current: string; why: string; evaluated_from?: string | null; previous?: string | null; proposed_next?: string | null;
  transition_this_evaluation: boolean; outcome?: string | null; condition?: string | null;
  condition_text?: string | null; meaning?: string | null; reason?: string | null; reasons: string[];
  progress?: {
    to: string; observations?: number | null; required_observations?: number | null;
    span_hours?: number | null; required_hours?: number | null;
    traffic_required?: string | null; traffic_status?: string | null;
  } | null;
  traffic_hold?: string | null;
  last_transition?: { from: string; to: string; kind?: string; at?: string; condition?: string } | null;
  entered_at?: string | null; evaluated_at?: string | null; observed_at?: string | null;
  evidence_used: { river_evidence_id?: string | null; risk_assessment_id?: string | null; risk_level?: string | null; supporting_evidence_ids: string[] };
  next_rules: { to: string; kind: string; text: string }[];
  threshold_check?: ThresholdCheck | null;
  scope?: string | null; applies_to?: string | null; order: string[]; selection: string;
}
export interface ThresholdCheck {
  configured: boolean; status?: string | null; level_m: number | null; warning_level_m: number | null;
  danger_level_m: number | null; recovery_level_m: number | null; below_recovery_level?: boolean | null;
  official: boolean; label: string;
}
export type CapStatus = "ALLOWED" | "RESTRICTED" | "DENIED";
export interface CapabilityCard {
  capability: string; name: string; purpose: string; status: CapStatus; reason: string;
  reason_code?: string | null; evidence_requirements: string; human_approval_required: boolean;
  capability_class?: string | null; state: string; permitted_states: string[];
  change: "GRANTED" | "REVOKED" | null; executes_actions: false;
}
export interface Insights {
  error?: string;
  is_simulated: boolean; headline: string; risk_score: RiskScore; trust: Trust;
  risk_explanation: {
    lines: { label: string; value: string; detail: string; level: string | null; direction: string; context_only: boolean }[];
    combined: string; category: string; raising: string[]; lowering: string[];
    missing: { item: string; reason: string }[]; adjustments: string[];
    rules: { rule_id: string; level: string | null; description: string }[];
    trace: string[]; uncertainties: string[];
  };
  agents: { weather: AgentCard; river: AgentCard; traffic: AgentCard };
  evidence_summary: {
    accepted: number; rejected: number; stale: number; missing: number; conflicting: number;
    records: number; records_by_class: Record<string, number>; claims_by_status: Record<string, number>;
    reverification: { status?: string | null; outcome?: string | null; attempts?: number | null };
    real_world_sufficient: boolean; deny_reasons: string[];
  };
  state: StateView; capabilities: CapabilityCard[];
  real_world_authorization?: string | null; disclaimer: string;
}

// ---- simulation document (src/simulation_engine.py) ----------------------------------

export interface SeriesEntry {
  t: string; tick: number; observation_id: string; level_m: number; rate_m_per_hour: number | null;
  rain_mm: number | null; rain_24h_mm: number | null; forecast_mm: number | null;
  risk_score: number | null; risk_category: string; trust_score: number; trust_band: string;
  state: string; condition: string | null; outcome: string | null; scenario: string;
}
export interface Transition {
  at?: string | null; evaluated_at?: string | null; tick?: number; from: string; to: string;
  kind?: string | null; condition?: string | null; reason?: string | null; risk_category?: string | null;
  risk_score?: number | null; trust_score?: number | null; evidence_id?: string | null;
  supporting_evidence_ids?: string[]; persistence?: { observations?: number | null; span_hours?: number | null };
  capabilities_granted?: string[]; capabilities_revoked?: string[]; scenario?: string | null;
  river_level_m?: number | null; detail?: string | null;
  threshold?: { name: string; level_m: number | null; label?: string | null; official?: boolean } | null;
}
export interface Activity { sim_time?: string | null; wall_time?: string | null; kind: string; text: string }
export interface ScenarioOption { name: string; label: string; description: string }
export interface SimulationInfo {
  label: string; session_id: string; running: boolean; speed: number; speeds: number[];
  tick_seconds: number; interval_seconds: number; simulated_minutes_per_tick: number;
  next_tick_in_seconds: number | null; tick: number; sim_time: string; started_sim_time: string;
  selected_scenario: string; active_conditions: string; scenarios: ScenarioOption[];
  cycle_schedule: { from_hour: number; scenario: string }[]; station_id: string; twin_of?: string | null;
  thresholds: { warning_level_m: number; danger_level_m: number; reference?: string; official: false };
  state_record_scope: string; state_record_persisted: false; real_state_affected: false;
  authorizes_real_world_actions: false; disclaimer: string;
  last_error?: { at: string; error: string } | null;
}
export interface EvidenceRecordSummary { evidence_id?: string | null; kind?: string | null; evidence_class?: string | null; observed_at?: string | null; validation?: string | null; findings?: string[] }
export interface ClaimSummary { claim_id?: string | null; status?: string | null; statement?: string | null }
export interface SimulationDoc {
  schema_version: "2.0"; kind: "simulation"; is_simulated: true; data_label: string; generated_at: string;
  insights: Insights; simulation: SimulationInfo;
  latest_observation: { observation_id: string; observed_at: string; level_m: number; station_id: string; rain_mm_last_hour: number | null; provenance: Record<string, unknown>; label: string };
  rainfall?: { t: string; mm: number; kind: string }[];
  series: SeriesEntry[]; readings: { observation_id: string; t: string; level_m: number; warmup: boolean }[];
  transitions: Transition[]; activity: Activity[];
  audit: { total_events: number; recent: { at: string; event: string }[]; storage: string };
  evidence_records: EvidenceRecordSummary[]; claims: ClaimSummary[];
  run?: Record<string, string | null>;
}

const isObj = (v: unknown): v is Record<string, unknown> => typeof v === "object" && v !== null && !Array.isArray(v);

/** Validate a /simulation response: it must be labelled SIMULATED LIVE and can never
 *  carry a real-world authority. Exported for tests. */
export function parseSimulation(json: unknown): Result<SimulationDoc> {
  if (!isObj(json)) return { ok: false, error: "Response is not a JSON object." };
  if (typeof json.error === "string") return { ok: false, error: json.error };
  if (json.schema_version !== "2.0" || json.kind !== "simulation")
    return { ok: false, error: "Not a simulation response." };
  const sim = json.simulation;
  if (json.is_simulated !== true || json.data_label !== SIM_LABEL || !isObj(sim) || sim.label !== SIM_LABEL)
    return { ok: false, error: "Simulation response is not labelled SIMULATED LIVE; not shown." };
  if (sim.authorizes_real_world_actions !== false || sim.real_state_affected !== false)
    return { ok: false, error: "Simulation response claims real-world authority; not shown." };
  const ins = json.insights;
  if (!isObj(ins) || !isObj(ins.risk_score) || !isObj(ins.trust) || !isObj(ins.state) || !Array.isArray(ins.capabilities))
    return { ok: false, error: "Simulation response has no insights." };
  if (ins.real_world_authorization !== "RESTRICTED")
    return { ok: false, error: "Simulated evidence can never authorize a real-world action; not shown." };
  for (const key of ["series", "readings", "transitions", "activity"])
    if (!Array.isArray(json[key])) return { ok: false, error: `Section "${key}" is malformed.` };
  return { ok: true, value: json as unknown as SimulationDoc };
}

// ---- the normalized view ---------------------------------------------------------------

export interface Point { t: number; v: number }
export interface View {
  source: Source; label: string; simulated: boolean;
  insights: Insights | null; insightsError: string | null;
  generatedAt: string; evaluatedAt: string | null;
  title: string; subtitle: string;
  river: { points: Point[]; warning: number | null; danger: number | null; recovery: number | null; official: boolean };
  rain: { t: number; v: number; kind: "observed" | "forecast" }[];
  rate: Point[]; risk: { t: number; v: number | null; category: string }[]; trust: Point[];
  states: { t: number; state: string }[];
  transitions: Transition[]; activity: Activity[]; audit: { at: string; event: string }[];
  records: EvidenceRecordSummary[]; claims: ClaimSummary[];
  sim: SimulationInfo | null; latest: SimulationDoc["latest_observation"] | null;
  historyNote: string | null;
}

const num = (v: unknown): number | null => (isNum(v) ? v : null);

function thresholds(ins: Insights | null) {
  const m = ins?.agents?.river?.metrics ?? {};
  return { warning: num(m.warning_level_m), danger: num(m.danger_level_m),
    recovery: num(ins?.state?.threshold_check?.recovery_level_m), official: m.thresholds_official === true };
}

export function fromSimulation(doc: SimulationDoc): View {
  const pts = (rows: SeriesEntry[], f: (e: SeriesEntry) => number | null) =>
    rows.flatMap((e) => { const t = epoch(e.t); const v = f(e); return t !== null && v !== null ? [{ t, v }] : []; });
  const s = doc.series;
  const th = thresholds(doc.insights);
  return {
    source: "simulation", label: SIM_LABEL, simulated: true,
    insights: doc.insights.error ? null : doc.insights, insightsError: doc.insights.error ?? null,
    generatedAt: doc.generated_at, evaluatedAt: doc.simulation.sim_time,
    title: "Simulated live demonstration",
    subtitle: `Conditions: ${doc.simulation.scenarios.find((x) => x.name === doc.simulation.selected_scenario)?.label ?? doc.simulation.selected_scenario}`,
    river: {
      points: doc.readings.flatMap((r) => { const t = epoch(r.t); return t !== null ? [{ t, v: r.level_m }] : []; }),
      ...th,
    },
    // Hourly simulated rain generated by the engine (never Open-Meteo values).
    rain: (doc.rainfall ?? []).flatMap((r) => { const t = epoch(r.t); return t !== null && isNum(r.mm) ? [{ t, v: r.mm, kind: "observed" as const }] : []; }),
    rate: pts(s, (e) => num(e.rate_m_per_hour)),
    risk: s.flatMap((e) => { const t = epoch(e.t); return t !== null ? [{ t, v: num(e.risk_score), category: e.risk_category }] : []; }),
    trust: pts(s, (e) => num(e.trust_score)),
    states: s.flatMap((e) => { const t = epoch(e.t); return t !== null ? [{ t, state: e.state }] : []; }),
    transitions: doc.transitions, activity: doc.activity, audit: doc.audit?.recent ?? [],
    records: doc.evidence_records ?? [], claims: doc.claims ?? [],
    sim: doc.simulation, latest: doc.latest_observation, historyNote: null,
  };
}

export function fromDashboard(doc: DashboardDoc, source: Source): View {
  const ins = doc.insights && !doc.insights.error ? doc.insights : null;
  const th = thresholds(ins);
  const points = (doc.river?.series?.points ?? []).flatMap((p) => {
    const t = epoch(p.time); return t !== null && isNum(p.level_m) ? [{ t, v: p.level_m }] : [];
  });
  const rain = (doc.weather?.hourly ?? []).flatMap((h) => {
    const t = epoch(h.time);
    return t !== null && isNum(h.precipitation_mm)
      ? [{ t, v: h.precipitation_mm, kind: h.kind === "FORECAST" ? "forecast" as const : "observed" as const }] : [];
  });
  const last = doc.state?.persisted?.last_transition as Transition | null | undefined;
  const scenario = doc.scenario ?? "";
  return {
    source, label: doc.data_label, simulated: doc.is_simulated,
    insights: ins, insightsError: doc.insights?.error ?? (doc.insights ? null : "This response carries no insights block."),
    generatedAt: doc.generated_at, evaluatedAt: ins?.state?.evaluated_at ?? doc.generated_at,
    title: source === "real" ? "Real operational data (CWC)" : `Scenario test case: ${scenario}`,
    subtitle: source === "real" ? (doc.location?.description ?? "") : (doc.scenario_description ?? ""),
    river: { points, ...th }, rain, rate: [], risk: [], trust: [], states: [],
    transitions: last && last.from && last.to ? [last] : [],
    activity: (doc.audit?.events ?? []).slice().reverse().map((e) => ({ wall_time: e.at, kind: "audit", text: e.event ?? "" })),
    audit: (doc.audit?.events ?? []).slice().reverse().map((e) => ({ at: e.at ?? "", event: e.event ?? "" })),
    records: (doc.evidence_layer?.records ?? []).map((r) => ({
      evidence_id: r.evidence_id, kind: r.kind, evidence_class: r.evidence_class, observed_at: r.observed_at,
      validation: r.validation?.status, findings: (r.validation?.findings ?? []).map((f) => f.code ?? ""),
    })),
    claims: (doc.evidence_layer?.claims ?? []).map((c) => ({ claim_id: c.claim_id, status: c.status, statement: c.statement })),
    sim: null, latest: null,
    historyNote: source === "real"
      ? "Risk and trust history is built only from evaluations that actually happened; a single real evaluation has no history yet."
      : "A scenario test case is one fixed evaluation, so it has no history. Use the live simulation to watch values change.",
  };
}

// ---- API -------------------------------------------------------------------------------

export async function fetchSimulation(signal?: AbortSignal): Promise<Result<SimulationDoc>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/simulation`, { signal, headers: { Accept: "application/json" } });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    return { ok: false, error: "Cannot reach the API (network error)." };
  }
  return parseResponse(response, parseSimulation);
}

export type ControlAction = "start" | "pause" | "resume" | "reset" | "scenario" | "speed";

/** Simulation controls. The backend owns every decision; there is no way to send a state. */
export async function controlSimulation(
  action: ControlAction, extra: { scenario?: string; speed?: number } = {},
): Promise<Result<SimulationDoc>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}/simulation/control`, {
      method: "POST", headers: { "Content-Type": "application/json", Accept: "application/json" },
      body: JSON.stringify({ action, ...extra }),
    });
  } catch {
    return { ok: false, error: "Cannot reach the API (network error)." };
  }
  return parseResponse(response, parseSimulation);
}

async function parseResponse<T>(response: Response, parse: (j: unknown) => Result<T>): Promise<Result<T>> {
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    return { ok: false, status: response.status, error: response.status >= 500
      ? `Cannot reach the backend API (HTTP ${response.status}). Is the Python API running?`
      : `API returned HTTP ${response.status} with a non-JSON body.` };
  }
  if (!response.ok) {
    const msg = isObj(body) && typeof body.error === "string" ? body.error : `HTTP ${response.status}`;
    return { ok: false, error: msg, status: response.status };
  }
  return parse(body);
}

export async function fetchView(source: Source, scenario: string, signal?: AbortSignal): Promise<Result<View>> {
  if (source === "simulation") {
    const r = await fetchSimulation(signal);
    return r.ok ? { ok: true, value: fromSimulation(r.value) } : r;
  }
  const r = await fetchDashboard(source === "real" ? "real" : "simulated", source === "real" ? null : scenario, signal);
  return r.ok ? { ok: true, value: fromDashboard(r.value, source) } : r;
}

// ---- presentation helpers (labels and tones only; no values are derived) ----------------

export type Tone = "ok" | "caution" | "warn" | "danger" | "info" | "unknown" | "neutral";
export const RISK_TONE: Record<string, Tone> = { LOW: "ok", MODERATE: "caution", HIGH: "warn", CRITICAL: "danger", INDETERMINATE: "unknown" };
export const TRUST_TONE: Record<string, Tone> = { HIGH: "ok", MODERATE: "caution", LOW: "warn", VERY_LOW: "danger" };
export const STATE_TONE: Record<string, Tone> = { NORMAL: "ok", ALERT: "caution", EMERGENCY: "danger", RECOVERY: "info" };
export const AGENT_TONE: Record<string, Tone> = { AVAILABLE: "ok", STALE: "caution", MISSING: "warn", CONFLICTING: "danger" };
export const CAP_TONE: Record<string, Tone> = { ALLOWED: "ok", RESTRICTED: "caution", DENIED: "danger" };
export const STATES = ["NORMAL", "ALERT", "EMERGENCY", "RECOVERY"];
export const SCENARIO_TEST_CASES: { name: string; label: string }[] = [
  { name: "low-rain-valid-river", label: "Low risk, reliable evidence" },
  { name: "rising-river-increasing-rain", label: "Rising river, increasing rain" },
  { name: "simulated-high-risk", label: "High risk, reliable evidence" },
  { name: "heavy-forecast-stable-river", label: "Heavy forecast, stable river" },
  { name: "stale-river", label: "Stale river evidence" },
  { name: "missing-weather", label: "Missing weather evidence" },
  { name: "conflicting-evidence", label: "Conflicting river sources" },
  { name: "recovery-after-high-water", label: "Recovery after high water" },
];
