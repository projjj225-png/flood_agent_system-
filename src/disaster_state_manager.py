"""Disaster State Manager: deterministic, configurable NORMAL/ALERT/EMERGENCY/RECOVERY.

It classifies each evaluation from verified evidence and decides whether the
disaster state should change. It never authorizes, executes or triggers any
action: EMERGENCY is a classification, not a permission. Capabilities are still
decided only by capability_control, and real-world actions still need a human.

Design (see README, "Disaster State Manager"):
- Inputs: the current STATE RECORD (explicit, caller-held; no global state),
  the river evidence envelope, the reverification block, the weather evidence
  and the workflow's risk assessment.
- The supplied risk assessment is not trusted: the manager recomputes it from
  the same evidence with assess_flood_risk and requires the same assessment_id.
- Evidence validity is the evidence gate's (assess_evidence_for_authorization):
  integrity, conflicts, freshness, quality, simulation. A real-scope record also
  needs verified-live evidence that is fresh at decision time
  (capability_control.evidence_stale_at). Simulated and historical records may
  use their own evidence but stay labelled as such and can never be real.
- Each valid evaluation is classified into one condition (EMERGENCY, ALERT,
  NORMAL, IMPROVED or ELEVATED); invalid ones are INSUFFICIENT.
- Transitions follow config/disaster_state.json: per-state ordered rules with
  persistence counted in DISTINCT observations and hours of observed time.
  Escalation rules come first and are never held back by de-escalation timers.
- Outcomes: TRANSITION_ACCEPTED, TRANSITION_REJECTED, STATE_UNCHANGED,
  INSUFFICIENT_EVIDENCE, EVALUATION_FAILED. Only an accepted transition changes
  the state; every other outcome keeps it.

All criteria are PROTOTYPE configuration, not official warning levels.
"""

from __future__ import annotations

import argparse
import copy
import hashlib
import json
import math
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.capability_control import (
    EVIDENCE_NOT_VERIFIED_LIVE,
    EVIDENCE_SIMULATED,
    DisasterState,
    assess_evidence_for_authorization,
    evidence_stale_at,
)
from src.risk_assessment import (
    HAZARD_LEVELS,
    INDETERMINATE,
    MODE_HISTORICAL,
    MODE_REAL,
    MODE_SIMULATED,
    MODE_UNKNOWN,
    assess_flood_risk,
    assessment_mode_for,
    compute_assessment_id,
)
from src.river_monitoring_agent import PROJECT_DIR, load_json_file, parse_timestamp

SCHEMA_VERSION = "1.0"
MANAGER_NAME = "DisasterStateManager"
DEFAULT_STATE_CONFIG_PATH = PROJECT_DIR / "config" / "disaster_state.json"

STATES = tuple(s.value for s in DisasterState)
NORMAL, ALERT, EMERGENCY, RECOVERY = STATES
SCOPES = (MODE_REAL, MODE_HISTORICAL, MODE_SIMULATED)
LEGAL_TRANSITIONS = {
    NORMAL: (ALERT,),
    ALERT: (EMERGENCY, NORMAL),
    EMERGENCY: (RECOVERY,),
    RECOVERY: (EMERGENCY, ALERT, NORMAL),
}

ACCEPTED = "TRANSITION_ACCEPTED"
REJECTED = "TRANSITION_REJECTED"
UNCHANGED = "STATE_UNCHANGED"
INSUFFICIENT = "INSUFFICIENT_EVIDENCE"
FAILED = "EVALUATION_FAILED"
OUTCOMES = (ACCEPTED, REJECTED, UNCHANGED, INSUFFICIENT, FAILED)

COND_EMERGENCY, COND_ALERT, COND_NORMAL, COND_IMPROVED = "EMERGENCY", "ALERT", "NORMAL", \
    "IMPROVED"
COND_ELEVATED, COND_INSUFFICIENT = "ELEVATED", "INSUFFICIENT"
CONDITION_ORDER = (COND_EMERGENCY, COND_ALERT, COND_NORMAL, COND_IMPROVED)
CONDITIONS = CONDITION_ORDER + (COND_ELEVATED, COND_INSUFFICIENT)
CONFIDENCE_ORDER = ("NONE", "LOW", "MEDIUM", "HIGH")
RULE_KINDS = ("escalation", "de-escalation")

# Evidence-gate reasons a non-real scope may carry; a real record tolerates none.
SCOPE_TOLERATED_REASONS = {
    MODE_REAL: frozenset(),
    MODE_HISTORICAL: frozenset({EVIDENCE_NOT_VERIFIED_LIVE}),
    MODE_SIMULATED: frozenset({EVIDENCE_SIMULATED, EVIDENCE_NOT_VERIFIED_LIVE}),
}

