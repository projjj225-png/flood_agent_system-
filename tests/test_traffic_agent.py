"""Tests for the Traffic Agent. No live network access is used."""

import copy
import json
from datetime import datetime, timezone

import pytest
import requests

from src.traffic_agent import (
    BLOCKED,
    CLOSED,
    FREE_FLOW,
    HEAVY,
    MODERATE,
    SEVERE,
    SOURCE_TYPE_SIMULATED,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    UNKNOWN,
    LiveTrafficSource,
    SimulatedTrafficSource,
    TrafficAgent,
    assess_route,
    classify_speed_ratio,
    distance_to_polyline_m,
    list_scenarios,
    load_config,
    main,
    parse_segment,
    save_result,
    validate_config,
)

SECRET = "sk-test-SECRET123"
NOW = datetime(2026, 10, 10, 12, 0, tzinfo=timezone.utc)
CONFIG = load_config()
SEGMENT_IDS = [s["id"] for s in CONFIG["segments"]]


def road_through(lat, lon, offset=0.0):
    """A short provider road passing ``offset`` degrees of latitude north of a point."""
    return {"coordinate": [{"latitude": lat + offset - 0.001, "longitude": lon - 0.002},
                           {"latitude": lat + offset, "longitude": lon},
                           {"latitude": lat + offset + 0.001, "longitude": lon + 0.002}]}


def flow(current=50, free=50, confidence=0.95, closure=False, at=None, **extra):
    """Flow payload whose road passes through segment ``at`` (default: the first)."""
    at = at or CONFIG["segments"][0]
    data = {"frc": "FRC2", "currentSpeed": current, "freeFlowSpeed": free,
            "currentTravelTime": 120, "freeFlowTravelTime": 100,
            "confidence": confidence, "roadClosure": closure,
            "coordinates": road_through(at["latitude"], at["longitude"])}
    data.update(extra)
    return {"flowSegmentData": data}


class FakeResponse:
    def __init__(self, status_code=200, body=None, invalid_json=False):
        self.status_code = status_code
        self._body = body
        self._invalid = invalid_json

    def json(self):
        if self._invalid:
            raise ValueError("Expecting value")
        return self._body


class FakeSession:
    """Answers by segment point; records every URL it was asked for."""

    def __init__(self, by_point=None, default=None, exc=None):
        self.by_point = by_point or {}
        # Without a default, each point gets a free-flowing road through it.
        self.default = default
        self.exc = exc
        self.urls = []

    def get(self, url, timeout=None):
        self.urls.append(url)
        if self.exc:
            raise self.exc
        for point, response in self.by_point.items():
            if point in url:
                return response
        if self.default is not None:
            return self.default
        seg = next(s for s in CONFIG["segments"] if point_of(s["id"]) in url)
        return FakeResponse(200, flow(at=seg))


def point_of(segment_id):
    seg = next(s for s in CONFIG["segments"] if s["id"] == segment_id)
    return f"{seg['latitude']},{seg['longitude']}"


def live_agent(session, key=SECRET):
    return TrafficAgent(LiveTrafficSource(key, session=session), CONFIG, clock=lambda: NOW)


def simulated(name):
    return TrafficAgent(SimulatedTrafficSource(f"data/simulated/traffic/{name}.json"), CONFIG)


# ---- classification and parsing -------------------------------------------

@pytest.mark.parametrize("ratio,expected", [
    (1.0, FREE_FLOW), (0.8, FREE_FLOW), (0.79, MODERATE), (0.5, MODERATE),
    (0.49, HEAVY), (0.25, HEAVY), (0.24, SEVERE), (0.0, SEVERE), (None, UNKNOWN),
])
def test_classify_speed_ratio(ratio, expected):
    assert classify_speed_ratio(ratio, CONFIG["congestion_thresholds"]) == expected


def test_parse_segment_valid():
    errors = []
    seg = parse_segment(CONFIG["segments"][0], flow(current=30, free=60), CONFIG, errors)
    assert seg["condition"] == MODERATE
    assert seg["speed_ratio"] == 0.5
    assert seg["delay_s"] == 20
    assert seg["returned_road"]["point_count"] == 3
    assert seg["returned_road"]["match_distance_m"] < 1
    assert seg["low_confidence"] is False
    assert errors == []


def test_speed_above_free_flow_is_capped_at_ratio_one():
    seg = parse_segment(CONFIG["segments"][0], flow(current=70, free=50), CONFIG, [])
    assert seg["speed_ratio"] == 1.0
    assert seg["condition"] == FREE_FLOW


def test_road_closure_overrides_speeds():
    seg = parse_segment(CONFIG["segments"][0], flow(current=50, closure=True), CONFIG, [])
    assert seg["condition"] == CLOSED


