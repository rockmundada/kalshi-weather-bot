"""
NWS Data Source - Forecasts, CLI, AFD, Precipitation
Covers: hourly temp forecasts, Area Forecast Discussion (qualitative),
CLI reports (settlement truth), and precipitation forecasting.
Also: website tabular forecast scraping for cross-checking API data.

UNIFIED VERSION: Combines ChatGPT's dynamic /points grid resolution with
Claude's tabular cross-checking and ghost flag system.
"""
import re, logging, requests
from datetime import datetime, timezone, timedelta, date
from typing import Dict, List, Optional
from bs4 import BeautifulSoup
from config import CITIES, DATA_SOURCES, bankers_round_half_up, is_dst

logger = logging.getLogger(__name__)
UA = {"User-Agent": DATA_SOURCES["nws_user_agent"]}
NWS = DATA_SOURCES["nws_api_base"]


def _nws_get(url: str, timeout: int = 15) -> Optional[Dict]:
    """Make NWS API request with proper user agent."""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code == 200:
            return r.json()
        logger.warning(f"NWS {r.status_code}: {url}")
    except Exception as e:
        logger.warning(f"NWS error: {url}: {e}")
    return None


# =========================================================================
# DYNAMIC GRID RESOLUTION via /points (from ChatGPT's bot)
# More robust than hardcoded grid x/y — adapts if NWS changes grids.
# =========================================================================
_POINTS_CACHE: Dict[str, Optional[Dict]] = {}


def _get_point_metadata(lat: float, lon: float) -> Optional[Dict]:
    """Resolve NWS grid + forecast URLs for a lat/lon via /points.

    Using /gridpoints/{office}/{x},{y} with hardcoded x/y is brittle and can
    drift / be wrong. The /points endpoint is the canonical mapping.
    """
    # Round key a bit to avoid tiny float representation changes.
    key = f"{lat:.4f},{lon:.4f}"
    if key in _POINTS_CACHE:
        return _POINTS_CACHE[key]

    url = f"{NWS}/points/{lat},{lon}"
    data = _nws_get(url)
    if data and isinstance(data, dict) and data.get('properties'):
        _POINTS_CACHE[key] = data
        return data

    # Cache failures too to avoid repeated calls.
    _POINTS_CACHE[key] = None  # type: ignore
    return None


def _resolve_nws_urls(city: Dict) -> Dict[str, Optional[str]]:
    """Return forecast URLs + resolved grid info for a city.

    Falls back to configured office/x/y if /points fails.
    """
    lat = float(city.get('lat'))
    lon = float(city.get('lon'))

    point = _get_point_metadata(lat, lon)
    if point and point.get('properties'):
        p = point['properties']
        return {
            'forecast': p.get('forecast'),
            'forecastHourly': p.get('forecastHourly'),
            'forecastGridData': p.get('forecastGridData'),
            'gridId': p.get('gridId'),
            'gridX': str(p.get('gridX')) if p.get('gridX') is not None else None,
            'gridY': str(p.get('gridY')) if p.get('gridY') is not None else None,
        }

    # Fallback to hardcoded config
    grid = city.get('nws_grid') or {}
    office = grid.get('office')
    x = grid.get('x')
    y = grid.get('y')
    def _mk(path: str) -> Optional[str]:
        if office and x is not None and y is not None:
            return f"{NWS}/gridpoints/{office}/{x},{y}/{path}"
        return None

    return {
        'forecast': _mk('forecast'),
        'forecastHourly': _mk('forecast/hourly'),
        'forecastGridData': (f"{NWS}/gridpoints/{office}/{x},{y}" if office and x is not None and y is not None else None),
        'gridId': office,
        'gridX': str(x) if x is not None else None,
        'gridY': str(y) if y is not None else None,
    }


def _nws_text(url: str, timeout: int = 15) -> Optional[str]:
    """Fetch NWS text product."""
    try:
        r = requests.get(url, headers=UA, timeout=timeout)
        if r.status_code == 200:
            return r.text
    except Exception as e:
        logger.warning(f"NWS text error: {e}")
    return None


