// Small presentational building blocks. Status colours are reserved for statuses
// and always come with a text label, never colour alone.
import type { ReactNode } from "react";
import type { Tone } from "../model";

const PATHS: Record<string, string> = {
  overview: "M3 3h7v9H3zM14 3h7v5h-7zM14 12h7v9h-7zM3 16h7v5H3z",
  monitor: "M3 12h4l3-8 4 16 3-8h4",
  risk: "M12 3a9 9 0 1 0 9 9M12 12l6-6M12 3v3M21 12h-3",
  evidence: "M9 11l3 3 8-8M20 12v7a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h9",
  state: "M4 21V4M4 4h13l-3 4 3 4H4",
  capability: "M12 2l8 4v6c0 5-3.5 8.5-8 10-4.5-1.5-8-5-8-10V6z",
  analytics: "M4 20V10M10 20V4M16 20v-7M22 20H2",
  history: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5M12 7v5l3 3",
  rain: "M7 15a4 4 0 0 1-.5-8A6 6 0 0 1 18 8a4 4 0 0 1 0 8M9 19l-1 2M13 19l-1 2M17 19l-1 2",
  river: "M2 8c2-2 4-2 6 0s4 2 6 0 4-2 6 0M2 14c2-2 4-2 6 0s4 2 6 0 4-2 6 0M2 20c2-2 4-2 6 0s4 2 6 0 4-2 6 0",
  traffic: "M5 17h14M6 17V9l2-4h8l2 4v8M6 9h12M8 21v-4M16 21v-4",
  check: "M5 12l5 5L20 7",
  x: "M6 6l12 12M18 6L6 18",
  alert: "M12 3l10 18H2zM12 10v5M12 18v.5",
  info: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 11v6M12 7.5v.5",
  play: "M7 4l13 8-13 8z",
  pause: "M7 4h4v16H7zM14 4h4v16h-4z",
  reset: "M3 12a9 9 0 1 0 3-6.7L3 8M3 3v5h5",
  clock: "M12 22a10 10 0 1 0 0-20 10 10 0 0 0 0 20zM12 6v6l4 2",
  lock: "M5 11h14v10H5zM8 11V7a4 4 0 0 1 8 0v4",
  user: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zM4 21a8 8 0 0 1 16 0",
  arrow: "M5 12h14M13 6l6 6-6 6",
  up: "M12 19V5M5 12l7-7 7 7",
  down: "M12 5v14M5 12l7 7 7-7",
};

export function Icon({ name, size = 18, title }: { name: string; size?: number; title?: string }) {
  return (
    <svg className="icon" width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor"
      strokeWidth={2} strokeLinecap="round" strokeLinejoin="round" aria-hidden={title ? undefined : true} role={title ? "img" : undefined}>
      {title && <title>{title}</title>}
      <path d={PATHS[name] ?? PATHS.info} />
    </svg>
  );
}

const STATUS_ICON: Partial<Record<Tone, string>> = { ok: "check", caution: "alert", warn: "alert", danger: "x", info: "info", unknown: "info" };

/** A status label: tone colour + icon + text. */
export function Pill({ tone = "neutral", children, icon = true, title }: { tone?: Tone; children: ReactNode; icon?: boolean; title?: string }) {
  return (
    <span className={`pill pill-${tone}`} title={title}>
      {icon && STATUS_ICON[tone] && <Icon name={STATUS_ICON[tone]!} size={12} />}
      {children}
    </span>
  );
}

export function Tag({ children, kind = "plain" }: { children: ReactNode; kind?: "plain" | "sim" | "real" | "test" }) {
  return <span className={`tag tag-${kind}`}>{children}</span>;
}

