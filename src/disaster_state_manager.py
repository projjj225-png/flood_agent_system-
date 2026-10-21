"""Deterministic disaster-state manager with recovery-aware capability revocation.

Target state from risk: LOW -> NORMAL, MODERATE/HIGH -> ALERT, SEVERE -> EMERGENCY.

Transitions (one evaluation = one call of ``next_state``):
- Escalation is immediate (NORMAL -> ALERT -> EMERGENCY, or straight to
  EMERGENCY). RECOVERY -> EMERGENCY if risk becomes SEVERE again.
- De-escalation needs persistence: N consecutive evaluations with a lower
  target (settings). EMERGENCY never goes straight to NORMAL: it moves to
  RECOVERY first, and RECOVERY returns to NORMAL only after sustained LOW risk.
- UNKNOWN risk (unusable evidence) HOLDS the current state and resets the
  de-escalation counter: missing evidence never counts as improvement and
  never lowers a state.

On every transition the capabilities lost (``revoked``) and gained are
computed from capability_control.STATE_CAPABILITIES and audited, which is the
recovery-aware revocation step (e.g. EMERGENCY -> RECOVERY revokes
request_human_emergency_authorization and propose_evacuation_plan).

State is persisted in a JSON file. Simulated runs use their own file, so
simulated evidence can never change the real disaster state.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from src.capability_control import DisasterState, permitted_capabilities
from src.risk_assessment import RISK_HIGH, RISK_LOW, RISK_MODERATE, RISK_SEVERE, RISK_UNKNOWN

NORMAL, ALERT, EMERGENCY, RECOVERY = (s.value for s in DisasterState)
TARGET_STATE = {RISK_LOW: NORMAL, RISK_MODERATE: ALERT, RISK_HIGH: ALERT,
                RISK_SEVERE: EMERGENCY}
RANK = {NORMAL: 0, ALERT: 1, EMERGENCY: 2}
DEFAULT_STATE_SETTINGS = {
    "alert_to_normal_after": 3,
    "emergency_to_recovery_after": 3,
    "recovery_to_normal_after": 6,
    "history_limit": 500,
}


def next_state(current: str, risk_level: str, pending: int,
               settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Pure transition function. ``pending`` counts consecutive lower targets."""
    settings = {**DEFAULT_STATE_SETTINGS, **(settings or {})}
    current = DisasterState(current).value
    if risk_level == RISK_UNKNOWN or risk_level not in TARGET_STATE:
        return {"from": current, "to": current, "pending": 0, "changed": False,
                "reason": "HOLD_EVIDENCE_UNKNOWN"}
    target = TARGET_STATE[risk_level]

    def result(to: str, pending_count: int, reason: str) -> dict[str, Any]:
        return {"from": current, "to": to, "pending": pending_count,
                "changed": to != current, "reason": reason, "target": target}

    if current == RECOVERY:
        if target == EMERGENCY:
            return result(EMERGENCY, 0, "RE_ESCALATION_DURING_RECOVERY")
        if target == NORMAL:
            count = pending + 1
            if count >= settings["recovery_to_normal_after"]:
                return result(NORMAL, 0, "RECOVERY_COMPLETE")
            return result(RECOVERY, count, "RECOVERY_IN_PROGRESS")
        return result(RECOVERY, 0, "RECOVERY_RISK_STILL_ELEVATED")

    if RANK[target] > RANK[current]:
        return result(target, 0, "ESCALATION")
    if RANK[target] == RANK[current]:
        return result(current, 0, "NO_CHANGE")
    count = pending + 1
    if current == EMERGENCY:
        if count >= settings["emergency_to_recovery_after"]:
            return result(RECOVERY, 0, "EMERGENCY_TO_RECOVERY")
        return result(EMERGENCY, count, "DEESCALATION_PENDING")
    if count >= settings["alert_to_normal_after"]:
        return result(NORMAL, 0, "ALERT_CLEARED")
    return result(ALERT, count, "DEESCALATION_PENDING")


def capability_changes(old: str, new: str) -> dict[str, list[str]]:
    before, after = permitted_capabilities(old), permitted_capabilities(new)
    return {"revoked": sorted(before - after), "granted": sorted(after - before)}


class StateStore:
    """JSON-file state: {state, since, pending, updated_at, history[]}."""

    def __init__(self, path: str | Path, simulated: bool = False,
                 settings: dict[str, Any] | None = None):
        self.path = Path(path)
        self.simulated = simulated
        self.settings = {**DEFAULT_STATE_SETTINGS, **(settings or {})}

    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            return {"state": NORMAL, "since": None, "pending": 0, "updated_at": None,
                    "simulated": self.simulated, "history": []}
        data = json.loads(self.path.read_text(encoding="utf-8"))
        DisasterState(data["state"])  # refuse corrupted state files
        if bool(data.get("simulated")) != self.simulated:
            raise ValueError(f"{self.path} belongs to a "
                             f"{'simulated' if data.get('simulated') else 'real'} run")
        return data

    def apply(self, risk: dict[str, Any], at: datetime,
              evidence_ref: str | None = None) -> dict[str, Any]:
        """Evaluate one risk result, persist, and return the transition record."""
        data = self.load()
        transition = next_state(data["state"], risk["risk_level"], data.get("pending", 0),
                                self.settings)
        transition.update(at=at.astimezone(timezone.utc).isoformat(),
                          risk_level=risk["risk_level"], evidence_ref=evidence_ref,
                          **capability_changes(transition["from"], transition["to"]))
        if transition["changed"]:
            data["since"] = transition["at"]
        data.update(state=transition["to"], pending=transition["pending"],
                    updated_at=transition["at"], simulated=self.simulated)
        data["history"] = (data.get("history") or []) + [
            {k: transition[k] for k in ("at", "from", "to", "risk_level", "reason",
                                        "pending", "revoked", "granted", "evidence_ref")}]
        data["history"] = data["history"][-self.settings["history_limit"]:]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        tmp.replace(self.path)  # atomic: a crash never leaves a half-written state
        return transition