# =========================================================================
# HOURLY FORECAST
# =========================================================================
def get_hourly_forecast(city_key: str, contract_date: "date | None" = None) -> Dict:
    """Get NWS hourly forecast for temperature and precipitation.

    Args:
        city_key: city identifier
        contract_date: the date we're pricing contracts for.
                       If None, defaults to today in city's timezone.
                       This matters for PST cities late at night when
                       local date hasn't flipped to the contract date yet.
    """
    city = CITIES[city_key]
    resolved = _resolve_nws_urls(city)
    url = resolved.get('forecastHourly')
    if not url:
        grid = city['nws_grid']
        url = f"{NWS}/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast/hourly"

    result = {
        # Daily high for the contract date (full-day forecast)
        "forecast_high_day": None,
        "forecast_high_day_source": None,
        # True when hourly forecast only covers remaining hours in the day
        "forecast_high_day_is_partial": None,
        # Legacy alias (kept for backward compatibility)
        "forecast_high_today": None,
        "forecast_high_remaining": None,
        "forecast_low_today": None,
        "hourly_temps": [],
        "temp_trend": "unknown",
        "hours_remaining": None,
        "hours_to_peak": None,
        "hours_covered_today": 0,
        "forecast_high_source": "nws_hourly",
        "precip_probability_today": 0,
        "max_precip_prob_hour": None,
        "total_qpf_today_in": 0.0,  # Quantitative Precipitation Forecast
        "rain_likely_today": False,
        "error": None,
    }

    data = _nws_get(url)
    if not data:
        result["error"] = "Failed to fetch hourly forecast"
        return result

    periods = data.get("properties", {}).get("periods", [])
    if not periods:
        result["error"] = "No forecast periods"
        return result

    from zoneinfo import ZoneInfo
    tz = ZoneInfo(city["timezone"])
    now = datetime.now(tz)
    today = contract_date if contract_date else now.date()

    # Track if the contract date hasn't started locally yet
    contract_not_started = now.date() < today

    today_temps = []
    today_precip_probs = []

    for p in periods:
        try:
            start = datetime.fromisoformat(p["startTime"])
            start_local = start.astimezone(tz)

            if start_local.date() == today:
                temp_f = p.get("temperature")
                if temp_f is not None:
                    today_temps.append({
                        "time": start_local.isoformat(),
                        "hour": start_local.strftime("%I%p"),
                        "temp_f": temp_f,
                        "short": p.get("shortForecast", ""),
                    })

                # Precipitation probability
                precip = p.get("probabilityOfPrecipitation", {})
                prob = precip.get("value", 0) or 0
                today_precip_probs.append(prob)

            elif start_local.date() > today:
                break
        except (ValueError, KeyError):
            continue

    if today_temps:
        temps = [t["temp_f"] for t in today_temps]
        # NOTE: For a contract date already in progress, the hourly feed only
        # covers remaining hours, so this is a *remaining* high, not full-day.
        # We'll later reconcile with gridpoint max in get_all_nws_data.
        result["forecast_high_day"] = max(temps)
        result["forecast_high_day_source"] = "nws_hourly_max"
        result["forecast_high_today"] = result["forecast_high_day"]
        # If the contract date has already started, hourly periods only cover remaining hours
        result["forecast_high_day_is_partial"] = (not contract_not_started)
        result["forecast_low_today"] = min(temps)
        result["hourly_temps"] = today_temps[:24]
        result["hours_covered_today"] = len(today_temps)

        # Temperature trend
        if len(temps) >= 3:
            recent = temps[-3:]
            if all(recent[i] >= recent[i-1] for i in range(1, len(recent))):
                result["temp_trend"] = "rising"
            elif all(recent[i] <= recent[i-1] for i in range(1, len(recent))):
                result["temp_trend"] = "falling"
            else:
                result["temp_trend"] = "mixed"

        # Remaining-day high (future hours only)
        if not contract_not_started:
            future = []
            for t in today_temps:
                try:
                    ts = t.get("time")
                    if not ts:
                        continue
                    dt = datetime.fromisoformat(ts)
                    if dt >= now:
                        future.append(t)
                except (ValueError, TypeError):
                    continue
            if future:
                temps_future = [t["temp_f"] for t in future if t.get("temp_f") is not None]
                if temps_future:
                    result["forecast_high_remaining"] = max(temps_future)
                    # Hours until forecast peak (remaining hours)
                    peak_time = None
                    peak_temp = result["forecast_high_remaining"]
                    for t in future:
                        if t.get("temp_f") == peak_temp:
                            try:
                                peak_time = datetime.fromisoformat(t["time"])
                                break
                            except (ValueError, TypeError):
                                pass
                    if peak_time is not None:
                        result["hours_to_peak"] = max(0.0, round((peak_time - now).total_seconds() / 3600, 1))

        # Hours remaining in the contract day (local midnight-to-midnight window)
        if contract_not_started:
            # Contract date hasn't started - full day of heating ahead
            result["hours_remaining"] = 24
        else:
            end_local = datetime(today.year, today.month, today.day, 23, 59, 59, tzinfo=tz)
            # NWS temperature day is Local Standard Time. During DST, the effective
            # window runs ~1am–12:59am local daylight time (+1h).
            if is_dst(city_key):
                end_local = end_local + timedelta(hours=1)
            remaining = (end_local - now).total_seconds() / 3600
            result["hours_remaining"] = max(0, round(remaining, 1))

    if today_precip_probs:
        result["precip_probability_today"] = max(today_precip_probs)
        result["rain_likely_today"] = max(today_precip_probs) >= 40
        # Find peak precip hour
        max_idx = today_precip_probs.index(max(today_precip_probs))
        if max_idx < len(today_temps):
            result["max_precip_prob_hour"] = today_temps[max_idx].get("hour", "")

    return result


