"""Append-only JSON Lines audit log.

Each line is one event: {"at", "run_id", "component", "event", "details"}.
Writing never raises: an audit failure must not stop evidence from being
produced, but it is reported back to the caller (``AuditLog.write_errors``)
so the result can say that its audit trail is incomplete.

Details are redacted before writing: values under secret-like keys
(password, token, api_key, ...), credentials embedded in URLs and secret
query parameters are replaced with "[REDACTED]", and long strings are
truncated. No current source uses credentials; this keeps a future one
from leaking into the log through an error message or a request URL.
"""

from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

REDACTED = "[REDACTED]"
MAX_TEXT_LENGTH = 2000
# Whole key names (or *_<name>) only, so e.g. "real_world_authorization" is kept.
_SECRET_KEY = re.compile(r"(^|_)(password|passwd|secret|token|api_?key|apikey|cookie|"
                         r"credentials?|private_key|authorization)$", re.IGNORECASE)
_SECRET_KEY_EXEMPT = frozenset({"real_world_authorization"})
_URL_USERINFO = re.compile(r"(?<=://)[^/\s:@]+:[^/\s@]+@")
_SECRET_QUERY = re.compile(r"([?&](?:api_?key|apikey|key|token|access_token|secret|password|"
                           r"signature|sig)=)[^&\s#'\"]+", re.IGNORECASE)
_BEARER = re.compile(r"(Bearer\s+)[A-Za-z0-9._~+/=-]+", re.IGNORECASE)


def redact(value: Any, depth: int = 0) -> Any:
    """Copy of ``value`` with secrets removed (see module docstring)."""
    if depth > 20:
        return "[TRUNCATED: nested too deeply]"
    if isinstance(value, dict):
        return {k: REDACTED if isinstance(k, str) and _SECRET_KEY.search(k)
                and k not in _SECRET_KEY_EXEMPT else redact(v, depth + 1)
                for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact(v, depth + 1) for v in value]
    if isinstance(value, str):
        text = _BEARER.sub(r"\1" + REDACTED, _SECRET_QUERY.sub(
            r"\1" + REDACTED, _URL_USERINFO.sub(REDACTED + "@", value)))
        return text if len(text) <= MAX_TEXT_LENGTH else text[:MAX_TEXT_LENGTH] + "…[truncated]"
    return value


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
        try:
            entry = {"at": self.clock().astimezone(timezone.utc).isoformat(), "run_id": run_id,
                     "component": self.component, "event": event,
                     "details": redact(details)}
            line = json.dumps(entry, ensure_ascii=False, default=str, sort_keys=True)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except Exception as exc:  # never raise: report the failure instead
            self.write_errors.append(redact(f"{event}: {type(exc).__name__}: {exc}"))
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