AUDIT_EVENTS = {
    ACCEPTED: "disaster_state_transition_accepted",
    REJECTED: "disaster_state_transition_rejected",
    UNCHANGED: "disaster_state_unchanged",
    INSUFFICIENT: "disaster_state_insufficient_evidence",
    FAILED: "disaster_state_evaluation_failed",
}

DISCLAIMER = ("Disaster-state classification for decision support. It does not authorize, "
              "execute or trigger any action; capabilities are decided by capability_control "
              "and real-world actions require human approval. Criteria are prototype "
              "configuration, not official warning levels.")


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------

def _level(value: Any, name: str, levels: tuple[str, ...], optional: bool = False) -> Any:
    if value is None and optional:
        return None
    if value not in levels:
        raise ValueError(f"State config: {name} must be one of {', '.join(levels)} "
                         f"(got {value!r}).")
    return value


def _rule_ids(value: Any, name: str) -> list[str]:
    if not isinstance(value, list) or not all(isinstance(v, str) and v for v in value):
        raise ValueError(f"State config: {name} must be a list of rule IDs.")
    return list(value)


def validate_state_config(config: Any) -> dict[str, Any]:
    """Check a state config. Raises ValueError for any setup mistake (fail safe)."""
    if not isinstance(config, dict):
        raise ValueError("State config must be a JSON object.")
    allowed = config.get("allowed_transitions")
    if not isinstance(allowed, dict) or set(allowed) != set(STATES):
        raise ValueError(f"State config: allowed_transitions needs exactly {', '.join(STATES)}.")
    clean_allowed: dict[str, list[str]] = {}
    for source, targets in allowed.items():
        if not isinstance(targets, list) or len(set(targets)) != len(targets) \
                or not all(t in STATES and t != source for t in targets):
            raise ValueError(f"State config: allowed_transitions.{source} must list other "
                             "known states, without repeats.")
        clean_allowed[source] = list(targets)
    # Safety invariants that configuration cannot relax.
    if set(clean_allowed[EMERGENCY]) & {NORMAL, ALERT}:
        raise ValueError("State config: EMERGENCY may only de-escalate through RECOVERY.")
    if EMERGENCY in clean_allowed[NORMAL]:
        raise ValueError("State config: NORMAL -> EMERGENCY is not permitted; escalation "
                         "passes through ALERT so an emergency is never declared on a "
                         "single observation.")
    if any(tuple(clean_allowed[state]) != LEGAL_TRANSITIONS[state] for state in STATES):
        raise ValueError("State config: allowed_transitions does not match the legal "
                         "NORMAL -> ALERT -> EMERGENCY -> RECOVERY -> NORMAL cycle.")

    conditions = config.get("conditions")
    if not isinstance(conditions, dict) or not set(CONDITION_ORDER) <= set(conditions):
        raise ValueError(f"State config: conditions needs {', '.join(CONDITION_ORDER)}.")
    clean_conditions: dict[str, dict[str, Any]] = {}
    for name in (COND_EMERGENCY, COND_ALERT):
        c = conditions[name]
        if not isinstance(c, dict):
            raise ValueError(f"State config: conditions.{name} must be an object.")
        clean_conditions[name] = {
            "min_risk_level": _level(c.get("min_risk_level"), f"{name}.min_risk_level",
                                     HAZARD_LEVELS),
            "min_confidence": _level(c.get("min_confidence"), f"{name}.min_confidence",
                                     CONFIDENCE_ORDER[1:]),
            "river_factor_min_level": _level(c.get("river_factor_min_level"),
                                             f"{name}.river_factor_min_level", HAZARD_LEVELS,
                                             optional=True),
        }
    if clean_conditions[COND_EMERGENCY]["river_factor_min_level"] is None:
        raise ValueError("State config: EMERGENCY needs river_factor_min_level; rainfall "
                         "alone is never sufficient for an emergency.")
    for name in (COND_NORMAL, COND_IMPROVED):
        c = conditions[name]
        if not isinstance(c, dict):
            raise ValueError(f"State config: conditions.{name} must be an object.")
        clean_conditions[name] = {
            "max_risk_level": _level(c.get("max_risk_level"), f"{name}.max_risk_level",
                                     HAZARD_LEVELS),
            "min_confidence": _level(c.get("min_confidence"), f"{name}.min_confidence",
                                     CONFIDENCE_ORDER[1:]),
            "required_rules_all": _rule_ids(c.get("required_rules_all", []),
                                            f"{name}.required_rules_all"),
            "forbidden_rules_any": _rule_ids(c.get("forbidden_rules_any", []),
                                             f"{name}.forbidden_rules_any"),
        }

    rules = config.get("rules")
    if not isinstance(rules, dict) or set(k for k in rules if not k.startswith("_")) \
            != set(STATES):
        raise ValueError(f"State config: rules needs exactly {', '.join(STATES)}.")
    clean_rules: dict[str, list[dict[str, Any]]] = {}
    for source in STATES:
        entries = rules[source]
        if not isinstance(entries, list):
            raise ValueError(f"State config: rules.{source} must be a list.")
        clean_rules[source] = []
        seen_de_escalation = False
        for i, rule in enumerate(entries):
            where = f"rules.{source}[{i}]"
            if not isinstance(rule, dict):
                raise ValueError(f"State config: {where} must be an object.")
            to = rule.get("to")
            if to not in clean_allowed[source]:
                raise ValueError(f"State config: {where}.to {to!r} is not an allowed "
                                 f"transition from {source}.")
            when = rule.get("when")
            if not isinstance(when, list) or not when or not set(when) <= set(CONDITION_ORDER):
                raise ValueError(f"State config: {where}.when must list conditions from "
                                 f"{', '.join(CONDITION_ORDER)}.")
            kind = _level(rule.get("kind"), f"{where}.kind", RULE_KINDS)
            expected_kind = ("escalation" if (source, to) in (
                (NORMAL, ALERT), (ALERT, EMERGENCY), (RECOVERY, EMERGENCY),
                (RECOVERY, ALERT)) else "de-escalation")
            if kind != expected_kind:
                raise ValueError(f"State config: {where}.kind must be {expected_kind} "
                                 f"for {source} -> {to}.")
            obs = rule.get("min_observations")
            hours = rule.get("min_hours")
            gap = rule.get("gap_breaks_run")
            if isinstance(obs, bool) or not isinstance(obs, int) or obs < 1:
                raise ValueError(f"State config: {where}.min_observations must be an integer "
                                 ">= 1.")
            if isinstance(hours, bool) or not isinstance(hours, (int, float)) \
                    or not math.isfinite(hours) or hours < 0:
                raise ValueError(f"State config: {where}.min_hours must be a number >= 0.")
            if not isinstance(gap, bool):
                raise ValueError(f"State config: {where}.gap_breaks_run must be true or false.")
            spacing = rule.get("min_spacing_hours")
            max_gap = rule.get("max_gap_hours")
            if isinstance(spacing, bool) or not isinstance(spacing, (int, float)) \
                    or not math.isfinite(spacing) or spacing <= 0:
                raise ValueError(f"State config: {where}.min_spacing_hours must be a number "
                                 "> 0.")
            if max_gap is not None and (isinstance(max_gap, bool)
                                        or not isinstance(max_gap, (int, float))
                                        or not math.isfinite(max_gap) or max_gap < spacing):
                raise ValueError(f"State config: {where}.max_gap_hours must be null or a "
                                 "number >= min_spacing_hours.")
            if kind == "de-escalation" and max_gap is None:
                raise ValueError(f"State config: {where}: de-escalation needs max_gap_hours, "
                                 "so a silent or delayed feed cannot look like sustained "
                                 "improvement.")
            if to == EMERGENCY and obs < 2:
                raise ValueError(f"State config: {where}: an EMERGENCY transition needs at "
                                 "least 2 observations.")
            allowed_conditions = {
                (NORMAL, ALERT): {COND_ALERT, COND_EMERGENCY},
                (ALERT, EMERGENCY): {COND_EMERGENCY},
                (ALERT, NORMAL): {COND_NORMAL},
                (EMERGENCY, RECOVERY): {COND_IMPROVED, COND_NORMAL},
                (RECOVERY, EMERGENCY): {COND_EMERGENCY},
                (RECOVERY, ALERT): {COND_ALERT, COND_EMERGENCY},
                (RECOVERY, NORMAL): {COND_NORMAL},
            }[(source, to)]
            if not set(when) <= allowed_conditions:
                raise ValueError(f"State config: {where}.when is unsafe for {source} -> {to}.")
            if kind == "de-escalation" and not gap:
                raise ValueError(f"State config: {where}: de-escalation needs an unbroken "
                                 "run (gap_breaks_run true).")
            if kind == "escalation" and seen_de_escalation:
                raise ValueError(f"State config: rules.{source}: escalation rules must come "
                                 "before de-escalation rules.")
            seen_de_escalation |= kind == "de-escalation"
            clean_rules[source].append({"to": to, "when": list(when), "kind": kind,
                                        "min_observations": obs, "min_hours": float(hours),
                                        "gap_breaks_run": gap,
                                        "min_spacing_hours": float(spacing),
                                        "max_gap_hours": None if max_gap is None
                                        else float(max_gap)})
    limit = config.get("history_limit", 200)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 10 <= limit <= 10000:
        raise ValueError("State config: history_limit must be an integer 10-10000.")
    return {"allowed_transitions": clean_allowed, "conditions": clean_conditions,
            "rules": clean_rules, "history_limit": limit,
            "reference": config.get("reference")}


