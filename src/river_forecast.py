"""River-level forecasting for Upper Anicut: chronological validation, honest publishing.

    python -m src.river_forecast            # validation table per horizon
    python -m src.river_forecast --json

Every predicted value is a MODEL PREDICTION, never a CWC measurement. Forecasts
are display-only: they never feed the risk engine, the Disaster State Manager or
capability control, and they authorize nothing.

Models (plain Python, no new dependencies):
- persistence:          level(t+H) = level(t). The reference every model must beat.
- moving_average_6h:    level(t+H) = mean of the last 6 hourly readings.
- linear_trend_6h:      least-squares line through the last 6 hourly readings,
                        extended H hours.
- ridge_ar_6h:          ridge regression of the change level(t+H) - level(t) on the
                        recent changes level(t) - level(t-k), k = 1..5, and the
                        offset of the 6-h mean. Inputs standardised with training
                        statistics only; the penalty is chosen on validation data.

Leakage prevention:
- A sample exists only if every input reading (t-5h .. t) AND the target (t+H) is a
  QC-accepted MEASURED reading at exactly that hour. Nothing is interpolated.
- Features use readings at or before the forecast origin t only.
- Chronological split of forecast origins: train 70 % | validation 15 % | test 15 %,
  never shuffled, with an embargo of H + 6 + 24 h between parts so no target of one
  part overlaps the inputs of the next.
- Model choice and the penalty use validation data; the test part is used once, to
  confirm. Prediction intervals come from validation residuals (5th-95th
  percentile); their coverage is measured on the test part.

Publishing rule per horizon: a forecast is shown only if the model chosen on
validation beats persistence by at least 2 % MAE on validation AND on the untouched
test part, its interval covers at least 80 % of test values, and the test part has enough
samples. Otherwise the horizon is NOT_PUBLISHED, with the reason. A live number is
produced only if the latest input readings are fresh and complete.
"""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

HORIZONS_HOURS = (1, 3, 6, 12, 24)
WINDOW_HOURS = 6
EMBARGO_EXTRA_HOURS = 24
TRAIN_FRACTION, VALIDATION_FRACTION = 0.70, 0.15
RIDGE_PENALTIES = (0.01, 0.1, 1.0, 10.0, 100.0)
MIN_TEST_SAMPLES = 100
MIN_TRAIN_SAMPLES = 300
MIN_INTERVAL_COVERAGE = 0.80
# A model must beat persistence by a margin, not by chance: on a pure random walk
# (where persistence is optimal) tiny "wins" of ~0.5 % occur by luck.
MIN_SKILL_VS_PERSISTENCE = 0.02
INTERVAL_QUANTILES = (0.05, 0.95)
MAX_INPUT_AGE_HOURS = 3.0  # the River Agent's freshness limit
MODEL_LABEL = "MODEL PREDICTION"
BASELINE = "persistence"
CANDIDATES = ("moving_average_6h", "linear_trend_6h", "ridge_ar_6h")


# --------------------------------------------------------------------------
# Samples (no interpolation, inputs only from the past)
# --------------------------------------------------------------------------

def input_window(readings: dict[datetime, float], origin: datetime) -> list[float] | None:
    """Levels at origin-5h .. origin (oldest first), or None if any hour is missing."""
    values = []
    for k in range(WINDOW_HOURS - 1, -1, -1):
        value = readings.get(origin - timedelta(hours=k))
        if value is None:
            return None
        values.append(value)
    return values


def build_samples(readings: dict[datetime, float], horizon: int) -> list[dict[str, Any]]:
    """One sample per origin with a complete input window and a measured target."""
    samples = []
    step = timedelta(hours=horizon)
    for origin in sorted(readings):
        window = input_window(readings, origin)
        target = readings.get(origin + step)
        if window is None or target is None:
            continue
        samples.append({"origin": origin, "window": window, "target": target})
    return samples


