"""Read-only local HTTP API for river evidence (standard library only).

    python -m src.river_api --port 8765

Endpoints (GET only, JSON responses):
    /health                         -> {"status": "ok", "schema_version": ...}
    /river/evidence?mode=auto       -> full evidence dict (see src.river_service)
    /river/evidence?mode=historical&as_of=2025-12-30T06:00:00%2B05:30
    /river/summary?mode=auto        -> decision_support block + identifiers

Binds to 127.0.0.1 by default. Live/auto results are cached for
``cache_seconds`` (default 300) so callers cannot hammer the provider; the
cache age is reported in the X-Evidence-Cache-Age header and the response.
"""

from __future__ import annotations

import argparse
import json
import sys
import threading
import time
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlsplit

from src.river_monitoring_agent import parse_timestamp
from src.river_service import (
    DEFAULT_CONFIG_PATH,
    MODE_AUTO,
    MODE_LIVE,
    MODES,
    SCHEMA_VERSION,
    RiverEvidenceService,
)


class EvidenceProvider:
    """Wraps the service with a small, thread-safe cache for live/auto calls."""

    def __init__(self, service: RiverEvidenceService, cache_seconds: float = 300.0,
                 monotonic=time.monotonic):
        self.service = service
        self.cache_seconds = cache_seconds
        self.monotonic = monotonic
        self._cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self._lock = threading.Lock()

    def get(self, mode: str, as_of: datetime | None) -> tuple[dict[str, Any], float | None]:
        cacheable = mode in (MODE_AUTO, MODE_LIVE) and self.cache_seconds > 0
        with self._lock:
            if cacheable and mode in self._cache:
                stored_at, evidence = self._cache[mode]
                age = self.monotonic() - stored_at
                if age < self.cache_seconds:
                    return evidence, age
            evidence = self.service.get_evidence(mode=mode, as_of=as_of)
            if cacheable:
                self._cache[mode] = (self.monotonic(), evidence)
            return evidence, 0.0 if cacheable else None


DEFAULT_REQUEST_TIMEOUT = 10.0  # seconds
MAX_DISCARD_BYTES = 64 * 1024


ENDPOINTS = [
    "GET /health",
    "GET /river/evidence?mode=auto|live|historical|simulated[&as_of=ISO]",
    "GET /river/summary?mode=...",
    "GET /dashboard?mode=real",
    "GET /dashboard?mode=simulated&scenario=NAME&state=NORMAL|ALERT|EMERGENCY|RECOVERY",
    "GET /dashboard?mode=simulated_live&scenario=stable|slowly-rising|rapidly-rising|falling"
    "&state=NORMAL|ALERT|EMERGENCY|RECOVERY",
    "GET /dashboard/analysis   (historical CWC analysis + forecast validation)",
    "GET /dashboard/scenarios",
    "GET /dashboard/sequence?name=flood-cycle|short-rise",
]


