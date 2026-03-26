"""
IEM (Iowa Environmental Mesonet) CLI JSON access.
Provides daily climate summaries (high/low/precip, month-to-date).
"""
import logging
from datetime import date, datetime
from typing import Dict, Optional
from zoneinfo import ZoneInfo

import requests

from config import CITIES

log = logging.getLogger(__name__)


def _to_float(val) -> Optional[float]:
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except Exception:
        return None


def get_cli_iem(city_key: str, contract_date: "date | None" = None) -> Optional[Dict]:
    """Fetch CLI summary from IEM JSON for a given station and date.

    Returns dict with:
      - high_temp_f, low_temp_f, precip_inches, month_to_date_precip_in
      - report_date (YYYY-MM-DD)
      - is_today (bool)
    """
    city = CITIES[city_key]
    # IEM CLI endpoint uses 3-letter CLI station codes (e.g., NYC, MDW, DEN),
    # NOT 4-character METAR IDs (e.g., KNYC). Prefer cli_station first.
    station = city.get("cli_station") or city.get("station_id")
    if not station:
        return None
    if contract_date is None:
        contract_date = date.today()

    url = f"https://mesonet.agron.iastate.edu/json/cli.py?station={station}&year={contract_date.year}"
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        data = r.json()
        results = data.get("results", [])
        target = contract_date.isoformat()
        # Determine local "today" for this city to avoid same-day CLI misuse
        tz = ZoneInfo(city.get("timezone", "UTC"))
        local_today = datetime.now(tz).date()

        for row in results:
            if row.get("valid") == target:
                report_date = row.get("valid")
                is_today = (contract_date == local_today)
                # IEM CLI is settlement-quality only AFTER the local day has completed
                is_settled = contract_date < local_today
                return {
                    "high_temp_f": _to_float(row.get("high")),
                    "low_temp_f": _to_float(row.get("low")),
                    "precip_inches": _to_float(row.get("precip")),
                    "month_to_date_precip_in": _to_float(row.get("precip_month")),
                    "report_date": report_date,
                    "is_today": is_today,
                    "is_settled": is_settled,
                    "source": "iem",
                }
    except Exception as e:
        log.debug(f"IEM CLI fetch failed for {city_key}: {e}")
    return None
