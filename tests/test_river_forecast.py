"""Forecasting (src.river_forecast): leakage, chronological validation, honest
publishing, stale-input refusal, dashboard route, and separation from risk,
state and capabilities. Offline; the real export is used only where noted."""

import json
import math
import random
import threading
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

import pytest

from src.dashboard_service import DashboardService
from src.hydro_analysis import load_analysis_inputs
from src.river_api import create_server
from src.river_forecast import (
    BASELINE,
    MODEL_LABEL,
    WINDOW_HOURS,
    build_samples,
    chronological_split,
    evaluate,
    fit_ridge,
    forecast_now,
    input_window,
    metrics,
    predict_linear_trend,
    predict_moving_average,
    predict_persistence,
    predict_ridge,
    public,
)
from src.river_monitoring_agent import PROJECT_DIR
from tests.test_cwc_ingest import row, write_raw
from tests.test_dashboard_api import dashboard as real_dashboard
from tests.test_river_service import clean_live_session, make_service

RAW = PROJECT_DIR / "data" / "cwc" / "rwl_tel_hr_cwc_009_2021_2025.csv"
T0 = datetime(2024, 1, 1, tzinfo=timezone.utc)


def series(kind="tide", n=3000, seed=7, start=T0):
    """Hourly synthetic readings: 'tide' is predictable, 'walk' is a random walk."""
    rng = random.Random(seed)
    level, out = 74.0, []
    for h in range(n):
        if kind == "walk":
            level += rng.gauss(0, 0.01)
            value = level
        else:
            value = 74 + 0.3 * math.sin(2 * math.pi * h / 48) + rng.gauss(0, 0.003)
        out.append({"observed_at": start + timedelta(hours=h), "river_level_m": round(value, 3)})
    return out


def as_map(readings):
    return {o["observed_at"]: o["river_level_m"] for o in readings}


# ---- samples: no interpolation, no look-ahead ----------------------------------------

def test_a_sample_needs_a_complete_measured_window_and_target():
    readings = as_map(series(n=30))
    del readings[T0 + timedelta(hours=10)]                    # one missing hour
    samples = build_samples(readings, horizon=3)
    origins = {s["origin"] for s in samples}
    for affected in range(10, 16):                            # window contains hour 10
        assert T0 + timedelta(hours=affected) not in origins
    assert T0 + timedelta(hours=7) not in origins             # target would be hour 10
    assert T0 + timedelta(hours=9) in origins


def test_features_use_only_readings_at_or_before_the_origin():
    readings = as_map(series(n=40))
    origin = T0 + timedelta(hours=20)
    window = input_window(readings, origin)
    future_changed = {t: (v + 5 if t > origin else v) for t, v in readings.items()}
    assert input_window(future_changed, origin) == window
    model = fit_ridge(build_samples(readings, 1), 1.0)
    for predict in (lambda w: predict_persistence(w, 3), lambda w: predict_moving_average(w, 3),
                    lambda w: predict_linear_trend(w, 3), lambda w: predict_ridge(model, w)):
        assert predict(window) == predict(input_window(future_changed, origin))


def test_split_is_chronological_with_an_embargo():
    samples = build_samples(as_map(series(n=2000)), horizon=6)
    train, validation, test, info = chronological_split(list(reversed(samples)), horizon=6)
    embargo = timedelta(hours=6 + WINDOW_HOURS + 24)
    assert max(s["origin"] for s in train) + embargo < min(s["origin"] for s in validation)
    assert max(s["origin"] for s in validation) + embargo < min(s["origin"] for s in test)
    # No target of an earlier part reaches the input window of a later one.
    assert max(s["origin"] for s in train) + timedelta(hours=6) < \
        min(s["origin"] for s in validation) - timedelta(hours=WINDOW_HOURS)
    assert info["method"].startswith("chronological")


def test_metrics_are_computed_as_defined():
    m = metrics([1.0, 2.0, 4.0], [1.0, 3.0, 2.0], persistence_mae=2.0)  # errors 0, -1, +2
    assert m["mae_m"] == pytest.approx(1.0, abs=1e-4)
    assert m["rmse_m"] == pytest.approx(math.sqrt(5 / 3), abs=1e-4)
    assert m["bias_m"] == pytest.approx(1 / 3, abs=1e-4)
    assert m["skill_vs_persistence"] == pytest.approx(0.5, abs=1e-4)
    assert m["r2"] == pytest.approx(1 - 5 / 2, abs=1e-4)                # SST = 2
    assert metrics([], [])["samples"] == 0


