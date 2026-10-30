// Charts. Every point comes from the backend (simulation history, river series or
// weather hourly values); nothing is interpolated or invented. One y-axis per chart.
import {
  Bar, BarChart, CartesianGrid, Line, LineChart, ReferenceDot, ReferenceLine, ResponsiveContainer, Tooltip, XAxis, YAxis,
} from "recharts";
import type { Point } from "../model";
import { STATE_TONE } from "../model";
import { fmtNum, fmtShort } from "../format";
import { Empty } from "./ui";

const INK = "#1b3a63";
const GRID = "#e6ebf2";
const AXIS = { fontSize: 11, fill: "#5b6b80" };
const timeTick = (t: number) => fmtShort(new Date(t).toISOString()).replace(/^\d+ \w+,?\s*/, "");
const timeLabel = (t: unknown) => (typeof t === "number" ? fmtShort(new Date(t).toISOString()) + " IST" : "");

export interface RefLine { y: number; label: string; color: string }

export function TimeChart({ data, unit, digits = 2, refs = [], domain, height = 220, color = INK, name, step = false }: {
  data: Point[]; unit: string; digits?: number; refs?: RefLine[]; domain?: [number | "auto", number | "auto"];
  height?: number; color?: string; name: string; step?: boolean;
}) {
  if (data.length < 2) return <Empty>Not enough {name.toLowerCase()} readings yet to draw a chart.</Empty>;
  const values = data.map((d) => d.v).concat(refs.map((r) => r.y));
  const pad = (Math.max(...values) - Math.min(...values)) * 0.1 || 0.1;
  const auto: [number, number] = [Math.min(...values) - pad, Math.max(...values) + pad];
  return (
    <figure className="chart" aria-label={`${name} chart (${unit})`}>
      <ResponsiveContainer width="100%" height={height}>
        <LineChart data={data} margin={{ top: 8, right: 16, bottom: 0, left: 0 }}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={timeTick}
            tick={AXIS} minTickGap={36} stroke={GRID} />
          <YAxis domain={domain ?? auto} tick={AXIS} width={52} stroke={GRID}
            tickFormatter={(v: number) => v.toFixed(digits >= 3 ? 2 : digits > 1 ? 1 : 0)} />
          <Tooltip labelFormatter={timeLabel} formatter={(v: number) => [fmtNum(v, unit, digits), name]} />
          {refs.map((r) => (
            <ReferenceLine key={r.label} y={r.y} stroke={r.color} strokeDasharray="5 4"
              label={{ value: r.label, position: "insideTopLeft", fontSize: 11, fill: r.color }} />
          ))}
          <Line type={step ? "stepAfter" : "monotone"} dataKey="v" name={name} stroke={color} strokeWidth={2}
            dot={false} activeDot={{ r: 4 }} isAnimationActive={false} />
        </LineChart>
      </ResponsiveContainer>
    </figure>
  );
}

export function RainChart({ data, height = 200 }: { data: { t: number; v: number; kind: "observed" | "forecast" }[]; height?: number }) {
  if (!data.length) return <Empty>No rainfall values available.</Empty>;
  const rows = data.map((d) => ({ t: d.t, observed: d.kind === "observed" ? d.v : null, forecast: d.kind === "forecast" ? d.v : null }));
  const hasForecast = data.some((d) => d.kind === "forecast");
  return (
    <figure className="chart" aria-label="Hourly rainfall chart (mm)">
      {hasForecast && (
        <div className="chart-legend">
          <span><i className="sw sw-obs" /> Past hours</span><span><i className="sw sw-fc" /> Forecast</span>
        </div>
      )}
      <ResponsiveContainer width="100%" height={height}>
        <BarChart data={rows} margin={{ top: 8, right: 16, bottom: 0, left: 0 }} barCategoryGap={1}>
          <CartesianGrid stroke={GRID} vertical={false} />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]} tickFormatter={timeTick}
            tick={AXIS} minTickGap={36} stroke={GRID} />
          <YAxis tick={AXIS} width={52} stroke={GRID} />
          <Tooltip labelFormatter={timeLabel} formatter={(v: number, n: string) => [fmtNum(v, "mm", 1), n === "observed" ? "Rain (past hour)" : "Forecast"]} />
          <Bar dataKey="observed" fill="#2d5f9a" radius={[3, 3, 0, 0]} isAnimationActive={false} />
          {hasForecast && <Bar dataKey="forecast" fill="#9cc0e4" radius={[3, 3, 0, 0]} isAnimationActive={false} />}
        </BarChart>
      </ResponsiveContainer>
    </figure>
  );
}

