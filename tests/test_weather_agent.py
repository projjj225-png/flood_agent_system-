"""Tests for the Weather Agent. No live network access is used."""

import copy
import json
from datetime import datetime, timedelta, timezone

import pytest
import requests

from src.weather_agent import (
    CURRENT_VARIABLES,
    HOURLY_VARIABLES,
    SOURCE_TYPE,
    STATUS_OK,
    STATUS_PARTIAL,
    STATUS_UNAVAILABLE,
    WeatherAgent,
    calculate_rainfall_accumulation,
    save_live_sample,
)

CONFIGURED_URL = (
    "https://api.open-meteo.com/v1/forecast?latitude=10.888611&longitude=78.58"
    "&hourly=precipitation,rain,wind_gusts_10m,wind_speed_10m,precipitation_probability,"
    "temperature_2m,relative_humidity_2m&current=temperature_2m,relative_humidity_2m,"
    "precipitation,rain,wind_speed_10m,wind_direction_10m,wind_gusts_10m"
    "&timezone=Asia%2FKolkata"
)
IST = timezone(timedelta(hours=5, minutes=30))
# Hourly series in the fixture starts here (local IST time) and lasts 72 hours.
SERIES_START = datetime(2026, 10, 9, 0, 0, tzinfo=IST)
SERIES_HOURS = 72
# 2026-10-10 14:20 IST: last completed hour ends at 14:00 (index 38).
REFERENCE = datetime(2026, 10, 10, 14, 20, tzinfo=IST)
RETRIEVED_AT = REFERENCE.astimezone(timezone.utc)


def make_payload(start=SERIES_START, hours=SERIES_HOURS):
    times = [(start + timedelta(hours=i)).strftime("%Y-%m-%dT%H:%M") for i in range(hours)]
    hourly = {"time": times}
    for var in HOURLY_VARIABLES:
        hourly[var] = [round(i / 10, 1) for i in range(hours)]
    hourly["rain"] = [round(i / 20, 2) for i in range(hours)]
    return {
        "latitude": 10.875,
        "longitude": 78.625,
        "generationtime_ms": 0.1,
        "utc_offset_seconds": 19800,
        "timezone": "Asia/Kolkata",
        "timezone_abbreviation": "GMT+5:30",
        "elevation": 81.0,
        "current_units": {
            "time": "iso8601", "interval": "seconds", "temperature_2m": "°C",
            "relative_humidity_2m": "%", "precipitation": "mm", "rain": "mm",
            "wind_speed_10m": "km/h", "wind_direction_10m": "°", "wind_gusts_10m": "km/h",
        },
        "current": {
            "time": "2026-10-10T14:15", "interval": 900, "temperature_2m": 31.2,
            "relative_humidity_2m": 68, "precipitation": 0.0, "rain": 0.0,
            "wind_speed_10m": 12.4, "wind_direction_10m": 220, "wind_gusts_10m": 25.6,
        },
        "hourly_units": {
            "time": "iso8601", "precipitation": "mm", "rain": "mm",
            "wind_gusts_10m": "km/h", "wind_speed_10m": "km/h",
            "precipitation_probability": "%", "temperature_2m": "°C",
            "relative_humidity_2m": "%",
        },
        "hourly": hourly,
    }


class FakeResponse:
    def __init__(self, status_code=200, json_data=None, text=None):
        self.status_code = status_code
        self._json = json_data
        self.text = text if text is not None else json.dumps(json_data)

    def json(self):
        if self._json is None:
            return json.loads(self.text)  # raises ValueError on bad JSON
        return self._json


class FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append((url, timeout))
        if self.exc:
            raise self.exc
        return self.response


def parse(payload):
    return WeatherAgent().parse_response(payload, RETRIEVED_AT)


def assert_no_invented_values(result):
    assert all(v is None for v in result["current_weather"]["values"].values())
    for window in result["rainfall_accumulation"]["windows"].values():
        assert window["precipitation"]["total"] is None
        assert window["precipitation"]["status"] == "UNAVAILABLE"


# ---- configuration ----------------------------------------------------------

def test_source_url_matches_configured_endpoint():
    assert WeatherAgent().source_url == CONFIGURED_URL


def test_fetch_uses_configured_url_and_timeout():
    session = FakeSession(FakeResponse(json_data=make_payload()))
    WeatherAgent(session=session).run()
    url, timeout = session.calls[0]
    assert url == CONFIGURED_URL
    assert timeout is not None


# ---- successful parsing -----------------------------------------------------

