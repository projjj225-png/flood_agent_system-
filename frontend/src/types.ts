// Shapes returned by the Python API (src/dashboard_service.py). Every field is
// optional or nullable on purpose: the UI must cope with missing data and must
// never fill a gap with a made-up value.

// "simulated_live": the Real-Time River Simulation (fabricated readings every 5 min).
export type Mode = "real" | "simulated" | "simulated_live";
export type RiskLevel = "LOW" | "MODERATE" | "HIGH" | "CRITICAL" | "INDETERMINATE";
export type DisasterState = "NORMAL" | "ALERT" | "EMERGENCY" | "RECOVERY";

export interface Issue {
  code?: string;
  severity?: string;
  field?: string | null;
  message?: string;
}

export interface RainWindow {
  period_start?: string | null;
  period_end?: string | null;
  precipitation?: { status?: string; total?: number | null; reason?: string | null };
  rain?: { status?: string; total?: number | null; reason?: string | null };
}

export interface HourlyPoint {
  time: string;
  precipitation_mm: number | null;
  rain_mm: number | null;
  precipitation_probability_pct: number | null;
  temperature_c: number | null;
  kind: "MODEL_ESTIMATE_PAST" | "FORECAST";
}

export interface WeatherBlock {
  status?: string;
  reason?: string;
  fetch_error?: string;
  source?: string | null;
  source_type?: string | null;
  is_simulated?: boolean;
  retrieved_at?: string | null;
  timezone?: { requested?: string; returned?: string; abbreviation?: string };
  grid_location?: { latitude?: number | null; longitude?: number | null; elevation_m?: number | null };
  current?: {
    status?: string;
    time?: string | null;
    interval_seconds?: number | null;
    values?: Record<string, number | null>;
    missing?: Record<string, string>;
  };
  units?: { current?: Record<string, string | null>; hourly?: Record<string, string | null> };
  rainfall?: Record<string, RainWindow>;
  hourly?: HourlyPoint[];
  errors?: Issue[];
}

export interface SeriesPoint {
  time: string;
  level_m: number | null;
  qc_code?: string | null;
}

export interface RiverBlock {
  station_id?: string | null;
  status?: string | null;
  source_label?: string | null;
  source?: string | null;
  is_simulated?: boolean;
  level?: { value_m?: number | null; role?: string; observed_at?: string | null; datum?: string | null };
  freshness?: {
    status?: string;
    age_hours?: number | null;
    max_age_hours?: number | null;
    reference_time?: string | null;
    reference_mode?: string | null;
    reason?: string | null;
  };
  evidence_grade?: string | null;
  is_verified_live?: boolean;
  source_verification?: { status?: string | null; failed_checks?: string[]; reason?: string | null };
  recent_quality?: { status?: string; rejected_fraction?: number | null; readings_in_window?: number | null; reason?: string | null };
  qc?: Record<string, unknown>;
  counts?: Record<string, number | null>;
  trend?: { status?: string; direction?: string | null; rate_of_change_m_per_hour?: number | null; level_change_m?: number | null; window_hours?: number | null; reason?: string | null };
  thresholds?: { status?: string; warning_level_m?: number | null; danger_level_m?: number | null; threshold_is_official?: boolean; threshold_reference?: string | null; reason?: string | null };
  cross_source_check?: { status?: string | null; conflicts?: number | null; conflicts_affecting_current_evidence?: number | null; tolerance_m?: number | null };
  reverification?: { status?: string | null; outcome?: string | null; reason?: string | null };
  fallback?: { used?: boolean; reason?: string | null };
  live_fetch?: { attempted?: boolean; status?: string | null; verification_status?: string | null; error?: string | null; retrieved_at?: string | null };
  reasons_not_usable?: string[];
  warnings?: Issue[];
  errors?: Issue[];
  series?: { status?: string; reason?: string | null; points?: SeriesPoint[]; rejected?: SeriesPoint[]; hours?: number; window_start?: string; window_end?: string };
}

export interface RiskFactor {
  rule_id: string;
  factor?: string;
  hazard_level?: string | null;
  source?: string;
  description?: string;
}

export interface RiskBlock {
  risk_level?: RiskLevel | string;
  risk_level_meaning?: string | null;
  hazard_indicated?: string | null;
  assessment_mode?: string;
  is_simulated?: boolean;
  reference_time?: string | null;
  assessment_timestamp?: string | null;
  risk_factors?: RiskFactor[];
  rules_fired?: string[];
  decision_trace?: string[];
  evidence_quality?: {
    confidence?: string;
    confidence_downgrades?: string[];
    confidence_applies_to?: string;
    river_blocking_reasons?: string[];
    weather_not_used_reasons?: string[];
  };
  uncertainties?: { code: string; description?: string }[];
  missing_data?: { item: string; reason?: string }[];
  recommended_monitoring_actions?: { code: string; description?: string }[];
  requires_human_review?: boolean;
  human_review_reasons?: string[];
  calibrated_probability?: number | null;
  authorizes_actions?: boolean;
  disclaimer?: string;
  error?: string;
}

export interface StateHistoryEntry {
  observed_at?: string | null;
  condition?: string;
  risk_level?: string | null;
  evaluated_at?: string | null;
  reasons?: string[] | null;  // null on valid entries (only INSUFFICIENT ones carry reasons)
}