def test_linear_trend_and_moving_average_baselines():
    window = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0]
    assert predict_persistence(window, 3) == 6.0
    assert predict_moving_average(window, 3) == 3.5
    assert predict_linear_trend(window, 3) == pytest.approx(9.0)


# ---- publishing ----------------------------------------------------------------------

def test_a_predictable_series_is_published_and_beats_persistence():
    result = evaluate(series("tide"), "S", horizons=(1, 6))
    for h in result["horizons"]:
        assert h["published"], h["reason"]
        chosen = h["models"][h["selected_model"]]
        assert chosen["test"]["skill_vs_persistence"] > 0.5
        assert chosen["interval"]["test_coverage"] >= 0.8
        assert set(h["models"]) == {BASELINE, "moving_average_6h", "linear_trend_6h",
                                    "ridge_ar_6h"}


@pytest.mark.parametrize("seed", [7, 8, 9])
def test_persistence_wins_on_a_random_walk_and_nothing_is_published(seed):
    result = evaluate(series("walk", seed=seed), "S", horizons=(1, 6))
    for h in result["horizons"]:
        assert not h["published"] and "persistence" in h["reason"]


def test_insufficient_data_publishes_nothing():
    h = evaluate(series(n=200), "S", horizons=(1,))["horizons"][0]
    assert not h["published"] and h["reason"].startswith("Insufficient data")


def test_outputs_never_claim_authority():
    result = evaluate(series(n=200), "S", horizons=(1,))
    assert result["label"] == MODEL_LABEL
    assert result["affects_risk_assessment"] is False
    assert result["affects_disaster_state"] is False and result["authorizes_actions"] is False
    json.dumps(public(result))                               # JSON-safe for the API


# ---- live forecasts: never from stale or incomplete inputs ---------------------------

def test_fresh_complete_inputs_give_a_labelled_prediction_with_uncertainty():
    readings = series("tide")
    evaluation = evaluate(readings, "S", horizons=(1, 6))
    latest = readings[-1]["observed_at"]
    for f in forecast_now(evaluation, readings, latest + timedelta(minutes=30)):
        assert f["status"] == "FORECAST" and f["label"] == MODEL_LABEL
        low, high = f["interval_m"]
        assert low <= f["predicted_level_m"] <= high
        assert f["valid_for"] == (latest + timedelta(hours=f["horizon_hours"])).isoformat()
        assert f["test_metrics"]["mae_m"] > 0


def test_stale_inputs_give_no_number():
    readings = series("tide")
    evaluation = evaluate(readings, "S", horizons=(1,))
    f = forecast_now(evaluation, readings, readings[-1]["observed_at"] + timedelta(days=300))[0]
    assert f["status"] == "NO_FORECAST_INPUT_STALE" and f["predicted_level_m"] is None
    assert "days old" in f["reason"]


def test_incomplete_inputs_give_no_number():
    readings = series("tide")
    evaluation = evaluate(readings, "S", horizons=(1,))
    gappy = readings[:-3] + readings[-1:]                     # hours -3, -2 missing
    f = forecast_now(evaluation, gappy, readings[-1]["observed_at"])[0]
    assert f["status"] == "NO_FORECAST_INPUT_INCOMPLETE" and f["predicted_level_m"] is None


def test_unpublished_horizons_give_no_number_even_with_fresh_inputs():
    readings = series("walk")
    evaluation = evaluate(readings, "S", horizons=(1,))
    f = forecast_now(evaluation, readings, readings[-1]["observed_at"])[0]
    assert f["status"] == "NOT_PUBLISHED" and f["predicted_level_m"] is None


# ---- separation from risk, state and capabilities ------------------------------------

@pytest.mark.parametrize("module", ["risk_assessment", "disaster_state_manager",
                                    "capability_control", "river_agent_workflow",
                                    "evidence_layer", "river_service"])
def test_decision_modules_never_use_forecasts_or_analysis(module):
    source = (PROJECT_DIR / "src" / f"{module}.py").read_text(encoding="utf-8")
    assert "river_forecast" not in source and "hydro_analysis" not in source


def test_real_mode_decisions_are_identical_with_or_without_the_analysis(tmp_path):
    def real_doc(path, with_analysis):
        dash = real_dashboard(path, clean_live_session(),
                              analysis_loader=lambda: small_inputs(path))
        if with_analysis:
            dash.analysis()
        doc, _ = dash.get("real")
        return doc
    a, b = real_doc(tmp_path / "a", False), real_doc(tmp_path / "b", True)
    assert "forecast" not in b and "analysis" not in b
    assert a["risk"]["risk_level"] == b["risk"]["risk_level"]
    assert a["safety"]["decisions"] == b["safety"]["decisions"]
    assert a["state"]["evaluation"]["outcome"] == b["state"]["evaluation"]["outcome"]