def test_successful_response_parsing():
    result = parse(make_payload())
    assert result["status"] == STATUS_OK
    assert result["agent_name"] == "WeatherAgent"
    assert result["source_type"] == SOURCE_TYPE == "MODEL_BASED_CURRENT_AND_FORECAST"
    assert result["source_url"] == CONFIGURED_URL
    assert result["requested_location"]["latitude"] == 10.888611
    assert result["requested_location"]["longitude"] == 78.58
    assert result["returned_grid_location"]["latitude"] == 10.875
    assert result["returned_grid_location"]["longitude"] == 78.625
    assert result["returned_grid_location"]["elevation_m"] == 81.0
    assert result["timezone"]["returned"] == "Asia/Kolkata"
    assert result["timezone"]["utc_offset_seconds"] == 19800
    assert result["retrieved_at"] == RETRIEVED_AT.isoformat()
    assert result["current_weather"]["time"] == "2026-10-10T14:15:00+05:30"
    assert result["current_weather"]["values"]["temperature_2m"] == 31.2
    assert result["current_weather"]["status"] == "AVAILABLE"
    assert result["hourly_weather"]["time"][0] == "2026-10-09T00:00:00+05:30"
    assert len(result["hourly_weather"]["variables"]["precipitation"]) == SERIES_HOURS
    assert result["units"]["current"]["temperature_2m"] == "°C"
    assert result["units"]["hourly"]["precipitation"] == "mm"
    assert result["errors"] == []
    for key in ("agent_name", "status", "source", "source_url", "source_type",
                "requested_location", "returned_grid_location", "retrieved_at",
                "timezone", "current_weather", "hourly_weather",
                "rainfall_accumulation", "units", "errors"):
        assert key in result


def test_run_with_mocked_api_is_timezone_aware():
    agent = WeatherAgent(session=FakeSession(FakeResponse(json_data=make_payload())))
    result = agent.run()
    assert result["status"] == STATUS_OK
    assert datetime.fromisoformat(result["retrieved_at"]).tzinfo is not None
    assert agent.to_dict() == result


# ---- missing fields / sections ----------------------------------------------

def test_missing_current_section_is_partial():
    payload = make_payload()
    del payload["current"]
    result = parse(payload)
    assert result["status"] == STATUS_PARTIAL
    assert result["current_weather"]["status"] == "UNAVAILABLE"
    assert all(v is None for v in result["current_weather"]["values"].values())
    assert any(e["field"] == "current" for e in result["errors"])


def test_missing_current_time_discards_current_values():
    payload = make_payload()
    del payload["current"]["time"]
    result = parse(payload)
    assert result["current_weather"]["status"] == "UNAVAILABLE"
    assert result["current_weather"]["values"]["temperature_2m"] is None


def test_missing_hourly_time_makes_hourly_unavailable():
    payload = make_payload()
    del payload["hourly"]["time"]
    result = parse(payload)
    assert result["status"] == STATUS_PARTIAL
    assert result["hourly_weather"]["status"] == "UNAVAILABLE"
    assert result["rainfall_accumulation"]["windows"]["3h"]["precipitation"]["total"] is None


def test_missing_both_sections_is_data_unavailable():
    payload = make_payload()
    del payload["current"]
    del payload["hourly"]
    result = parse(payload)
    assert result["status"] == STATUS_UNAVAILABLE
    assert_no_invented_values(result)


def test_missing_utc_offset_is_data_unavailable():
    payload = make_payload()
    del payload["utc_offset_seconds"]
    result = parse(payload)
    assert result["status"] == STATUS_UNAVAILABLE
    assert any(e["field"] == "utc_offset_seconds" for e in result["errors"])
    # Returned location is still preserved for traceability.
    assert result["returned_grid_location"]["latitude"] == 10.875
    assert_no_invented_values(result)


def test_missing_returned_coordinates_not_invented():
    payload = make_payload()
    del payload["latitude"]
    result = parse(payload)
    assert result["status"] == STATUS_PARTIAL
    assert result["returned_grid_location"]["latitude"] is None
    assert "latitude" in result["returned_grid_location"]["reason"]


def test_hourly_variable_length_mismatch_is_rejected():
    payload = make_payload()
    payload["hourly"]["precipitation"] = payload["hourly"]["precipitation"][:-1]
    result = parse(payload)
    assert result["hourly_weather"]["variables"]["precipitation"] is None
    assert "precipitation" in result["hourly_weather"]["missing"]
    acc = result["rainfall_accumulation"]["windows"]["3h"]
    assert acc["precipitation"]["status"] == "UNAVAILABLE"
    assert acc["rain"]["status"] == "AVAILABLE"


def test_api_error_payload_is_data_unavailable():
    result = parse({"error": True, "reason": "Latitude must be in range"})
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "API_ERROR"


def test_non_object_payload_is_data_unavailable():
    result = parse([1, 2, 3])
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "INVALID_PAYLOAD"


