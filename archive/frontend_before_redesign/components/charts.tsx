// Charts. Missing values stay missing (null -> no bar / no point); nothing is
// interpolated or padded to make a chart look complete.
import {
  Bar, BarChart, CartesianGrid, Cell, ComposedChart, Legend, Line, ReferenceLine,
  ResponsiveContainer, Scatter, Tooltip, XAxis, YAxis,
} from "recharts";
import type { HourlyPoint, SequenceStep, SeriesPoint } from "../types";
import { epoch, fmtNum, fmtShort, isNum, STATE_ORDER } from "../format";

const COLORS = {
  estimate: "#64748b", forecast: "#2563eb", level: "#0e7490", rejected: "#dc2626",
  warning: "#d97706", danger: "#b91c1c", state: "#7c3aed",
};

export function RainfallChart({ points }: { points: HourlyPoint[] }) {
  // One series, coloured per hour by kind. (Two stacked series fail: each hour has a
  // null in one of them, and Recharts then draws no bars at all.)
  const data = points.map((p, i) => ({
    i, time: p.time, kind: p.kind,
    value: isNum(p.precipitation_mm) ? p.precipitation_mm : null,
    probability: p.precipitation_probability_pct,
  }));
  const missing = data.filter((d) => d.value === null).length;
  const firstForecast = data.find((d) => d.kind === "FORECAST");
  const tickEvery = Math.max(1, Math.ceil(data.length / 7));
  const kindName = (k: string) => (k === "FORECAST" ? "Forecast" : "Model estimate (past)");
  return (
    <figure className="chart" aria-label="Hourly precipitation, past model estimates and forecast">
      <ResponsiveContainer width="100%" height={270}>
        <BarChart data={data} margin={{ top: 8, right: 16, bottom: 28, left: 8 }} barCategoryGap={0}>
          <CartesianGrid strokeDasharray="3 3" vertical={false} />
          <XAxis dataKey="i" interval={0} fontSize={11} tickLine={false}
                 ticks={data.filter((d) => d.i % tickEvery === 0).map((d) => d.i)}
                 tickFormatter={(i: number) => fmtShort(data[i]?.time)}
                 label={{ value: "Hour ending (IST)", position: "insideBottom", offset: -18, fontSize: 12 }} />
          <YAxis fontSize={11} label={{ value: "mm per hour", angle: -90, position: "insideLeft", fontSize: 12 }} />
          <Tooltip
            labelFormatter={(i: number) => `Hour ending ${fmtShort(data[i]?.time)} IST`}
            formatter={(v: number, _n: string, item: { payload?: { probability?: number | null; kind?: string } }) =>
              [`${fmtNum(v, "mm")} (chance ${fmtNum(item.payload?.probability ?? null, "%", 0)})`,
               kindName(item.payload?.kind ?? "")]} />
          <Legend verticalAlign="top" height={28}
                  payload={[{ value: "Model estimate (past)", type: "square", color: COLORS.estimate, id: "est" },
                            { value: "Forecast", type: "square", color: COLORS.forecast, id: "fc" }]} />
          {firstForecast && <ReferenceLine x={firstForecast.i} stroke="#0f172a" strokeDasharray="4 2"
                                           label={{ value: "now", position: "insideTopRight", fontSize: 11 }} />}
          <Bar dataKey="value" name="Precipitation" isAnimationActive={false}>
            {data.map((d) => <Cell key={d.i} fill={d.kind === "FORECAST" ? COLORS.forecast : COLORS.estimate} />)}
          </Bar>
        </BarChart>
      </ResponsiveContainer>
      <figcaption>
        Open-Meteo hour T is the total for the hour ending at T. Past hours are model estimates,
        not gauge observations. {missing > 0 ? `${missing} hour(s) have no value and are left blank.` : "No missing hours."}
      </figcaption>
    </figure>
  );
}