# =========================================================================
# GRIDPOINT FORECAST (includes QPF - Quantitative Precipitation)
# =========================================================================
def get_gridpoint_data(city_key: str, contract_date: "date | None" = None) -> Dict:
    """Get raw gridpoint data including QPF (quantitative precip forecast)."""
    city = CITIES[city_key]
    resolved = _resolve_nws_urls(city)
    url = resolved.get('forecastGridData')
    if not url:
        grid = city['nws_grid']
        url = f"{NWS}/gridpoints/{grid['office']}/{grid['x']},{grid['y']}"

    result = {
        "qpf_today_in": 0.0,
        "snow_today_in": 0.0,
        "max_temp_today": None,
        "min_temp_today": None,
        "error": None,
    }

    data = _nws_get(url, timeout=20)
    if not data:
        result["error"] = "Failed to fetch gridpoint data"
        return result

    props = data.get("properties", {})
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(city["timezone"])
    today = contract_date if contract_date else datetime.now(tz).date()

    # Max temperature
    max_temps = props.get("maxTemperature", {}).get("values", [])
    for v in max_temps:
        try:
            vtime = datetime.fromisoformat(v["validTime"].split("/")[0])
            if vtime.astimezone(tz).date() == today and v["value"] is not None:
                result["max_temp_today"] = round(v["value"] * 9/5 + 32, 1)
                break
        except (ValueError, KeyError):
            continue

    # QPF (quantitative precipitation forecast)
    qpf_values = props.get("quantitativePrecipitation", {}).get("values", [])
    total_qpf_mm = 0.0
    for v in qpf_values:
        try:
            vtime = datetime.fromisoformat(v["validTime"].split("/")[0])
            if vtime.astimezone(tz).date() == today and v["value"] is not None:
                total_qpf_mm += v["value"]
        except (ValueError, KeyError):
            continue
    result["qpf_today_in"] = round(total_qpf_mm / 25.4, 2)  # mm to inches

    # Snowfall
    snow_values = props.get("snowfallAmount", {}).get("values", [])
    total_snow_mm = 0.0
    for v in snow_values:
        try:
            vtime = datetime.fromisoformat(v["validTime"].split("/")[0])
            if vtime.astimezone(tz).date() == today and v["value"] is not None:
                total_snow_mm += v["value"]
        except (ValueError, KeyError):
            continue
    result["snow_today_in"] = round(total_snow_mm / 25.4, 2)

    return result


# =========================================================================
# CLI (Daily Climate Report) - SETTLEMENT TRUTH
# =========================================================================
def get_cli_report(city_key: str) -> Dict:
    """Fetch and parse the CLI report for settlement verification."""
    city = CITIES[city_key]
    cli_station = city["cli_station"]

    result = {
        "available": False,
        "high_temp_f": None,
        "low_temp_f": None,
        "precip_inches": None,
        "snow_inches": None,
        "report_date": None,
        "full_text": None,
        "is_final": False,
        "error": None,
    }

    # Try NWS API for CLI product
    url = f"{NWS}/products/types/CLI/locations/{city['wfo']}"
    data = _nws_get(url)

    if not data:
        # Fallback: try direct product URL
        url2 = f"https://forecast.weather.gov/product.php?site={city['wfo']}&product=CLI&issuedby={cli_station}"
        text = _nws_text(url2)
        if text:
            return _parse_cli_text(text, cli_station, city["timezone"])
        result["error"] = "CLI not available"
        return result

    products = data.get("@graph", [])
    if not products:
        result["error"] = "No CLI products found"
        return result

    # Get most recent CLI
    product_url = products[0].get("@id", "")
    if product_url:
        product_data = _nws_get(product_url)
        if product_data:
            text = product_data.get("productText", "")
            if text:
                return _parse_cli_text(text, cli_station, city["timezone"])

    result["error"] = "Could not fetch CLI text"
    return result


