// Panels shared by several pages. They only display backend values (src/insights.py).
import type { ReactNode } from "react";
import type { AgentCard, CapabilityCard, Insights, RiskScore, StateView, Transition, Trust } from "../model";
import { AGENT_TONE, CAP_TONE, RISK_TONE, STATE_TONE, STATES, TRUST_TONE } from "../model";
import { fmtAge, fmtNum, fmtTime, isNum } from "../format";
import { Disclosure, Empty, Gauge, Icon, Id, KV, Meter, Pill } from "./ui";

const titleCase = (s: string) => s.charAt(0) + s.slice(1).toLowerCase().replace(/_/g, " ");
const cap = (s?: string | null) => (s ? s.replace(/_/g, " ").toLowerCase() : "—");

// ---- risk ------------------------------------------------------------------------------

export function RiskGauge({ risk, size }: { risk: RiskScore; size?: number }) {
  const tone = RISK_TONE[risk.category] ?? "unknown";
  return <Gauge score={risk.available ? risk.score : null} tone={tone} size={size}
    label={risk.available ? `${risk.category} RISK` : "RISK: INDETERMINATE"}
    caption={risk.available && risk.band ? `${risk.category.toLowerCase()} band ${risk.band[0]}–${risk.band[1]}` : "no defensible score"} />;
}

export function RiskBands({ risk }: { risk: RiskScore }) {
  return (
    <div className="bands" aria-label="Risk score bands (prototype presentation bands)">
      {(["LOW", "MODERATE", "HIGH", "CRITICAL"] as const).map((c) => {
        const [lo, hi] = risk.bands[c] ?? [0, 0];
        const on = risk.category === c;
        return (
          <div key={c} className={`band band-${RISK_TONE[c]} ${on ? "on" : ""}`} style={{ flexGrow: hi - lo + 1 }}>
            <strong>{c}</strong><span>{lo}–{hi}</span>
            {on && isNum(risk.score) && <i className="band-marker" style={{ left: `${((risk.score - lo) / (hi - lo + 1)) * 100}%` }} aria-hidden />}
          </div>
        );
      })}
    </div>
  );
}

export function WhyRisk({ ins, compact = false }: { ins: Insights; compact?: boolean }) {
  const ex = ins.risk_explanation;
  const indeterminate = ex.category === "INDETERMINATE";
  return (
    <div className="why">
      <ul className="why-list">
        {ex.lines.map((l) => (
          <li key={l.label} className={`why-${l.direction}`}>
            <span className="why-dir" aria-hidden>
              <Icon name={l.direction === "raises" ? "up" : l.direction === "lowers" ? "down" : "info"} size={14} />
            </span>
            <span className="why-label">{l.label}</span>
            <span className="why-value">{l.value}{l.context_only && l.label !== "Evidence trust" ? " (context only)" : ""}</span>
            {!compact && <span className="why-detail">{l.detail}</span>}
          </li>
        ))}
      </ul>
      <p className={`why-combined tone-${RISK_TONE[ex.category] ?? "unknown"}`}>
        <strong>Combined assessment:</strong> {ex.combined}
      </p>
      {indeterminate && ex.missing.length > 0 && (
        <div className="missing">
          <strong>What is missing for a defensible assessment</strong>
          <ul>{ex.missing.map((m) => <li key={m.item}>{m.reason}</li>)}</ul>
        </div>
      )}
      {!compact && ex.adjustments.length > 0 && <p className="small muted">{ex.adjustments.join(" ")}</p>}
    </div>
  );
}

// ---- trust -----------------------------------------------------------------------------

export function TrustGauge({ trust, size }: { trust: Trust; size?: number }) {
  return <Gauge score={trust.score} tone={TRUST_TONE[trust.band] ?? "unknown"} size={size} label={trust.label}
    caption={trust.is_simulated ? "simulated evidence" : "evidence reliability"} />;
}

export function TrustFactors({ trust }: { trust: Trust }) {
  return (
    <ul className="factor-list">
      {trust.factors.map((f) => (
        <li key={f.key}>
          <div className="factor-head"><span>{f.label}</span><strong>{f.score}</strong></div>
          <Meter value={f.score} tone={f.score >= 80 ? "ok" : f.score >= 60 ? "caution" : f.score >= 30 ? "warn" : "danger"} label={f.label} />
          <span className="factor-detail">{f.detail} · weight {f.weight}</span>
        </li>
      ))}
    </ul>
  );
}

export function TrustDeductions({ trust }: { trust: Trust }) {
  if (!trust.deductions.length) return <p className="small muted">No deductions.</p>;
  return (
    <ul className="deductions">
      {trust.deductions.map((d, i) => (
        <li key={i}><span className="deduct">−{d.points}</span> <strong>{d.factor}:</strong> {d.reason}</li>
      ))}
    </ul>
  );
}

// ---- disaster state --------------------------------------------------------------------

