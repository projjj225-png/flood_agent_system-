"""End-to-end tests for the local HTTP API (real sockets on 127.0.0.1, offline data)."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from src.river_api import EvidenceProvider, create_server
from tests.nwdp_fixtures import FakeSession
from tests.test_river_service import clean_live_session, make_service


@pytest.fixture
def api(tmp_path):
    session = clean_live_session()
    service = make_service(tmp_path, session)
    server = create_server(service, "127.0.0.1", 0, cache_seconds=300)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    yield base, session
    server.shutdown()
    server.server_close()


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=10) as response:
            return response.status, dict(response.headers), json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), json.loads(err.read())


def test_health(api):
    base, _ = api
    status, _, body = get(base + "/health")
    assert status == 200 and body["status"] == "ok"


def test_evidence_live_and_cache(api):
    base, session = api
    status, headers, body = get(base + "/river/evidence?mode=live")
    assert status == 200
    assert body["source_label"] == "LIVE_VERIFIED"
    assert body["river_level_m"] == 74.95
    calls_after_first = len(session.calls)
    status, headers, again = get(base + "/river/evidence?mode=live")
    assert again["evidence_id"] == body["evidence_id"]
    assert len(session.calls) == calls_after_first  # served from cache
    assert "X-Evidence-Cache-Age" in headers


def test_summary_historical_replay(api):
    base, _ = api
    status, _, body = get(base + "/river/summary?mode=historical&as_of=2025-12-30T06:00:00%2B05:30")
    assert status == 200
    assert body["source_label"] == "HISTORICAL_CSV"
    assert body["decision_support"]["last_known_level_m"] == 74.9
    assert body["decision_support"]["current_evidence_usable"] is False


@pytest.mark.parametrize("path,code", [
    ("/river/evidence?mode=guess", 400),
    ("/river/evidence?mode=historical&as_of=yesterday", 400),
    ("/river/evidence?mode=live&as_of=2025-12-30T06:00:00Z", 400),
    ("/nowhere", 404),
])
def test_bad_requests(api, path, code):
    base, _ = api
    status, _, body = get(base + path)
    assert status == code and "error" in body


def test_post_is_rejected(api):
    base, _ = api
    request = urllib.request.Request(base + "/river/evidence", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as info:
        urllib.request.urlopen(request, timeout=10)
    assert info.value.code == 405


def test_internal_error_never_returns_data(tmp_path):
    class Broken:
        def get_evidence(self, mode, as_of):
            raise RuntimeError("boom")
    provider = EvidenceProvider(Broken(), cache_seconds=0)
    from src.river_api import make_handler
    from http.server import ThreadingHTTPServer
    server = ThreadingHTTPServer(("127.0.0.1", 0), make_handler(provider))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        status, _, body = get(f"http://127.0.0.1:{server.server_address[1]}/river/evidence")
        assert status == 500 and body["status"] == "DATA_UNAVAILABLE"
        assert "river_level_m" not in body
    finally:
        server.shutdown()
        server.server_close()


def test_cache_expiry():
    calls = []

    class Service:
        def get_evidence(self, mode, as_of):
            calls.append(mode)
            return {"n": len(calls)}
    now = [0.0]
    provider = EvidenceProvider(Service(), cache_seconds=300, monotonic=lambda: now[0])
    provider.get("auto", None)
    now[0] = 299
    assert provider.get("auto", None)[0] == {"n": 1}
    now[0] = 301
    assert provider.get("auto", None)[0] == {"n": 2}
    provider.get("historical", None)
    provider.get("historical", None)
    assert calls.count("historical") == 2  # replay modes are never cached