export interface StateBlock {
  persisted?: {
    available?: boolean;
    state?: string | null;
    scope?: string | null;
    entered_at?: string | null;
    last_transition?: { from?: string; to?: string; at?: string; kind?: string } | null;
    record_file?: string | null;
    problem?: string | null;
  };
  state_used_for_capability_checks?: string | null;
  state_basis?: string;
  evaluation?: {
    outcome?: string;
    scope?: string | null;
    current_state?: string | null;
    proposed_state?: string | null;
    resulting_state?: string | null;
    condition?: string | null;
    reason?: string | null;
    reasons?: string[];
    persistence?: { observations?: number; required_observations?: number; span_hours?: number; required_hours?: number; satisfied?: boolean; run_broken_by_gap_hours?: number | null } | null;
    transition_rule?: { to?: string; kind?: string } | null;
    observed_at?: string | null;
    evaluated_at?: string | null;
    state_applies_to?: string | null;
    is_simulated?: boolean;
    audit_written?: boolean;
  };
  history?: StateHistoryEntry[];
  record_saved_by_dashboard?: boolean;
  note?: string;
}

export interface Decision {
  capability?: string;
  capability_class?: string | null;
  allowed?: boolean;
  reason?: string;
  evidence_reasons?: string[];
  human_approval_required?: boolean;
  state?: string;
}

export interface SafetyBlock {
  real_world_authorization?: string;
  pending_human_approval?: string[];
  decisions?: Decision[];
  evidence_assessment?: { eligible?: boolean; reasons?: string[]; input_problems?: string[] };
  reason?: string;
  executes_actions?: boolean;
  note?: string;
}

export interface AuditEvent {
  at?: string;
  event?: string;
  written?: boolean;
  details?: Record<string, unknown>;
}

export interface EvidenceFinding { code: string; severity: string; detail: string }

export interface EvidenceRecord {
  evidence_id: string;
  kind: string;
  evidence_class: string;
  measurement_basis?: string;
  source?: { label?: string | null; name?: string | null; provider?: string | null;
    reference?: string | null; independence?: string | null };
  observed_at?: string | null;
  retrieved_at?: string | null;
  freshness?: { status?: string; age_hours?: number | null; max_age_hours?: number | null };
  verification?: { status?: string };
  uncertainty?: string[];
  limitations?: string[];
  validation?: { status?: string; findings?: EvidenceFinding[]; error_codes?: string[] };
  contradicts_current_reading?: boolean;
}

export interface EvidenceClaim {
  claim_id: string;
  statement: string;
  status: string;
  safety_critical?: boolean;
  supporting_evidence_ids?: string[];
  contradicting_evidence_ids?: string[];
  explanation?: string[];
}

export interface ReverificationAttempt {
  attempt?: number;
  started_at?: string | null;
  outcome?: string;
  conflicts_remaining?: number | null;
  error?: string | null;
}

export interface EvidenceLayerBlock {
  available: boolean;
  reason?: string;
  error?: string | null;
  reference_time?: string | null;
  records?: EvidenceRecord[];
  claims?: EvidenceClaim[];
  summary?: { records_by_class?: Record<string, number>; invalid_records?: string[];
    claims_by_status?: Record<string, number> };
  real_world_evidence_sufficient?: boolean;
  deny_reasons?: string[];
  audit_complete?: boolean;
  class_descriptions?: Record<string, string>;
  disclaimer?: string | null;
  reverification_attempts?: ReverificationAttempt[];
}

export interface DashboardDoc {
  schema_version: string;
  generated_at: string;
  mode: Mode;
  is_simulated: boolean;
  data_label: string;
  timezone?: string;
  location?: { description?: string; latitude?: number; longitude?: number };
  cache_seconds?: number;
  cache_age_seconds?: number;
  scenario?: string;
  scenario_description?: string;
  start_state?: string;
  error?: string;
  weather?: WeatherBlock;
  river?: RiverBlock;
  risk?: RiskBlock;
  state?: StateBlock;
  safety?: SafetyBlock;
  evidence_layer?: EvidenceLayerBlock;
  audit?: { events?: AuditEvent[]; write_errors?: string[]; log_file?: string | null };
  simulation?: SimulationInfo;
}

/** One evaluation of the real-time simulation session (all values from the backend). */
export interface SimulationTick {
  evaluated_at?: string;
  catch_up?: boolean;
  observed_at?: string | null;
  river_level_m?: number | null;
  freshness?: string | null;
  trend?: string | null;
  risk_level?: string;
  condition?: string | null;
  outcome?: string;
  state_before?: string | null;
  state_after?: string | null;
  real_world_authorization?: string;
}

export interface SimulationInfo {
  label: string;
  session_id?: string;
  scenario?: string;
  scenario_description?: string;
  start_state?: string;
  current_state?: string;
  started_at?: string;
  station_id?: string;
  twin_of?: string | null;
  qc_rules_from?: string;
  interval_minutes?: number;
  readings_held?: number;
  readings_generated?: number;
  latest_generated_at?: string | null;
  next_reading_due?: string | null;
  evaluations?: number;
  thresholds?: { warning_level_m?: number; danger_level_m?: number; reference?: string; official?: boolean };
  timeline?: SimulationTick[];
  state_record_scope?: string;
  state_record_persisted?: boolean;
  authorizes_real_world_actions: boolean;
  disclaimer?: string;
}

export interface SequenceStep {
  hour: number;
  as_of: string;
  river_level_m: number | null;
  risk_level: string;
  condition: string | null;
  outcome: string;
  state_before: string | null;
  state_after: string | null;
  reason: string | null;
}

export interface SequenceDoc {
  sequence: string;
  description?: string;
  is_simulated: boolean;
  data_label: string;
  steps: SequenceStep[];
}

export interface ScenarioList {
  scenarios: { name: string; description: string }[];
  sequences: { name: string; description: string }[];
  start_states: string[];
  live_simulation?: {
    available?: boolean;
    reason?: string;
    scenarios?: { name: string; description: string }[];
    interval_minutes?: number;
    data_label?: string;
  };
}