# ---- dashboard route -----------------------------------------------------------------

IST = timezone(timedelta(hours=5, minutes=30))


def small_inputs(tmp_path):
    """Real loader and QC on a small raw file in the export's layout."""
    tmp_path.mkdir(parents=True, exist_ok=True)
    start = datetime(2025, 1, 1, tzinfo=IST)
    rows = []
    for o in series("tide", n=2500, start=start):
        t = o["observed_at"].astimezone(IST).strftime("%d-%m-%Y %H:%M")
        rows.append(row("UPPER ANICUT", t, f"{o['river_level_m']:.3f}"))
        rows.append(row("MUSIRI", t, f"{84 + (o['river_level_m'] - 74) * 0.1:.3f}"))
    rows.append(row("Mettur Reservoir", "01-01-2025 00:00", "230.0"))
    return load_analysis_inputs(raw_path=write_raw(tmp_path / "raw.csv", rows))


def analysis_dashboard(tmp_path, clock):
    service = make_service(tmp_path, clock=clock)
    return DashboardService(service, state_file=tmp_path / "none.json",
                            audit_path=tmp_path / "dash.jsonl",
                            weather_fetcher=lambda: pytest.fail("no weather"),
                            analysis_loader=lambda: small_inputs(tmp_path))


def test_analysis_document_is_labelled_and_separate(tmp_path):
    clock = lambda: datetime(2026, 10, 30, tzinfo=timezone.utc)
    doc = analysis_dashboard(tmp_path, clock).analysis()
    assert "error" not in doc, doc.get("error")
    assert doc["mode"] == "analysis" and doc["data_label"] == "HISTORICAL CWC DATA"
    assert doc["is_simulated"] is False
    ua = doc["analysis"]["stations"]["UPPER_ANICUT"]["analysis"]
    assert ua["data_class"] == "HISTORICAL_CWC_DATA" and ua["freshness"]["status"] == "STALE"
    forecast = doc["forecast"]
    assert forecast["label"] == "MODEL PREDICTION"
    assert forecast["affects_risk_assessment"] is False and forecast["authorizes_actions"] is False
    published = [h for h in forecast["evaluation"]["horizons"] if h["published"]]
    assert published                                          # the tide is predictable ...
    assert all(f["predicted_level_m"] is None for f in forecast["live"])  # ... but stale
    assert forecast["input"]["status"] == "STALE" and forecast["input"]["max_age_hours"] == 3.0
    assert {f["status"] for f in forecast["live"]} <= {"NO_FORECAST_INPUT_STALE",
                                                       "NOT_PUBLISHED"}
    assert str(tmp_path) not in json.dumps(doc)


def get(url):
    try:
        with urllib.request.urlopen(url, timeout=60) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as err:
        return err.code, json.loads(err.read())


def test_http_analysis_route(tmp_path):
    dash = analysis_dashboard(tmp_path, lambda: datetime(2026, 10, 30, tzinfo=timezone.utc))
    server = create_server(dash.service, "127.0.0.1", 0, dashboard=dash)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    try:
        status, body = get(base + "/dashboard/analysis")
        assert status == 200 and body["data_label"] == "HISTORICAL CWC DATA"
        status, body = get(base + "/dashboard/analysis?station=../x")
        assert status == 400
    finally:
        server.shutdown()
        server.server_close()


# ---- the real export -----------------------------------------------------------------

@pytest.mark.skipif(not RAW.is_file(), reason="raw CWC export not present")
def test_real_export_publishes_no_forecast_that_does_not_beat_persistence():
    inputs = load_analysis_inputs()
    accepted = inputs["qc"]["UPPER_ANICUT"][0]
    evaluation = evaluate(accepted, "UPPER_ANICUT")
    for h in evaluation["horizons"]:
        chosen = h["models"][h["selected_model"]]
        if h["published"]:
            assert chosen["test"]["skill_vs_persistence"] >= 0.02
            assert chosen["validation"]["skill_vs_persistence"] >= 0.02
    now = datetime(2026, 10, 30, tzinfo=timezone.utc)
    assert all(f["predicted_level_m"] is None for f in forecast_now(evaluation, accepted, now))
