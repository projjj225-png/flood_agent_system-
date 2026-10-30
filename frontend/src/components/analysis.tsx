// Historical CWC analysis, forecast validation and station comparison
// (/dashboard/analysis). Every value comes from the backend; nothing is computed
// here, and a predicted number is shown only when the backend returned one.
import type { AnalysisDoc, AnalysisStation, DashboardDoc } from "../types";
import { fmtAge, fmtNum, fmtShort, fmtTime, humanize, isNum } from "../format";
import { Badge, Field, Section, Unavailable } from "./common";
import { riverDataClass } from "./sections";
import { ForecastSection } from "./forecast";

const WINDOWS = ["1h", "3h", "6h", "24h"];
const pct = (x?: number | null) => (isNum(x) ? `${(x * 100).toFixed(1)}%` : "—");
const period = (from?: string | null, to?: string | null) =>
  from || to ? `${fmtTime(from)} – ${fmtTime(to)}` : "—";

/** Three river sources side by side, each with its own label, date and age. Values come
 *  from two separate backend responses and are never merged or substituted. */
export function DataSourcesStrip({ doc, analysis, analysisError }: {
  doc: DashboardDoc; analysis: AnalysisDoc | null; analysisError?: string | null;
}) {
  const r = doc.river;
  const live = riverDataClass(r?.source_label, r?.freshness?.status);
  const primary = analysis?.analysis?.primary_station;
  const hist = primary ? analysis?.analysis?.stations?.[primary] : undefined;
  const h = hist?.analysis;
  const f = analysis?.forecast;
  const horizons = f?.evaluation?.horizons ?? [];
  const predictions = (f?.live ?? []).filter((x) => x.status === "FORECAST" && isNum(x.predicted_level_m));
  return (
    <section aria-label="River data sources">
      <h2 className="strip-heading">River data sources (shown separately, never mixed)</h2>
      <div className="kpis" role="list">
        <article className="kpi" role="listitem">
          <h3>NWDP live feed</h3>
          <p className={`kpi-value ${r?.level?.role === "LAST_KNOWN_NOT_CURRENT" ? "is-stale" : ""}`}>
            {isNum(r?.level?.value_m) ? fmtNum(r?.level?.value_m, "m", 3) : <span className="muted">No valid reading</span>}
          </p>
          <p className="kpi-note">Observed {fmtTime(r?.level?.observed_at)} · {fmtAge(r?.freshness?.age_hours)}</p>
          <div className="kpi-badges"><Badge tone={live.tone}>{live.text}</Badge></div>
        </article>
        <article className="kpi" role="listitem">
          <h3>Local CWC export ({hist?.station_id ?? "—"})</h3>
          {h?.latest ? (
            <>
              <p className="kpi-value">{fmtNum(h.latest.level_m, "m", 3)}</p>
              <p className="kpi-note">Observed {fmtTime(h.latest.observed_at)} · {fmtAge(h.freshness?.age_hours)} · {h.accepted_readings} valid readings</p>
            </>
          ) : <p className="kpi-value muted">{analysisError ? "Unavailable" : "Loading…"}</p>}
          <div className="kpi-badges"><Badge tone="neutral">HISTORICAL CWC DATA</Badge></div>
          {analysisError && <p className="kpi-note">{analysisError}</p>}
        </article>
        <article className="kpi" role="listitem">
          <h3>River-level forecast</h3>
          {predictions.length ? predictions.map((x) => (
            <p key={x.horizon_hours} className="kpi-note">+{x.horizon_hours} h: <strong>{fmtNum(x.predicted_level_m, "m", 3)}</strong> ({fmtNum(x.interval_m?.[0], "", 3)}–{fmtNum(x.interval_m?.[1], "m", 3)})</p>
          )) : <p className="kpi-value muted">{analysis ? "No forecast" : analysisError ? "Unavailable" : "Loading…"}</p>}
          {analysis && <p className="kpi-note">{horizons.filter((x) => x.published).length} of {horizons.length} horizons beat persistence in validation{f?.input?.status && f.input.status !== "FRESH" ? `; inputs ${f.input.status.toLowerCase()}` : ""}</p>}
          <div className="kpi-badges"><Badge tone="info">MODEL PREDICTION</Badge></div>
        </article>
      </div>
    </section>
  );
}