def chronological_split(samples: list[dict[str, Any]], horizon: int
                        ) -> tuple[list[dict[str, Any]], list[dict[str, Any]],
                                   list[dict[str, Any]], dict[str, Any]]:
    """Train | validation | test by origin time, with an embargo between parts."""
    ordered = sorted(samples, key=lambda s: s["origin"])
    n = len(ordered)
    cut1, cut2 = int(n * TRAIN_FRACTION), int(n * (TRAIN_FRACTION + VALIDATION_FRACTION))
    embargo = timedelta(hours=horizon + WINDOW_HOURS + EMBARGO_EXTRA_HOURS)
    train = ordered[:cut1]
    validation = [s for s in ordered[cut1:cut2]
                  if not train or s["origin"] > train[-1]["origin"] + embargo]
    test = [s for s in ordered[cut2:]
            if not validation or s["origin"] > validation[-1]["origin"] + embargo]

    def period(part):
        return [part[0]["origin"].isoformat(), part[-1]["origin"].isoformat()] if part else None
    return train, validation, test, {
        "method": "chronological by forecast origin, never shuffled",
        "fractions": [TRAIN_FRACTION, VALIDATION_FRACTION,
                      round(1 - TRAIN_FRACTION - VALIDATION_FRACTION, 2)],
        "embargo_hours": horizon + WINDOW_HOURS + EMBARGO_EXTRA_HOURS,
        "samples": {"train": len(train), "validation": len(validation), "test": len(test)},
        "periods": {"train": period(train), "validation": period(validation),
                    "test": period(test)},
    }


# --------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------

def predict_persistence(window: list[float], horizon: int) -> float:
    return window[-1]


def predict_moving_average(window: list[float], horizon: int) -> float:
    return statistics.fmean(window)


def predict_linear_trend(window: list[float], horizon: int) -> float:
    xs = list(range(-(len(window) - 1), 1))  # hours relative to the origin
    mx, my = statistics.fmean(xs), statistics.fmean(window)
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, window)) / sum((x - mx) ** 2 for x in xs)
    return my + slope * (horizon - mx)


def ridge_features(window: list[float]) -> list[float]:
    latest = window[-1]
    return [latest - window[-1 - k] for k in range(1, WINDOW_HOURS)] + \
        [statistics.fmean(window) - latest]


def _solve(matrix: list[list[float]], vector: list[float]) -> list[float]:
    """Gaussian elimination with partial pivoting (small, well-conditioned systems)."""
    n = len(vector)
    a = [row[:] + [vector[i]] for i, row in enumerate(matrix)]
    for col in range(n):
        pivot = max(range(col, n), key=lambda r: abs(a[r][col]))
        a[col], a[pivot] = a[pivot], a[col]
        if abs(a[col][col]) < 1e-12:
            raise ValueError("singular system")
        for r in range(n):
            if r != col:
                factor = a[r][col] / a[col][col]
                for c in range(col, n + 1):
                    a[r][c] -= factor * a[col][c]
    return [a[i][n] / a[i][i] for i in range(n)]


def fit_ridge(samples: list[dict[str, Any]], penalty: float) -> dict[str, Any]:
    """Ridge on standardised features; the intercept is not penalised."""
    rows = [ridge_features(s["window"]) for s in samples]
    targets = [s["target"] - s["window"][-1] for s in samples]
    k = len(rows[0])
    means = [statistics.fmean(r[j] for r in rows) for j in range(k)]
    stds = [statistics.pstdev(r[j] for r in rows) or 1.0 for j in range(k)]
    z = [[(r[j] - means[j]) / stds[j] for j in range(k)] for r in rows]
    y_mean = statistics.fmean(targets)
    yc = [t - y_mean for t in targets]
    n = len(z)
    xtx = [[sum(row[i] * row[j] for row in z) + (penalty * n if i == j else 0.0)
            for j in range(k)] for i in range(k)]
    xty = [sum(row[i] * y for row, y in zip(z, yc)) for i in range(k)]
    return {"weights": _solve(xtx, xty), "means": means, "stds": stds, "intercept": y_mean,
            "penalty": penalty, "trained_on": n}