def load_state_config(path: str | Path = DEFAULT_STATE_CONFIG_PATH) -> dict[str, Any]:
    return validate_state_config(load_json_file(path, "State config"))


# --------------------------------------------------------------------------
# State record (explicit, caller-held persistence)
# --------------------------------------------------------------------------

def _record_digest(record: dict[str, Any]) -> str:
    stable = {k: v for k, v in record.items() if k != "record_id"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str)
                          .encode("utf-8")).hexdigest()


def new_state_record(state: str, scope: str) -> dict[str, Any]:
    """A fresh record for a state the CALLER asserts (no history)."""
    if state not in STATES:
        raise ValueError(f"Unknown disaster state: {state!r}")
    if scope not in SCOPES:
        raise ValueError(f"State scope must be one of {', '.join(SCOPES)}")
    record = {"schema_version": SCHEMA_VERSION, "scope": scope, "state": state,
              "entered_at": None, "origin": "CALLER_SUPPLIED_STATE", "history": [],
              "last_transition": None}
    record["record_id"] = _record_digest(record)
    return record


def validate_state_record(record: Any) -> list[str]:
    """Problems with a record (field paths only). Empty list means valid."""
    if not isinstance(record, dict):
        return [f"record: expected object, got {type(record).__name__}"]
    problems = []
    if record.get("state") not in STATES:
        problems.append("record.state: not a known disaster state")
    if record.get("scope") not in SCOPES:
        problems.append("record.scope: not one of " + ", ".join(SCOPES))
    if record.get("schema_version") != SCHEMA_VERSION:
        problems.append("record.schema_version: unsupported")
    if record.get("entered_at") is not None \
            and parse_timestamp(record.get("entered_at")) is None:
        problems.append("record.entered_at: not a timestamp")
    transition = record.get("last_transition")
    if transition is not None:
        legal = (isinstance(transition, dict)
                 and transition.get("from") in STATES
                 and transition.get("to") in LEGAL_TRANSITIONS.get(transition.get("from"), ()))
        if not legal:
            problems.append("record.last_transition: illegal transition")
    history = record.get("history")
    if not isinstance(history, list):
        problems.append("record.history: expected list")
    else:
        for i, entry in enumerate(history):
            if not isinstance(entry, dict) or entry.get("condition") not in CONDITIONS:
                problems.append(f"record.history[{i}]: malformed entry")
                break
            if entry["condition"] != COND_INSUFFICIENT \
                    and parse_timestamp(entry.get("observed_at")) is None:
                problems.append(f"record.history[{i}].observed_at: not a timestamp")
                break
    if record.get("record_id") != _record_digest(record):
        problems.append("record.record_id: does not match the record content")
    return problems