export function Card({ title, icon, actions, children, className = "", id, subtitle }: {
  title?: ReactNode; icon?: string; actions?: ReactNode; children: ReactNode; className?: string; id?: string; subtitle?: ReactNode;
}) {
  return (
    <section className={`card ${className}`} id={id} aria-label={typeof title === "string" ? title : undefined}>
      {(title || actions) && (
        <header className="card-head">
          <div className="card-title">
            {icon && <span className="card-icon"><Icon name={icon} size={16} /></span>}
            <div>
              {title && <h3>{title}</h3>}
              {subtitle && <p className="card-sub">{subtitle}</p>}
            </div>
          </div>
          {actions && <div className="card-actions">{actions}</div>}
        </header>
      )}
      {children}
    </section>
  );
}

export function PageHeader({ title, lead, children }: { title: string; lead: string; children?: ReactNode }) {
  return (
    <header className="page-head">
      <div>
        <h2>{title}</h2>
        <p>{lead}</p>
      </div>
      {children && <div className="page-head-extra">{children}</div>}
    </header>
  );
}

export function Empty({ children }: { children: ReactNode }) {
  return <div className="empty" role="status"><Icon name="info" size={16} /> <span>{children}</span></div>;
}

export function Stat({ label, value, note, tone }: { label: string; value: ReactNode; note?: ReactNode; tone?: Tone }) {
  return (
    <div className={`stat ${tone ? `stat-${tone}` : ""}`}>
      <span className="stat-label">{label}</span>
      <span className="stat-value">{value}</span>
      {note && <span className="stat-note">{note}</span>}
    </div>
  );
}

/** Circular score indicator. ``score`` null means no defensible number: the ring is
 *  dashed and the label says why, instead of drawing 0. */
export function Gauge({ score, tone, label, caption, size = 148 }: {
  score: number | null; tone: Tone; label: string; caption?: string; size?: number;
}) {
  const r = 52;
  const c = 2 * Math.PI * r;
  const filled = score === null ? 0 : Math.max(0, Math.min(100, score)) / 100;
  return (
    <div className={`gauge gauge-${tone}`} style={{ width: size }}>
      <svg viewBox="0 0 120 120" width={size} height={size} role="img"
        aria-label={score === null ? `${label}: no score` : `${label}: ${score} out of 100`}>
        <circle cx="60" cy="60" r={r} className="gauge-track" strokeDasharray={score === null ? "4 6" : undefined} />
        {score !== null && (
          <circle cx="60" cy="60" r={r} className="gauge-fill" strokeDasharray={`${c * filled} ${c}`}
            transform="rotate(-90 60 60)" />
        )}
        <text x="60" y={score === null ? 66 : 62} textAnchor="middle" className="gauge-score">{score === null ? "—" : score}</text>
        {score !== null && <text x="60" y="80" textAnchor="middle" className="gauge-out">/ 100</text>}
      </svg>
      <div className="gauge-label">{label}</div>
      {caption && <div className="gauge-caption">{caption}</div>}
    </div>
  );
}

/** Horizontal 0-100 bar with an accessible value. */
export function Meter({ value, tone = "info", label }: { value: number | null; tone?: Tone; label: string }) {
  const v = value === null ? 0 : Math.max(0, Math.min(100, value));
  return (
    <div className="meter" role="meter" aria-label={label} aria-valuemin={0} aria-valuemax={100} aria-valuenow={value ?? undefined}>
      <div className={`meter-fill meter-${tone}`} style={{ width: `${v}%` }} />
    </div>
  );
}

export function Disclosure({ summary, children }: { summary: ReactNode; children: ReactNode }) {
  return (
    <details className="disclosure">
      <summary>{summary}</summary>
      <div className="disclosure-body">{children}</div>
    </details>
  );
}

export function KV({ items }: { items: [ReactNode, ReactNode][] }) {
  return (
    <dl className="kv">
      {items.map(([k, v], i) => (
        <div key={i}><dt>{k}</dt><dd>{v}</dd></div>
      ))}
    </dl>
  );
}

/** Long technical identifiers, shortened for display (the full value stays in the title). */
export function Id({ value }: { value?: string | null }) {
  if (!value) return <span className="muted">—</span>;
  return <code className="id" title={value}>{value.length > 18 ? `${value.slice(0, 12)}…${value.slice(-4)}` : value}</code>;
}
