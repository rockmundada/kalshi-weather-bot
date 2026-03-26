"""
Wethr.net API v2 integration (optional).
Docs: https://wethr.net/edu/api-docs
Requires WETHR_API_KEY in config (Professional/Developer tiers).
"""
import logging
from datetime import date
import requests

from config import CITIES, DATA_SOURCES, WETHR_API_KEY

log = logging.getLogger(__name__)

BASE = DATA_SOURCES.get("wethr_api_base", "https://wethr.net/api/v2")


def _headers() -> dict:
    if not WETHR_API_KEY:
        return {}
    return {"Authorization": f"Bearer {WETHR_API_KEY}"}


def get_wethr_high_low(city_key: str, logic: str = "nws") -> dict:
    """Get Wethr high/low calculation for the current trading day (LST logic)."""
    if not WETHR_API_KEY:
        return {}
    station = CITIES[city_key]["station_id"]
    url = f"{BASE}/observations.php"
    params = {"station_code": station, "mode": "wethr_high", "logic": logic}
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return {
                "wethr_high_f": data.get("wethr_high"),
                "wethr_low_f": data.get("wethr_low"),
                "time_of_high_utc": data.get("time_of_high_utc"),
                "time_of_low_utc": data.get("time_of_low_utc"),
                "units": data.get("units"),
                "source": "wethr",
            }
        log.warning(f"Wethr high/low HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Wethr high/low error for {city_key}: {e}")
    return {}


def get_wethr_precip_mtd(city_key: str) -> dict:
    """Get Wethr precipitation MTD totals (CLI + today live)."""
    if not WETHR_API_KEY:
        return {}
    station = CITIES[city_key]["station_id"]
    url = f"{BASE}/precipitation.php"
    params = {"station_code": station}
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return {
                "official_mtd": data.get("official_mtd"),
                "today_precip": data.get("today_precip"),
                "total_mtd": data.get("total_mtd"),
                "has_trace": data.get("has_trace"),
                "cli_date": data.get("cli_date"),
                "units": data.get("units"),
                "source": "wethr",
            }
        log.warning(f"Wethr precip HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Wethr precip error for {city_key}: {e}")
    return {}


def get_wethr_nws_forecast(city_key: str, contract_date: date | None = None) -> dict:
    """Get Wethr NWS hourly forecast (LST-aligned)."""
    if not WETHR_API_KEY:
        return {}
    station = CITIES[city_key]["station_id"]
    url = f"{BASE}/nws_forecasts.php"
    params = {"station_code": station}
    if contract_date is not None:
        params["date"] = contract_date.isoformat()
    try:
        r = requests.get(url, headers=_headers(), params=params, timeout=20)
        if r.status_code == 200:
            data = r.json()
            return {
                "forecast_date": data.get("forecast_date"),
                "high": data.get("high"),
                "low": data.get("low"),
                "hourly_temps": data.get("hourly_temps"),
                "time_convention": data.get("time_convention"),
                "source": "wethr",
            }
        log.warning(f"Wethr NWS forecast HTTP {r.status_code}: {r.text[:200]}")
    except Exception as e:
        log.warning(f"Wethr NWS forecast error for {city_key}: {e}")
    return {}

