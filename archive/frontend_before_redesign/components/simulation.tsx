// Real-Time River Simulation panel. Every value comes from the backend
// (src/river_simulation.py through the existing workflow); nothing is computed here.
import type { DashboardDoc } from "../types";
import { fmtAge, fmtNum, fmtShort, fmtTime, humanize, isNum, RISK_TONE, STATE_TONE } from "../format";
import { Badge, Field, Section, Unavailable } from "./common";

const TICKS_SHOWN = 12;

export function LiveSimulationPanel({ doc }: { doc: DashboardDoc }) {
  const sim = doc.simulation;
  const river = doc.river;
  const trend = river?.trend;
  const quality = river?.recent_quality;
  const qc = river?.qc as { accepted?: number; checked?: number } | undefined;
  const evaluation = doc.state?.evaluation;
  const state = evaluation?.resulting_state ?? sim?.current_state;
  const risk = doc.risk?.risk_level;
  if (!sim) return <Unavailable>No simulation session in this response.</Unavailable>;
  const ticks = (sim.timeline ?? []).slice(-TICKS_SHOWN).reverse();
  return (
    <Section id="live-sim" title="Real-Time River Simulation"
      subtitle={`${sim.station_id ?? "—"} (simulated twin of ${sim.twin_of ?? "—"}) · scenario ${sim.scenario ?? "—"} · a new reading every ${sim.interval_minutes ?? "—"} min`}
      badges={<>
        <Badge tone="sim">SIMULATED LIVE</Badge>
        <Badge tone="neutral">Not CWC data</Badge>
      </>}>
      <p className="note"><strong>{sim.disclaimer}</strong></p>
      <div className="kpis" role="list">
        <article className="kpi" role="listitem">
          <h3>Simulated river level</h3>
          {/* 3 decimals, as generated: never rounded to look like the 72.0 m fill value. */}
          <p className="kpi-value">{isNum(river?.level?.value_m) ? fmtNum(river?.level?.value_m, "m", 3) : <span className="muted">Unavailable</span>}</p>
          <p className="kpi-note">Generated reading · {fmtShort(river?.level?.observed_at)} IST</p>
          <div className="kpi-badges"><Badge tone="sim">SIMULATED LIVE</Badge></div>
        </article>
        <article className="kpi" role="listitem">
          <h3>Freshness</h3>
          <p className="kpi-value"><Badge tone={river?.freshness?.status === "FRESH" ? "ok" : "warn"}>{river?.freshness?.status ?? "UNKNOWN"}</Badge></p>
          <p className="kpi-note">{fmtAge(river?.freshness?.age_hours)} (limit {fmtNum(river?.freshness?.max_age_hours, "h", 0)})</p>
        </article>
        <article className="kpi" role="listitem">
          <h3>Trend</h3>
          <p className="kpi-value">{trend?.direction ?? <span className="muted">Unavailable</span>}</p>
          <p className="kpi-note">{isNum(trend?.rate_of_change_m_per_hour) ? `${fmtNum(trend?.rate_of_change_m_per_hour, "m/h", 3)} over ${fmtNum(trend?.window_hours, "h", 0)}` : trend?.reason ?? "—"}</p>
        </article>
        <article className="kpi" role="listitem">
          <h3>Data quality</h3>
          <p className="kpi-value"><Badge tone={quality?.status === "ACCEPTABLE" ? "ok" : "warn"}>{quality?.status ?? "UNKNOWN"}</Badge></p>
          <p className="kpi-note">QC accepted {qc?.accepted ?? "—"} of {qc?.checked ?? "—"} · rejected share {isNum(quality?.rejected_fraction) ? `${(quality!.rejected_fraction! * 100).toFixed(1)}%` : "—"}</p>
        </article>
        <article className="kpi" role="listitem">
          <h3>Risk</h3>
          <p className="kpi-value"><Badge tone={RISK_TONE[risk ?? "INDETERMINATE"] ?? "unknown"}>{risk ?? "UNKNOWN"}</Badge></p>
          <p className="kpi-note">Confidence {doc.risk?.evidence_quality?.confidence ?? "—"} (simulation only)</p>
        </article>
        <article className="kpi" role="listitem">
          <h3>Disaster state</h3>
          <p className="kpi-value"><Badge tone={STATE_TONE[state ?? ""] ?? "unknown"}>{state ?? "UNKNOWN"}</Badge></p>
          <p className="kpi-note">{evaluation?.current_state ?? "—"} → {evaluation?.resulting_state ?? "—"} · {humanize(evaluation?.outcome)}</p>
        </article>
      </div>
      <div className="grid-2">
        <dl className="fields">
          <Field label="Reading timestamp">{fmtTime(river?.level?.observed_at)}</Field>
          <Field label="Next reading due">{fmtTime(sim.next_reading_due)}</Field>
          <Field label="Session started">{fmtTime(sim.started_at)} · start state {sim.start_state}</Field>
          <Field label="Readings generated">{sim.readings_generated ?? "—"} ({sim.readings_held ?? "—"} held) · {sim.evaluations ?? "—"} evaluations</Field>
        </dl>
        <dl className="fields">
          <Field label="Validation">QC rules of {sim.qc_rules_from ?? "—"} (range, 72.0 m fill value, spikes), applied by the River Agent</Field>
          <Field label="TEST thresholds">warning {fmtNum(sim.thresholds?.warning_level_m, "m", 2)} · danger {fmtNum(sim.thresholds?.danger_level_m, "m", 2)} · {sim.thresholds?.official ? "official" : "NOT official"}</Field>
          <Field label="State record">scope {sim.state_record_scope ?? "—"} · {sim.state_record_persisted ? "saved" : "in memory only, never saved"}</Field>
          <Field label="Real-world authorization"><Badge tone="danger">{doc.safety?.real_world_authorization ?? "RESTRICTED"}</Badge> Simulated evidence never authorizes real-world actions</Field>
        </dl>
      </div>
      <h3>Recent evaluations</h3>
      {ticks.length === 0 ? <Unavailable>No evaluations yet.</Unavailable> : (
        <table className="table">
            <thead>
              <tr><th>Reading (IST)</th><th>Level</th><th>Freshness</th><th>Trend</th><th>Risk</th><th>Condition</th><th>State</th><th>Real-world</th></tr>
            </thead>
            <tbody>
              {ticks.map((t, i) => (
                <tr key={`${t.evaluated_at}-${i}`}>
                  <td>{fmtShort(t.observed_at)}{t.catch_up ? " (catch-up)" : ""}</td>
                  <td>{fmtNum(t.river_level_m, "m", 3)}</td>
                  <td>{t.freshness ?? "—"}</td>
                  <td>{t.trend ?? "—"}</td>
                  <td><Badge tone={RISK_TONE[t.risk_level ?? ""] ?? "unknown"}>{t.risk_level ?? "—"}</Badge></td>
                  <td>{humanize(t.condition)}</td>
                  <td>{t.state_before === t.state_after ? t.state_after : `${t.state_before} → ${t.state_after}`}</td>
                  <td>{t.real_world_authorization ?? "—"}</td>
                </tr>
              ))}
            </tbody>
        </table>
      )}
    </Section>
  );
}
