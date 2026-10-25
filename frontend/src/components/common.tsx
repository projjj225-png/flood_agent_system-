import type { ReactNode } from "react";
import { humanize } from "../format";

export function Badge({ tone = "neutral", children, title }: {
  tone?: string; children: ReactNode; title?: string;
}) {
  return <span className={`badge badge-${tone}`} title={title}>{children}</span>;
}

export function Section({ id, title, subtitle, badges, children }: {
  id: string; title: string; subtitle?: ReactNode; badges?: ReactNode; children: ReactNode;
}) {
  return (
    <section className="panel" id={id} aria-labelledby={`${id}-title`}>
      <header className="panel-head">
        <div>
          <h2 id={`${id}-title`}>{title}</h2>
          {subtitle && <p className="panel-sub">{subtitle}</p>}
        </div>
        {badges && <div className="panel-badges">{badges}</div>}
      </header>
      {children}
    </section>
  );
}

/** Shown wherever data is missing; says why instead of showing a number. */
export function Unavailable({ children }: { children: ReactNode }) {
  return <div className="unavailable" role="status">{children}</div>;
}

export function Field({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div className="field">
      <dt>{label}</dt>
      <dd>{children}</dd>
    </div>
  );
}

export function CodeList({ items, tone = "neutral", empty = "None" }: {
  items?: (string | null | undefined)[]; tone?: string; empty?: string;
}) {
  const list = (items ?? []).filter((x): x is string => typeof x === "string" && x.length > 0);
  if (!list.length) return <span className="muted">{empty}</span>;
  return (
    <ul className="chips">
      {list.map((code, i) => (
        <li key={`${code}-${i}`}><Badge tone={tone} title={code}>{humanize(code)}</Badge></li>
      ))}
    </ul>
  );
}

export function SimulatedBanner({ text }: { text: string }) {
  return (
    <div className="sim-banner" role="note">
      <strong>SIMULATED DATA</strong> <span>{text}</span>
    </div>
  );
}