def load_state_record(path: str | Path) -> dict[str, Any]:
    record = load_json_file(path, "State record")
    problems = validate_state_record(record)
    if problems:
        raise ValueError(f"State record {path} is invalid: " + "; ".join(problems))
    return record


def save_state_record(record: dict[str, Any], path: str | Path) -> Path:
    """Write atomically (temp file + replace) so a crash never leaves half a record."""
    problems = validate_state_record(record)
    if problems:
        raise ValueError("Refusing to save an invalid state record: " + "; ".join(problems))
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(record, handle, indent=1, sort_keys=True)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise
    return path


# --------------------------------------------------------------------------
# Classification and persistence
# --------------------------------------------------------------------------

def _rank(value: Any, order: tuple[str, ...]) -> int:
    return order.index(value) if value in order else -1


def classify(risk: dict[str, Any], cfg: dict[str, Any]) -> tuple[str, list[str]]:
    """The first condition the (recomputed) risk assessment meets, with reasons."""
    level = risk.get("risk_level")
    confidence = (risk.get("evidence_quality") or {}).get("confidence")
    fired = set(risk.get("rules_fired") or [])
    river_levels = [f.get("hazard_level") for f in risk.get("risk_factors") or []
                    if isinstance(f, dict) and f.get("source") == "river"]
    conditions = cfg["conditions"]
    for name in (COND_EMERGENCY, COND_ALERT):
        c = conditions[name]
        need_river = c["river_factor_min_level"]
        if (_rank(level, HAZARD_LEVELS) >= _rank(c["min_risk_level"], HAZARD_LEVELS)
                and _rank(confidence, CONFIDENCE_ORDER) >= _rank(c["min_confidence"],
                                                                CONFIDENCE_ORDER)
                and (need_river is None or any(_rank(lv, HAZARD_LEVELS)
                                               >= _rank(need_river, HAZARD_LEVELS)
                                               for lv in river_levels))):
            return name, [f"risk {level} (confidence {confidence}) meets the {name} "
                          "condition" + (f", with a river factor >= {need_river}"
                                         if need_river else "")]
    for name in (COND_NORMAL, COND_IMPROVED):
        c = conditions[name]
        if (0 <= _rank(level, HAZARD_LEVELS) <= _rank(c["max_risk_level"], HAZARD_LEVELS)
                and _rank(confidence, CONFIDENCE_ORDER) >= _rank(c["min_confidence"],
                                                                CONFIDENCE_ORDER)
                and set(c["required_rules_all"]) <= fired
                and not set(c["forbidden_rules_any"]) & fired):
            return name, [f"risk {level} with rules {sorted(set(c['required_rules_all']))} "
                          f"meets the {name} condition"]
    return COND_ELEVATED, [f"risk {level} meets no transition condition (held by hysteresis)"]