def _parse_cli_text(text: str, station: str, tz_name: str | None = None) -> Dict:
    """Parse CLI report text to extract temperatures and precip."""
    from zoneinfo import ZoneInfo

    result = {
        "available": True,
        "high_temp_f": None,
        "low_temp_f": None,
        "precip_inches": None,
        "snow_inches": None,
        "month_to_date_precip_in": None,
        "report_date": None,
        "report_date_iso": None,
        "full_text": text[:2000],
        "is_final": False,
        "is_today": False,  # Must verify date before trusting
        "error": None,
    }

    # Extract max temp: "MAXIMUM  31" or "TEMPERATURE (F)...MAXIMUM  31"
    m = re.search(r"MAXIMUM\s+(\d+)", text)
    if m:
        result["high_temp_f"] = int(m.group(1))

    m = re.search(r"MINIMUM\s+(\d+)", text)
    if m:
        result["low_temp_f"] = int(m.group(1))

    # Precipitation
    m = re.search(r"PRECIPITATION\s*\(IN\).*?(\d+\.\d+|T|0\.00|0)", text, re.DOTALL)
    if m:
        val = m.group(1)
        if val == 'T':
            result["precip_inches"] = 0.001  # Trace
        else:
            try:
                result["precip_inches"] = float(val)
            except ValueError:
                pass

    # Snowfall
    m = re.search(r"SNOWFALL\s*\(IN\).*?(\d+\.\d+|T|0\.0|0)", text, re.DOTALL)
    if m:
        val = m.group(1)
        if val == 'T':
            result["snow_inches"] = 0.001
        else:
            try:
                result["snow_inches"] = float(val)
            except ValueError:
                pass

    # Month-to-date precipitation (look for "MONTH TO DATE" line)
    m = re.search(r"MONTH\s+TO\s+DATE\s+(\d+\.\d+|T|0\.00|0)", text)
    if m:
        val = m.group(1)
        if val == 'T':
            result["month_to_date_precip_in"] = 0.001
        else:
            try:
                result["month_to_date_precip_in"] = float(val)
            except ValueError:
                pass

    # Report date - "CLIMATE SUMMARY FOR JANUARY 28 2026" (match exact phrase)
    m = re.search(r"(?:CLIMATE\s+SUMMARY\s+)?FOR\s+(\w+\s+\d+\s+\d{4})", text, re.IGNORECASE)
    if m:
        date_str = m.group(1).strip()
        result["report_date"] = date_str
        try:
            # Normalize month to title case for strptime %B (e.g. JANUARY -> January)
            parts = date_str.split()
            if len(parts) == 3:
                date_str = f"{parts[0].title()} {parts[1]} {parts[2]}"
            report_date = datetime.strptime(date_str, "%B %d %Y").date()
            tzinfo = ZoneInfo(tz_name) if tz_name else timezone.utc
            today = datetime.now(tzinfo).date()
            result["report_date_iso"] = report_date.isoformat()
            result["is_today"] = (report_date == today)
            if not result["is_today"]:
                logger.info(f"  CLI report is for {report_date}, not today ({today}) - ignoring for settlement")
        except ValueError:
            pass

    return result


# =========================================================================
# AFD (Area Forecast Discussion) - QUALITATIVE METEOROLOGIST INSIGHTS
# =========================================================================
def get_afd(city_key: str) -> Dict:
    """Fetch and parse AFD for meteorologist insights."""
    city = CITIES[city_key]
    wfo = city["wfo"]

    result = {
        "available": False,
        "full_text": None,
        "synopsis": None,
        "short_term": None,
        "temperature_discussion": None,
        "precipitation_discussion": None,
        "uncertainty_notes": [],
        "confidence_level": None,
        "key_phrases": [],
        "error": None,
    }

    # Fetch AFD from NWS API
    url = f"{NWS}/products/types/AFD/locations/{wfo}"
    data = _nws_get(url)

    if not data:
        result["error"] = "AFD not available"
        return result

    products = data.get("@graph", [])
    if not products:
        result["error"] = "No AFD products"
        return result

    # Get most recent
    product_url = products[0].get("@id", "")
    if not product_url:
        result["error"] = "No AFD URL"
        return result

    product_data = _nws_get(product_url)
    if not product_data:
        result["error"] = "Could not fetch AFD"
        return result

    text = product_data.get("productText", "")
    if not text:
        result["error"] = "Empty AFD"
        return result

    result["available"] = True
    result["full_text"] = text[:5000]

    # Parse sections
    text_upper = text.upper()

    # Synopsis
    m = re.search(r"\.SYNOPSIS\.{3}(.*?)(?:\.\w+\.{3}|$)", text, re.DOTALL | re.IGNORECASE)
    if m:
        result["synopsis"] = m.group(1).strip()[:1000]

    # Short term discussion
    m = re.search(r"\.SHORT TERM\.{3}(.*?)(?:\.\w+\.{3}|$)", text, re.DOTALL | re.IGNORECASE)
    if m:
        result["short_term"] = m.group(1).strip()[:2000]

    # Look for temperature-related sentences
    temp_sentences = []
    for line in text.split('\n'):
        line_lower = line.lower()
        if any(w in line_lower for w in ['temperature', 'high temp', 'low temp', 'warm', 'cold',
                                          'degrees', 'heating', 'cooling', 'above normal', 'below normal']):
            temp_sentences.append(line.strip())
    result["temperature_discussion"] = ' '.join(temp_sentences[:5]) if temp_sentences else None

    # Precipitation discussion
    precip_sentences = []
    for line in text.split('\n'):
        line_lower = line.lower()
        if any(w in line_lower for w in ['rain', 'snow', 'precipitation', 'shower', 'storm',
                                          'drizzle', 'accumulation', 'moisture', 'dry']):
            precip_sentences.append(line.strip())
    result["precipitation_discussion"] = ' '.join(precip_sentences[:5]) if precip_sentences else None

    # Uncertainty signals
    uncertainty_words = ['uncertain', 'tricky', 'challenge', 'difficult', 'disagreement',
                        'spread', 'low confidence', 'volatile', 'significant difference',
                        'model spread', 'ensemble', 'diverge']
    for word in uncertainty_words:
        if word in text.lower():
            result["uncertainty_notes"].append(word)

    # Confidence level
    if 'high confidence' in text.lower():
        result["confidence_level"] = "high"
    elif 'low confidence' in text.lower() or len(result["uncertainty_notes"]) >= 2:
        result["confidence_level"] = "low"
    else:
        result["confidence_level"] = "moderate"

    # Key phrases for trading
    key_patterns = [
        r"high[s]? (?:near|around|in the|of) (\d+)",
        r"low[s]? (?:near|around|in the|of) (\d+)",
        r"(\d+(?:\.\d+)?)\s*inch(?:es)?\s*(?:of\s+)?(?:rain|precipitation|snow)",
    ]
    for pat in key_patterns:
        matches = re.findall(pat, text, re.IGNORECASE)
        for match in matches:
            result["key_phrases"].append(match)

    return result


