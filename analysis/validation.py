"""Lightweight validation metrics for high-temp forecasts."""
from __future__ import annotations

import csv
import json
import os
from datetime import date, datetime
from typing import Dict, List, Tuple

from config import CITIES, PREDICTIONS_LOG_PATH, bankers_round_half_up
from data_sources.iem import get_cli_iem
from analysis.edge import _parse_temp_subtitle

_VALIDATION_CACHE: dict | None = None


def _to_float(val):
    try:
        if val is None or val == "":
            return None
        return float(val)
    except Exception:
        return None


def _actual_in_bounds(actual_temp_f: float, bounds: dict) -> int | None:
    if actual_temp_f is None or bounds is None:
        return None
    actual_int = bankers_round_half_up(actual_temp_f)
    kind = bounds.get("kind")
    if kind == "range":
        low = bounds.get("low")
        high = bounds.get("high")
        if low is None or high is None:
            return None
        return 1 if low <= actual_int <= high else 0
    if kind == "below":
        cap = bounds.get("cap")
        if cap is None:
            return None
        return 1 if actual_int <= cap else 0
    if kind == "above":
        floor = bounds.get("floor")
        if floor is None:
            return None
        return 1 if actual_int >= floor else 0
    return None


def compute_validation_stats(
    predictions_path: str | None = None,
    output_path: str | None = None,
    iem_fetcher=get_cli_iem,
) -> dict:
    """Compute per-city MAE and Brier score for settled high-temp contracts."""
    global _VALIDATION_CACHE

    if _VALIDATION_CACHE is not None:
        return _VALIDATION_CACHE

    predictions_path = predictions_path or PREDICTIONS_LOG_PATH
    output_path = output_path or os.path.join("data", "validation_stats.json")

    if not predictions_path or not os.path.exists(predictions_path):
        return {}

    rows: List[dict] = []
    with open(predictions_path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            if row.get("market_type") != "high_temp":
                continue
            rows.append(row)

    if not rows:
        return {}

    # Unique forecasts for MAE (per run)
    forecast_map: Dict[Tuple[str, str, str], float] = {}
    brier_rows: List[Tuple[str, str, float, str]] = []

    for row in rows:
        city = row.get("city")
        contract_date = row.get("contract_date")
        run_ts = row.get("run_ts_utc") or ""
        forecast_high = _to_float(row.get("forecast_high_f"))
        fair_prob = _to_float(row.get("fair_prob"))
        subtitle = row.get("contract_subtitle") or ""

        if city and contract_date and run_ts and forecast_high is not None:
            key = (city, contract_date, run_ts)
            if key not in forecast_map:
                forecast_map[key] = forecast_high

        if city and contract_date and fair_prob is not None and subtitle:
            brier_rows.append((city, contract_date, fair_prob, subtitle))

    # Fetch actual highs for settled dates
    actuals: Dict[Tuple[str, str], float] = {}
    unique_dates = {(c, d) for c, d, _ in forecast_map.keys()} | {(c, d) for c, d, _, _ in brier_rows}
    for city, dkey in unique_dates:
        try:
            d = date.fromisoformat(dkey)
        except Exception:
            continue
        try:
            cli = iem_fetcher(city, contract_date=d)
        except Exception:
            cli = None
        if cli and cli.get("is_settled") and cli.get("high_temp_f") is not None:
            actuals[(city, dkey)] = float(cli.get("high_temp_f"))

    if not actuals:
        return {}

    # Aggregate per city
    per_city: dict = {}
    for city_key in CITIES.keys():
        per_city[city_key] = {
            "mae_f": None,
            "brier": None,
            "n_mae": 0,
            "n_brier": 0,
        }

    mae_errs: Dict[str, List[float]] = {c: [] for c in CITIES.keys()}
    brier_errs: Dict[str, List[float]] = {c: [] for c in CITIES.keys()}

    for (city, dkey, _run_ts), forecast_high in forecast_map.items():
        actual = actuals.get((city, dkey))
        if actual is None:
            continue
        mae_errs.setdefault(city, []).append(abs(forecast_high - actual))

    for (city, dkey, fair_prob, subtitle) in brier_rows:
        actual = actuals.get((city, dkey))
        if actual is None:
            continue
        bounds = _parse_temp_subtitle(subtitle)
        outcome = _actual_in_bounds(actual, bounds)
        if outcome is None:
            continue
        brier = (fair_prob - outcome) ** 2
        brier_errs.setdefault(city, []).append(brier)

    for city, errs in mae_errs.items():
        if errs:
            per_city[city]["mae_f"] = round(sum(errs) / len(errs), 3)
            per_city[city]["n_mae"] = len(errs)

    for city, errs in brier_errs.items():
        if errs:
            per_city[city]["brier"] = round(sum(errs) / len(errs), 3)
            per_city[city]["n_brier"] = len(errs)

    stats = {
        "updated_at": datetime.utcnow().isoformat(),
        "per_city": per_city,
    }

    # Persist
    try:
        os.makedirs(os.path.dirname(output_path), exist_ok=True)
        with open(output_path, "w") as f:
            json.dump(stats, f, indent=2)
    except Exception:
        pass

    _VALIDATION_CACHE = stats
    return stats


def load_validation_stats(path: str | None = None) -> dict:
    global _VALIDATION_CACHE
    if _VALIDATION_CACHE is not None:
        return _VALIDATION_CACHE
    path = path or os.path.join("data", "validation_stats.json")
    if not os.path.exists(path):
        _VALIDATION_CACHE = {}
        return _VALIDATION_CACHE
    try:
        with open(path) as f:
            _VALIDATION_CACHE = json.load(f)
    except Exception:
        _VALIDATION_CACHE = {}
    return _VALIDATION_CACHE


def compute_nws_bias(city_key: str, lookback_days: int = 30) -> dict:
    """
    Compute rolling NWS forecast bias for a city over recent history.

    Reads the predictions CSV log and compares each run's forecast_high_f
    against the actual CLI settlement high. Returns the mean signed error
    (positive = NWS consistently forecasts warm, negative = cold).

    This allows the bot to learn and correct for systematic NWS bias.
    For example, if NWS consistently forecasts 1°F warm for NYC in February,
    the bot should subtract 1°F from NWS forecasts.

    Returns: {
        "bias_f": float or None,   # mean signed error (forecast - actual)
        "abs_bias_f": float or None,  # mean absolute error
        "n_samples": int,
        "confidence": str,  # "none", "low", "medium", "high"
        "city": str,
    }

    Source: u/hediwinn (DailyDewPoint creator) on r/Kalshi — tracks NWS
    forecast issuance accuracy and generates bias-corrected nowcasts.
    """
    predictions_path = PREDICTIONS_LOG_PATH
    result = {"bias_f": None, "abs_bias_f": None, "n_samples": 0,
              "confidence": "none", "city": city_key}

    if not predictions_path or not os.path.exists(predictions_path):
        return result

    # Read predictions, group by (city, contract_date, run_ts) to get unique forecasts
    forecast_map: Dict[Tuple[str, str], List[float]] = {}  # (city, date) -> [forecast_highs]
    try:
        with open(predictions_path, newline="") as f:
            reader = csv.DictReader(f)
            for row in reader:
                if row.get("city") != city_key:
                    continue
                if row.get("market_type") != "high_temp":
                    continue
                contract_date = row.get("contract_date")
                forecast_high = _to_float(row.get("forecast_high_f"))
                if contract_date and forecast_high is not None:
                    # Only include recent data within lookback window
                    try:
                        d = date.fromisoformat(contract_date)
                        days_ago = (date.today() - d).days
                        if days_ago > lookback_days or days_ago < 0:
                            continue
                    except Exception:
                        continue
                    forecast_map.setdefault((city_key, contract_date), []).append(forecast_high)
    except Exception:
        return result

    if not forecast_map:
        return result

    # For each date, compute average forecast and compare to actual CLI
    errors: List[float] = []
    for (city, dkey), forecasts in forecast_map.items():
        avg_forecast = sum(forecasts) / len(forecasts)
        try:
            d = date.fromisoformat(dkey)
        except Exception:
            continue
        # Only compare settled dates (in the past)
        if d >= date.today():
            continue
        try:
            cli = get_cli_iem(city, contract_date=d)
        except Exception:
            cli = None
        if cli and cli.get("is_settled") and cli.get("high_temp_f") is not None:
            actual = float(cli["high_temp_f"])
            errors.append(avg_forecast - actual)  # positive = forecast was warm

    result["n_samples"] = len(errors)
    if not errors:
        return result

    result["bias_f"] = round(sum(errors) / len(errors), 2)
    result["abs_bias_f"] = round(sum(abs(e) for e in errors) / len(errors), 2)

    # Confidence based on sample size
    if len(errors) >= 20:
        result["confidence"] = "high"
    elif len(errors) >= 10:
        result["confidence"] = "medium"
    elif len(errors) >= 5:
        result["confidence"] = "low"
    else:
        result["confidence"] = "none"

    return result


def get_calibration_badge(city_key: str, stats: dict | None = None) -> dict | None:
    stats = stats or load_validation_stats()
    per_city = stats.get("per_city", {}) if isinstance(stats, dict) else {}
    city_stats = per_city.get(city_key)
    if not city_stats:
        return None
    mae = city_stats.get("mae_f")
    brier = city_stats.get("brier")
    n_mae = city_stats.get("n_mae", 0)
    n_brier = city_stats.get("n_brier", 0)
    if mae is None and brier is None:
        return None
    parts = []
    if mae is not None:
        parts.append(f"MAE={mae:.2f}°F")
    if brier is not None:
        parts.append(f"Brier={brier:.2f}")
    badge = " | ".join(parts)
    return {
        "badge": badge,
        "mae_f": mae,
        "brier": brier,
        "n_mae": n_mae,
        "n_brier": n_brier,
    }
