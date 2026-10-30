"""Reviewer-facing insights derived from ONE workflow result (src.river_agent_workflow).

Nothing here decides anything. Risk level, evidence eligibility, state transitions and
capability decisions are the workflow's own; this module only reads them and writes
them out in plain language for the dashboard:

  risk_score        Flood Risk Score 0-100 (src.risk_score)
  trust             Evidence Trust Score 0-100 (src.trust_score)
  risk_explanation  "Why this risk level?": contributors raising / lowering the risk
  agents            one card per agent (Weather, River, Traffic): status AVAILABLE /
                    STALE / MISSING / CONFLICTING, what the evidence says, accepted or not
  evidence_summary  accepted / rejected / stale / missing / conflicting counts
  state             accepted state, previous, proposed next, persistence progress, reason
  capabilities      one card per capability: ALLOWED / RESTRICTED / DENIED and why
  headline          one-sentence answer to "what is happening?"
"""

from __future__ import annotations

from typing import Any

from src.capability_control import (
    REAL_WORLD_CAPABILITIES,
    STATE_CAPABILITIES,
    DisasterState,
)
from src.risk_score import compute_risk_score, rain_term
from src.trust_score import compute_trust_score

STATE_ORDER = ("NORMAL", "ALERT", "EMERGENCY", "RECOVERY")
STATE_MEANING = {
    "NORMAL": "Conditions are stable; no configured warning criterion is persistently met.",
    "ALERT": "Warning criteria are met: closer monitoring and preparation are warranted.",
    "EMERGENCY": "Severe criteria are met with sufficient evidence.",
    "RECOVERY": "Conditions have improved after an emergency; emergency-only capabilities "
                "are revoked until the river is confirmed back to normal.",
}
CONDITION_TEXT = {
    "EMERGENCY": "Severe: the river is at or above the danger level (CRITICAL risk)",
    "ALERT": "Warning: the river is at or above the warning level (HIGH or worse risk)",
    "NORMAL": "Normal: LOW risk, river below the recovery level and steady or falling",
    "IMPROVED": "Improving: river below the recovery level and not rising, at most MODERATE risk",
    "ELEVATED": "Elevated: some indicators are up, but no transition condition is met",
    "INSUFFICIENT": "Insufficient evidence: no condition can be judged",
}
CAPABILITY_INFO = {
    "read_river": (
        "Read river data", "Read the River Agent's water-level evidence.",
        "Read-only: needs only a valid disaster state."),
    "request_evidence_reverification": (
        "Request evidence reverification",
        "Re-check conflicting river readings against an independent source.",
        "Internal: permitted from ALERT onwards; results stay inside the system and are "
        "labelled."),
    "prepare_advisory": (
        "Prepare advisory", "Draft a flood advisory for a human to review.",
        "Real-world: verified live, fresh, acceptable-quality, non-simulated evidence with no "
        "unresolved conflict, confirmed by the Evidence Layer."),
    "propose_evacuation_plan": (
        "Propose evacuation plan",
        "Draft an evacuation proposal for a human to review. Never executed by the system.",
        "Real-world: same evidence requirements as an advisory; permitted in ALERT and "
        "EMERGENCY only."),
    "request_human_emergency_authorization": (
        "Request human emergency authorization",
        "Ask a human authority to authorize emergency measures.",
        "Real-world: same evidence requirements; permitted in EMERGENCY only."),
}
REASON_TEXT = {
    "EVIDENCE_SIMULATED": "evidence is simulated",
    "EVIDENCE_NOT_VERIFIED_LIVE": "evidence is not verified live CWC data",
    "EVIDENCE_STALE": "evidence is stale",
    "EVIDENCE_STALE_AT_DECISION": "evidence was stale at decision time",
    "EVIDENCE_UNAVAILABLE": "evidence is unavailable",
    "EVIDENCE_QUALITY_NOT_ACCEPTABLE": "recent data quality is not acceptable",
    "EVIDENCE_HISTORICAL_FALLBACK": "only historical fallback data is available",
    "EVIDENCE_UNRESOLVED_CONFLICT": "sources disagree and the conflict is unresolved",
    "EVIDENCE_NOT_ASSESSED": "evidence could not be assessed",
    "EVIDENCE_MALFORMED": "evidence is malformed",
    "EVIDENCE_INTEGRITY_FAILED": "evidence failed its integrity check",
    "EVIDENCE_LAYER_INSUFFICIENT": "the Evidence Layer found the evidence insufficient",
    "AUDIT_WRITE_FAILED": "the decision could not be audited",
    "AUDIT_TRAIL_INCOMPLETE": "the audit trail is incomplete",
    "WEATHER_NOT_PROVIDED": "no weather evidence was provided",
    "WEATHER_STALE": "weather evidence is stale",
    "WEATHER_UNAVAILABLE": "the weather service returned no data",
    "TRAFFIC_ROUTE_BLOCKED": "an evacuation route is blocked",
    "TRAFFIC_UNAVAILABLE": "traffic evidence is unavailable",
    "TRAFFIC_NOT_PROVIDED": "no traffic evidence was provided",
    "TRAFFIC_STALE": "traffic evidence is stale",
}


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def reason_text(code: Any) -> str:
    if not isinstance(code, str):
        return str(code)
    if code in REASON_TEXT:
        return REASON_TEXT[code]
    if code.startswith("C") and "_" in code and code.split("_", 1)[0][1:].isdigit():
        return "claim " + code.split("_", 1)[1].replace("_", " ").lower()
    return code.replace("_", " ").lower()