# =========================================================================
# TABULAR FORECAST (Website scraping - independent cross-check)
# From Claude's bot — catches NWS API vs website discrepancies.
# =========================================================================
def get_tabular_forecast(city_key: str, contract_date: "date | None" = None) -> Dict:
    """Scrape the NWS website tabular/digital forecast as an independent
    cross-check against the NWS API endpoints.

    The NWS website (forecast.weather.gov) sometimes shows different values
    than the API (api.weather.gov). E.g. Miami API=81F vs website=75F.
    This function scrapes the website "digital" forecast table to catch
    such discrepancies.

    Returns:
        dict with tabular_high_f, tabular_temps, hours_covered, error
    """
    city = CITIES[city_key]
    lat = city["lat"]
    lon = city["lon"]

    result = {
        "tabular_high_f": None,
        "tabular_temps": [],
        "hours_covered": 0,
        "error": None,
    }

    url = f"https://forecast.weather.gov/MapClick.php?lat={lat}&lon={lon}&FcstType=digital"

    try:
        r = requests.get(url, headers={"User-Agent": UA.get("User-Agent", "kalshi-bot")},
                         timeout=20)
        if r.status_code != 200:
            result["error"] = f"HTTP {r.status_code} from NWS tabular"
            return result
    except Exception as e:
        result["error"] = f"Failed to fetch NWS tabular: {e}"
        return result

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(city["timezone"])
        target_date = contract_date if contract_date else datetime.now(tz).date()
        # Format target date as MM/DD for matching NWS table headers
        target_mmdd = target_date.strftime("%m/%d")

        soup = BeautifulSoup(r.text, "html.parser")
        tables = soup.find_all("table")

        data_table = None
        for tbl in tables:
            for row in tbl.find_all("tr"):
                cells = [c.get_text(strip=True) for c in row.find_all(["td", "th"])]
                if cells and "Temperature" in cells[0] and len(cells) > 10:
                    data_table = tbl
                    break
            if data_table is not None:
                break

        if data_table is None:
            result["error"] = "Could not find temperature data table in NWS tabular page"
            return result

        rows = data_table.find_all("tr")
        all_temps_for_date = []

        # Track the last known date across sections so that Section 2's
        # unlabeled leading columns (which continue from Section 1's end
        # date) are correctly assigned. The NWS only labels the FIRST
        # occurrence of a new date in each section.
        last_known_date = None

        i = 0
        while i < len(rows):
            cells = [c.get_text(strip=True) for c in rows[i].find_all(["td", "th"])]
            if not cells:
                i += 1
                continue

            if cells[0] == "Date":
                date_col_indices = []
                # Carry forward the last known date from the previous section
                current_date = last_known_date
                for col_idx in range(1, len(cells)):
                    val = cells[col_idx].strip()
                    if val and "/" in val:
                        current_date = val
                    if current_date == target_mmdd:
                        date_col_indices.append(col_idx)
                # Remember the last date we saw for the next section
                if current_date:
                    last_known_date = current_date

                if not date_col_indices:
                    i += 1
                    continue

                for j in range(i + 1, min(i + 18, len(rows))):
                    temp_cells = [c.get_text(strip=True) for c in rows[j].find_all(["td", "th"])]
                    if not temp_cells:
                        continue
                    if "Temperature" in temp_cells[0] and "Dewpoint" not in temp_cells[0]:
                        for col_idx in date_col_indices:
                            if col_idx < len(temp_cells):
                                try:
                                    temp = int(temp_cells[col_idx])
                                    all_temps_for_date.append(temp)
                                except (ValueError, IndexError):
                                    pass
                        break

            i += 1

        if all_temps_for_date:
            result["tabular_high_f"] = max(all_temps_for_date)
            result["tabular_temps"] = all_temps_for_date
            result["hours_covered"] = len(all_temps_for_date)
            logger.info(
                f"  NWS tabular ({city_key}): max={max(all_temps_for_date)}F "
                f"from {len(all_temps_for_date)} hourly values"
            )
        else:
            result["error"] = f"No temperature data found for {target_mmdd} in tabular forecast"

    except Exception as e:
        result["error"] = f"Error parsing NWS tabular: {e}"
        logger.warning(f"  NWS tabular parse error for {city_key}: {e}")

    return result


