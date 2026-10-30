"""Historical series endpoint for the Forecast & Analytics page (/dashboard/analysis/series).

Display only: QC-accepted measured readings of one station, window statistics and data
quality. Thinned charts keep actual readings (bucket min/max), never averages.
"""

import json
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from src.dashboard_service import SERIES_MAX_POINTS, DashboardService, thin_points
from src.river_api import create_server
from tests.test_river_service import make_service

END = datetime(2025, 12, 30, 5, 0, tzinfo=timezone.utc)


def readings(station, hours, base):
    return [{"observed_at": END - timedelta(hours=h), "river_level_m": round(base + 0.01 * (h % 7), 3),
             "station_id": station} for h in range(hours)]


def fake_inputs():
    a = readings("UPPER_ANICUT", 2000, 74.0)
    rejected = [{"observed_at": END - timedelta(hours=2, minutes=30), "river_level_m": 72.0,
                 "qc_code": "PLACEHOLDER_VALUE"}]
    b = readings("MUSIRI", 50, 81.0)
    return {"primary": "UPPER_ANICUT", "river_ids": ["UPPER_ANICUT", "MUSIRI"],
            "registry": {"stations": {"UPPER_ANICUT": {"source_name": "UPPER ANICUT"},
                                      "MUSIRI": {"source_name": "MUSIRI"}}},
            "qc": {"UPPER_ANICUT": (a, rejected, {"accepted": 2000, "checked": 2001}),
                   "MUSIRI": (b, [], {"accepted": 50, "checked": 50})}}


@pytest.fixture
def dash(tmp_path):
    return DashboardService(make_service(tmp_path), state_file=None,
                            audit_path=tmp_path / "a.jsonl", analysis_loader=fake_inputs)


def test_window_statistics_and_quality(dash):
    doc = dash.analysis_series("UPPER_ANICUT", "24h")
    assert doc["data_label"] == "HISTORICAL CWC DATA" and doc["is_simulated"] is False
    assert doc["latest"] == {"level_m": 74.0, "observed_at": END.isoformat()}
    assert doc["window_end"] == END.isoformat()
    window = [r for r in fake_inputs()["qc"]["UPPER_ANICUT"][0]
              if r["observed_at"] >= END - timedelta(hours=24)]
    levels = [r["river_level_m"] for r in window]
    assert doc["stats"] == {"min_m": min(levels), "max_m": max(levels),
                            "mean_m": round(sum(levels) / len(levels), 3), "readings": len(levels)}
    assert doc["quality"]["rejected"] == 1 and doc["quality"]["accepted"] == len(window)
    assert doc["quality"]["rejected_by_reason"] == {"PLACEHOLDER_VALUE": 1}
    assert len(doc["points"]) == len(window) and doc["thinned"] is False


def test_long_ranges_are_thinned_to_actual_readings(dash):
    doc = dash.analysis_series("UPPER_ANICUT", "all")
    actual = {(r["observed_at"].isoformat(), r["river_level_m"])
              for r in fake_inputs()["qc"]["UPPER_ANICUT"][0]}
    assert doc["thinned"] is True and len(doc["points"]) <= SERIES_MAX_POINTS
    assert all((p["time"], p["level_m"]) in actual for p in doc["points"])
    assert doc["stats"]["readings"] == 2000  # statistics use every reading, not the thinned set
    levels = [p["level_m"] for p in doc["points"]]
    assert max(levels) == doc["stats"]["max_m"] and min(levels) == doc["stats"]["min_m"]


def test_thinning_keeps_time_order():
    rows = readings("X", 5000, 70.0)[::-1]
    pts = thin_points(sorted(rows, key=lambda r: r["observed_at"]), 100)
    times = [p["time"] for p in pts]
    assert times == sorted(times) and len(pts) <= 100


def test_station_selector_and_validation(dash):
    assert dash.analysis_series("MUSIRI", "7d")["stats"]["readings"] == 50
    assert dash.analysis_series(None, "7d")["station_id"] == "UPPER_ANICUT"
    with pytest.raises(ValueError):
        dash.analysis_series("METTUR", "7d")
    with pytest.raises(ValueError):
        dash.analysis_series("UPPER_ANICUT", "1y")


def test_series_route(dash):
    server = create_server(dash.service, "127.0.0.1", 0, dashboard=dash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        with urllib.request.urlopen(base + "/dashboard/analysis/series?station=MUSIRI&range=24h",
                                    timeout=30) as r:
            assert json.loads(r.read())["station_id"] == "MUSIRI"
        for bad in ("?station=../x&range=24h", "?range=forever", "?station=MUSIRI&path=/etc"):
            with pytest.raises(urllib.error.HTTPError) as info:
                urllib.request.urlopen(base + "/dashboard/analysis/series" + bad, timeout=30)
            assert info.value.code == 400
    finally:
        server.shutdown()
        server.server_close()