def run_statistics(history: list[dict[str, Any]], rule: dict[str, Any]) -> dict[str, Any]:
    """Most recent run of adequately sampled observations whose condition is in rule['when'].

    Walking back from the newest entry, the run ends at an entry of another
    condition, at an INSUFFICIENT entry if ``gap_breaks_run``, or where two
    consecutive observations are more than ``max_gap_hours`` apart (so a silent or
    delayed feed can never look like sustained conditions). An observation counts
    only if it is at least ``min_spacing_hours`` after the previously counted one
    (bunched readings count once); a repeated timestamp never counts twice.
    """
    when, gap_breaks = rule["when"], rule["gap_breaks_run"]
    max_gap, min_spacing = rule["max_gap_hours"], rule["min_spacing_hours"]
    counted: list[tuple[datetime, dict[str, Any]]] = []
    seen: set[str] = set()
    previous: datetime | None = None
    skipped, gap_break, largest_gap = 0, None, 0.0
    for entry in reversed(history):
        if entry["condition"] == COND_INSUFFICIENT:
            if gap_breaks:
                break
            continue
        if entry["condition"] not in when:
            break
        moment = parse_timestamp(entry["observed_at"])
        if entry["observed_at"] in seen:
            skipped += 1
            continue
        seen.add(entry["observed_at"])
        if previous is not None:
            gap = (previous - moment).total_seconds() / 3600
            largest_gap = max(largest_gap, gap)
            if max_gap is not None and gap > max_gap:
                gap_break = round(gap, 3)
                break
        previous = moment
        if counted and (counted[-1][0] - moment).total_seconds() / 3600 < min_spacing:
            skipped += 1
            continue
        counted.append((moment, entry))
    span = (counted[0][0] - counted[-1][0]).total_seconds() / 3600 if len(counted) > 1 else 0.0
    return {"observations": len(counted), "span_hours": round(span, 3),
            "evidence_ids": [e.get("evidence_id") for _, e in reversed(counted)],
            "since": counted[-1][1]["observed_at"] if counted else None,
            "readings_skipped_as_duplicate_or_too_close": skipped,
            "largest_gap_hours": round(largest_gap, 3), "run_broken_by_gap_hours": gap_break,
            "max_gap_hours": max_gap, "min_spacing_hours": min_spacing}


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _base_result(record: Any, now: Any) -> dict[str, Any]:
    state = record.get("state") if isinstance(record, dict) else None
    return {
        "schema_version": SCHEMA_VERSION, "manager": MANAGER_NAME,
        "outcome": FAILED, "scope": record.get("scope") if isinstance(record, dict) else None,
        "current_state": state if state in STATES else None,
        "proposed_state": None, "resulting_state": state if state in STATES else None,
        "condition": None, "transition_rule": None, "reason": None, "reasons": [],
        "evidence_id": None, "observed_at": None, "risk_assessment_id": None,
        "risk_level": None, "supporting_evidence_ids": [], "persistence": None,
        "evaluated_at": now.isoformat() if isinstance(now, datetime) else None,
        "is_simulated": isinstance(record, dict) and record.get("scope") == MODE_SIMULATED,
        "state_applies_to": None, "authorizes_actions": False, "executes_actions": False,
        "record": copy.deepcopy(record) if isinstance(record, dict) else None,
        "record_changed": False, "disclaimer": DISCLAIMER,
    }


def _finish(result: dict[str, Any], outcome: str, reason: str,
            reasons: list[str] | None = None) -> dict[str, Any]:
    result["outcome"] = outcome
    result["reason"] = reason
    result["reasons"] = list(reasons or [])
    if result["outcome"] != ACCEPTED:
        result["resulting_state"] = result["current_state"]
    scope = result.get("scope")
    result["state_applies_to"] = ("SIMULATION_ONLY" if scope == MODE_SIMULATED else
                                  "HISTORICAL_REPLAY" if scope == MODE_HISTORICAL else
                                  "CURRENT_CONDITIONS" if scope == MODE_REAL else None)
    stable = {k: v for k, v in result.items() if k not in ("evaluated_at", "record")}
    result["state_decision_id"] = hashlib.sha256(json.dumps(
        stable, sort_keys=True, default=str).encode("utf-8")).hexdigest()
    return result


