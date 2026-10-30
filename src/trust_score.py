"""Evidence Trust Score (0-100): how reliable the evidence is, NOT how dangerous the flood is.

Deterministic and documented. Seven factors, weighted in config/flood_scoring.json:

  authenticity        what kind of source each agent's evidence is (evidence class)
  freshness           age of each agent's evidence against its own freshness limit
  quality             QC acceptance / validation status / segment coverage
  completeness        which of the three agents supplied usable evidence
  consistency         agreement between sources (cross-source conflicts, a blocked
                      road while the river looks normal, river/weather time mismatch,
                      contradicted claims)
  timestamp_validity  future timestamps, retrieved-before-observed, missing times
  provenance          traceable evidence id and content hash + verification status

Per-agent factors are combined with the agent weights (river 0.5, weather 0.3,
traffic 0.2) over ALL three agents: an agent that supplied no evidence earns no
credit in any factor (missing evidence is never "fine"), and completeness reports
which agents are missing. Two hard caps keep missing primary evidence from ever looking
trustworthy: no river evidence caps the score at 29, river evidence that the risk
engine could not use (stale, poor quality, unresolved conflict, ...) caps it at 59.

SIMULATED evidence can be fresh, complete and consistent, so it can score well, but
its authenticity is that of fabricated data and it is always reported as SIMULATED,
never as verified real-world evidence.
"""

from __future__ import annotations

from typing import Any

from src.river_monitoring_agent import parse_timestamp

FACTOR_LABELS = {
    "authenticity": "Source authenticity",
    "freshness": "Evidence freshness",
    "quality": "Data quality",
    "completeness": "Completeness",
    "consistency": "Consistency between sources",
    "timestamp_validity": "Timestamp validity",
    "provenance": "Provenance & verification",
}
PER_AGENT_FACTORS = ("authenticity", "freshness", "quality", "timestamp_validity", "provenance")
BAD_TIME_CODES = ("FUTURE_TIMESTAMP", "RETRIEVED_BEFORE_OBSERVED")
METHOD = ("Weighted sum of seven documented factors (config/flood_scoring.json). Per-agent "
          "factors use the agent weights over all three agents; a missing agent earns no "
          "credit. Caps: no river evidence <= 29, unusable river evidence <= 59. "
          "Prototype bands.")