export function StateTimeline({ state }: { state: StateView }) {
  return (
    <ol className="timeline" aria-label="Disaster state timeline">
      {STATES.map((s, i) => {
        const current = state.current === s;
        const previous = !current && state.previous === s;
        const proposed = !current && state.proposed_next === s;
        return (
          <li key={s} className={`tl-step tl-${STATE_TONE[s]} ${current ? "is-current" : ""} ${previous ? "is-previous" : ""} ${proposed ? "is-proposed" : ""}`}
            aria-current={current ? "step" : undefined}>
            <span className="tl-dot" aria-hidden>{i + 1}</span>
            <span className="tl-name">{s}</span>
            <span className="tl-tag">{current ? "Current" : previous ? "Previous" : proposed ? "Proposed next" : " "}</span>
          </li>
        );
      })}
    </ol>
  );
}

export function Progress({ state }: { state: StateView }) {
  const p = state.progress;
  if (!p) return null;
  const obs = isNum(p.observations) && isNum(p.required_observations) ? Math.min(100, (p.observations / Math.max(1, p.required_observations)) * 100) : 0;
  const hrs = isNum(p.span_hours) && isNum(p.required_hours) && p.required_hours > 0 ? Math.min(100, (p.span_hours / p.required_hours) * 100) : 100;
  return (
    <div className="progress">
      <div className="progress-head"><Icon name="arrow" size={14} /> Towards <strong>{p.to}</strong></div>
      <div className="progress-row"><span>Observations {p.observations ?? 0} / {p.required_observations ?? "—"}</span><Meter value={obs} tone="info" label="Observations towards next state" /></div>
      {isNum(p.required_hours) && p.required_hours > 0 && (
        <div className="progress-row"><span>Persistence {fmtNum(p.span_hours, "h", 1)} / {p.required_hours} h</span><Meter value={hrs} tone="info" label="Hours towards next state" /></div>
      )}
      {p.traffic_required && <div className="progress-row"><span>Evacuation routes: {cap(p.traffic_status)}</span></div>}
    </div>
  );
}

export function StateSummary({ state, compact = false }: { state: StateView; compact?: boolean }) {
  const tone = STATE_TONE[state.current] ?? "neutral";
  return (
    <div className="state-summary">
      <div className={`state-badge state-${tone}`}><Icon name="state" size={16} /> {state.current}</div>
      <p className="state-meaning">{state.meaning}</p>
      <p className="state-why"><strong>Why:</strong> {state.why}{" "}
        {state.transition_this_evaluation ? <span className="changed">Changed this evaluation.</span> : null}</p>
      {!compact && <p className="small muted">{state.reason}</p>}
      {state.traffic_hold && <p className="hold"><Icon name="traffic" size={14} /> Held in {state.current}: evacuation routes are not confirmed usable.</p>}
    </div>
  );
}

export function TransitionList({ transitions }: { transitions: Transition[] }) {
  if (!transitions.length) return <Empty>No automatic transitions yet.</Empty>;
  return (
    <ol className="transitions">
      {transitions.slice().reverse().map((t, i) => (
        <li key={i}>
          <div className="tr-head">
            <Pill tone={STATE_TONE[t.from] ?? "neutral"} icon={false}>{t.from}</Pill>
            <Icon name="arrow" size={14} />
            <Pill tone={STATE_TONE[t.to] ?? "neutral"} icon={false}>{t.to}</Pill>
            <time>{fmtTime(t.at)}</time>
          </div>
          <p className="tr-reason">{t.reason ?? `${t.kind ?? "transition"} (${cap(t.condition)})`}</p>
          <div className="tr-meta small muted">
            {isNum(t.river_level_m) && <span>River {fmtNum(t.river_level_m, "m", 2)}</span>}
            {t.threshold && <span className="tr-threshold">Threshold: {t.threshold.name} {fmtNum(t.threshold.level_m ?? null, "m", 2)} ({t.threshold.label ?? (t.threshold.official ? "OFFICIAL" : "TEST")})</span>}
            {isNum(t.risk_score) && <span>Risk {t.risk_category} {t.risk_score}</span>}
            {isNum(t.trust_score) && <span>Trust {t.trust_score}</span>}
            {t.persistence && <span>{t.persistence.observations} observation(s) over {fmtNum(t.persistence.span_hours, "h", 1)}</span>}
            {(t.capabilities_granted ?? []).length > 0 && <span className="granted">Granted: {t.capabilities_granted!.map(cap).join(", ")}</span>}
            {(t.capabilities_revoked ?? []).length > 0 && <span className="revoked">Revoked: {t.capabilities_revoked!.map(cap).join(", ")}</span>}
          </div>
          {t.evidence_id && <Disclosure summary="Evidence used"><KV items={[
            ...(t.detail ? [["Decision", t.detail] as [string, string]] : []),
            ["Triggering evidence", <Id value={t.evidence_id} />],
            ["Supporting evidence", (t.supporting_evidence_ids ?? []).map((x) => <Id key={x} value={x} />)]]} /></Disclosure>}
        </li>
      ))}
    </ol>
  );
}

// ---- agents ----------------------------------------------------------------------------

function agentIcon(agent: string) {
  return agent.startsWith("Weather") ? "rain" : agent.startsWith("River") ? "river" : "traffic";
}