def make_handler(provider: EvidenceProvider,
                 request_timeout: float | None = DEFAULT_REQUEST_TIMEOUT,
                 dashboard: Any = None):
    class Handler(BaseHTTPRequestHandler):
        server_version = "RiverEvidenceAPI/" + SCHEMA_VERSION

        def _send(self, code: int, body: dict[str, Any], cache_age: float | None = None):
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            if cache_age is not None:
                self.send_header("X-Evidence-Cache-Age", f"{cache_age:.1f}")
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter test output
            sys.stderr.write("river_api: " + (fmt % args) + "\n")

        def do_GET(self) -> None:  # noqa: N802 (http.server API)
            parts = urlsplit(self.path)
            query = {k: v[-1] for k, v in parse_qs(parts.query).items()}
            if parts.path == "/health":
                return self._send(200, {"status": "ok", "schema_version": SCHEMA_VERSION})
            if parts.path == "/":
                return self._send(200, {"service": "River evidence API (read-only)",
                                        "schema_version": SCHEMA_VERSION,
                                        "endpoints": ENDPOINTS
                                        if dashboard is not None else ENDPOINTS[:3]})
            if parts.path.startswith("/dashboard") and dashboard is not None:
                return self._dashboard(parts.path, query)
            if parts.path not in ("/river/evidence", "/river/summary"):
                return self._send(404, {"error": "not found", "path": parts.path})
            mode = query.get("mode", MODE_AUTO)
            if mode not in MODES:
                return self._send(400, {"error": f"mode must be one of {', '.join(MODES)}"})
            as_of = None
            if "as_of" in query:
                as_of = parse_timestamp(query["as_of"])
                if as_of is None:
                    return self._send(400, {"error": "as_of must be a timezone-aware ISO 8601 "
                                                     "time (encode '+' as %2B)"})
            try:
                evidence, age = provider.get(mode, as_of)
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # never crash the server; never invent data
                return self._send(500, {"error": f"internal error: {type(exc).__name__}",
                                        "status": "DATA_UNAVAILABLE"})
            if age is not None:
                evidence = {**evidence, "cache_age_seconds": round(age, 1)}
            if parts.path == "/river/summary":
                body = {k: evidence[k] for k in ("schema_version", "evidence_id", "status",
                                                 "source_label", "station_id", "observed_at",
                                                 "generated_at")}
                body["decision_support"] = evidence["decision_support"]
                return self._send(200, body, age)
            return self._send(200, evidence, age)

        def _dashboard(self, path: str, query: dict[str, str]) -> None:
            """Read-only dashboard routes. Only whitelisted query keys are accepted."""
            allowed = {"/dashboard": {"mode", "scenario", "state"},
                       "/dashboard/scenarios": set(), "/dashboard/sequence": {"name"},
                       "/dashboard/analysis": set()}
            if path not in allowed:
                return self._send(404, {"error": "not found", "path": path})
            unknown = sorted(set(query) - allowed[path])
            if unknown:
                return self._send(400, {"error": f"unknown query parameter(s): "
                                                 f"{', '.join(unknown)}"})
            try:
                if path == "/dashboard/scenarios":
                    return self._send(200, dashboard.scenarios())
                if path == "/dashboard/analysis":
                    doc = dashboard.analysis()
                    return self._send(500 if doc.get("error") else 200, doc)
                if path == "/dashboard/sequence":
                    doc, age = dashboard.sequence(query.get("name", "flood-cycle"))
                else:
                    doc, age = dashboard.get(query.get("mode", "real"), query.get("scenario"),
                                             query.get("state"))
            except ValueError as exc:
                return self._send(400, {"error": str(exc)})
            except Exception as exc:  # never crash the server; never invent data
                return self._send(500, {"error": f"internal error: {type(exc).__name__}",
                                        "status": "DATA_UNAVAILABLE"})
            body = {**doc, "cache_age_seconds": round(age, 1)}
            return self._send(500 if doc.get("error") else 200, body, age)

        def _discard_body(self) -> None:
            """Read (and drop) the declared request body before replying.

            Closing a socket whose receive buffer still holds unread bytes makes the
            OS send a TCP RST instead of a normal close; the client then loses the
            response (ConnectionAbortedError/ConnectionResetError). Bodies over
            MAX_DISCARD_BYTES or an invalid length are not read and the connection
            is closed, as before.
            """
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if 0 < length <= MAX_DISCARD_BYTES:
                try:
                    self.rfile.read(length)
                except OSError:  # includes the socket timeout for a stalled client
                    pass
            self.close_connection = True

        def do_POST(self) -> None:  # noqa: N802
            self._discard_body()
            self._send(405, {"error": "read-only API"})

    Handler.timeout = request_timeout  # socket timeout: a stalled client cannot pin a thread
    return Handler


def create_server(service: RiverEvidenceService, host: str = "127.0.0.1", port: int = 8765,
                  cache_seconds: float = 300.0,
                  request_timeout: float = DEFAULT_REQUEST_TIMEOUT,
                  dashboard: Any = None) -> ThreadingHTTPServer:
    """``dashboard``: a DashboardService for the /dashboard routes (None: not served)."""
    return ThreadingHTTPServer((host, port),
                               make_handler(EvidenceProvider(service, cache_seconds),
                                            request_timeout, dashboard))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Serve river evidence over local HTTP.")
    parser.add_argument("--config", default=str(DEFAULT_CONFIG_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--cache-seconds", type=float, default=300.0)
    parser.add_argument("--state-file", default="outputs/state/real_state.json",
                        help="Real-scope state record shown by /dashboard (read only; never "
                             "written by the API).")
    parser.add_argument("--no-dashboard", action="store_true",
                        help="Serve only the original /health and /river routes.")
    args = parser.parse_args(argv)
    try:
        service = RiverEvidenceService.from_file(args.config)
    except ValueError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2
    dashboard = None
    if not args.no_dashboard:
        from src.dashboard_service import DashboardService
        dashboard = DashboardService(service, state_file=args.state_file,
                                     cache_seconds=args.cache_seconds)
    server = create_server(service, args.host, args.port, args.cache_seconds,
                           dashboard=dashboard)
    print(f"River evidence API on http://{args.host}:{server.server_address[1]} "
          "(Ctrl+C to stop)")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