def test_closure_without_speeds_is_still_closed():
    payload = flow(closure=True)
    del payload["flowSegmentData"]["currentSpeed"]
    seg = parse_segment(CONFIG["segments"][0], payload, CONFIG, [])
    assert seg["condition"] == CLOSED
    assert seg["speed_ratio"] is None


@pytest.mark.parametrize("field,value", [
    ("currentSpeed", None), ("currentSpeed", "fast"), ("currentSpeed", -5),
    ("freeFlowSpeed", 0), ("freeFlowSpeed", float("nan")),
])
def test_missing_or_invalid_speed_is_unknown_never_free_flow(field, value):
    errors = []
    seg = parse_segment(CONFIG["segments"][0], flow(**{field: value}), CONFIG, errors)
    assert seg["condition"] == UNKNOWN
    assert seg["current_speed_kmph"] is None
    assert errors[0]["code"] == "MISSING_VALUE"


@pytest.mark.parametrize("payload", [None, [], {"other": 1}, {"flowSegmentData": "x"}])
def test_invalid_payload(payload):
    errors = []
    seg = parse_segment(CONFIG["segments"][0], payload, CONFIG, errors)
    assert seg["condition"] == UNKNOWN
    assert errors[0]["code"] == "INVALID_PAYLOAD"


@pytest.mark.parametrize("confidence", [0.3, None, 1.7])
def test_low_missing_or_invalid_confidence_is_flagged(confidence):
    errors = []
    seg = parse_segment(CONFIG["segments"][0], flow(confidence=confidence), CONFIG, errors)
    assert seg["condition"] == FREE_FLOW
    assert seg["low_confidence"] is True
    assert "LOW_CONFIDENCE" in [e["code"] for e in errors]


def test_missing_road_closure_is_warned_not_assumed():
    payload = flow()
    del payload["flowSegmentData"]["roadClosure"]
    errors = []
    seg = parse_segment(CONFIG["segments"][0], payload, CONFIG, errors)
    assert seg["road_closure"] is None
    assert any(e["code"] == "MISSING_FIELD" and e["severity"] == "warning" for e in errors)


# ---- road matching ------------------------------------------------------------

def test_distance_to_polyline():
    line = [(10.0, 78.0), (10.0, 78.01)]
    assert distance_to_polyline_m(10.0, 78.005, line) < 0.01
    assert distance_to_polyline_m(10.001, 78.005, line) == pytest.approx(111.2, abs=0.5)
    # Beyond the end: distance to the end point, not to the infinite line.
    assert distance_to_polyline_m(10.0, 78.02, line) == pytest.approx(1095, abs=5)


def test_road_far_from_point_is_rejected_as_mismatch():
    seg_cfg = CONFIG["segments"][0]
    far = road_through(seg_cfg["latitude"], seg_cfg["longitude"], offset=0.0054)  # ~540 m from the slanted road
    errors = []
    seg = parse_segment(seg_cfg, flow(coordinates=far), CONFIG, errors)
    assert seg["condition"] == UNKNOWN
    assert seg["current_speed_kmph"] is None
    assert seg["returned_road"]["match_distance_m"] > 500
    assert errors[0]["code"] == "ROAD_MISMATCH"


def test_road_within_limit_is_accepted():
    seg_cfg = CONFIG["segments"][0]
    near = road_through(seg_cfg["latitude"], seg_cfg["longitude"], offset=0.0008)  # ~90 m
    seg = parse_segment(seg_cfg, flow(coordinates=near), CONFIG, [])
    assert seg["condition"] == FREE_FLOW


def test_missing_geometry_is_warned_not_rejected():
    payload = flow()
    del payload["flowSegmentData"]["coordinates"]
    errors = []
    seg = parse_segment(CONFIG["segments"][0], payload, CONFIG, errors)
    assert seg["condition"] == FREE_FLOW
    assert seg["returned_road"] is None
    assert [e["code"] for e in errors] == ["NO_GEOMETRY"]


def test_shared_provider_segment_is_claimed_once():
    first, second = CONFIG["segments"][1], CONFIG["segments"][2]
    # One long provider road through both points, as TomTom returned on NH 81.
    long_road = {"coordinate": [
        {"latitude": first["latitude"], "longitude": first["longitude"]},
        {"latitude": second["latitude"], "longitude": second["longitude"]}]}
    response = FakeResponse(200, flow(current=40, free=50, coordinates=long_road))
    session = FakeSession(by_point={point_of(first["id"]): response,
                                    point_of(second["id"]): response})
    result = live_agent(session).run()
    assert result["segments"][second["id"]]["duplicate_of"] == first["id"]
    assert result["segments"][second["id"]]["condition"] == FREE_FLOW
    subjects = [c["subject"] for c in result["claims"]]
    assert first["id"] in subjects and second["id"] not in subjects
    assert "DUPLICATE_PROVIDER_SEGMENT" in [e["code"] for e in result["errors"]]