/** The disaster state over time as a strip of labelled segments. */
export function StateStrip({ states }: { states: { t: number; state: string }[] }) {
  if (!states.length) return <Empty>No state history yet.</Empty>;
  const runs: { state: string; from: number; to: number; n: number }[] = [];
  for (const s of states) {
    const last = runs[runs.length - 1];
    if (last && last.state === s.state) { last.to = s.t; last.n += 1; } else runs.push({ state: s.state, from: s.t, to: s.t, n: 1 });
  }
  const total = states.length;
  return (
    <div className="state-strip" role="list" aria-label="Disaster state over time">
      {runs.map((r, i) => (
        <div key={i} role="listitem" className={`strip-seg strip-${STATE_TONE[r.state] ?? "neutral"}`}
          style={{ flexGrow: r.n, flexBasis: 0 }} title={`${r.state}: ${timeLabel(r.from)} – ${timeLabel(r.to)}`}>
          <span>{r.n / total > 0.08 ? r.state : ""}</span>
        </div>
      ))}
    </div>
  );
}

// ---- combined river level + rainfall panel ---------------------------------------------

export const RANGE_HOURS = [1, 6, 12, 24] as const;
export type RangeHours = (typeof RANGE_HOURS)[number];

/** Readings in the ``hours`` before the latest reading (backend timestamps; nothing added). */
export function windowPoints<T extends { t: number }>(points: T[], hours: number, end?: number): T[] {
  if (!points.length) return [];
  const last = end ?? points[points.length - 1].t;
  return points.filter((p) => p.t >= last - hours * 3_600_000 && p.t <= last);
}

export function windowStats(points: Point[]) {
  if (!points.length) return null;
  const values = points.map((p) => p.v);
  return { latest: values[values.length - 1], min: Math.min(...values), max: Math.max(...values),
    change: values[values.length - 1] - values[0], first: points[0].t, last: points[points.length - 1].t };
}

export interface Crossing { t: number; v: number; threshold: "warning" | "danger"; direction: "up" | "down" }

/** Where consecutive actual readings cross a configured threshold (marked at the later reading). */
export function thresholdCrossings(points: Point[], warning: number | null, danger: number | null): Crossing[] {
  const out: Crossing[] = [];
  for (let i = 1; i < points.length; i++) {
    const a = points[i - 1].v, b = points[i].v;
    for (const [name, level] of [["warning", warning], ["danger", danger]] as const) {
      if (level === null) continue;
      if (a < level && b >= level) out.push({ t: points[i].t, v: b, threshold: name, direction: "up" });
      if (a >= level && b < level) out.push({ t: points[i].t, v: b, threshold: name, direction: "down" });
    }
  }
  return out;
}

/** Y range that makes small changes visible; a threshold is drawn when it is near the data. */
export function levelDomain(points: Point[], thresholds: (number | null)[]): { domain: [number, number]; shown: number[] } {
  const values = points.map((p) => p.v);
  const lo = Math.min(...values), hi = Math.max(...values);
  const span = Math.max(hi - lo, 0.1);
  const near = Math.max(0.4, span * 1.5);
  const shown = thresholds.filter((x): x is number => x !== null && x >= lo - near && x <= hi + near);
  const all = [lo, hi, ...shown];
  const min = Math.min(...all), max = Math.max(...all);
  const pad = Math.max((max - min) * 0.08, 0.02);
  return { domain: [min - pad, max + pad], shown };
}