export function RiverChart({ points, rejected, warning, danger, official }: {
  points: SeriesPoint[]; rejected: SeriesPoint[];
  warning?: number | null; danger?: number | null; official?: boolean;
}) {
  const line = points.filter((p) => isNum(p.level_m)).map((p) => ({ t: epoch(p.time), level: p.level_m }));
  const bad = rejected.filter((p) => isNum(p.level_m)).map((p) => ({ t: epoch(p.time), rejected: p.level_m, code: p.qc_code }));
  const values = [...line.map((d) => d.level as number), ...(isNum(warning) ? [warning] : []), ...(isNum(danger) ? [danger] : [])];
  const lo = Math.min(...values), hi = Math.max(...values);
  const pad = Math.max(0.2, (hi - lo) * 0.1);
  const thresholdTag = official ? "official" : "TEST, not official";
  return (
    <figure className="chart" aria-label="River level over the last readings">
      <ResponsiveContainer width="100%" height={260}>
        <ComposedChart margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="t" type="number" scale="time" domain={["dataMin", "dataMax"]}
                 tickFormatter={(t: number) => fmtShort(new Date(t).toISOString())} fontSize={11}
                 label={{ value: "Observation time (IST)", position: "insideBottom", offset: -18, fontSize: 12 }} />
          <YAxis domain={[Math.floor((lo - pad) * 10) / 10, Math.ceil((hi + pad) * 10) / 10]} fontSize={11}
                 label={{ value: "Level (m)", angle: -90, position: "insideLeft", fontSize: 12 }} />
          <Tooltip labelFormatter={(t: number) => `${fmtShort(new Date(t).toISOString())} IST`}
                   formatter={(v: number, name: string) => [fmtNum(v, "m", 2), name]} />
          <Legend verticalAlign="top" height={28} />
          {isNum(warning) && <ReferenceLine y={warning} stroke={COLORS.warning} strokeDasharray="6 3"
                                            label={{ value: `Warning ${warning} m (${thresholdTag})`, position: "insideTopLeft", fontSize: 11 }} />}
          {isNum(danger) && <ReferenceLine y={danger} stroke={COLORS.danger} strokeDasharray="6 3"
                                           label={{ value: `Danger ${danger} m (${thresholdTag})`, position: "insideTopLeft", fontSize: 11 }} />}
          <Line data={line} dataKey="level" name="QC-accepted reading" stroke={COLORS.level}
                strokeWidth={2} dot={{ r: 3 }} connectNulls={false} isAnimationActive={false} />
          {bad.length > 0 && <Scatter data={bad} dataKey="rejected" name="Rejected by QC (not used)"
                                      fill={COLORS.rejected} shape="cross" isAnimationActive={false} />}
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}

export function StateTimelineChart({ steps }: { steps: SequenceStep[] }) {
  const data = steps.map((s) => ({
    hour: s.hour,
    level: isNum(s.river_level_m) ? s.river_level_m : null,
    state: s.state_after ? STATE_ORDER.indexOf(s.state_after) : null,
    stateName: s.state_after, risk: s.risk_level, outcome: s.outcome,
  }));
  return (
    <figure className="chart" aria-label="Simulated river level and disaster state over the flood cycle">
      <ResponsiveContainer width="100%" height={280}>
        <ComposedChart data={data} margin={{ top: 8, right: 16, bottom: 28, left: 8 }}>
          <CartesianGrid strokeDasharray="3 3" />
          <XAxis dataKey="hour" type="number" domain={["dataMin", "dataMax"]} fontSize={11}
                 label={{ value: "Simulated river hour (from 2026-10-10 00:00 UTC)", position: "insideBottom", offset: -18, fontSize: 12 }} />
          <YAxis yAxisId="level" fontSize={11}
                 label={{ value: "Level (m)", angle: -90, position: "insideLeft", fontSize: 12 }} />
          <YAxis yAxisId="state" orientation="right" domain={[0, 3]} ticks={[0, 1, 2, 3]} width={88}
                 tickFormatter={(v: number) => STATE_ORDER[v] ?? ""} fontSize={11} />
          <Tooltip labelFormatter={(h: number) => `Hour ${h}`}
                   formatter={(v: number, name: string) =>
                     name === "Disaster state" ? [STATE_ORDER[v] ?? "—", name] : [fmtNum(v, "m", 2), name]} />
          <Legend verticalAlign="top" height={28} />
          <Line yAxisId="level" dataKey="level" name="Simulated river level" stroke={COLORS.level}
                dot={false} strokeWidth={2} connectNulls={false} isAnimationActive={false} />
          <Line yAxisId="state" dataKey="state" name="Disaster state" type="stepAfter"
                stroke={COLORS.state} strokeWidth={2} dot={{ r: 2 }} isAnimationActive={false} />
        </ComposedChart>
      </ResponsiveContainer>
    </figure>
  );
}