# ---- missing weather values -------------------------------------------------

def test_missing_and_null_current_values_are_not_replaced():
    payload = make_payload()
    del payload["current"]["rain"]
    payload["current"]["precipitation"] = None
    payload["current"]["wind_speed_10m"] = "n/a"
    result = parse(payload)
    current = result["current_weather"]
    assert result["status"] == STATUS_PARTIAL
    assert current["status"] == "PARTIAL"
    for var in ("rain", "precipitation", "wind_speed_10m"):
        assert current["values"][var] is None  # never 0.0 / "no rain"
        assert var in current["missing"]
    assert current["values"]["temperature_2m"] == 31.2


def test_null_hourly_values_are_kept_as_null():
    payload = make_payload()
    payload["hourly"]["temperature_2m"][50] = None
    result = parse(payload)
    assert result["hourly_weather"]["variables"]["temperature_2m"][50] is None
    assert result["hourly_weather"]["null_counts"]["temperature_2m"] == 1
    assert result["hourly_weather"]["status"] == "PARTIAL"


# ---- network / HTTP / JSON errors ------------------------------------------

@pytest.mark.parametrize("exc, code", [
    (requests.exceptions.Timeout("timed out"), "NETWORK_TIMEOUT"),
    (requests.exceptions.ConnectionError("no route"), "NETWORK_ERROR"),
    (requests.exceptions.TooManyRedirects("loop"), "REQUEST_FAILED"),
])
def test_network_errors_return_data_unavailable(exc, code):
    agent = WeatherAgent(session=FakeSession(exc=exc))
    result = agent.run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == code
    assert agent.raw_payload is None
    assert_no_invented_values(result)


def test_http_error_includes_api_reason():
    response = FakeResponse(400, {"error": True, "reason": "Cannot initialize WeatherVariable"})
    result = WeatherAgent(session=FakeSession(response)).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "HTTP_ERROR"
    assert "400" in result["errors"][0]["message"]
    assert "Cannot initialize" in result["errors"][0]["message"]


def test_http_error_with_non_json_body():
    response = FakeResponse(503, text="<html>Service Unavailable</html>")
    result = WeatherAgent(session=FakeSession(response)).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "HTTP_ERROR"
    assert_no_invented_values(result)


def test_invalid_json_returns_data_unavailable():
    response = FakeResponse(200, text="{not valid json")
    result = WeatherAgent(session=FakeSession(response)).run()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "INVALID_JSON"
    assert_no_invented_values(result)


# ---- rainfall accumulation --------------------------------------------------

def test_rainfall_accumulation_uses_completed_hours():
    acc = parse(make_payload())["rainfall_accumulation"]
    # Fixture: precipitation at index i = i/10, rain = i/20. Index 38 = 10 Oct 14:00.
    assert acc["unit"] == "mm"
    w3, w6, w24 = acc["windows"]["3h"], acc["windows"]["6h"], acc["windows"]["24h"]
    assert w3["period_start"] == "2026-10-10T11:00:00+05:30"
    assert w3["period_end"] == "2026-10-10T14:00:00+05:30"
    assert w3["precipitation"]["total"] == pytest.approx(3.6 + 3.7 + 3.8)
    assert w3["rain"]["total"] == pytest.approx((36 + 37 + 38) / 20)
    assert w6["precipitation"]["total"] == pytest.approx(sum(range(33, 39)) / 10)
    assert w24["precipitation"]["total"] == pytest.approx(sum(range(15, 39)) / 10)
    assert w24["period_start"] == "2026-10-09T14:00:00+05:30"


def test_future_forecast_hours_are_excluded():
    payload = make_payload()
    future_start = 39  # 10 Oct 15:00 onwards is after the reference time
    for i in range(future_start, SERIES_HOURS):
        payload["hourly"]["precipitation"][i] = 999.0
        payload["hourly"]["rain"][i] = 999.0
    acc = parse(payload)["rainfall_accumulation"]
    for window in acc["windows"].values():
        assert window["precipitation"]["total"] < 999
        assert window["rain"]["total"] < 999
    assert acc["windows"]["3h"]["precipitation"]["total"] == pytest.approx(11.1)


def test_incomplete_current_hour_is_excluded_until_it_ends():
    hourly = parse(make_payload())["hourly_weather"]
    just_before = calculate_rainfall_accumulation(
        hourly, datetime(2026, 10, 10, 14, 59, 59, tzinfo=IST), windows=(3,))
    on_the_hour = calculate_rainfall_accumulation(
        hourly, datetime(2026, 10, 10, 15, 0, tzinfo=IST), windows=(3,))
    assert just_before["windows"]["3h"]["period_end"] == "2026-10-10T14:00:00+05:30"
    assert on_the_hour["windows"]["3h"]["period_end"] == "2026-10-10T15:00:00+05:30"
    assert on_the_hour["windows"]["3h"]["precipitation"]["total"] == pytest.approx(3.7 + 3.8 + 3.9)