function StationSummary({ st }: { st: AnalysisStation }) {
  const a = st.analysis;
  const stale = a?.freshness?.status !== "FRESH";
  return (
    <div className="card">
      <span className="eyebrow">{st.role === "primary" ? "Primary station" : "Upstream candidate"}</span>
      <h3>{st.station_id} <span className="muted">({st.source_name})</span></h3>
      <p>
        <Badge tone="neutral">HISTORICAL CWC DATA</Badge>{" "}
        <Badge tone={stale ? "warn" : "ok"}>{a?.freshness?.status ?? "UNKNOWN"}</Badge>{" "}
        {st.load?.identity?.verified ? <Badge tone="ok">identity verified</Badge> : <Badge tone="danger">identity NOT verified</Badge>}
      </p>
      {a?.latest ? (
        <dl className="fields compact">
          <Field label="Latest measured reading">{fmtNum(a.latest.level_m, "m", 3)}</Field>
          <Field label="Rows in file">{st.load?.rows_read ?? "—"} · {period(st.load?.first_observed_at, st.load?.last_observed_at)}</Field>
          <Field label="Valid readings">{a.accepted_readings ?? "—"} · {period(a.record?.period?.[0], a.record?.period?.[1])}</Field>
          <Field label="Observation time">{fmtTime(a.latest.observed_at)}</Field>
          <Field label="Evidence age">{fmtAge(a.freshness?.age_hours)}</Field>
          <Field label="Rejected readings">{a.rejected_readings ?? "—"} (never used; never set to zero)</Field>
          <Field label="Rejected because">{Object.entries(a.rejected_by_reason ?? {}).map(([k, v]) => `${humanize(k)} ${v}`).join(" · ") || "—"}</Field>
          <Field label="Completeness">{pct(a.completeness?.overall)} of hourly slots · {a.gaps?.count ?? "—"} gaps &gt; {a.gaps?.threshold_hours ?? 3} h</Field>
          <Field label="Record in file">max {fmtNum(a.record?.max_m, "m", 3)} ({fmtShort(a.record?.max_at)}) · min {fmtNum(a.record?.min_m, "m", 3)} ({fmtShort(a.record?.min_at)})</Field>
          <Field label="Last 24 h of data">mean {fmtNum(a.rolling_24h?.mean_m, "m", 3)} · std {fmtNum(a.rolling_24h?.std_m, "m", 3)} · {a.rolling_24h?.readings ?? "—"} readings</Field>
          <Field label="Time basis">{st.load?.provenance?.time_basis ?? "—"} · offset {st.load?.provenance?.assumed_utc_offset} ({st.load?.provenance?.timestamp_status})</Field>
          <Field label="Datum">{st.load?.provenance?.datum ?? "—"} ({st.load?.provenance?.datum_status ?? "—"}) · zero gauge {fmtNum(st.rl_zero_gauge_m, "m", 3)}</Field>
        </dl>
      ) : <Unavailable>No QC-accepted readings.</Unavailable>}
      {a?.trends && (
        <table className="table">
          <thead><tr><th>Window</th><th>Change</th><th>Rate</th><th>Direction</th><th>Readings</th></tr></thead>
          <tbody>
            {WINDOWS.map((w) => {
              const t = a.trends?.[w];
              return (
                <tr key={w}>
                  <td>{w} before the latest reading</td>
                  <td>{t?.status === "AVAILABLE" ? fmtNum(t.level_change_m, "m", 3) : <span className="muted">{t?.reason ?? "unavailable"}</span>}</td>
                  <td>{t?.status === "AVAILABLE" ? fmtNum(t.rate_of_change_m_per_hour, "m/h", 3) : "—"}</td>
                  <td>{t?.direction ?? "—"}</td>
                  <td>{t?.observations_in_window ?? "—"}</td>
                </tr>
              );
            })}
          </tbody>
        </table>
      )}
    </div>
  );
}