def predict_ridge(model: dict[str, Any], window: list[float]) -> float:
    features = ridge_features(window)
    change = model["intercept"] + sum(
        w * (f - m) / s for w, f, m, s in zip(model["weights"], features, model["means"],
                                              model["stds"]))
    return window[-1] + change


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------

def _quantile(values: list[float], q: float) -> float:
    ordered = sorted(values)
    pos = q * (len(ordered) - 1)
    lo, hi = math.floor(pos), math.ceil(pos)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def metrics(predictions: list[float], actual: list[float],
            persistence_mae: float | None = None) -> dict[str, Any]:
    n = len(actual)
    if n == 0:
        return {"samples": 0}
    errors = [p - a for p, a in zip(predictions, actual)]
    mae = statistics.fmean(abs(e) for e in errors)
    mean_actual = statistics.fmean(actual)
    sst = sum((a - mean_actual) ** 2 for a in actual)
    out = {"samples": n, "mae_m": round(mae, 4),
           "rmse_m": round(math.sqrt(statistics.fmean(e * e for e in errors)), 4),
           "bias_m": round(statistics.fmean(errors), 4),
           "r2": round(1 - sum(e * e for e in errors) / sst, 4) if sst > 0 else None}
    if persistence_mae:
        out["skill_vs_persistence"] = round(1 - mae / persistence_mae, 4)
    return out


def _predictor(name: str, train: list[dict[str, Any]], validation: list[dict[str, Any]],
               horizon: int) -> tuple[Callable[[list[float]], float], dict[str, Any]]:
    if name == BASELINE:
        return (lambda w: predict_persistence(w, horizon)), {}
    if name == "moving_average_6h":
        return (lambda w: predict_moving_average(w, horizon)), {}
    if name == "linear_trend_6h":
        return (lambda w: predict_linear_trend(w, horizon)), {}
    best = None
    for penalty in RIDGE_PENALTIES:  # chosen on validation, never on test
        model = fit_ridge(train, penalty)
        mae = statistics.fmean(abs(predict_ridge(model, s["window"]) - s["target"])
                               for s in validation)
        if best is None or mae < best[0]:
            best = (mae, model)
    model = best[1]
    return (lambda w: predict_ridge(model, w)), {
        "penalty": model["penalty"], "trained_on": model["trained_on"],
        "features": ["level(t) - level(t-%dh)" % k for k in range(1, WINDOW_HOURS)]
        + ["mean(6h) - level(t)"],
        "weights_standardised": [round(w, 5) for w in model["weights"]],
        "intercept_m": round(model["intercept"], 5)}


