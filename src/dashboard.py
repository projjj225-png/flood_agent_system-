"""Static HTML dashboard from orchestrator outputs (standard library only).

    python -m src.dashboard --timeline outputs/simulated_flood_event_timeline.json
    python -m src.dashboard --cycle outputs/flood_cycle.json
    python -m src.dashboard --timeline ... --cycle ... --output outputs/dashboard.html

Shows the current cycle (state, risk, evidence reasons, capability decisions)
and/or an event timeline (river level with thresholds, risk and state bands,
transitions and revoked capabilities). Simulated content is bannered.
"""
from __future__ import annotations

import argparse
import html
import json
import sys
from pathlib import Path
from typing import Any

from src.river_monitoring_agent import PROJECT_DIR

STATE_COLORS = {"NORMAL": "#2e7d32", "ALERT": "#f9a825", "EMERGENCY": "#c62828",
                "RECOVERY": "#1565c0"}


def _e(value: Any) -> str:
    return html.escape("—" if value is None else str(value))


def _chart(timeline: list[dict[str, Any]], warning: float | None,
           danger: float | None) -> str:
    points = [(i, r["level_m"]) for i, r in enumerate(timeline) if r.get("level_m") is not None]
    if not points:
        return "<p>No usable levels to plot.</p>"
    width, height, pad = 900, 260, 40
    values = [v for _, v in points] + [x for x in (warning, danger) if x is not None]
    low, high = min(values) - 0.2, max(values) + 0.2
    n = max(len(timeline) - 1, 1)
    x = lambda i: pad + (width - 2 * pad) * i / n
    y = lambda v: height - pad - (height - 2 * pad) * (v - low) / (high - low)
    bands = "".join(
        f'<rect x="{x(i) - (width - 2 * pad) / n / 2:.1f}" y="{height - pad + 6}" '
        f'width="{(width - 2 * pad) / n:.1f}" height="10" fill="{STATE_COLORS.get(r["state"], "#999")}">'
        f'<title>{_e(r["as_of"])}: {_e(r["state"])}</title></rect>'
        for i, r in enumerate(timeline))
    line = " ".join(f"{x(i):.1f},{y(v):.1f}" for i, v in points)
    rules = ""
    for value, label, color in ((warning, "warning", "#f9a825"), (danger, "danger", "#c62828")):
        if value is not None:
            rules += (f'<line x1="{pad}" x2="{width - pad}" y1="{y(value):.1f}" y2="{y(value):.1f}" '
                      f'stroke="{color}" stroke-dasharray="6 4"/>'
                      f'<text x="{width - pad + 4}" y="{y(value) + 4:.1f}" class="lbl">{label} '
                      f'{value} m</text>')
    return (f'<svg viewBox="0 0 {width + 70} {height + 20}" role="img" '
            f'aria-label="River level over time">'
            f'<line x1="{pad}" x2="{pad}" y1="{pad}" y2="{height - pad}" class="axis"/>'
            f'<text x="4" y="{y(high - 0.2) + 4:.1f}" class="lbl">{high - 0.2:.2f}</text>'
            f'<text x="4" y="{y(low + 0.2) + 4:.1f}" class="lbl">{low + 0.2:.2f}</text>'
            f'{rules}<polyline points="{line}" fill="none" class="lvl" stroke-width="2.5"/>'
            f'{bands}</svg>')