function AgentFacts({ card }: { card: AgentCard }) {
  const m = card.metrics;
  const n = (k: string) => (isNum(m[k]) ? (m[k] as number) : null);
  if (card.agent.startsWith("River")) {
    return <KV items={[
      ["River level", <strong>{fmtNum(n("level_m"), "m", 2)}</strong>],
      ["Trend", `${titleCase(String(m.trend ?? "unknown"))}${n("rate_m_per_hour") !== null ? ` (${n("rate_m_per_hour")! >= 0 ? "+" : ""}${fmtNum(n("rate_m_per_hour"), "m/h", 2)})` : ""}`],
      ["Thresholds", n("warning_level_m") !== null ? `warning ${fmtNum(n("warning_level_m"), "m", 2)} · danger ${fmtNum(n("danger_level_m"), "m", 2)} (${m.threshold_label})` : "not configured"],
      ["Quality", `${cap(String(m.quality ?? ""))}${n("qc_checked") ? ` · ${n("qc_accepted")}/${n("qc_checked")} passed QC` : ""}`],
      ["Evidence age", fmtAge(n("age_hours"))],
    ]} />;
  }
  if (card.agent.startsWith("Weather")) {
    return <KV items={[
      ["Rainfall (24 h)", <strong>{fmtNum(n("rain_24h_mm"), "mm", 1)}</strong>],
      ["Last 6 h", fmtNum(n("rain_6h_mm"), "mm", 1)],
      [`Forecast (${String(m.forecast_hours ?? 24)} h)`, fmtNum(n("forecast_mm"), "mm", 1)],
      ["Evidence age", fmtAge(n("age_hours"))],
    ]} />;
  }
  const routes = (m.routes as { id: string; name?: string; condition?: string; role?: string }[] | undefined) ?? [];
  return <KV items={[
    ...routes.map((r): [string, ReactNode] => [`${titleCase(r.role ?? "route")} route`,
      <Pill tone={r.condition === "BLOCKED" ? "danger" : r.condition === "SEVERE" || r.condition === "HEAVY" ? "warn" : "ok"} icon={false}>{cap(r.condition)}</Pill>]),
    ["Flood closures", ((m.closed_segments as string[] | undefined) ?? []).length ? (m.closed_segments as string[]).join("; ") : "none reported"],
    ["Retrieved", fmtTime(m.retrieved_at as string | undefined)],
  ]} />;
}

export function AgentCardView({ card, compact = false }: { card: AgentCard; compact?: boolean }) {
  return (
    <article className={`agent agent-${AGENT_TONE[card.status]}`} aria-label={card.agent}>
      <header className="agent-head">
        <span className="agent-icon"><Icon name={agentIcon(card.agent)} size={18} /></span>
        <h4>{card.agent}</h4>
        <Pill tone={AGENT_TONE[card.status]}>{card.status}</Pill>
      </header>
      <p className="agent-tells">{card.tells}</p>
      {!compact && <AgentFacts card={card} />}
      <div className={`agent-verdict ${card.accepted ? "ok" : "no"}`}>
        <Icon name={card.accepted ? "check" : "x"} size={14} />
        <span><strong>{card.accepted ? "Accepted" : "Not accepted"}.</strong> {card.why}</span>
      </div>
      {!compact && (
        <footer className="agent-foot small muted">
          <span>Source: {card.source}</span>
          {isNum(card.trust_score) && <span>Agent trust {card.trust_score}/100</span>}
          {card.evidence_id && <span>ID <Id value={card.evidence_id} /></span>}
        </footer>
      )}
    </article>
  );
}

// ---- capabilities ----------------------------------------------------------------------

export function CapabilityCardView({ card }: { card: CapabilityCard }) {
  return (
    <article className={`capability cap-${CAP_TONE[card.status]}`} aria-label={card.name}>
      <header className="cap-head">
        <h4>{card.name}</h4>
        <Pill tone={CAP_TONE[card.status]}>{card.status}</Pill>
      </header>
      <p className="cap-purpose">{card.purpose}</p>
      <p className="cap-reason"><strong>Why:</strong> {card.reason}</p>
      {card.change && <p className={`cap-change ${card.change === "REVOKED" ? "revoked" : "granted"}`}>
        <Icon name={card.change === "REVOKED" ? "lock" : "check"} size={13} /> {card.change === "REVOKED" ? "Revoked" : "Granted"} by the latest transition</p>}
      <KV items={[
        ["Current state", card.state],
        ["Permitted in", card.permitted_states.join(", ") || "no state"],
        ["Evidence required", card.evidence_requirements],
        ["Human approval", card.human_approval_required ? "Required — the system only proposes" : "Not required (internal / read-only)"],
      ]} />
    </article>
  );
}

export function CapabilityMini({ cards }: { cards: CapabilityCard[] }) {
  return (
    <ul className="cap-mini">
      {cards.map((c) => (
        <li key={c.capability}><span>{c.name}</span><Pill tone={CAP_TONE[c.status]}>{c.status}</Pill></li>
      ))}
    </ul>
  );
}