def test_utc_reference_time_aligns_to_local_hours():
    # 08:50 UTC == 14:20 IST; flooring must happen in IST (UTC+05:30).
    hourly = parse(make_payload())["hourly_weather"]
    acc = calculate_rainfall_accumulation(
        hourly, datetime(2026, 10, 10, 8, 50, tzinfo=timezone.utc), windows=(3,))
    assert acc["windows"]["3h"]["period_end"] == "2026-10-10T14:00:00+05:30"
    assert acc["windows"]["3h"]["precipitation"]["total"] == pytest.approx(11.1)


def test_window_not_covered_by_series_is_unavailable():
    # Default endpoint: hourly series starts at 00:00 today, so at 14:20 only 14
    # completed hours exist and the 24h total cannot be computed.
    payload = make_payload(start=datetime(2026, 10, 10, 0, 0, tzinfo=IST), hours=48)
    acc = parse(payload)["rainfall_accumulation"]
    assert acc["windows"]["6h"]["precipitation"]["status"] == "AVAILABLE"
    w24 = acc["windows"]["24h"]
    assert w24["precipitation"]["status"] == "UNAVAILABLE"
    assert w24["precipitation"]["total"] is None
    assert "does not cover" in w24["precipitation"]["reason"]


def test_null_inside_window_makes_total_unavailable():
    payload = make_payload()
    payload["hourly"]["precipitation"][37] = None  # 10 Oct 13:00
    acc = parse(payload)["rainfall_accumulation"]
    w3 = acc["windows"]["3h"]
    assert w3["precipitation"]["status"] == "UNAVAILABLE"
    assert w3["precipitation"]["total"] is None
    assert w3["rain"]["status"] == "AVAILABLE"  # other variable unaffected


def test_accumulation_requires_timezone_aware_reference():
    hourly = parse(make_payload())["hourly_weather"]
    acc = calculate_rainfall_accumulation(hourly, datetime(2026, 10, 10, 14, 20))
    assert all(w["precipitation"]["status"] == "UNAVAILABLE" for w in acc["windows"].values())


def test_agent_accumulation_method_uses_last_result():
    agent = WeatherAgent()
    agent._result = parse(make_payload())
    acc = agent.calculate_rainfall_accumulation()
    assert acc["windows"]["3h"]["precipitation"]["total"] == pytest.approx(11.1)
    later = agent.calculate_rainfall_accumulation(datetime(2026, 10, 10, 16, 5, tzinfo=IST))
    assert later["windows"]["3h"]["period_end"] == "2026-10-10T16:00:00+05:30"


# ---- unavailable data handling ---------------------------------------------

def test_to_dict_before_run_is_data_unavailable():
    result = WeatherAgent().to_dict()
    assert result["status"] == STATUS_UNAVAILABLE
    assert result["errors"][0]["code"] == "NOT_RUN"
    assert_no_invented_values(result)


def test_to_dict_returns_independent_copy():
    agent = WeatherAgent(session=FakeSession(FakeResponse(json_data=make_payload())))
    agent.run()
    first = agent.to_dict()
    first["current_weather"]["values"]["temperature_2m"] = -100
    assert agent.to_dict()["current_weather"]["values"]["temperature_2m"] == 31.2


def test_unavailable_result_has_all_fields_null():
    result = WeatherAgent(session=FakeSession(exc=requests.exceptions.Timeout())).run()
    assert result["returned_grid_location"]["latitude"] is None
    assert result["timezone"]["returned"] is None
    assert result["hourly_weather"]["time"] == []
    assert set(result["current_weather"]["missing"]) == set(CURRENT_VARIABLES)
    assert all(u is None for u in result["units"]["hourly"].values())


def test_save_refuses_unavailable_or_unbacked_results(tmp_path):
    unavailable = WeatherAgent().to_dict()
    with pytest.raises(ValueError):
        save_live_sample(unavailable, {"some": "payload"}, tmp_path)
    ok = parse(make_payload())
    with pytest.raises(ValueError):
        save_live_sample(ok, None, tmp_path)
    assert list(tmp_path.iterdir()) == []


def test_save_labels_file_with_retrieval_time(tmp_path):
    payload = make_payload()
    path = save_live_sample(parse(payload), copy.deepcopy(payload), tmp_path)
    assert path.name == "weather_live_20261010T085000Z.json"
    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved["retrieved_at"] == RETRIEVED_AT.isoformat()
    assert saved["raw_api_response"] == payload
