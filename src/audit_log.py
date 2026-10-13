"""Append-only JSON Lines audit log.

Each line is one event: {"at", "run_id", "component", "event", "details"}.
Writing never raises: an audit failure must not stop evidence from being
produced, but it is reported back to the caller (``AuditLog.write_errors``)
so the result can say that its audit trail is incomplete.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable


class AuditLog:
    def __init__(self, path: str | Path | None,
                 clock: Callable[[], datetime] | None = None,
                 component: str = "river"):
        self.path = Path(path) if path else None
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        self.component = component
        self.write_errors: list[str] = []
        self.events_written = 0

    def log(self, run_id: str, event: str, **details: Any) -> bool:
        if self.path is None:
            return False
        entry = {"at": self.clock().astimezone(timezone.utc).isoformat(), "run_id": run_id,
                 "component": self.component, "event": event, "details": details}
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True)
                             + "\n")
        except (OSError, TypeError, ValueError) as exc:
            self.write_errors.append(f"{event}: {exc}")
            return False
        self.events_written += 1
        return True


def read_audit_log(path: str | Path) -> list[dict[str, Any]]:
    """Read all events (skipping unreadable lines). For tests and review."""
    events = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        try:
            events.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return events