# =========================================================================
# QUICK FORECAST RECHECK (pre-trade freshness validation)
# =========================================================================
def quick_forecast_recheck(city_key: str) -> Optional[float]:
    """Lightweight NWS hourly re-fetch to get current forecast max.

    Used as a pre-trade freshness check: if the forecast has shifted >=1°F
    since the original analysis, the trade should be reconsidered.
    Returns the current hourly max temperature in °F, or None on failure.
    """
    try:
        city = CITIES[city_key]
        resolved = _resolve_nws_urls(city)
        url = resolved.get("forecast_hourly")
        if not url:
            grid = city["nws_grid"]
            url = f"{NWS}/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast/hourly"

        data = _nws_get(url)
        if not data:
            return None

        from zoneinfo import ZoneInfo
        tz = ZoneInfo(city["timezone"])
        today = datetime.now(tz).date()

        periods = data.get("properties", {}).get("periods", [])
        max_temp = None
        for p in periods:
            try:
                start_dt = datetime.fromisoformat(p["startTime"]).astimezone(tz)
                if start_dt.date() == today:
                    t = p.get("temperature")
                    if t is not None and (max_temp is None or t > max_temp):
                        max_temp = t
            except (ValueError, KeyError, TypeError):
                continue

        return float(max_temp) if max_temp is not None else None
    except Exception as exc:
        logger.debug(f"Quick forecast recheck failed for {city_key}: {exc}")
        return None


# =========================================================================
# COMBINED NWS DATA
# =========================================================================
def _get_daily_forecast_high(city_key: str, contract_date: "date | None" = None) -> Optional[Dict]:
    """Fetch the NWS /forecast (semi-daily periods) to get the official daytime high.

    This is the authoritative NWS daily high forecast — NOT the hourly remaining-only max.
    Returns {"high_f": float, "source": "nws_daily_forecast"} or None.
    """
    city = CITIES[city_key]
    resolved = _resolve_nws_urls(city)
    url = resolved.get('forecast')
    if not url:
        grid = city["nws_grid"]
        url = f"{NWS}/gridpoints/{grid['office']}/{grid['x']},{grid['y']}/forecast"
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(city["timezone"])
        today = contract_date if contract_date else datetime.now(tz).date()

        data = _nws_get(url)
        if not data:
            return None
        periods = data.get("properties", {}).get("periods", [])
        for p in periods:
            if not p.get("isDaytime", False):
                continue
            try:
                start_dt = datetime.fromisoformat(p["startTime"]).astimezone(tz)
                if start_dt.date() == today:
                    temp = p.get("temperature")
                    if temp is not None:
                        return {"high_f": float(temp), "source": "nws_daily_forecast"}
            except (ValueError, KeyError, TypeError):
                continue
    except Exception as exc:
        logger.debug(f"NWS daily forecast fetch failed for {city_key}: {exc}")
    return None