def test_config_rejects_bad_match_limit():
    bad = copy.deepcopy(CONFIG)
    bad["max_match_distance_m"] = 0
    with pytest.raises(ValueError, match="max_match_distance_m"):
        validate_config(bad)


# ---- route assessment ---------------------------------------------------------

def segments_with(**conditions):
    out = {}
    for seg_cfg in CONFIG["segments"]:
        sid = seg_cfg["id"]
        seg = parse_segment(seg_cfg, flow(at=seg_cfg), CONFIG, [])
        if sid in conditions:
            seg["condition"] = conditions[sid]
        out[sid] = seg
    return out


def test_route_takes_worst_segment():
    route = assess_route(CONFIG["routes"][0], segments_with(NH81_KUDAMURUTI=HEAVY))
    assert route["condition"] == HEAVY
    assert route["worst_segment"] == "NH81_KUDAMURUTI"
    assert route["passable"] is True


def test_route_with_unknown_segment_is_unknown():
    route = assess_route(CONFIG["routes"][0], segments_with(NH81_KUDAMURUTI=UNKNOWN))
    assert route["condition"] == UNKNOWN
    assert route["passable"] is None


def test_closure_wins_over_unknown():
    route = assess_route(CONFIG["routes"][0],
                         segments_with(NH81_KUDAMURUTI=UNKNOWN, NH81_JEEYAPURAM_EAST=CLOSED))
    assert route["condition"] == BLOCKED
    assert route["passable"] is False


# ---- simulated scenarios ---------------------------------------------------------

def test_every_scenario_is_labelled_simulated():
    assert len(list_scenarios()) >= 5
    for name in list_scenarios():
        result = simulated(name).run()
        assert result["is_simulated"] is True
        assert result["source_type"] == SOURCE_TYPE_SIMULATED
        assert all(c["is_simulated"] for c in result["claims"])
        assert result["data_notes"][0].startswith("FABRICATED")


@pytest.mark.parametrize("name,status,primary,alternate,advisory", [
    ("normal_flow", STATUS_OK, FREE_FLOW, FREE_FLOW, None),
    ("evacuation_congestion", STATUS_OK, SEVERE, MODERATE, "CONSIDER_ALTERNATE_ROUTE"),
    ("flood_road_closure", STATUS_OK, BLOCKED, FREE_FLOW, "CONSIDER_ALTERNATE_ROUTE"),
    ("shared_nh81_closed", STATUS_OK, BLOCKED, BLOCKED, "NO_CLEAR_ROUTE"),
    ("degraded_data", STATUS_PARTIAL, UNKNOWN, UNKNOWN, "ROUTE_STATUS_UNKNOWN"),
])
def test_scenarios(name, status, primary, alternate, advisory):
    result = simulated(name).run()
    assert result["status"] == status
    assert result["routes"]["EVAC_PRIMARY_NH81"]["condition"] == primary
    assert result["routes"]["EVAC_ALTERNATE_INLAND"]["condition"] == alternate
    types = [a["type"] for a in result["advisories"]]
    assert types == ([advisory] if advisory else [])
    assert all(a["advisory_only"] for a in result["advisories"])


def test_claims_skip_unknown_items():
    result = simulated("degraded_data").run()
    subjects = {c["subject"] for c in result["claims"]}
    assert "NH81_KUDAMURUTI" not in subjects
    assert "CHATHIRAM_CITY_ENTRY" not in subjects
    assert "EVAC_PRIMARY_NH81" not in subjects
    assert "NH81_MUKKOMBU_JEEYAPURAM" in subjects


def test_route_claim_confidence_is_weakest_segment():
    result = simulated("evacuation_congestion").run()
    claim = next(c for c in result["claims"] if c["subject"] == "EVAC_ALTERNATE_INLAND")
    assert claim["confidence"] == 0.9


def test_low_confidence_alternate_is_not_recommended(tmp_path):
    doc = json.loads(open("data/simulated/traffic/flood_road_closure.json").read())
    doc["responses"]["VAYALUR_RD_SOMARASAMPETTAI"]["body"]["flowSegmentData"]["confidence"] = 0.2
    path = tmp_path / "low_conf.json"
    path.write_text(json.dumps(doc))
    result = TrafficAgent(SimulatedTrafficSource(path), CONFIG).run()
    assert [a["type"] for a in result["advisories"]] == ["NO_CLEAR_ROUTE"]


