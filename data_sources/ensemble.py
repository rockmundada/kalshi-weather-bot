"""
Ensemble forecast blending from Open-Meteo.
Fetches multiple NWP models and produces inverse-MAE weighted forecasts
for both temperature and precipitation.
"""
import logging
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
import requests

from config import CITIES, DATA_SOURCES, MODEL_WEIGHTS, c_to_f

log = logging.getLogger(__name__)

OPEN_METEO_URL = DATA_SOURCES["open_meteo_url"]
MODELS = DATA_SOURCES["ensemble_models"]
FORECAST_DAYS = DATA_SOURCES.get("ensemble_forecast_days", 10)


def _fetch_model(model: str, lat: float, lon: float, tz: str) -> dict | None:
    """Fetch hourly data from one Open-Meteo model."""
    params = {
        "latitude": lat, "longitude": lon,
        "hourly": "temperature_2m,precipitation,precipitation_probability",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_sum",
        "models": model,
        "temperature_unit": "fahrenheit",
        "precipitation_unit": "inch",
        "timezone": tz,
        "forecast_days": FORECAST_DAYS,
    }
    try:
        r = requests.get(OPEN_METEO_URL, params=params, timeout=15)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        log.warning(f"Open-Meteo {model} fetch failed: {e}")
        return None


def _extract_daily_high(data: dict, target_date: date) -> float | None:
    """Pull daily max temp for target date."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    maxes = daily.get("temperature_2m_max", [])
    target_str = target_date.isoformat()
    for i, d in enumerate(dates):
        if d == target_str and i < len(maxes) and maxes[i] is not None:
            return maxes[i]
    return None


def _extract_daily_precip(data: dict, target_date: date) -> float | None:
    """Pull daily precip sum for target date (inches)."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    precip = daily.get("precipitation_sum", [])
    target_str = target_date.isoformat()
    for i, d in enumerate(dates):
        if d == target_str and i < len(precip) and precip[i] is not None:
            return precip[i]
    return None


def _extract_hourly_precip_prob(data: dict, target_date: date) -> list[float]:
    """Pull hourly precipitation probabilities for target date."""
    hourly = data.get("hourly", {})
    times = hourly.get("time", [])
    probs = hourly.get("precipitation_probability", [])
    target_str = target_date.isoformat()
    result = []
    for i, t in enumerate(times):
        if t.startswith(target_str) and i < len(probs) and probs[i] is not None:
            result.append(probs[i])
    return result