export function HistoricalAnalysisSection({ doc }: { doc: AnalysisDoc }) {
  const a = doc.analysis;
  const stations = Object.values(a?.stations ?? {});
  return (
    <Section id="historical" title="Historical CWC analysis"
      subtitle={`${a?.source?.raw_file ?? "CWC export"} · QC-accepted measured readings only; nothing interpolated`}
      badges={<Badge tone="neutral">HISTORICAL CWC DATA</Badge>}>
      <div className="alert" role="alert">
        <strong>HISTORICAL DATA — NOT REAL-TIME.</strong> {a?.source?.note}
      </div>
      {stations.length ? <div className="grid-2">{stations.map((st) => <StationSummary key={st.station_id} st={st} />)}</div>
        : <Unavailable>No station analysis in this response.</Unavailable>}
    </Section>
  );
}

export function StationComparisonSection({ doc }: { doc: AnalysisDoc }) {
  const a = doc.analysis;
  const rel = a?.upstream_relationship;
  return (
    <Section id="stations" title="Station comparison"
      subtitle={`${a?.upstream_candidate ?? "—"} (upstream candidate) → ${a?.primary_station ?? "—"}`}
      badges={rel && <Badge tone={rel.verdict === "VERIFIED" ? "ok" : "warn"}>Upstream relationship {humanize(rel.verdict)}</Badge>}>
      {!rel ? <Unavailable>No upstream candidate configured.</Unavailable> : (
        <>
          {rel.verdict !== "VERIFIED" && (
            <div className="alert" role="alert">
              <strong>Upstream relationship NOT verified.</strong> {rel.reason} {a?.upstream_candidate} is therefore not used in forecasts or risk.
            </div>
          )}
          <dl className="fields">
            <Field label="Same river, main stem">{rel.metadata?.same_river && rel.metadata?.both_main_stem ? "Yes (from the CWC file)" : "No / unknown"}</Field>
            <Field label="Straight-line distance">{fmtNum(rel.metadata?.straight_line_km, "km", 1)} (river distance is longer)</Field>
            <Field label="Zero-gauge levels">{fmtNum(rel.metadata?.zero_gauge_rl_m?.upstream, "m", 1)} upstream → {fmtNum(rel.metadata?.zero_gauge_rl_m?.primary, "m", 1)} · {rel.metadata?.zero_gauge_falls_downstream ? "falls downstream, consistent with the order" : "does not fall downstream"}</Field>
            <Field label="Strongest correlation of level changes">{rel.best ? `r = ${rel.best.correlation} at ${rel.best.lag_hours} h lag (${rel.best.change_hours}-h changes, ${rel.best.pairs} pairs)` : "—"}</Field>
            <Field label="Acceptance rule">|r| ≥ {rel.acceptance_rule?.min_abs_correlation} with ≥ {rel.acceptance_rule?.min_pairs} pairs at lags 0–{rel.acceptance_rule?.max_lag_hours} h (changes compared, so datums cannot mislead)</Field>
            <Field label="Used in forecast / risk">{rel.used_in_forecast ? "forecast" : "not in forecast"} · {rel.used_in_risk_assessment ? "risk" : "not in risk"}</Field>
          </dl>
          <p className="small muted">{rel.note}</p>
        </>
      )}
      {!!a?.excluded_stations?.length && (
        <p className="small">Excluded: {a.excluded_stations.map((x) => `${x.source_name} (${x.kind}) — ${x.reason}`).join("; ")}</p>
      )}
    </Section>
  );
}

export function AnalysisSections({ doc }: { doc: AnalysisDoc }) {
  return (
    <>
      <HistoricalAnalysisSection doc={doc} />
      <ForecastSection doc={doc} />
      <StationComparisonSection doc={doc} />
    </>
  );
}