def evaluate_horizon(readings: dict[datetime, float], horizon: int) -> dict[str, Any]:
    samples = build_samples(readings, horizon)
    train, validation, test, split = chronological_split(samples, horizon)
    result: dict[str, Any] = {"horizon_hours": horizon, "split": split, "models": {},
                              "selected_model": None, "published": False,
                              "validation_status": "INSUFFICIENT_DATA"}
    if len(train) < MIN_TRAIN_SAMPLES or len(validation) < MIN_TEST_SAMPLES \
            or len(test) < MIN_TEST_SAMPLES:
        result["reason"] = (f"Insufficient data: {len(train)} train / {len(validation)} "
                            f"validation / {len(test)} test samples with complete measured "
                            f"inputs and targets (need {MIN_TRAIN_SAMPLES}/{MIN_TEST_SAMPLES}/"
                            f"{MIN_TEST_SAMPLES}).")
        return result
    fitted = {}
    for name in (BASELINE,) + CANDIDATES:
        predict, details = _predictor(name, train, validation, horizon)
        val_pred = [predict(s["window"]) for s in validation]
        residuals = [s["target"] - p for s, p in zip(validation, val_pred)]
        low, high = (_quantile(residuals, q) for q in INTERVAL_QUANTILES)
        test_pred = [predict(s["window"]) for s in test]
        covered = sum(p + low <= s["target"] <= p + high for s, p in zip(test, test_pred))
        fitted[name] = (predict, low, high)
        result["models"][name] = {
            "details": details,
            "validation": metrics(val_pred, [s["target"] for s in validation]),
            "test": metrics(test_pred, [s["target"] for s in test]),
            "interval": {"quantiles": list(INTERVAL_QUANTILES), "low_offset_m": round(low, 4),
                         "high_offset_m": round(high, 4),
                         "test_coverage": round(covered / len(test), 3)},
        }
    base_val = result["models"][BASELINE]["validation"]["mae_m"]
    base_test = result["models"][BASELINE]["test"]["mae_m"]
    for name, entry in result["models"].items():
        if name != BASELINE:
            entry["validation"]["skill_vs_persistence"] = round(
                1 - entry["validation"]["mae_m"] / base_val, 4) if base_val else None
            entry["test"]["skill_vs_persistence"] = round(
                1 - entry["test"]["mae_m"] / base_test, 4) if base_test else None
    chosen = min(CANDIDATES, key=lambda m: result["models"][m]["validation"]["mae_m"])
    result["selected_model"] = chosen
    entry = result["models"][chosen]
    problems = []
    margin = f"by at least {MIN_SKILL_VS_PERSISTENCE:.0%}"
    if (entry["validation"]["skill_vs_persistence"] or 0) < MIN_SKILL_VS_PERSISTENCE:
        problems.append(f"on validation, {chosen} (MAE {entry['validation']['mae_m']} m) does "
                        f"not beat persistence (MAE {base_val} m) {margin}")
    if (entry["test"]["skill_vs_persistence"] or 0) < MIN_SKILL_VS_PERSISTENCE:
        problems.append(f"on the test period, {chosen} (MAE {entry['test']['mae_m']} m) does not "
                        f"beat persistence (MAE {base_test} m) {margin}")
    if entry["interval"]["test_coverage"] < MIN_INTERVAL_COVERAGE:
        problems.append(f"its 90 % interval covers only {entry['interval']['test_coverage']:.0%} "
                        f"of test values (need {MIN_INTERVAL_COVERAGE:.0%})")
    result["published"] = not problems
    result["validation_status"] = "PUBLISHED" if not problems else "FAILED_VALIDATION"
    result["reason"] = ("Published: beats persistence on validation and test, interval "
                        "coverage acceptable." if not problems else
                        "Not published: " + "; ".join(problems) + ".")
    result["_fitted"] = fitted[chosen]
    return result


def evaluate(accepted: list[dict[str, Any]], station_id: str,
             horizons: tuple[int, ...] = HORIZONS_HOURS) -> dict[str, Any]:
    """Validation of every model for every horizon on QC-accepted readings."""
    readings = {o["observed_at"]: o["river_level_m"] for o in accepted}
    times = sorted(readings)
    return {
        "station_id": station_id, "label": MODEL_LABEL,
        "data": {"accepted_readings": len(readings),
                 "first": times[0].isoformat() if times else None,
                 "last": times[-1].isoformat() if times else None,
                 "basis": "QC-accepted MEASURED CWC readings (historical export); nothing "
                          "interpolated"},
        "models_compared": [BASELINE, *CANDIDATES],
        "publishing_rule": (f"Chosen on validation; published only if it beats persistence by "
                            f">= {MIN_SKILL_VS_PERSISTENCE:.0%} MAE on validation and test, "
                            f"interval coverage >= "
                            f"{MIN_INTERVAL_COVERAGE:.0%}, >= {MIN_TEST_SAMPLES} test samples."),
        "horizons": [evaluate_horizon(readings, h) for h in horizons],
        "affects_risk_assessment": False, "affects_disaster_state": False,
        "authorizes_actions": False,
    }