def evaluate_state(
    record: Any,
    river_evidence: Any,
    risk_assessment: Any,
    *,
    config: dict[str, Any],
    risk_config: dict[str, Any],
    now: datetime,
    weather: Any = None,
    reverification: Any = None,
    requested_state: str | None = None,
) -> dict[str, Any]:
    """Evaluate one step. Never raises; never changes the state except on ACCEPTED."""
    try:
        return _evaluate(record, river_evidence, risk_assessment, config, risk_config, now,
                         weather, reverification, requested_state)
    except Exception as exc:  # fail closed: no transition is invented
        result = _base_result(record if isinstance(record, dict) else None, now)
        return _finish(result, FAILED, f"State evaluation failed ({type(exc).__name__}).",
                       [f"{type(exc).__name__}: {exc}"])


def _evaluate(record, evidence, supplied_risk, cfg, risk_cfg, now, weather, reverification,
              requested_state) -> dict[str, Any]:
    result = _base_result(record, now)
    problems = validate_state_record(record)
    if problems:
        result["current_state"] = result["resulting_state"] = None
        return _finish(result, FAILED, "State record is invalid; no state can be assumed.",
                       problems)
    if not isinstance(now, datetime) or now.tzinfo is None:
        return _finish(result, FAILED, "Evaluation time must be timezone-aware.")
    if requested_state is not None and requested_state not in STATES:
        return _finish(result, REJECTED, "Requested state is not a known disaster state.",
                       ["UNKNOWN_REQUESTED_STATE"])
    current, scope = record["state"], record["scope"]
    if not isinstance(evidence, dict):
        return _record_insufficient(result, record, cfg, None, None, ["EVIDENCE_MISSING"])

    evidence_scope = assessment_mode_for(evidence)
    if evidence_scope != scope:
        code = ("SIMULATED_EVIDENCE_FOR_REAL_STATE" if evidence_scope == MODE_SIMULATED
                and scope == MODE_REAL else "EVIDENCE_SCOPE_MISMATCH")
        return _finish(result, REJECTED, f"Evidence scope {evidence_scope} cannot change a "
                       f"{scope} state record.", [code])

    # The supplied risk assessment is not trusted: recompute it from the same evidence.
    recomputed = assess_flood_risk(copy.deepcopy(evidence), copy.deepcopy(weather),
                                   copy.deepcopy(reverification), risk_cfg,
                                   clock=lambda: now)
    result["risk_assessment_id"] = recomputed["assessment_id"]
    result["risk_level"] = recomputed["risk_level"]
    result["evidence_id"] = evidence.get("evidence_id") if isinstance(
        evidence.get("evidence_id"), str) else None
    observed = parse_timestamp(evidence.get("observed_at"))
    result["observed_at"] = observed.isoformat() if observed else None

    blocking = []
    # The supplied assessment must be unedited (its content hashes to its ID) AND be
    # the assessment of this evidence (same ID as the recomputation). Classification
    # always uses the recomputed one.
    if not isinstance(supplied_risk, dict) \
            or supplied_risk.get("assessment_id") != recomputed["assessment_id"] \
            or compute_assessment_id(supplied_risk) != supplied_risk.get("assessment_id"):
        blocking.append("RISK_ASSESSMENT_MISMATCH")
    gate = assess_evidence_for_authorization(evidence, reverification)
    blocking += [r for r in gate["reasons"] if r not in SCOPE_TOLERATED_REASONS[scope]]
    if scope == MODE_REAL and evidence_stale_at(evidence, now):
        blocking.append("EVIDENCE_STALE_AT_DECISION")
    if recomputed["risk_level"] == INDETERMINATE:
        blocking.append("RISK_INDETERMINATE")
    if observed is None:
        blocking.append("OBSERVATION_TIME_MISSING")
    if blocking:
        return _record_insufficient(result, record, cfg, observed, result["evidence_id"],
                                    blocking)

    history = record["history"]
    last_valid = next((e for e in reversed(history) if e["condition"] != COND_INSUFFICIENT),
                      None)
    if last_valid and observed < parse_timestamp(last_valid["observed_at"]):
        return _finish(result, INSUFFICIENT, "Evidence is older than the state history; it "
                       "cannot change the state.", ["EVIDENCE_OLDER_THAN_HISTORY"])

    condition, why = classify(recomputed, cfg)
    result["condition"] = condition
    entry = {"observed_at": observed.isoformat(), "condition": condition,
             "evidence_id": result["evidence_id"], "risk_level": recomputed["risk_level"],
             "risk_assessment_id": recomputed["assessment_id"],
             "evaluated_at": now.isoformat()}
    new_record = copy.deepcopy(record)
    new_history = new_record["history"]
    # The same observation re-evaluated replaces its earlier entry, even if an
    # INSUFFICIENT entry came in between, so it is never counted twice. (Older
    # observations were rejected above, so only the last valid entry can match.)
    previous_index = next((i for i in range(len(new_history) - 1, -1, -1)
                           if new_history[i]["condition"] != COND_INSUFFICIENT), None)
    if previous_index is not None \
            and new_history[previous_index]["observed_at"] == entry["observed_at"]:
        del new_history[previous_index]
    new_history.append(entry)
    del new_history[:-cfg["history_limit"]]

    chosen, pending = None, None
    for rule in cfg["rules"][current]:
        if condition not in rule["when"]:
            continue
        stats = run_statistics(new_history, rule)
        status = {**stats, "required_observations": rule["min_observations"],
                  "required_hours": rule["min_hours"],
                  "satisfied": stats["observations"] >= rule["min_observations"]
                  and stats["span_hours"] >= rule["min_hours"]}
        if status["satisfied"]:
            chosen = (rule, status)
            break
        pending = pending or (rule, status)

    if chosen and chosen[0]["to"] not in cfg["allowed_transitions"][current]:
        chosen = None  # defensive: validation already guarantees this

    if requested_state is not None and requested_state != current:
        if requested_state not in cfg["allowed_transitions"][current]:
            return _finish(result, REJECTED, f"{current} -> {requested_state} is not a "
                           "permitted transition.", ["UNSUPPORTED_TRANSITION"] + why)
        if not chosen or chosen[0]["to"] != requested_state:
            result["proposed_state"] = requested_state
            return _finish(result, REJECTED, f"The evidence does not support {current} -> "
                           f"{requested_state}.", ["REQUESTED_TRANSITION_NOT_SUPPORTED"] + why)

    result["record"] = new_record
    result["record_changed"] = True
    if chosen:
        rule, status = chosen
        new_record["state"] = rule["to"]
        new_record["entered_at"] = observed.isoformat()
        new_record["origin"] = "STATE_MANAGER_TRANSITION"
        new_record["last_transition"] = {"from": current, "to": rule["to"],
                                         "kind": rule["kind"], "at": observed.isoformat(),
                                         "condition": condition}
        new_record["record_id"] = _record_digest(new_record)
        result.update(proposed_state=rule["to"], resulting_state=rule["to"],
                      transition_rule=rule, persistence=status,
                      supporting_evidence_ids=status["evidence_ids"])
        return _finish(result, ACCEPTED, f"{current} -> {rule['to']} ({rule['kind']}): "
                       f"{condition} held for {status['observations']} observation(s) over "
                       f"{status['span_hours']} h.", why)
    new_record["record_id"] = _record_digest(new_record)
    if pending:
        rule, status = pending
        result.update(proposed_state=rule["to"], transition_rule=rule, persistence=status,
                      supporting_evidence_ids=status["evidence_ids"])
        return _finish(result, UNCHANGED, f"{current} kept: {current} -> {rule['to']} needs "
                       f"{rule['min_observations']} observation(s) over {rule['min_hours']} h "
                       f"(have {status['observations']} over {status['span_hours']} h).", why)
    return _finish(result, UNCHANGED, f"{current} kept: condition {condition}.", why)