# =========================================================================
# 5-MINUTE ASOS OBSERVATIONS (Intraday spike detection)
# =========================================================================
def fetch_5min_max(station_id: str, contract_date: "date | None" = None,
                   tz_name: str | None = None) -> Dict:
    """Fetch 5-minute ASOS observations from NWS API to detect intraday spikes.

    The 5-min data is whole-°C precision, so it has ±1°F rounding uncertainty
    due to the F→C→F double-rounding chain. This means a reported 4°C (39°F)
    could be anywhere from 38.3–40.1°F in reality.

    We use this as a WARNING SIGNAL only — NOT as ground truth for CLI settlement.
    When the 5-min max exceeds the T-group max, it means the actual high is
    uncertain and could be 1-2°F higher than T-group indicates.

    Returns:
        dict with max_5min_c, max_5min_f, obs_count, precision, warning, etc.
    """
    result = {
        "max_5min_c": None,
        "max_5min_f": None,
        "obs_count": 0,
        "precision": "whole_C",  # ±0.5°C → ±1°F ambiguity
        "error": None,
    }

    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(tz_name) if tz_name else ZoneInfo("UTC")
        now_utc = datetime.now(timezone.utc)

        if contract_date is None:
            contract_date = datetime.now(tz).date()

        # Build UTC start time from midnight local on contract date
        local_midnight = datetime(contract_date.year, contract_date.month,
                                  contract_date.day, 0, 0, 0, tzinfo=tz)
        start_utc = local_midnight.astimezone(timezone.utc)

        # Don't fetch future data
        if start_utc > now_utc:
            result["error"] = "Contract date hasn't started yet"
            return result

        end_utc = min(now_utc, (local_midnight + timedelta(hours=24)).astimezone(timezone.utc))

        # NWS API observations endpoint — returns 5-min ASOS data
        start_str = start_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        end_str = end_utc.strftime("%Y-%m-%dT%H:%M:%SZ")
        url = (f"{NWS}/stations/{station_id}/observations"
               f"?start={start_str}&end={end_str}&limit=500")

        data = _nws_get(url, timeout=20)
        if not data:
            result["error"] = "Failed to fetch 5-min observations"
            return result

        features = data.get("features", [])
        if not features:
            # Try @graph format
            features = data.get("@graph", [])

        max_c = None
        count = 0
        for obs in features:
            props = obs.get("properties", obs) if isinstance(obs, dict) else {}
            temp_data = props.get("temperature", {})
            if isinstance(temp_data, dict):
                val_c = temp_data.get("value")
            else:
                val_c = temp_data  # sometimes it's a direct value

            if val_c is not None:
                try:
                    val_c = float(val_c)
                    count += 1
                    if max_c is None or val_c > max_c:
                        max_c = val_c
                except (TypeError, ValueError):
                    continue

        result["obs_count"] = count
        if max_c is not None:
            result["max_5min_c"] = round(max_c, 1)
            # Convert to °F using standard rounding
            max_f_raw = max_c * 9 / 5 + 32
            result["max_5min_f"] = round(max_f_raw)  # whole-°F, same as NWS display
            result["max_5min_f_raw"] = round(max_f_raw, 1)
            logger.info(f"  5-min ASOS {station_id}: max={max_c}°C ({result['max_5min_f']}°F) from {count} obs")
        else:
            result["error"] = f"No temperature values in {count} observations"

    except Exception as e:
        result["error"] = f"5-min fetch failed: {e}"
        logger.warning(f"  5-min ASOS fetch failed for {station_id}: {e}")

    return result