def forecast_now(evaluation: dict[str, Any], accepted: list[dict[str, Any]],
                 now: datetime, max_input_age_hours: float = MAX_INPUT_AGE_HOURS
                 ) -> list[dict[str, Any]]:
    """Live predictions per horizon, or the reason there is none. Never forecasts
    from stale or incomplete inputs."""
    readings = {o["observed_at"]: o["river_level_m"] for o in accepted}
    latest = max(readings) if readings else None
    age = (now - latest).total_seconds() / 3600 if latest else None
    window = input_window(readings, latest) if latest else None
    out = []
    for h in evaluation["horizons"]:
        entry = {"horizon_hours": h["horizon_hours"], "label": MODEL_LABEL,
                 "model": h.get("selected_model"), "published": h["published"],
                 "predicted_level_m": None, "interval_m": None, "valid_for": None,
                 "input_latest_observed_at": latest.isoformat() if latest else None,
                 "input_age_hours": None if age is None else round(age, 2)}
        if not h["published"]:
            entry.update(status="NOT_PUBLISHED", reason=h.get("reason"))
        elif latest is None or age > max_input_age_hours:
            entry.update(status="NO_FORECAST_INPUT_STALE",
                         reason=(f"The latest measured reading is {round(age / 24, 1)} days old "
                                 f"(limit {max_input_age_hours} h); no prediction is made from "
                                 "stale data." if latest else "No measured readings."))
        elif window is None:
            entry.update(status="NO_FORECAST_INPUT_INCOMPLETE",
                         reason=f"The last {WINDOW_HOURS} hourly readings are not all present.")
        else:
            predict, low, high = h["_fitted"]
            value = predict(window)
            entry.update(status="FORECAST", reason=None, predicted_level_m=round(value, 3),
                         interval_m=[round(value + low, 3), round(value + high, 3)],
                         valid_for=(latest + timedelta(hours=h["horizon_hours"])).isoformat(),
                         test_metrics=h["models"][h["selected_model"]]["test"])
        out.append(entry)
    return out


def public(evaluation: dict[str, Any]) -> dict[str, Any]:
    """The evaluation without fitted callables (JSON-safe)."""
    return {**evaluation, "horizons": [{k: v for k, v in h.items() if not k.startswith("_")}
                                       for h in evaluation["horizons"]]}


def main(argv: list[str] | None = None) -> int:
    from src.hydro_analysis import build_station_analysis

    parser = argparse.ArgumentParser(description="Validate river-level forecasts on the CWC "
                                                 "export (chronological, leakage-free).")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    now = datetime.now(timezone.utc)
    analysis, accepted = build_station_analysis(now)
    station = analysis["primary_station"]
    evaluation = evaluate(accepted[station], station)
    live = forecast_now(evaluation, accepted[station], now)
    if args.json:
        print(json.dumps({"evaluation": public(evaluation), "forecast": live}, indent=2,
                         default=str))
        return 0
    print(f"{MODEL_LABEL}S for {station} - validation on {evaluation['data']['accepted_readings']}"
          f" QC-accepted readings ({evaluation['data']['first']} .. {evaluation['data']['last']})")
    for h in evaluation["horizons"]:
        print(f"\n+{h['horizon_hours']} h  samples {h['split']['samples']}  "
              f"test period {h['split']['periods']['test']}")
        for name, m in h.get("models", {}).items():
            t = m["test"]
            print(f"  {name:18} test MAE {t['mae_m']:.4f}  RMSE {t['rmse_m']:.4f}  bias "
                  f"{t['bias_m']:+.4f}  R2 {t['r2']}  skill {t.get('skill_vs_persistence', '-')}"
                  f"  coverage {m['interval']['test_coverage']}")
        print(f"  -> {h['reason']}")
    for f in live:
        print(f"+{f['horizon_hours']} h: {f['status']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