def _record_insufficient(result, record, cfg, observed, evidence_id, reasons):
    """INSUFFICIENT: state kept; a gap is recorded so de-escalation runs restart."""
    new_record = copy.deepcopy(record)
    new_record["history"].append({
        "observed_at": observed.isoformat() if observed else None,
        "condition": COND_INSUFFICIENT, "evidence_id": evidence_id,
        "reasons": list(reasons), "evaluated_at": result["evaluated_at"]})
    del new_record["history"][:-cfg["history_limit"]]
    new_record["record_id"] = _record_digest(new_record)
    result.update(condition=COND_INSUFFICIENT, record=new_record, record_changed=True)
    return _finish(result, INSUFFICIENT, "Evidence is not sufficient to evaluate the disaster "
                   "state; the state is unchanged.", reasons)


def failed_result(record: Any, now: Any, error: str) -> dict[str, Any]:
    return _finish(_base_result(record if isinstance(record, dict) else None, now), FAILED,
                   "State evaluation could not run; the state is unchanged.", [error])


# --------------------------------------------------------------------------
# Audit (existing append-only log; fail closed)
# --------------------------------------------------------------------------

def audit_and_finalize(result: dict[str, Any], original_record: Any, audit_log: Any,
                       run_id: str) -> dict[str, Any]:
    """Record the evaluation. If it cannot be recorded, nothing it decided stands:
    an accepted transition becomes REJECTED (AUDIT_WRITE_FAILED) and the original
    record is returned unchanged."""
    try:
        written = audit_log.log(
            run_id, AUDIT_EVENTS.get(result.get("outcome"), AUDIT_EVENTS[FAILED]),
            outcome=result.get("outcome"), scope=result.get("scope"),
            current_state=result.get("current_state"),
            proposed_state=result.get("proposed_state"),
            resulting_state=result.get("resulting_state"), condition=result.get("condition"),
            reason=result.get("reason"), reasons=result.get("reasons"),
            evidence_id=result.get("evidence_id"), observed_at=result.get("observed_at"),
            risk_assessment_id=result.get("risk_assessment_id"),
            supporting_evidence_ids=result.get("supporting_evidence_ids"),
            persistence=result.get("persistence"),
            state_decision_id=result.get("state_decision_id"),
            authorizes_actions=False) is True
    except Exception:
        written = False
    result["audit_written"] = written
    if not written:
        revert_result(result, original_record, "AUDIT_WRITE_FAILED")
    return result