def _obj(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _num(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _freshness_score(status: Any, age: Any, max_age: Any) -> tuple[float, str]:
    age_h, limit = _num(age), _num(max_age)
    if status == "FRESH":
        ratio = min(1.0, (age_h or 0.0) / limit) if limit else 0.0
        return 100 - 20 * ratio, (f"fresh ({age_h:g} h old, limit {limit:g} h)"
                                  if age_h is not None and limit else "fresh")
    if status == "STALE":
        return 10.0, (f"stale ({age_h:g} h old, limit {limit:g} h)"
                      if age_h is not None and limit else "stale")
    return 0.0, "age unknown"


def _record(result: dict[str, Any], kind: str) -> dict[str, Any] | None:
    for rec in _obj(result.get("evidence_layer")).get("records") or []:
        if isinstance(rec, dict) and rec.get("kind") == kind:
            return rec
    return None


def _codes(rec: dict[str, Any]) -> list[str]:
    return [f.get("code") for f in _obj(rec.get("validation")).get("findings") or []
            if isinstance(f, dict)]


def band_for(score: float, bands: dict[str, list[int]]) -> str:
    for name in ("HIGH", "MODERATE", "LOW", "VERY_LOW"):
        lo, hi = bands[name]
        if lo <= round(score) <= hi:
            return name
    return "VERY_LOW"


# --------------------------------------------------------------------------
# Agents
# --------------------------------------------------------------------------

def river_agent(result: dict[str, Any], auth: dict[str, float]) -> dict[str, Any]:
    rec = _record(result, "RIVER_LEVEL")
    eq = _obj(_obj(result.get("risk_assessment")).get("evidence_quality"))
    out: dict[str, Any] = {"agent": "River Agent", "present": False, "notes": [],
                           "evidence_class": (rec or {}).get("evidence_class") or "UNAVAILABLE",
                           "usable": eq.get("river_usable_for_hazard") is True,
                           "blocking_reasons": list(eq.get("river_blocking_reasons") or [])}
    value = _obj((rec or {}).get("value"))
    if not rec or rec.get("evidence_class") == "UNAVAILABLE" or value.get("river_level_m") is None:
        out["notes"].append("No river reading could be obtained.")
        return out
    out["present"] = True
    klass = rec["evidence_class"]
    out["authenticity"] = float(auth.get(klass, 0))
    out["notes"].append(f"authenticity: {klass.replace('_', ' ').lower()}")
    fresh = _obj(rec.get("freshness"))
    out["freshness"], text = _freshness_score(fresh.get("status"), fresh.get("age_hours"),
                                              fresh.get("max_age_hours"))
    out["notes"].append(f"freshness: {text}")
    quality = _obj(rec.get("quality"))
    accepted, checked = _num(quality.get("qc_accepted")), _num(quality.get("qc_checked"))
    ratio = accepted / checked if accepted is not None and checked else None
    recent = quality.get("recent_quality")
    if recent == "ACCEPTABLE":
        out["quality"] = 70 + 30 * (ratio if ratio is not None else 1.0)
    elif recent == "SUSPECT":
        out["quality"] = 45.0
    else:
        out["quality"] = 10.0
    out["notes"].append(f"quality: recent data {str(recent or 'unknown').lower()}"
                        + (f", {int(accepted)}/{int(checked)} readings passed QC"
                           if ratio is not None else ""))
    codes = _codes(rec)
    bad_time = [c for c in codes if c in BAD_TIME_CODES]
    out["timestamp_validity"] = 0.0 if bad_time or not rec.get("observed_at") else 100.0
    if bad_time:
        out["notes"].append("timestamps: " + ", ".join(bad_time).replace("_", " ").lower())
    source = _obj(rec.get("source"))
    traceable = bool(rec.get("evidence_id") and (source.get("content_sha256")
                                                 or source.get("reference")))
    verification = _obj(rec.get("verification")).get("status")
    verified = {"VERIFIED": 100.0, "FAILED": 0.0, "NOT_APPLICABLE": 50.0}.get(verification, 40.0)
    out["provenance"] = 0.5 * (100.0 if traceable else 30.0) + 0.5 * verified
    out["notes"].append("provenance: " + ("hashed, traceable source" if traceable
                                          else "source not fully traceable")
                        + f"; verification {str(verification or 'unknown').lower()}")
    out["completeness"] = 1.0
    return out


def weather_agent(result: dict[str, Any], auth: dict[str, float]) -> dict[str, Any]:
    rec = _record(result, "WEATHER_MODEL")
    wx = _obj(_obj(_obj(result.get("risk_assessment")).get("supporting_evidence")).get("weather"))
    out: dict[str, Any] = {"agent": "Weather Agent", "present": False, "notes": [],
                           "evidence_class": (rec or {}).get("evidence_class") or "UNAVAILABLE",
                           "usable": wx.get("used") is True,
                           "blocking_reasons": list(wx.get("not_used_reasons") or [])}
    if not rec or rec.get("evidence_class") == "UNAVAILABLE":
        out["notes"].append("No weather evidence was available.")
        return out
    out["present"] = True
    klass = rec["evidence_class"]
    out["authenticity"] = float(auth.get(klass, 0))
    out["notes"].append(f"authenticity: {klass.replace('_', ' ').lower()}")
    fresh = _obj(rec.get("freshness"))
    out["freshness"], text = _freshness_score(fresh.get("status"), fresh.get("age_hours"),
                                              fresh.get("max_age_hours"))
    out["notes"].append(f"freshness: {text}")
    validation = _obj(rec.get("validation"))
    errors = validation.get("error_codes") or []
    rain = _obj(_obj(rec.get("value")).get("rainfall_mm"))
    available = [k for k, v in rain.items() if _obj(v).get("status") == "AVAILABLE"]
    if errors:
        out["quality"] = 20.0
    elif _obj(rec.get("quality")).get("status") == "PARTIAL" or len(available) < len(rain):
        out["quality"] = 60.0
    else:
        out["quality"] = 100.0
    out["notes"].append("quality: " + (("validation errors " + ", ".join(errors).lower())
                                       if errors else f"{len(available)}/{len(rain)} rainfall "
                                       "windows available"))
    codes = _codes(rec)
    bad_time = [c for c in codes if c in BAD_TIME_CODES]
    out["timestamp_validity"] = 0.0 if bad_time or not rec.get("observed_at") else 100.0
    simulated = klass == "SIMULATED"
    out["provenance"] = 0.5 * (100.0 if rec.get("evidence_id") else 30.0) + \
        0.5 * (50.0 if simulated else 60.0)
    out["notes"].append("provenance: traceable record; "
                        + ("generated by the simulation" if simulated
                           else "model output cannot be verified as a measurement"))
    out["completeness"] = 1.0 if len(available) == len(rain) else 0.5
    out["time_mismatch"] = "TIME_MISMATCH" in codes
    return out


def traffic_agent(result: dict[str, Any], auth: dict[str, float]) -> dict[str, Any]:
    tr = _obj(result.get("traffic_evidence"))
    se = _obj(result.get("state_evaluation"))
    tv = _obj(se.get("traffic"))
    agent_class = _obj(_obj(se.get("agent_evidence")).get("traffic")).get("evidence_class")
    out: dict[str, Any] = {"agent": "Traffic Agent", "present": False, "notes": [],
                           "evidence_class": agent_class or "UNAVAILABLE",
                           "usable": tv.get("status") in ("ROUTES_USABLE", "ROUTE_BLOCKED",
                                                          "ROUTES_UNCERTAIN"),
                           "blocking_reasons": list(tv.get("reasons") or [])}
    if not tr or tr.get("status") not in ("OK", "PARTIAL"):
        out["notes"].append("No traffic evidence was available."
                            if not tr else f"Traffic Agent status {tr.get('status')}.")
        return out
    out["present"] = True
    simulated = tr.get("is_simulated") is True
    klass = "SIMULATED" if simulated else "REAL_TIME_PROBE_TRAFFIC"
    out["authenticity"] = float(auth.get(klass, 0))
    out["notes"].append(f"authenticity: {'simulated' if simulated else 'live probe'} traffic")
    out["freshness"], text = _freshness_score(
        "STALE" if tv.get("status") == "STALE" else "FRESH" if tv.get("age_hours") is not None
        else None, tv.get("age_hours"), tv.get("max_age_hours"))
    out["notes"].append(f"freshness: {text}")
    summary = _obj(tr.get("summary"))
    total, avail = _num(summary.get("segments_total")), _num(summary.get("segments_available"))
    confidences = [c for c in (_num(_obj(s).get("confidence"))
                               for s in _obj(tr.get("segments")).values()) if c is not None]
    coverage = (avail / total) if total else 0.0
    mean_conf = sum(confidences) / len(confidences) if confidences else 0.5
    out["quality"] = 100 * coverage * mean_conf
    out["notes"].append(f"quality: {int(avail or 0)}/{int(total or 0)} road segments, "
                        f"mean confidence {mean_conf:.2f}")
    retrieved = parse_timestamp(tr.get("retrieved_at"))
    evaluated = parse_timestamp(se.get("evaluated_at"))
    future = bool(retrieved and evaluated and (retrieved - evaluated).total_seconds() > 300)
    out["timestamp_validity"] = 0.0 if retrieved is None or future else 100.0
    out["provenance"] = 0.5 * (100.0 if tr.get("claims") else 30.0) + \
        0.5 * (50.0 if simulated else 70.0)
    out["completeness"] = 1.0 if tr.get("status") == "OK" else 0.5
    out["contradicts_river"] = agent_class == "CONFLICTING"
    return out


# --------------------------------------------------------------------------
# Overall score
# --------------------------------------------------------------------------

def _river_conflict(result: dict[str, Any]) -> str | None:
    """'unresolved', 'resolved' or None (no current-evidence conflict)."""
    initial = _num(result.get("initial_conflicts")) or 0
    remaining = _num(result.get("conflicts_remaining")) or 0
    outcome = _obj(result.get("reverification")).get("outcome")
    blocking = _obj(_obj(result.get("risk_assessment")).get("evidence_quality")).get(
        "river_blocking_reasons") or []
    if remaining > 0 or "EVIDENCE_UNRESOLVED_CONFLICT" in blocking or \
            outcome in ("CONFLICT_PERSISTS", "FAILED", "NOT_AUTHORIZED") and initial > 0:
        return "unresolved"
    if initial > 0 and outcome == "RESOLVED":
        return "resolved"
    return None


def compute_trust_score(result: dict[str, Any] | None,
                        scoring: dict[str, Any]) -> dict[str, Any]:
    """Evidence Trust Score block. Pure and deterministic; never raises for bad input."""
    result = _obj(result)
    weights = {k: float(v) for k, v in scoring["trust_factor_weights"].items()
               if not k.startswith("_")}
    agent_w = {k: float(v) for k, v in scoring["trust_agent_weights"].items()
               if not k.startswith("_")}
    auth = {k: float(v) for k, v in scoring["trust_authenticity"].items()
            if not k.startswith("_")}
    pen = scoring["trust_consistency_penalties"]
    caps_cfg = scoring["trust_caps"]
    agents = {"river": river_agent(result, auth), "weather": weather_agent(result, auth),
              "traffic": traffic_agent(result, auth)}
    present = {k: a for k, a in agents.items() if a["present"]}
    deductions: list[dict[str, Any]] = []
    factors: dict[str, dict[str, Any]] = {}

    for key in PER_AGENT_FACTORS:
        value = sum(agent_w[k] * a[key] for k, a in present.items())
        detail = "; ".join(f"{a['agent']}: {round(a[key])}" if a["present"]
                           else f"{a['agent']}: missing (0)" for a in agents.values())
        factors[key] = {"score": value, "detail": detail}

    completeness = sum(agent_w[k] * a.get("completeness", 0.0) for k, a in agents.items())
    missing = [a["agent"] for a in agents.values() if not a["present"]]
    factors["completeness"] = {"score": 100 * completeness,
                               "detail": ("all three agents supplied evidence" if not missing
                                          else "missing: " + ", ".join(missing))}

    consistency, reasons = 100.0, []
    conflict = _river_conflict(result)
    if conflict == "unresolved":
        consistency -= pen["unresolved_river_conflict"]
        reasons.append("river sources disagree and the conflict is unresolved")
    elif conflict == "resolved":
        consistency -= pen["resolved_river_conflict"]
        reasons.append("river sources disagreed; independent reverification resolved it")
    if agents["traffic"].get("contradicts_river"):
        consistency -= pen["traffic_contradicts_river"]
        reasons.append("an evacuation road is reported blocked while the river looks normal")
    if agents["weather"].get("time_mismatch"):
        consistency -= pen["weather_time_mismatch"]
        reasons.append("weather and river evidence are too far apart in time")
    contradicted = [c.get("claim_id") for c in _obj(result.get("evidence_layer")).get("claims")
                    or [] if isinstance(c, dict) and c.get("contradicting_evidence_ids")]
    if contradicted:
        consistency -= pen["contradicted_claim"] * len(contradicted)
        reasons.append(f"{len(contradicted)} claim(s) contradicted by other evidence")
    factors["consistency"] = {"score": max(0.0, consistency),
                              "detail": "; ".join(reasons) or "no disagreement between sources"}

    rows, total = [], 0.0
    for key in ("authenticity", "freshness", "quality", "completeness", "consistency",
                "timestamp_validity", "provenance"):
        score = max(0.0, min(100.0, factors[key]["score"]))
        points = weights[key] * score / 100
        total += points
        lost = weights[key] - points
        rows.append({"key": key, "label": FACTOR_LABELS[key], "score": round(score),
                     "weight": weights[key], "points": round(points, 1),
                     "deduction": round(lost, 1), "detail": factors[key]["detail"]})
        if lost >= 0.5:
            deductions.append({"factor": FACTOR_LABELS[key], "points": round(lost, 1),
                               "reason": factors[key]["detail"]})

    caps = []
    river = agents["river"]
    if not river["present"]:
        caps.append({"cap": caps_cfg["river_missing"],
                     "reason": "No river evidence: the primary hazard evidence is missing."})
    elif not river["usable"]:
        caps.append({"cap": caps_cfg["river_unusable"],
                     "reason": "River evidence was not usable for the risk assessment ("
                     + ", ".join(r.replace("EVIDENCE_", "").replace("_", " ").lower()
                                 for r in river["blocking_reasons"]) + ")."})
    raw = total
    for cap in caps:
        if total > cap["cap"]:
            deductions.append({"factor": "Cap", "points": round(total - cap["cap"], 1),
                               "reason": cap["reason"]})
            total = float(cap["cap"])
    score = round(total)
    band = band_for(score, scoring["trust_bands"])
    simulated = any(a.get("evidence_class") == "SIMULATED" for a in present.values())
    agent_rows = {}
    for key, a in agents.items():
        agent_score = (round(sum(weights[f] * a[f] for f in PER_AGENT_FACTORS)
                             / sum(weights[f] for f in PER_AGENT_FACTORS))
                       if a["present"] else None)
        agent_rows[key] = {"agent": a["agent"], "present": a["present"], "score": agent_score,
                           "evidence_class": a["evidence_class"], "accepted": a["usable"],
                           "notes": a["notes"],
                           **{f: (round(a[f]) if a["present"] else None)
                              for f in PER_AGENT_FACTORS}}
    deductions.sort(key=lambda d: -d["points"])
    return {
        "score": score, "raw_score": round(raw, 1), "band": band,
        "label": band.replace("_", " ") + " TRUST",
        "is_simulated": simulated,
        "simulated_note": ("Includes SIMULATED evidence generated by this system: internally "
                           "consistent and traceable, but NOT verified real-world evidence."
                           if simulated else None),
        "factors": rows, "agents": agent_rows, "deductions": deductions, "caps": caps,
        "bands": {k: v for k, v in scoring["trust_bands"].items() if not k.startswith("_")},
        "method": METHOD,
        # Convenience summaries for the dashboard's headline cards.
        "source_reliability": round(factors["authenticity"]["score"]),
        "freshness": round(factors["freshness"]["score"]),
        "completeness": round(factors["completeness"]["score"]),
        "consistency": round(factors["consistency"]["score"]),
    }