def _extract_monthly_precip(data: dict, today: date) -> float | None:
    """Sum daily precip from 1st of month through end of forecast."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    precip = daily.get("precipitation_sum", [])
    month_start = today.replace(day=1)
    total = 0.0
    found_any = False
    for i, d_str in enumerate(dates):
        d = date.fromisoformat(d_str)
        if d >= month_start and i < len(precip) and precip[i] is not None:
            total += precip[i]
            found_any = True
    return total if found_any else None


def _extract_remaining_precip(data: dict, today: date) -> float | None:
    """Sum daily precip from today through end of forecast."""
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    precip = daily.get("precipitation_sum", [])
    total = 0.0
    found_any = False
    for i, d_str in enumerate(dates):
        d = date.fromisoformat(d_str)
        if d >= today and i < len(precip) and precip[i] is not None:
            total += precip[i]
            found_any = True
    return total if found_any else None


def _extract_forecast_days(data: dict) -> int:
    daily = data.get("daily", {})
    dates = daily.get("time", [])
    return len(dates)


def get_ensemble_forecast(city_key: str, target_date: date | None = None) -> dict:
    """
    Fetch all configured models and blend with inverse-MAE weights.
    
    Returns:
        dict with:
        - model_highs: {model: temp_f}
        - weighted_high_f: blended forecast high
        - model_spread_f: max - min across models
        - model_precip: {model: inches}
        - weighted_precip_in: blended forecast precip
        - precip_probability: avg probability of any precip
        - monthly_precip_remaining: blended precip for remaining forecast days
        - forecast_days: number of daily forecast days returned
        - models_used: list of models that returned data
        - raw_data: full model outputs for Claude analysis
    """
    city = CITIES[city_key]
    lat, lon = city["lat"], city["lon"]
    tz = city["timezone"]
    if target_date is None:
        target_date = datetime.now(ZoneInfo(tz)).date()

    model_highs = {}
    model_precip = {}
    model_precip_probs = {}
    model_monthly = {}
    model_remaining = {}
    model_forecast_days = {}
    raw_data = {}

    for model in MODELS:
        data = _fetch_model(model, lat, lon, tz)
        if data is None:
            continue
        raw_data[model] = data

        high = _extract_daily_high(data, target_date)
        if high is not None:
            model_highs[model] = high

        precip = _extract_daily_precip(data, target_date)
        if precip is not None:
            model_precip[model] = precip

        probs = _extract_hourly_precip_prob(data, target_date)
        if probs:
            model_precip_probs[model] = max(probs)  # peak probability

        monthly = _extract_monthly_precip(data, target_date)
        if monthly is not None:
            model_monthly[model] = monthly

        remaining = _extract_remaining_precip(data, target_date)
        if remaining is not None:
            model_remaining[model] = remaining

        model_forecast_days[model] = _extract_forecast_days(data)

    # Weighted blend for temperature
    weighted_high = None
    if model_highs:
        w_sum = 0.0
        val_sum = 0.0
        for m, temp in model_highs.items():
            w = MODEL_WEIGHTS.get(m, {}).get("weight", 0.25)
            val_sum += temp * w
            w_sum += w
        if w_sum > 0:
            weighted_high = val_sum / w_sum

    # Weighted blend for precip
    weighted_precip = None
    if model_precip:
        w_sum = 0.0
        val_sum = 0.0
        for m, p in model_precip.items():
            w = MODEL_WEIGHTS.get(m, {}).get("weight", 0.25)
            val_sum += p * w
            w_sum += w
        if w_sum > 0:
            weighted_precip = val_sum / w_sum

    # Average precip probability
    avg_precip_prob = None
    if model_precip_probs:
        avg_precip_prob = sum(model_precip_probs.values()) / len(model_precip_probs)

    # Weighted monthly precip
    weighted_monthly = None
    if model_monthly:
        w_sum = 0.0
        val_sum = 0.0
        for m, p in model_monthly.items():
            w = MODEL_WEIGHTS.get(m, {}).get("weight", 0.25)
            val_sum += p * w
            w_sum += w
        if w_sum > 0:
            weighted_monthly = val_sum / w_sum

    # Weighted remaining precip (from today through forecast end)
    weighted_remaining = None
    if model_remaining:
        w_sum = 0.0
        val_sum = 0.0
        for m, p in model_remaining.items():
            w = MODEL_WEIGHTS.get(m, {}).get("weight", 0.25)
            val_sum += p * w
            w_sum += w
        if w_sum > 0:
            weighted_remaining = val_sum / w_sum

    # Forecast days (average across models to be safe)
    forecast_days = None
    if model_forecast_days:
        forecast_days = int(round(sum(model_forecast_days.values()) / len(model_forecast_days)))

    spread = None
    if model_highs:
        spread = max(model_highs.values()) - min(model_highs.values())

    return {
        "model_highs": model_highs,
        "weighted_high_f": weighted_high,
        "model_spread_f": spread,
        "model_precip": model_precip,
        "weighted_precip_in": weighted_precip,
        "precip_probability": avg_precip_prob,
        "model_precip_probs": model_precip_probs,
        "monthly_precip_forecast": weighted_monthly,
        "monthly_precip_remaining": weighted_remaining,
        "forecast_days": forecast_days,
        "model_monthly": model_monthly,
        "models_used": list(raw_data.keys()),
        "raw_data": raw_data,
    }