def revert_result(result: dict[str, Any], original_record: Any, code: str) -> dict[str, Any]:
    """Undo whatever an evaluation decided (used when its audit trail is incomplete)."""
    if result.get("outcome") == ACCEPTED:
        result["outcome"] = REJECTED
        result["reason"] = f"Transition not applied: {code}."
    result["resulting_state"] = result.get("current_state")
    result["record"] = copy.deepcopy(original_record) if isinstance(original_record,
                                                                    dict) else None
    result["record_changed"] = False
    if code not in result.get("reasons", []):
        result["reasons"] = list(result.get("reasons", [])) + [code]
    return result


# --------------------------------------------------------------------------
# Simulated flood-cycle sequence (offline demonstration)
# --------------------------------------------------------------------------

CYCLE_DIR = "data/simulated/flood_cycle"
SEQUENCES = {
    # River hours (from 2026-10-10T00:00Z) evaluated, 30 min after each reading.
    "flood-cycle": list(range(30, 119, 2)),
    "short-rise": list(range(40, 56, 2)),
}


def run_state_sequence(name: str, service: Any, audit_log: Any,
                       record: dict[str, Any] | None = None,
                       hours: list[int] | None = None) -> list[dict[str, Any]]:
    """Run the workflow over the simulated flood cycle, carrying the state record
    forward explicitly (fabricated data; TEST thresholds; scope simulated)."""
    from datetime import timedelta

    from src.risk_assessment import load_simulated_weather
    from src.river_agent_workflow import TEST_THRESHOLDS_PATH, run_river_workflow
    from src.river_service import RiverEvidenceService

    config = json.loads(json.dumps(service.config))
    config["simulated"] = {**(config.get("simulated") or {}),
                           "path": f"{CYCLE_DIR}/river.csv", "cross_check_paths": []}
    config["thresholds_path"] = TEST_THRESHOLDS_PATH
    cycle_service = RiverEvidenceService(config, base_dir=service.base_dir,
                                         session=service.session, sleep=service.sleep,
                                         clock=service.clock)
    record = record or new_state_record(NORMAL, MODE_SIMULATED)
    start = datetime(2026, 10, 10, 0, 30, tzinfo=timezone.utc)
    steps = []
    for hour in hours if hours is not None else SEQUENCES[name]:
        as_of = start + timedelta(hours=hour)
        weather = load_simulated_weather(service.resolve_path(f"{CYCLE_DIR}/weather.json"),
                                         retrieved_at=as_of)
        result = run_river_workflow(cycle_service, audit_log, mode=MODE_SIMULATED,
                                    disaster_state=record["state"], as_of=as_of,
                                    cross_check_paths=[], independent_paths=[],
                                    weather=weather, state_record=record,
                                    scenario=f"state-sequence:{name}")
        state = result["state_evaluation"]
        if state.get("record") is not None and not result["workflow_audit_write_errors"]:
            record = state["record"]
        steps.append({"hour": hour, "as_of": as_of.isoformat(),
                      "river_level_m": result["simulated_river_level_m"],
                      "risk_level": result["risk_assessment"]["risk_level"],
                      "condition": state["condition"], "outcome": state["outcome"],
                      "state_before": state["current_state"],
                      "state_after": state["resulting_state"], "reason": state["reason"]})
    return steps


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run a simulated disaster-state sequence.")
    parser.add_argument("--sequence", choices=tuple(SEQUENCES), default="flood-cycle")
    parser.add_argument("--audit", default="outputs/audit/state_sequence_audit.jsonl")
    parser.add_argument("--json", action="store_true", help="Print every step as JSON")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    from src.audit_log import AuditLog
    from src.river_service import RiverEvidenceService

    service = RiverEvidenceService.from_file()
    steps = run_state_sequence(args.sequence, service,
                               AuditLog(service.resolve_path(args.audit),
                                        component="state_sequence"))
    if args.json:
        print(json.dumps(steps, indent=2, ensure_ascii=False))
    else:
        print("SIMULATED flood cycle (fabricated data, TEST thresholds; not real conditions)")
        for s in steps:
            change = (f"{s['state_before']} -> {s['state_after']}"
                      if s["state_before"] != s["state_after"] else s["state_after"])
            print(f"h{s['hour']:>3}  {s['river_level_m']!s:>5} m  risk {s['risk_level']:<13}"
                  f" {s['condition'] or '-':<12} {s['outcome']:<22} {change}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
