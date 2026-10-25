// API access and runtime validation. The page never trusts a response just
// because it parsed as JSON: anything malformed, or any real-mode response that
// contains simulated parts, is rejected and shown as an error.
import type { DashboardDoc, Mode, ScenarioList, SequenceDoc } from "./types";

export const API_BASE: string = (import.meta.env?.VITE_API_BASE_URL as string | undefined) ?? "/api";
const pollEnv = Number(import.meta.env?.VITE_POLL_SECONDS ?? 60);
export const POLL_SECONDS: number = Number.isFinite(pollEnv) ? Math.max(30, pollEnv) : 60;

export type Result<T> = { ok: true; value: T } | { ok: false; error: string; status?: number };

const isObj = (v: unknown): v is Record<string, unknown> =>
  typeof v === "object" && v !== null && !Array.isArray(v);

/** Validate a /dashboard response. Exported for tests. */
export function parseDashboard(json: unknown): Result<DashboardDoc> {
  if (!isObj(json)) return { ok: false, error: "Response is not a JSON object." };
  if (typeof json.error === "string") return { ok: false, error: json.error };
  if (json.schema_version !== "1.0")
    return { ok: false, error: `Unsupported schema_version ${String(json.schema_version)}.` };
  if (json.mode !== "real" && json.mode !== "simulated")
    return { ok: false, error: "Response has no valid mode." };
  if (typeof json.is_simulated !== "boolean" || typeof json.generated_at !== "string")
    return { ok: false, error: "Response is missing is_simulated or generated_at." };
  for (const key of ["weather", "river", "risk", "state", "safety", "audit"]) {
    if (json[key] !== undefined && json[key] !== null && !isObj(json[key]))
      return { ok: false, error: `Section "${key}" is malformed.` };
  }
  // Real and simulated data must never be mixed silently.
  const simulatedParts = ["weather", "river", "risk"].filter(
    (k) => isObj(json[k]) && (json[k] as Record<string, unknown>).is_simulated === true,
  );
  if (json.mode === "real" && (json.is_simulated || simulatedParts.length > 0))
    return { ok: false, error: `Real-mode response contains simulated data (${simulatedParts.join(", ") || "flag"}); not shown.` };
  if (json.mode === "simulated" && !json.is_simulated)
    return { ok: false, error: "Simulated-mode response is not labelled simulated; not shown." };
  return { ok: true, value: json as unknown as DashboardDoc };
}

export function parseSequence(json: unknown): Result<SequenceDoc> {
  if (!isObj(json)) return { ok: false, error: "Response is not a JSON object." };
  if (typeof json.error === "string") return { ok: false, error: json.error };
  if (json.is_simulated !== true || !Array.isArray(json.steps))
    return { ok: false, error: "Sequence response is malformed or not labelled simulated." };
  return { ok: true, value: json as unknown as SequenceDoc };
}

export function parseScenarios(json: unknown): Result<ScenarioList> {
  if (!isObj(json) || !Array.isArray(json.scenarios) || !Array.isArray(json.start_states))
    return { ok: false, error: "Scenario list is malformed." };
  return { ok: true, value: json as unknown as ScenarioList };
}

async function getJson(path: string, signal?: AbortSignal): Promise<Result<unknown>> {
  let response: Response;
  try {
    response = await fetch(`${API_BASE}${path}`, { signal, headers: { Accept: "application/json" } });
  } catch (err) {
    if ((err as Error).name === "AbortError") throw err;
    return { ok: false, error: "Cannot reach the API (network error)." };
  }
  let body: unknown;
  try {
    body = await response.json();
  } catch {
    // The dev/preview proxy answers 5xx with a plain body when the backend is down.
    const error = response.status >= 500
      ? `Cannot reach the backend API (HTTP ${response.status} from the proxy). Is the Python API running?`
      : `API returned HTTP ${response.status} with a non-JSON body.`;
    return { ok: false, error, status: response.status };
  }
  if (!response.ok) {
    const message = isObj(body) && typeof body.error === "string" ? body.error : `HTTP ${response.status}`;
    return { ok: false, error: message, status: response.status };
  }
  return { ok: true, value: body };
}

export async function fetchDashboard(
  mode: Mode, scenario: string | null, state: string | null, signal?: AbortSignal,
): Promise<Result<DashboardDoc>> {
  const params = new URLSearchParams({ mode });
  if (mode === "simulated") {
    if (scenario) params.set("scenario", scenario);
    if (state) params.set("state", state);
  }
  const r = await getJson(`/dashboard?${params}`, signal);
  return r.ok ? parseDashboard(r.value) : r;
}

export async function fetchSequence(name: string, signal?: AbortSignal): Promise<Result<SequenceDoc>> {
  const r = await getJson(`/dashboard/sequence?${new URLSearchParams({ name })}`, signal);
  return r.ok ? parseSequence(r.value) : r;
}

export async function fetchScenarios(signal?: AbortSignal): Promise<Result<ScenarioList>> {
  const r = await getJson("/dashboard/scenarios", signal);
  return r.ok ? parseScenarios(r.value) : r;
}

export async function fetchHealth(signal?: AbortSignal): Promise<boolean> {
  const r = await getJson("/health", signal);
  return r.ok && isObj(r.value) && r.value.status === "ok";
}