def get_all_nws_data(city_key: str, contract_date: "date | None" = None) -> Dict:
    """Fetch all NWS data for a city.

    Reconciliation strategy (unified):
      - Prefer NWS daily forecast as the authoritative pre-CLI high
      - Fall back to gridpoint max, then hourly max
      - Cross-check ALL sources (daily vs hourly vs gridpoint vs tabular website)
      - Flag disagreements as ghost flags for the LLM to reason about
      - Override only when strong evidence of stale data (>=5F gap + good coverage)

    Args:
        city_key: city identifier
        contract_date: the date we're pricing contracts for.
    """
    hourly = get_hourly_forecast(city_key, contract_date=contract_date)
    gridpoint = get_gridpoint_data(city_key, contract_date=contract_date)

    # Fetch the authoritative NWS daily high forecast from /forecast endpoint
    daily = _get_daily_forecast_high(city_key, contract_date=contract_date)

    # Reconcile: prefer NWS daily forecast > gridpoint max > hourly remaining
    # This is the key fix: hourly only gives "remaining" hours for today,
    # which systematically underestimates the high. The /forecast daytime
    # period gives the actual forecasted daily high.
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo(CITIES[city_key]["timezone"])
        now_local = datetime.now(tz)
        today_local = contract_date if contract_date else now_local.date()
        gp_max = gridpoint.get("max_temp_today")
        h_max = hourly.get("forecast_high_day")
        daily_high = daily["high_f"] if daily else None
        hours_covered = hourly.get("hours_covered_today", 0)

        # Store all raw values for downstream cross-checking
        hourly["_nws_daily_high"] = daily_high
        hourly["_nws_gridpoint_max"] = gp_max
        hourly["_nws_hourly_max"] = h_max
        hourly["_nws_hourly_hours_covered"] = hours_covered

        # Priority: NWS daily forecast > gridpoint max > hourly
        if daily_high is not None:
            # The daily forecast is the most authoritative pre-CLI source
            hourly["forecast_high_day"] = daily_high
            hourly["forecast_high_day_source"] = "nws_daily_forecast"
            hourly["forecast_high_day_is_partial"] = False
        elif gp_max is not None:
            use_gp = False
            if h_max is None:
                use_gp = True
            elif hours_covered < 6:
                use_gp = True
            elif gp_max - h_max >= 3:
                use_gp = True
            if use_gp:
                hourly["forecast_high_day"] = gp_max
                hourly["forecast_high_day_source"] = "gridpoint_max"
                hourly["forecast_high_day_is_partial"] = False

        # -- NWS Cross-Check: daily vs hourly vs gridpoint --
        # Catches cases where /forecast daily product disagrees with the
        # more granular /forecast/hourly product (e.g. Miami 81F vs 76F).
        disagreements = []

        if daily_high is not None and h_max is not None and hours_covered >= 12:
            gap = abs(daily_high - h_max)
            if gap >= 3.0:
                disagreements.append(
                    f"NWS daily ({daily_high:.0f}F) vs hourly ({h_max:.0f}F) gap={gap:.1f}F"
                )
                logger.warning(f"  NWS CROSS-CHECK: {disagreements[-1]} for {city_key}")

        if daily_high is not None and gp_max is not None:
            gap = abs(daily_high - gp_max)
            if gap >= 3.0:
                disagreements.append(
                    f"NWS daily ({daily_high:.0f}F) vs gridpoint ({gp_max:.0f}F) gap={gap:.1f}F"
                )
                logger.warning(f"  NWS CROSS-CHECK: {disagreements[-1]} for {city_key}")

        hourly["_nws_cross_check_warnings"] = disagreements

        # Override: if hourly covers nearly the full day (>=18h) and disagrees
        # with daily by >=5F, the daily product is likely stale — prefer hourly
        if (daily_high is not None and h_max is not None
                and hours_covered >= 18
                and abs(daily_high - h_max) >= 5.0):
            logger.warning(
                f"  NWS daily ({daily_high:.0f}F) vs full-day hourly ({h_max:.0f}F) "
                f"gap >=5F with {hours_covered}h coverage — OVERRIDING to hourly for {city_key}"
            )
            hourly["forecast_high_day"] = h_max
            hourly["forecast_high_day_source"] = "hourly_crosschecked"
            hourly["forecast_high_day_is_partial"] = False

        # -- NWS Tabular Cross-Check: API vs Website --
        # The NWS website tabular forecast is an independent data pipeline
        # from the API. When they disagree, it's a major red flag.
        tabular = get_tabular_forecast(city_key, contract_date=contract_date)
        hourly["_tabular"] = tabular
        tab_high = tabular.get("tabular_high_f")
        tab_hours = tabular.get("hours_covered", 0)
        hourly["_tabular_high"] = tab_high

        if tab_high is not None:
            api_high = hourly.get("forecast_high_day")
            if api_high is not None:
                tab_gap = abs(api_high - tab_high)
                hourly["_tabular_gap_f"] = round(tab_gap, 1)

                if tab_gap >= 3.0:
                    warn = (
                        f"NWS API ({api_high:.0f}F) vs website tabular "
                        f"({tab_high}F) gap={tab_gap:.0f}F"
                    )
                    disagreements.append(warn)
                    logger.warning(f"  NWS TABULAR CROSS-CHECK: {warn} for {city_key}")
                    hourly["_tabular_cross_check_warning"] = warn

                # Override: if tabular has good coverage (>=12 hours) and
                # disagrees with the API by >=5F, the website tabular data
                # is likely more current — override the API forecast
                if tab_gap >= 5.0 and tab_hours >= 12:
                    logger.warning(
                        f"  NWS API ({api_high:.0f}F) vs tabular ({tab_high}F) "
                        f"gap >=5F with {tab_hours}h coverage — "
                        f"OVERRIDING to tabular for {city_key}"
                    )
                    hourly["forecast_high_day"] = float(tab_high)
                    hourly["forecast_high_day_source"] = "tabular_crosschecked"
                    hourly["forecast_high_day_is_partial"] = False
            else:
                hourly["_tabular_gap_f"] = None
        else:
            hourly["_tabular_gap_f"] = None

        # Update the cross-check warnings with any tabular warnings added
        hourly["_nws_cross_check_warnings"] = disagreements

    except Exception:
        pass

    # Keep legacy field in sync for any downstream code
    if hourly.get("forecast_high_day") is not None:
        hourly["forecast_high_today"] = hourly["forecast_high_day"]
        if hourly.get("forecast_high_day_is_partial") is None:
            hourly["forecast_high_day_is_partial"] = False
    if hourly.get("forecast_high_day_source") and not hourly.get("forecast_high_source"):
        hourly["forecast_high_source"] = hourly["forecast_high_day_source"]

    # Grab tabular data for return dict (may have been set above in try block)
    tabular_data = hourly.get("_tabular", {})

    return {
        "hourly": hourly,
        "gridpoint": gridpoint,
        "daily": daily or {},
        "tabular": tabular_data,
        "cli": get_cli_report(city_key),
        "afd": get_afd(city_key),
    }