const fmtTimeTick = (t: number) => fmtShort(new Date(t).toISOString()).replace(/^\d+ \w+,?\s*/, "");

export function RiverRainPanel({ river, rain, rainLabel, range, onRange, showRain = true, height = 240 }: {
  river: { points: Point[]; warning: number | null; danger: number | null; recovery: number | null; official: boolean };
  rain: { t: number; v: number; kind: "observed" | "forecast" }[]; rainLabel: string;
  range: RangeHours; onRange: (r: RangeHours) => void; showRain?: boolean; height?: number;
}) {
  const pts = windowPoints(river.points, range);
  const stats = windowStats(pts);
  const kind = river.official ? "official" : "TEST";
  const selector = (
    <div className="segmented small-seg" role="radiogroup" aria-label="Time range">
      {RANGE_HOURS.map((h) => (
        <button key={h} role="radio" aria-checked={h === range} className={h === range ? "on" : ""} onClick={() => onRange(h)}>{h} h</button>
      ))}
    </div>
  );
  if (!stats || pts.length < 2) return <>{selector}<Empty>Not enough river readings in the last {range} h to draw a chart.</Empty></>;
  const { domain, shown } = levelDomain(pts, [river.warning, river.danger, river.recovery]);
  const crossings = thresholdCrossings(pts, river.warning, river.danger);
  const x: [number, number] = [stats.last - range * 3_600_000, stats.last];
  const rainIn = rain.filter((r) => r.t > x[0] && r.t <= x[1] + 3_600_000);
  const observed = rainIn.filter((r) => r.kind === "observed");
  const rainTotal = observed.reduce((s, r) => s + r.v, 0);
  const lines: { y: number; label: string; color: string }[] = [];
  if (river.danger !== null && shown.includes(river.danger)) lines.push({ y: river.danger, label: `Danger ${river.danger.toFixed(2)} m (${kind})`, color: "#c0392b" });
  if (river.warning !== null && shown.includes(river.warning)) lines.push({ y: river.warning, label: `Warning ${river.warning.toFixed(2)} m (${kind})`, color: "#b7791f" });
  if (river.recovery !== null && shown.includes(river.recovery)) lines.push({ y: river.recovery, label: `Recovery ${river.recovery.toFixed(2)} m (${kind})`, color: "#2d5f9a" });
  const offscreen = ([["Warning", river.warning], ["Danger", river.danger]] as [string, number | null][])
    .filter(([, v]) => v !== null && !shown.includes(v))
    .map(([n, v]) => `${n} ${(v as number).toFixed(2)} m is ${((v as number) - stats.max).toFixed(2)} m above the highest reading in view`);
  return (
    <div className="rr-panel">
      <div className="rr-head">
        {selector}
        <div className="rr-stats">
          <span><em>Latest</em> <strong>{fmtNum(stats.latest, "m", 2)}</strong></span>
          <span><em>Min</em> <strong>{fmtNum(stats.min, "m", 2)}</strong></span>
          <span><em>Max</em> <strong>{fmtNum(stats.max, "m", 2)}</strong></span>
          <span><em>Change</em> <strong className={stats.change > 0.005 ? "up" : stats.change < -0.005 ? "down" : ""}>
            {stats.change >= 0 ? "+" : "−"}{Math.abs(stats.change).toFixed(2)} m</strong></span>
          <span><em>Crossings</em> <strong>{crossings.length}</strong></span>
        </div>
      </div>
      <figure className="chart" aria-label={`River level, last ${range} h (m)`}>
        <div className="chart-legend">
          <span><i className="sw sw-line" /> River level (m)</span>
          {lines.map((r) => <span key={r.label}><i className="sw sw-dash" style={{ borderColor: r.color }} /> {r.label}</span>)}
          {crossings.length > 0 && <span><i className="sw sw-dot" /> threshold crossing</span>}
        </div>
        <ResponsiveContainer width="100%" height={height}>
          <LineChart data={pts} syncId="river-rain" margin={{ top: 14, right: 16, bottom: 0, left: 0 }}>
            <CartesianGrid stroke={GRID} vertical={false} />
            <XAxis dataKey="t" type="number" scale="time" domain={x} tickFormatter={fmtTimeTick} tick={AXIS} minTickGap={40} stroke={GRID} />
            <YAxis domain={domain} tick={AXIS} width={52} stroke={GRID}
              tickFormatter={(v: number) => v.toFixed(domain[1] - domain[0] < 0.5 ? 2 : 1)} />
            <Tooltip labelFormatter={timeLabel} formatter={(v: number) => [fmtNum(v, "m", 3), "River level"]} />
            {lines.map((r) => (
              <ReferenceLine key={r.label} y={r.y} stroke={r.color} strokeDasharray="5 4"
                label={{ value: r.label, position: "insideTopLeft", fontSize: 11, fill: r.color }} />
            ))}
            {/* "monotone" only shapes the drawn line; the plotted points are the backend's readings */}
            <Line type="monotone" dataKey="v" stroke={INK} strokeWidth={2} dot={pts.length <= 24 ? { r: 2 } : false}
              activeDot={{ r: 4 }} isAnimationActive={false} />
            {crossings.map((c, i) => (
              <ReferenceDot key={i} x={c.t} y={c.v} r={6} fill={c.threshold === "danger" ? "#d33a2c" : "#e0b11c"}
                stroke="#fff" strokeWidth={2}
                label={{ value: `${c.direction === "up" ? "▲" : "▼"} ${c.threshold}`, position: "top", fontSize: 10, fill: "#33445c" }} />
            ))}
          </LineChart>
        </ResponsiveContainer>
        {offscreen.length > 0 && <figcaption className="small muted">{offscreen.join(" · ")}.</figcaption>}
      </figure>
      {showRain && (
        <figure className="chart" aria-label={`Rainfall, last ${range} h (mm)`}>
          <div className="chart-legend">
            <span><i className="sw sw-obs" /> {rainLabel}</span>
            <span>{observed.length ? `${rainTotal.toFixed(1)} mm in this window` : "no hourly values in this window"}</span>
          </div>
          {rainIn.length ? (
            <ResponsiveContainer width="100%" height={110}>
              <BarChart data={rainIn.map((r) => ({ t: r.t - 1_800_000, observed: r.kind === "observed" ? r.v : null, forecast: r.kind === "forecast" ? r.v : null }))}
                syncId="river-rain" margin={{ top: 4, right: 16, bottom: 0, left: 0 }} barCategoryGap={1}>
                <CartesianGrid stroke={GRID} vertical={false} />
                <XAxis dataKey="t" type="number" scale="time" domain={x} tickFormatter={fmtTimeTick} tick={AXIS} minTickGap={40} stroke={GRID} />
                <YAxis tick={AXIS} width={52} stroke={GRID} />
                <Tooltip labelFormatter={(t) => (typeof t === "number" ? `Hour ending ${fmtShort(new Date(t + 1_800_000).toISOString())} IST` : "")}
                  formatter={(v: number, n: string) => [fmtNum(v, "mm", 1), n === "observed" ? "Rain" : "Forecast"]} />
                <Bar dataKey="observed" fill="#2d5f9a" radius={[3, 3, 0, 0]} isAnimationActive={false} />
                <Bar dataKey="forecast" fill="#9cc0e4" radius={[3, 3, 0, 0]} isAnimationActive={false} />
              </BarChart>
            </ResponsiveContainer>
          ) : <Empty>No rainfall values in the last {range} h.</Empty>}
        </figure>
      )}
    </div>
  );
}