def render(timeline_doc: dict[str, Any] | None, cycle: dict[str, Any] | None) -> str:
    parts = []
    banner = ('<div class="banner">SIMULATED / TEST content: fabricated data and TEST '
              'thresholds. Not real Cauvery conditions and never an authorization.</div>')
    if cycle:
        risk = cycle["risk"]
        reasons = cycle["combined_assessment"]["reasons"]
        threshold_note = ("no thresholds configured" if risk["thresholds"].get(
            "warning_level_m") is None else "indicative (thresholds not official)"
            if risk["indicative"] else "official thresholds")
        parts.append(f"""<section><h2>Current cycle</h2>
{banner if cycle.get('mode') == 'SIMULATED' else ''}
<div class="cards">
<div class="card"><span>State</span><b style="color:{STATE_COLORS.get(cycle['state_after'], 'inherit')}">{_e(cycle['state_after'])}</b><small>from {_e(cycle['state_before'])} ({_e(cycle['transition']['reason'])})</small></div>
<div class="card"><span>Risk</span><b>{_e(risk['risk_level'])}</b><small>{threshold_note}</small></div>
<div class="card"><span>River</span><b>{_e(cycle['river']['last_known_level_m'])} m</b><small>{_e(cycle['river']['source_label'])}, {_e(cycle['river']['freshness'])}</small></div>
<div class="card"><span>Real-world authorization</span><b>{_e(cycle['real_world_authorization'])}</b><small>{_e(cycle['mode'])}</small></div>
</div>
<h3>Why</h3><ul>{''.join(f'<li><code>{_e(r)}</code></li>' for r in reasons) or '<li>All evidence conditions met.</li>'}</ul>
<h3>Capabilities</h3><table><tr><th>Capability</th><th>Allowed</th><th>Reason</th><th>Evidence reasons</th></tr>
{''.join(f"<tr><td>{_e(d['capability'])}</td><td>{'yes' if d['allowed'] else 'no'}</td><td>{_e(d['reason'])}</td><td>{_e(', '.join(d.get('evidence_reasons') or []))}</td></tr>" for d in cycle['capability_decisions'])}
</table></section>""")
    if timeline_doc:
        timeline = timeline_doc["timeline"]
        transitions = [r for r in timeline if r.get("transition")]
        thresholds = timeline_doc.get("thresholds") or {}
        warning, danger = thresholds.get("warning_level_m"), thresholds.get("danger_level_m")
        legend = " ".join(f'<span class="sw" style="background:{c}"></span>{s}'
                          for s, c in STATE_COLORS.items())
        parts.append(f"""<section><h2>Event timeline</h2>{banner}<p>{_e(timeline_doc.get('label'))}</p>
{_chart(timeline, warning, danger)}<p class="legend">State band: {legend}</p>
<h3>Transitions</h3><table><tr><th>Time (UTC)</th><th>Level (m)</th><th>Risk</th><th>New state</th><th>Reason</th><th>Revoked</th><th>Granted</th></tr>
{''.join(f"<tr><td class='nw'>{_e(r['as_of'][:16].replace('T', ' '))}</td><td>{_e(r['level_m'])}</td><td>{_e(r['risk'])}</td><td>{_e(r['state'])}</td><td>{_e(r['transition'])}</td><td>{_e(', '.join(r['revoked']))}</td><td>{_e(', '.join(r['granted']))}</td></tr>" for r in transitions)}
</table></section>""")
    body = "\n".join(parts) or "<p>No data. Run the orchestrator first.</p>"
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Flood Agent Dashboard</title>
<style>
:root{{--bg:#fafafa;--fg:#1b1b1b;--muted:#5f6368;--card:#fff;--line:#ddd;--lvl:#0b57d0}}
@media (prefers-color-scheme:dark){{:root{{--bg:#121212;--fg:#e8eaed;--muted:#9aa0a6;--card:#1e1e1e;--line:#3c4043;--lvl:#8ab4f8}}}}
body{{background:var(--bg);color:var(--fg);font:15px/1.5 system-ui,sans-serif;margin:0;padding:16px;max-width:1000px;margin:auto}}
.banner{{background:#fff3cd;color:#5c4400;padding:10px 14px;border-radius:6px;font-weight:600}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(200px,1fr));gap:12px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px;display:flex;flex-direction:column}}
.card span,small,.legend{{color:var(--muted)}} .card b{{font-size:1.4em}}
table{{border-collapse:collapse;width:100%;background:var(--card);display:block;overflow-x:auto}}
td,th{{border:1px solid var(--line);padding:6px 8px;text-align:left;font-size:13px}}
svg{{width:100%;height:auto;background:var(--card);border:1px solid var(--line);border-radius:8px}}
.lvl{{stroke:var(--lvl)}} .axis{{stroke:var(--muted)}} .lbl{{fill:var(--muted);font-size:11px}}
.nw{{white-space:nowrap}} section{{margin-top:24px}}
.sw{{display:inline-block;width:12px;height:12px;margin:0 4px 0 10px;vertical-align:middle}}
</style></head><body><h1>Flood Agent Dashboard: Upper Anicut</h1>{body}</body></html>"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build the static dashboard.")
    parser.add_argument("--timeline", default=None)
    parser.add_argument("--cycle", default=None)
    parser.add_argument("--output", default=str(PROJECT_DIR / "outputs" / "dashboard.html"))
    args = parser.parse_args(argv)
    load = lambda p: json.loads(Path(p).read_text(encoding="utf-8")) if p else None
    try:
        page = render(load(args.timeline), load(args.cycle))
    except (OSError, json.JSONDecodeError, KeyError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(page, encoding="utf-8")
    print(f"Dashboard written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