def test_simulated_file_must_declare_simulated(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"retrieved_at": "2026-10-10T12:00:00+00:00",
                                "responses": {}}))
    with pytest.raises(ValueError, match="simulated"):
        SimulatedTrafficSource(path)


def test_simulated_file_needs_aware_time(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"simulated": True, "retrieved_at": "2026-10-10T12:00:00",
                                "responses": {}}))
    with pytest.raises(ValueError, match="timezone-aware"):
        SimulatedTrafficSource(path)


# ---- live source (fake session) ---------------------------------------------------

def test_live_run_ok_and_key_never_in_output():
    session = FakeSession()
    result = live_agent(session).run()
    assert result["status"] == STATUS_OK
    assert result["is_simulated"] is False
    assert len(session.urls) == len(SEGMENT_IDS)
    assert all(f"key={SECRET}" in u and "unit=KMPH" in u for u in session.urls)
    assert SECRET not in json.dumps(result)


def test_missing_key_makes_no_requests():
    session = FakeSession()
    result = live_agent(session, key=None).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert session.urls == []
    assert result["errors"][0]["code"] == "MISSING_API_KEY"
    assert result["claims"] == []


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_or_quota_error_stops_further_requests(status):
    session = FakeSession(default=FakeResponse(status, {"error": "Forbidden"}))
    result = live_agent(session).run()
    assert len(session.urls) == 1
    assert result["status"] == STATUS_UNAVAILABLE
    assert f"HTTP {status}" in result["errors"][0]["message"]


def test_server_error_on_one_segment_gives_partial():
    session = FakeSession(by_point={point_of("NH81_KUDAMURUTI"): FakeResponse(503, None)})
    result = live_agent(session).run()
    assert len(session.urls) == len(SEGMENT_IDS)
    assert result["status"] == STATUS_PARTIAL
    assert result["segments"]["NH81_KUDAMURUTI"]["condition"] == UNKNOWN
    assert result["routes"]["EVAC_PRIMARY_NH81"]["condition"] == UNKNOWN


@pytest.mark.parametrize("exc,code", [
    (requests.exceptions.Timeout(f"timeout for ...&key={SECRET}"), "NETWORK_TIMEOUT"),
    (requests.exceptions.ConnectionError(f"failed ...?key={SECRET}"), "NETWORK_ERROR"),
    (requests.exceptions.RequestException("boom"), "REQUEST_FAILED"),
])
def test_network_errors_are_reported_and_redacted(exc, code):
    result = live_agent(FakeSession(exc=exc)).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == code
    assert SECRET not in json.dumps(result)


def test_invalid_json_response():
    result = live_agent(FakeSession(default=FakeResponse(invalid_json=True))).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "INVALID_JSON"


def test_to_dict_before_run_and_returns_copy():
    agent = live_agent(FakeSession())
    assert agent.to_dict()["errors"][0]["code"] == "NOT_RUN"
    agent.run()
    first = agent.to_dict()
    first["segments"].clear()
    assert agent.to_dict()["segments"]


# ---- config, saving, CLI ------------------------------------------------------------

def test_config_rejects_unknown_route_segment():
    bad = copy.deepcopy(CONFIG)
    bad["routes"][0]["segments"].append("NOPE")
    with pytest.raises(ValueError, match="unknown segments"):
        validate_config(bad)


def test_config_rejects_unordered_thresholds():
    bad = copy.deepcopy(CONFIG)
    bad["congestion_thresholds"]["heavy_min_ratio"] = 0.9
    with pytest.raises(ValueError, match="congestion_thresholds"):
        validate_config(bad)


def test_save_labels_mode_and_refuses_unavailable(tmp_path):
    agent = simulated("normal_flow")
    path = save_result(agent.run(), agent.raw_responses, tmp_path)
    assert path.name == "traffic_simulated_20261010T120000Z.json"
    assert "SIMULATED" in json.loads(path.read_text())["label"]
    unavailable = live_agent(FakeSession(), key=None).run()
    with pytest.raises(ValueError):
        save_result(unavailable, {}, tmp_path)


def test_cli_exit_codes(capsys, monkeypatch):
    monkeypatch.delenv("TOMTOM_API_KEY", raising=False)
    assert main(["--mode", "simulated", "--scenario", "normal_flow"]) == 0
    assert "SIMULATED" in capsys.readouterr().out
    assert main(["--mode", "simulated", "--scenario", "missing_scenario"]) == 2
    assert main(["--mode", "simulated"]) == 2
    assert main(["--mode", "live"]) == 1


def test_cli_json_output(capsys):
    assert main(["--mode", "simulated", "--scenario", "flood_road_closure", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["routes"]["EVAC_PRIMARY_NH81"]["condition"] == BLOCKED