def allowlist(state: str) -> frozenset[str]:
    try:
        return STATE_CAPABILITIES[DisasterState(state)]
    except ValueError:
        return frozenset()


def _record(result: dict[str, Any], kind: str) -> dict[str, Any]:
    for rec in _obj(result.get("evidence_layer")).get("records") or []:
        if isinstance(rec, dict) and rec.get("kind") == kind:
            return rec
    return {}


# --------------------------------------------------------------------------
# Agent cards
# --------------------------------------------------------------------------

def river_card(result: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    rec = _record(result, "RIVER_LEVEL")
    ev = _obj(result.get("evidence"))
    eq = _obj(_obj(result.get("risk_assessment")).get("evidence_quality"))
    value = _obj(rec.get("value"))
    fresh = _obj(rec.get("freshness"))
    th = _obj(ev.get("threshold_evaluation"))
    level = _num(value.get("river_level_m"))
    rate = _num(value.get("rate_of_change_m_per_hour"))
    blocking = list(eq.get("river_blocking_reasons") or [])
    simulated = rec.get("evidence_class") == "SIMULATED"
    unresolved = "EVIDENCE_UNRESOLVED_CONFLICT" in blocking or \
        (_num(result.get("conflicts_remaining")) or 0) > 0
    if level is None or rec.get("evidence_class") in (None, "UNAVAILABLE"):
        status = "MISSING"
    elif unresolved:
        status = "CONFLICTING"
    elif fresh.get("status") == "STALE":
        status = "STALE"
    else:
        status = "AVAILABLE"
    accepted = eq.get("river_usable_for_hazard") is True
    warning, danger = _num(th.get("warning_level_m")), _num(th.get("danger_level_m"))
    official = th.get("threshold_is_official") is True
    if level is None:
        tells = "No river reading is available, so the flood risk cannot be judged."
    else:
        where = th.get("status")
        position = {"AT_OR_ABOVE_DANGER": "above the danger level",
                    "AT_OR_ABOVE_WARNING": "above the warning level",
                    "BELOW_WARNING": "below the warning level"}.get(where, "not compared")
        direction = value.get("trend_direction")
        motion = (f"{str(direction).lower()} {abs(rate):.2f} m/h" if rate is not None
                  and direction else "trend unavailable")
        tells = (f"River at {level:.2f} m, {position}"
                 + (f" ({'official' if official else 'TEST'} warning {warning:.2f} m)"
                    if warning is not None else "") + f", {motion}.")
    if accepted:
        why = ("Fresh reading that passed quality control; used as the primary hazard "
               "indicator." + (" Simulated: valid inside the simulation only." if simulated
                               else ""))
    else:
        why = "Not used for the risk assessment: " + (
            ", ".join(reason_text(r) for r in blocking) or "no usable reading") + "."
    quality = _obj(rec.get("quality"))
    agent_trust = _obj(_obj(trust.get("agents")).get("river"))
    return {
        "agent": "River Agent", "status": status, "accepted": accepted,
        "is_simulated": simulated, "tells": tells, "why": why,
        "evidence_class": rec.get("evidence_class") or "UNAVAILABLE",
        "metrics": {
            "level_m": level, "observed_at": rec.get("observed_at"),
            "trend": value.get("trend_direction"), "rate_m_per_hour": rate,
            "threshold_status": th.get("status"), "warning_level_m": warning,
            "danger_level_m": danger, "thresholds_official": official,
            "threshold_label": "OFFICIAL" if official else "TEST / RESEARCH THRESHOLDS",
            "age_hours": _num(fresh.get("age_hours")),
            "max_age_hours": _num(fresh.get("max_age_hours")),
            "freshness": fresh.get("status"),
            "quality": quality.get("recent_quality"),
            "qc_accepted": quality.get("qc_accepted"), "qc_checked": quality.get("qc_checked"),
            "station_id": _obj(rec.get("location")).get("station_id"),
        },
        "source": ("SIMULATED river data (generated by this system)" if simulated else
                   _obj(rec.get("source")).get("provider") or "CWC river telemetry"
                   if level is not None else "None"),
        "source_label": _obj(rec.get("source")).get("label"),
        "trust_score": agent_trust.get("score"),
        "evidence_id": rec.get("evidence_id"),
        "reasons": blocking,
    }


def weather_card(result: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    rec = _record(result, "WEATHER_MODEL")
    wx = _obj(_obj(_obj(result.get("risk_assessment")).get("supporting_evidence")).get("weather"))
    fresh = _obj(rec.get("freshness"))
    codes = [f.get("code") for f in _obj(rec.get("validation")).get("findings") or []
             if isinstance(f, dict)]
    simulated = rec.get("evidence_class") == "SIMULATED"
    reasons = list(wx.get("not_used_reasons") or [])
    if not rec or rec.get("evidence_class") == "UNAVAILABLE":
        status = "MISSING"
    elif "MODE_MISMATCH" in codes or "TIME_MISMATCH" in codes:
        status = "CONFLICTING"
    elif fresh.get("status") == "STALE":
        status = "STALE"
    else:
        status = "AVAILABLE"
    observed = _num(wx.get("observed_24h_mm"))
    six = _num(wx.get("observed_6h_mm"))
    forecast = _num(wx.get("forecast_mm"))
    current = _obj(_obj(_obj(result.get("weather_evidence")).get("current_weather")).get("values"))
    if status == "MISSING":
        tells = "No rainfall evidence: the rainfall indicators cannot be evaluated."
    else:
        parts = []
        if observed is not None:
            parts.append(f"{rain_term(observed).lower()} rain in the past 24 h ({observed:.1f} mm)")
        if forecast is not None:
            parts.append(f"{rain_term(forecast).lower()} rain forecast for the next "
                         f"{wx.get('forecast_hours') or 24} h ({forecast:.1f} mm)")
        tells = (parts[0][0].upper() + "; ".join(parts)[1:] + ".") if parts else \
            "Weather evidence has no rainfall totals."
    accepted = wx.get("used") is True
    why = (("Fresh, valid weather evidence; used for the rainfall rules."
            + (" Simulated: valid inside the simulation only." if simulated else
               " Numerical model output, not a rain-gauge reading."))
           if accepted else "Not used: " + (", ".join(reason_text(r) for r in reasons)
                                            or "no usable weather evidence") + ".")
    agent_trust = _obj(_obj(trust.get("agents")).get("weather"))
    return {
        "agent": "Weather Agent", "status": status, "accepted": accepted,
        "is_simulated": simulated, "tells": tells, "why": why,
        "evidence_class": rec.get("evidence_class") or "UNAVAILABLE",
        "metrics": {"rain_24h_mm": observed, "rain_6h_mm": six, "forecast_mm": forecast,
                    "forecast_hours": wx.get("forecast_hours"),
                    "current_precipitation_mm": _num(current.get("precipitation")),
                    "temperature_c": _num(current.get("temperature_2m")),
                    "retrieved_at": rec.get("retrieved_at") or wx.get("retrieved_at"),
                    "age_hours": _num(fresh.get("age_hours")),
                    "max_age_hours": _num(fresh.get("max_age_hours")),
                    "freshness": fresh.get("status")},
        "source": ("SIMULATED weather (generated by this system)" if simulated else
                   "Open-Meteo numerical weather model" if rec else "None"),
        "trust_score": agent_trust.get("score"),
        "evidence_id": rec.get("evidence_id"),
        "reasons": reasons,
    }


def traffic_card(result: dict[str, Any], trust: dict[str, Any]) -> dict[str, Any]:
    tr = _obj(result.get("traffic_evidence"))
    se = _obj(result.get("state_evaluation"))
    tv = _obj(se.get("traffic"))
    agent_class = _obj(_obj(se.get("agent_evidence")).get("traffic")).get("evidence_class")
    simulated = tr.get("is_simulated") is True
    if not tr or tr.get("status") not in ("OK", "PARTIAL"):
        status = "MISSING"
    elif agent_class == "CONFLICTING":
        status = "CONFLICTING"
    elif tv.get("status") == "STALE":
        status = "STALE"
    else:
        status = "AVAILABLE"
    routes = []
    for rid, route in _obj(tr.get("routes")).items():
        route = _obj(route)
        routes.append({"id": rid, "name": route.get("name"), "role": route.get("role"),
                       "condition": route.get("condition"), "passable": route.get("passable"),
                       "reason": route.get("reason")})
    segments = [_obj(s) for s in _obj(tr.get("segments")).values()]
    closed = [s.get("name") for s in segments if s.get("road_closure")]
    congested = [s.get("name") for s in segments if s.get("condition") in ("HEAVY", "SEVERE")]
    if status == "MISSING":
        tells = "No road evidence: evacuation routes cannot be confirmed."
    elif closed:
        tells = (f"{len(closed)} road segment(s) closed by flooding; "
                 + ", ".join(f"{(r['name'] or r['id']).split(' via ')[-1]} "
                             f"{str(r['condition']).replace('_', ' ').lower()}"
                             for r in routes) + ".")
    elif congested:
        tells = f"{len(congested)} road segment(s) heavily congested; no closures reported."
    else:
        tells = "Roads flowing normally; all evacuation routes usable."
    accepted = tv.get("status") in ("ROUTES_USABLE", "ROUTE_BLOCKED", "ROUTES_UNCERTAIN")
    reasons = list(tv.get("reasons") or [])
    why = ("Accepted for the RECOVERY exit check (evacuation routes must be usable before "
           "returning to NORMAL). Traffic never escalates the disaster state."
           if accepted else "Not usable: " + (", ".join(reason_text(r) for r in reasons)
                                              or "no traffic evidence") + ".")
    if status == "CONFLICTING":
        why = ("Conflicts with the river evidence: a route is blocked while the river looks "
               "normal, so the system will not confirm a return to NORMAL.")
    agent_trust = _obj(_obj(trust.get("agents")).get("traffic"))
    return {
        "agent": "Traffic Agent", "status": status, "accepted": accepted,
        "is_simulated": simulated, "tells": tells, "why": why,
        "evidence_class": agent_class or "UNAVAILABLE",
        "metrics": {"routes": routes, "closed_segments": closed,
                    "congested_segments": congested,
                    "segments_available": _obj(tr.get("summary")).get("segments_available"),
                    "segments_total": _obj(tr.get("summary")).get("segments_total"),
                    "retrieved_at": tr.get("retrieved_at"),
                    "age_hours": _num(tv.get("age_hours")),
                    "max_age_hours": _num(tv.get("max_age_hours")),
                    "route_status": tv.get("status")},
        "source": ("SIMULATED traffic (generated by this system)" if simulated else
                   "TomTom live traffic flow" if tr else "None"),
        "trust_score": agent_trust.get("score"),
        "evidence_id": None,
        "reasons": reasons,
    }


def evidence_summary(cards: dict[str, dict[str, Any]], result: dict[str, Any]) -> dict[str, Any]:
    layer = _obj(result.get("evidence_layer"))
    statuses = [c["status"] for c in cards.values()]
    rev = _obj(result.get("reverification"))
    return {
        "accepted": sum(1 for c in cards.values() if c["accepted"]),
        "rejected": sum(1 for c in cards.values() if not c["accepted"]
                        and c["status"] != "MISSING"),
        "stale": statuses.count("STALE"), "missing": statuses.count("MISSING"),
        "conflicting": statuses.count("CONFLICTING"),
        "records": len(layer.get("records") or []),
        "records_by_class": _obj(_obj(layer.get("summary")).get("records_by_class")),
        "claims_by_status": _obj(_obj(layer.get("summary")).get("claims_by_status")),
        "reverification": {"status": rev.get("status"), "outcome": rev.get("outcome"),
                           "attempts": rev.get("attempts")},
        "real_world_sufficient": layer.get("real_world_evidence_sufficient") is True,
        "deny_reasons": [reason_text(r) for r in layer.get("deny_reasons") or []],
    }


# --------------------------------------------------------------------------
# Risk explanation
# --------------------------------------------------------------------------

def risk_explanation(score: dict[str, Any], trust: dict[str, Any],
                     risk: dict[str, Any]) -> dict[str, Any]:
    lines = [{"label": f["label"], "value": f["value_text"], "detail": f["detail"],
              "level": f["level"], "direction": f["direction"],
              "context_only": f.get("context_only", False)} for f in score["factors"]]
    lines.append({"label": "Evidence trust",
                  "value": f"{trust['label'].title()} ({trust['score']}/100"
                           + (", simulated)" if trust.get("is_simulated") else ")"),
                  "detail": "Reliability of the evidence; does not change the risk level.",
                  "level": None, "direction": "neutral", "context_only": True})
    category = score["category"]
    combined = (f"{category.title()} flood risk" if category != "INDETERMINATE"
                else "Risk indeterminate: no defensible assessment")
    return {
        "lines": lines, "combined": combined, "category": category,
        "raising": [f["label"] for f in score["factors"] if f["direction"] == "raises"],
        "lowering": [f["label"] for f in score["factors"] if f["direction"] == "lowers"],
        "missing": score.get("missing") or [],
        "adjustments": score.get("adjustments") or [],
        "rules": [{"rule_id": f.get("rule_id"), "level": f.get("hazard_level"),
                   "description": f.get("description")}
                  for f in risk.get("risk_factors") or [] if isinstance(f, dict)],
        "trace": list(risk.get("decision_trace") or []),
        "uncertainties": [u.get("description") for u in risk.get("uncertainties") or []
                          if isinstance(u, dict)],
    }


# --------------------------------------------------------------------------
# State and capabilities
# --------------------------------------------------------------------------

def _rule_text(rule: dict[str, Any]) -> str:
    when = " or ".join(rule.get("when") or [])
    text = (f"{rule.get('to')} when the evidence is {when} for "
            f"{rule.get('min_observations')} observation(s)")
    if rule.get("min_hours"):
        text += f" over at least {rule['min_hours']:g} h"
    if rule.get("requires_traffic"):
        text += " and every evacuation route is usable"
    return text + "."


# Conditions that, on their own, justify staying in each state.
SUPPORTING_CONDITIONS = {"NORMAL": ("NORMAL",), "ALERT": ("ALERT",), "EMERGENCY": ("EMERGENCY",),
                         "RECOVERY": ("IMPROVED", "NORMAL")}


def why_state(current: str | None, condition: str | None, accepted: bool,
              progress: dict[str, Any] | None, traffic_hold: str | None) -> str:
    """Why the ACCEPTED state is what it is (it can differ from the latest evidence,
    which may still be accumulating persistence)."""
    text = CONDITION_TEXT.get(condition, condition or "unknown")
    if accepted:
        return f"Entered {current} on this evaluation: {text}."
    if condition == "INSUFFICIENT":
        return (f"{current} is kept: the latest evidence is insufficient, and missing or "
                "unusable evidence never changes the state.")
    if traffic_hold:
        return (f"{current} is kept: the river evidence meets the exit criteria, but the "
                "evacuation routes are not confirmed usable.")
    if progress:
        return (f"{current} is kept while the evidence builds up: the latest evidence is "
                f"{condition} ({text.split(':')[0].lower()}), and {current} → "
                f"{progress['to']} needs {progress.get('required_observations')} "
                f"observation(s) (have {progress.get('observations')}).")
    if condition in SUPPORTING_CONDITIONS.get(current or "", ()):
        return f"{text}; the {current} criteria are still met."
    return (f"{current} is kept by hysteresis: the latest evidence is {condition} "
            f"({text.split(':')[0].lower()}), which meets no transition rule out of {current}.")


def state_view(result: dict[str, Any], state_config: dict[str, Any] | None) -> dict[str, Any]:
    se = _obj(result.get("state_evaluation"))
    record = _obj(se.get("record"))
    last = _obj(record.get("last_transition"))
    current = se.get("resulting_state") or record.get("state")
    proposed = se.get("proposed_state")
    persistence = _obj(se.get("persistence"))
    rules = []
    for rule in _obj(_obj(state_config).get("rules")).get(current) or []:
        if isinstance(rule, dict):
            rules.append({"to": rule.get("to"), "kind": rule.get("kind"),
                          "text": _rule_text(rule)})
    progress = None
    if persistence and proposed and proposed != current:
        progress = {"to": proposed,
                    "observations": persistence.get("observations"),
                    "required_observations": persistence.get("required_observations"),
                    "span_hours": persistence.get("span_hours"),
                    "required_hours": persistence.get("required_hours"),
                    "traffic_required": persistence.get("traffic_required"),
                    "traffic_status": persistence.get("traffic_status")}
    accepted = se.get("outcome") == "TRANSITION_ACCEPTED"
    condition = se.get("condition")
    return {
        "why": why_state(current, condition, accepted, progress, se.get("traffic_hold")),
        "current": current, "evaluated_from": se.get("current_state"),
        "previous": last.get("from"), "proposed_next": proposed if proposed != current else None,
        "transition_this_evaluation": accepted,
        "outcome": se.get("outcome"), "condition": condition,
        "condition_text": CONDITION_TEXT.get(condition, condition),
        "meaning": STATE_MEANING.get(current),
        "reason": se.get("reason"), "reasons": list(se.get("reasons") or []),
        "progress": progress, "traffic_hold": se.get("traffic_hold"),
        "last_transition": last or None,
        "entered_at": record.get("entered_at"),
        "evaluated_at": se.get("evaluated_at"), "observed_at": se.get("observed_at"),
        "evidence_used": {"river_evidence_id": se.get("evidence_id"),
                          "risk_assessment_id": se.get("risk_assessment_id"),
                          "risk_level": se.get("risk_level"),
                          "supporting_evidence_ids": list(se.get("supporting_evidence_ids")
                                                          or [])},
        "next_rules": rules, "scope": se.get("scope"),
        "threshold_check": se.get("threshold_check"),
        "applies_to": se.get("state_applies_to"),
        "order": list(STATE_ORDER),
        "selection": "AUTOMATIC",
    }


def capability_cards(result: dict[str, Any]) -> list[dict[str, Any]]:
    state = result.get("capability_state") or result.get("disaster_state")
    changes = _obj(result.get("capability_changes"))
    added = set(changes.get("allowlist_added") or [])
    revoked = set(changes.get("allowlist_revoked") or [])
    simulated = result.get("source_label") == "SIMULATED_TEST_DATA" or \
        result.get("mode") == "SIMULATED"
    cards = []
    for d in result.get("capability_decisions") or []:
        if not isinstance(d, dict):
            continue
        cap = d.get("capability")
        name, purpose, requirements = CAPABILITY_INFO.get(
            cap, (str(cap).replace("_", " ").capitalize(), "", ""))
        real_world = cap in REAL_WORLD_CAPABILITIES
        reason = d.get("reason")
        evidence_reasons = [reason_text(r) for r in d.get("evidence_reasons") or []]
        if d.get("allowed"):
            status = "ALLOWED"
            why = (f"Permitted in {state}; the evidence gate passed, so it can be put to a "
                   "human for approval." if real_world else f"Permitted in {state}.")
        elif reason == "CAPABILITY_DENIED":
            status = "DENIED"
            why = f"Not on the {state} allowlist."
            if cap in revoked:
                why = f"Revoked on entering {state}."
            elif state == "RECOVERY" and cap in allowlist("EMERGENCY") - allowlist("RECOVERY"):
                why = "Emergency-only capability: revoked during RECOVERY."
        else:
            status = "RESTRICTED"
            if simulated and real_world:
                why = (f"Permitted in {state}, but simulated evidence can never authorize a "
                       "real-world action (simulation exercise only).")
            else:
                why = (f"Permitted in {state}, but blocked: "
                       + (", ".join(dict.fromkeys(evidence_reasons)) or reason_text(reason))
                       + ".")
        cards.append({
            "capability": cap, "name": name, "purpose": purpose, "status": status,
            "reason": why, "reason_code": reason, "evidence_requirements": requirements,
            "human_approval_required": real_world, "capability_class": d.get("capability_class"),
            "state": state,
            "permitted_states": [s for s in STATE_ORDER if cap in allowlist(s)],
            "change": "GRANTED" if cap in added else "REVOKED" if cap in revoked else None,
            "executes_actions": False,
        })
    return cards


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def build_insights(result: dict[str, Any], scoring: dict[str, Any],
                   risk_config: dict[str, Any],
                   state_config: dict[str, Any] | None = None) -> dict[str, Any]:
    """All insight blocks for one workflow result (raw, before dashboard sanitizing)."""
    result = _obj(result)
    risk = _obj(result.get("risk_assessment"))
    thresholds = _obj(_obj(result.get("evidence")).get("threshold_evaluation"))
    traffic_view = _obj(_obj(result.get("state_evaluation")).get("traffic"))
    score = compute_risk_score(risk, thresholds, traffic_view, risk_config, scoring)
    trust = compute_trust_score(result, scoring)
    cards = {"weather": weather_card(result, trust), "river": river_card(result, trust),
             "traffic": traffic_card(result, trust)}
    state = state_view(result, state_config)
    capabilities = capability_cards(result)
    simulated = risk.get("is_simulated") is True or any(c["is_simulated"] for c in cards.values())
    river = cards["river"]["metrics"]
    if river["level_m"] is not None:
        trend = next((f["value_text"] for f in score["factors"] if f["key"] == "river_trend"),
                     str(river.get("trend") or "").lower() or None)
        what = f"River at {river['level_m']:.2f} m" + (f", {trend.lower()}" if trend else "")
    else:
        what = "No current river reading"
    risk_text = (f"{score['category']} risk ({score['score']}/100)" if score["available"]
                 else "risk INDETERMINATE")
    allowed = sum(1 for c in capabilities if c["status"] == "ALLOWED")
    headline = (("SIMULATED: " if simulated else "") + f"{what}; {risk_text}; "
                f"disaster state {state['current']}; evidence trust {trust['score']}/100; "
                f"{allowed} of {len(capabilities)} capabilities allowed.")
    return {
        "is_simulated": simulated, "headline": headline,
        "risk_score": score, "trust": trust,
        "risk_explanation": risk_explanation(score, trust, risk),
        "agents": cards, "evidence_summary": evidence_summary(cards, result),
        "state": state, "capabilities": capabilities,
        "real_world_authorization": result.get("real_world_authorization"),
        "disclaimer": ("Decision support only. Prototype thresholds and score bands are not "
                       "official flood-warning levels. The system never executes evacuations, "
                       "dispatches or public alerts."),
    }
