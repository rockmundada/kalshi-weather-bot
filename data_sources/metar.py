"""
METAR Data Source - AWC Cache + T-group + Synoptic + Precip Parsing
This is the MOST IMPORTANT data source. T-group precision = 0.1°C.

AWC cache: 1-5 min latency (vs 20+ min NWS API).
T-group example: T10331094 means temp=-3.3°C, dewpoint=-9.4°C
6-hr max: 1snTTT (1=group id, s=sign, n=tenths)
6-hr min: 2snTTT
24-hr: 4snTTTsnTTT (max then min)
Precip: P0000=hourly, 60000=6hr, 70000=24hr (in hundredths of inch)
Snow: 4/sss (depth in inches)
"""
import re, logging, requests
from datetime import datetime, timezone, timedelta
from typing import Dict, List, Optional, Tuple
from config import CITIES, DATA_SOURCES, c_to_f, bankers_round_half_up

logger = logging.getLogger(__name__)

# === REGEX PATTERNS ===
RE_TGROUP = re.compile(r"T(\d{4})(\d{4})")          # T[temp4][dewpt4]
RE_6HR_MAX = re.compile(r"\s1([01])(\d{3})(?:\s|$)")  # 1snTTT
RE_6HR_MIN = re.compile(r"\s2([01])(\d{3})(?:\s|$)")  # 2snTTT
RE_24HR = re.compile(r"\s4([01])(\d{3})([01])(\d{3})(?:\s|$)")  # 4snTTTsnTTT
RE_PRECIP_1HR = re.compile(r"\sP(\d{4})(?:\s|$)")     # Hourly precip
RE_PRECIP_6HR = re.compile(r"\s6(\d{4})(?:\s|$)")     # 6-hour precip
RE_PRECIP_24HR = re.compile(r"\s7(\d{4})(?:\s|$)")    # 24-hour precip
RE_SNOW_DEPTH = re.compile(r"\s4/(\d{3})(?:\s|$)")    # Snow depth
RE_BASIC_TEMP = re.compile(r"\s(M?\d{2})/(M?\d{2})\s")
RE_TIME = re.compile(r"(\d{2})(\d{2})(\d{2})Z")
RE_PRECIP_WEATHER = re.compile(r"(?:\+|-)?(?:VC)?(?:MI|PR|BC|DR|BL|SH|TS|FZ)?"
                                r"(?:DZ|RA|SN|SG|IC|PL|GR|GS|UP|BR|FG|FU|VA|DU|SA|HZ|PY)")


def _decode_tgroup_temp(code: str) -> float:
    """Decode 4-digit T-group to Celsius. First digit = sign (0=+, 1=-), rest = tenths."""
    sign = -1 if code[0] == '1' else 1
    return sign * int(code[1:]) / 10.0


def _decode_synoptic(sign: str, ttt: str) -> float:
    """Decode synoptic group to Celsius."""
    s = -1 if sign == '1' else 1
    return s * int(ttt) / 10.0


def _decode_basic_temp(code: str) -> int:
    """Decode basic METAR temp (M=minus)."""
    if code.startswith('M'):
        return -int(code[1:])
    return int(code)


def compute_fahr_ambiguity(celsius_whole: int) -> dict:
    """
    Given a whole-degree Celsius value from a METAR body (no T-group),
    compute the possible original Fahrenheit values.

    The 5-min ASOS process: measure F → round to whole F → convert to C → round to whole C.
    So a reported whole-degree C could have come from multiple original F values.

    This matters because the rounding chain can introduce ±1°F error.
    When the ambiguity spans a Kalshi bracket boundary, the observation
    is unreliable for edge computation.

    Source: u/adulting_dude on r/Kalshi — "5-minute stations...
    This process introduces a significant amount of rounding and conversion
    error... can be a degree or more higher or lower than the official high."

    Returns: {
        "reported_c": int,
        "f_candidates": list[int],  # all F values that round-trip to this C
        "f_low": int,               # lowest possible F
        "f_high": int,              # highest possible F
        "ambiguous": bool,          # True if f_low != f_high
        "ambiguity_range_f": int,   # f_high - f_low
    }
    """
    # Reverse: which whole-degree F values, when converted to C and rounded, give this C?
    # Process: F → C_exact = (F-32)*5/9 → C_rounded = bankers_round_half_up(C_exact)
    f_exact = celsius_whole * 9.0 / 5.0 + 32.0
    candidates = []
    for f_candidate in range(int(f_exact) - 3, int(f_exact) + 4):
        c_converted = (f_candidate - 32) * 5.0 / 9.0
        c_rounded = bankers_round_half_up(c_converted)
        if c_rounded == celsius_whole:
            candidates.append(f_candidate)

    if not candidates:
        # Fallback: just use the direct conversion
        candidates = [bankers_round_half_up(f_exact)]

    return {
        "reported_c": celsius_whole,
        "f_candidates": sorted(candidates),
        "f_low": min(candidates),
        "f_high": max(candidates),
        "ambiguous": len(candidates) > 1,
        "ambiguity_range_f": max(candidates) - min(candidates),
    }


def _is_hourly_report(obs_time_str: str) -> bool:
    """Check if a METAR was issued at the standard hourly time (:51-:55).

    Hourly stations record at the 51st-55th minute. These are the authoritative
    readings with minimal rounding error. Non-hourly (5-minute, SPECI) reports
    may have more rounding artifacts.

    Source: u/proteinofearth and u/adulting_dude on r/Kalshi.
    """
    if not obs_time_str:
        return False
    try:
        dt = datetime.fromisoformat(obs_time_str)
        return 51 <= dt.minute <= 55
    except (ValueError, TypeError):
        return False


def _has_precip_weather(raw: str) -> bool:
    """Check if METAR reports precipitation weather."""
    return bool(RE_PRECIP_WEATHER.search(raw))


def parse_single_metar(raw: str) -> Dict:
    """Parse a single METAR string into structured data."""
    result = {
        "raw": raw,
        "temp_c": None, "temp_f": None, "temp_precision": "none",
        "dewpoint_c": None,
        "tgroup_temp_c": None, "tgroup_temp_f": None, "tgroup_temp_f_rounded": None,
        "six_hr_max_c": None, "six_hr_max_f": None,
        "six_hr_min_c": None, "six_hr_min_f": None,
        "day_max_c": None, "day_max_f": None,
        "day_min_c": None, "day_min_f": None,
        "precip_1hr_in": None, "precip_6hr_in": None, "precip_24hr_in": None,
        "snow_depth_in": None,
        "has_precip_weather": _has_precip_weather(raw),
        "obs_time": None,
        "is_special": "SPECI" in raw,
        "is_hourly": False,         # True if issued at :51-:55 (authoritative)
        "temp_ambiguity": None,     # F→C→F ambiguity info (only when no T-group)
    }

    # Observation time
    m = RE_TIME.search(raw)
    if m:
        try:
            now = datetime.now(timezone.utc)
            day, hour, minute = int(m.group(1)), int(m.group(2)), int(m.group(3))
            obs = now.replace(day=day, hour=hour, minute=minute, second=0, microsecond=0)
            if obs > now:
                obs -= timedelta(days=28)  # Previous month
            result["obs_time"] = obs.isoformat()
            result["is_hourly"] = _is_hourly_report(obs.isoformat())
        except (ValueError, OverflowError):
            pass

    # T-group (HIGHEST PRIORITY for temperature)
    m = RE_TGROUP.search(raw)
    if m:
        tc = _decode_tgroup_temp(m.group(1))
        tf = c_to_f(tc)
        result["tgroup_temp_c"] = tc
        result["tgroup_temp_f"] = round(tf, 1)
        result["tgroup_temp_f_rounded"] = bankers_round_half_up(tf)
        result["temp_c"] = tc
        result["temp_f"] = round(tf, 1)
        result["temp_precision"] = "tgroup_0.1C"
        # Dewpoint
        dc = _decode_tgroup_temp(m.group(2))
        result["dewpoint_c"] = dc

    # Basic temp (fallback if no T-group)
    m = RE_BASIC_TEMP.search(raw)
    if m:
        basic_c = _decode_basic_temp(m.group(1))
        basic_f = c_to_f(basic_c)
        if result["temp_c"] is None:
            result["temp_c"] = float(basic_c)
            result["temp_f"] = round(basic_f, 1)
            result["temp_precision"] = "basic_1C"
            # No T-group — subject to F→C→F ambiguity (5-min station rounding)
            ambiguity = compute_fahr_ambiguity(basic_c)
            result["temp_ambiguity"] = ambiguity
            if ambiguity["ambiguous"]:
                result["temp_precision"] = "basic_1C_ambiguous"

    # 6-hour max
    m = RE_6HR_MAX.search(raw)
    if m:
        tc = _decode_synoptic(m.group(1), m.group(2))
        result["six_hr_max_c"] = tc
        result["six_hr_max_f"] = bankers_round_half_up(c_to_f(tc))

    # 6-hour min
    m = RE_6HR_MIN.search(raw)
    if m:
        tc = _decode_synoptic(m.group(1), m.group(2))
        result["six_hr_min_c"] = tc
        result["six_hr_min_f"] = bankers_round_half_up(c_to_f(tc))

    # 24-hour max/min
    m = RE_24HR.search(raw)
    if m:
        max_c = _decode_synoptic(m.group(1), m.group(2))
        min_c = _decode_synoptic(m.group(3), m.group(4))
        result["day_max_c"] = max_c
        result["day_max_f"] = bankers_round_half_up(c_to_f(max_c))
        result["day_min_c"] = min_c
        result["day_min_f"] = bankers_round_half_up(c_to_f(min_c))

    # Precipitation
    m = RE_PRECIP_1HR.search(raw)
    if m:
        val = int(m.group(1))
        result["precip_1hr_in"] = val / 100.0 if val < 9999 else None

    m = RE_PRECIP_6HR.search(raw)
    if m:
        val = int(m.group(1))
        result["precip_6hr_in"] = val / 100.0 if val < 9999 else None

    m = RE_PRECIP_24HR.search(raw)
    if m:
        val = int(m.group(1))
        result["precip_24hr_in"] = val / 100.0 if val < 9999 else None

    m = RE_SNOW_DEPTH.search(raw)
    if m:
        val = int(m.group(1))
        result["snow_depth_in"] = val if val < 999 else None

    return result


def fetch_awc_metars(station_id: str, hours: int = 3) -> List[str]:
    """Fetch recent METARs from AWC cache (1-5 min latency)."""
    try:
        url = DATA_SOURCES["awc_cache_url"]
        params = {
            "ids": station_id,
            "format": "raw",
            "hours": hours,
            "taf": "false",
        }
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200 and r.text.strip():
            lines = [l.strip() for l in r.text.strip().split('\n') if l.strip() and l.strip()[0:1].isalpha()]
            return lines
    except Exception as e:
        logger.warning(f"AWC cache error for {station_id}: {e}")
    return []


def fetch_awc_metars_multi(station_ids: List[str], hours: int = 3) -> Dict[str, List[str]]:
    """Fetch recent METARs for multiple stations in one call."""
    results: Dict[str, List[str]] = {sid: [] for sid in station_ids}
    if not station_ids:
        return results
    try:
        url = DATA_SOURCES["awc_cache_url"]
        params = {
            "ids": ",".join(station_ids),
            "format": "raw",
            "hours": hours,
            "taf": "false",
        }
        r = requests.get(url, params=params, timeout=20)
        if r.status_code == 200 and r.text.strip():
            lines = [l.strip() for l in r.text.strip().split('\n') if l.strip() and l.strip()[0:1].isalpha()]
            for line in lines:
                parts = line.split()
                if not parts:
                    continue
                if parts[0] in ("METAR", "SPECI") and len(parts) > 1:
                    st = parts[1]
                else:
                    st = parts[0]
                if st in results:
                    results[st].append(line)
    except Exception as e:
        logger.warning(f"AWC multi fetch error: {e}")
    return results

def get_metar_data(city_key: str,
                   contract_date: "date | None" = None,
                   prefetched_raw_metars: "Dict[str, List[str]] | None" = None) -> Dict:
    """Get comprehensive METAR data for a city.
    
    Args:
        city_key: city identifier
        contract_date: the date we're pricing contracts for (local time).
                       If None, defaults to today in city's timezone.
                       Used to filter observations to only include data from
                       after midnight on the contract date.
    
    Returns dict with:
    - latest: most recent parsed METAR
    - all_parsed: list of all parsed METARs
    - best_max_f: best estimate of today's max temp (filtered to contract_date only)
    - best_max_source: where the max came from
    - current_temp_f: current temperature
    - total_precip_today: accumulated precipitation today
    - has_rained_today: bool
    """
    from zoneinfo import ZoneInfo
    from datetime import date as date_type
    
    city = CITIES[city_key]
    station = city["station_id"]
    tz = ZoneInfo(city["timezone"])
    
    # Determine which date we're collecting data for
    now_local = datetime.now(tz)
    if contract_date is None:
        contract_date = now_local.date()
    
    # Midnight UTC for the contract date in the city's timezone
    # Observations before this are "yesterday" and should be excluded from max tracking
    contract_midnight_local = datetime(contract_date.year, contract_date.month, contract_date.day, 
                                        tzinfo=tz)
    contract_midnight_utc = contract_midnight_local.astimezone(timezone.utc)
    
    # Is the contract date in the future (hasn't started yet locally)?
    contract_not_started = now_local.date() < contract_date
    
    result = {
        "station": station,
        "station_is_backup": False,
        "latest": None,
        "all_parsed": [],
        "current_temp_f": None,
        "current_temp_precision": None,
        "best_max_f": None,
        "best_max_rounded": None,
        "best_max_source": None,
        "best_min_f": None,
        "best_min_source": None,
        "max_temps_today": [],  # All T-group temps observed today
        "total_precip_today": 0.0,
        "has_rained_today": False,
        "precip_observations": [],
        "contract_date": str(contract_date),
        "contract_not_started": contract_not_started,
        "hours_until_start": None,
        "latest_obs_time": None,
        "latest_obs_age_min": None,
        "error": None,
    }

    # Fetch from AWC cache (primary) and backup stations
    stations_to_try = [station] + city.get("backup_stations", [])
    raw_metars = []
    
    for st in stations_to_try:
        if prefetched_raw_metars is not None:
            metars = prefetched_raw_metars.get(st, [])
        else:
            metars = fetch_awc_metars(st, hours=DATA_SOURCES["awc_cache_hours"])
        if metars:
            raw_metars = metars
            result["station"] = st
            result["station_is_backup"] = (st != station)
            break

    if not raw_metars:
        result["error"] = f"No METARs available for {station}"
        logger.warning(result["error"])
        return result

    # Parse all METARs
    parsed_list = [parse_single_metar(raw) for raw in raw_metars]
    # Sort by observation time desc when available (AWC order is not guaranteed)
    def _obs_sort_key(p: Dict) -> float:
        ts = p.get("obs_time")
        if not ts:
            return 0.0
        try:
            return datetime.fromisoformat(ts).timestamp()
        except (ValueError, TypeError):
            return 0.0
    parsed_list.sort(key=_obs_sort_key, reverse=True)
    result["all_parsed"] = parsed_list
    
    if parsed_list:
        result["latest"] = parsed_list[0]  # Most recent
        result["current_temp_f"] = parsed_list[0].get("temp_f")
        result["current_temp_precision"] = parsed_list[0].get("temp_precision")
        result["latest_obs_time"] = parsed_list[0].get("obs_time")
        # Compute observation age in minutes (UTC)
        try:
            if result["latest_obs_time"]:
                obs_utc = datetime.fromisoformat(result["latest_obs_time"])
                age_min = (datetime.now(timezone.utc) - obs_utc).total_seconds() / 60.0
                if age_min >= 0:
                    result["latest_obs_age_min"] = round(age_min, 1)
        except (ValueError, TypeError):
            pass

    # Find best max temperature from all sources
    # IMPORTANT: Only include observations from AFTER midnight on the contract date
    # to avoid yesterday's high bleeding into today's max tracking.
    # The 24-hour synoptic max (4snTTTsnTTT) spans the prior 24h and MUST be excluded
    # since it will contain yesterday's high.
    max_temps = []
    
    for p in parsed_list:
        # Check if this observation is from the contract date
        obs_time_str = p.get("obs_time")
        obs_is_today = False  # require a valid observation time to avoid carryover
        if obs_time_str and not contract_not_started:
            try:
                obs_utc = datetime.fromisoformat(obs_time_str)
                obs_is_today = obs_utc >= contract_midnight_utc
            except (ValueError, TypeError):
                pass
        elif contract_not_started:
            # Contract date hasn't started locally - no observations are valid
            obs_is_today = False
        
        if not obs_is_today:
            logger.debug(f"  Skipping pre-midnight observation: {p.get('obs_time')}")
            continue
        
        # T-group temps (highest precision — 0.1°C)
        if p.get("tgroup_temp_f") is not None:
            max_temps.append(("tgroup", p["tgroup_temp_f"], p["tgroup_temp_f_rounded"]))
            result["max_temps_today"].append(p["tgroup_temp_f"])
        elif p.get("temp_f") is not None:
            # Basic METAR temp — whole °C precision, subject to ±1°F rounding
            # This catches SPECIs and non-hourly reports that lack T-groups
            basic_f = float(p["temp_f"])
            basic_c = (basic_f - 32) * 5 / 9
            basic_f_from_c = basic_c * 9 / 5 + 32  # round-trip to show precision loss
            basic_rounded = int(round(basic_f))
            max_temps.append(("basic_metar", basic_f, basic_rounded))
            result["max_temps_today"].append(basic_f)

        # 6-hour synoptic max - only use if well into the contract date
        # (the 6-hr window could span midnight)
        if p.get("six_hr_max_f") is not None:
            if obs_time_str:
                try:
                    obs_utc = datetime.fromisoformat(obs_time_str)
                    hours_since_midnight = (obs_utc - contract_midnight_utc).total_seconds() / 3600
                    if hours_since_midnight >= 6:
                        # Full 6-hr window is within contract date
                        max_temps.append(("synoptic_6hr", c_to_f(p["six_hr_max_c"]), p["six_hr_max_f"]))
                    else:
                        logger.debug(f"  Skipping 6hr max (spans midnight): {p['six_hr_max_f']}°F")
                except (ValueError, TypeError):
                    # If we can't verify time, skip to avoid carryover
                    logger.debug(f"  Skipping 6hr max (no reliable obs time): {p['six_hr_max_f']}°F")
        
        # 24-hour max: ALWAYS SKIP for max tracking - it contains yesterday's high
        # (Only useful as a cross-reference, not for today's max)
        if p.get("day_max_f") is not None:
            logger.debug(f"  Skipping 24hr synoptic max (contains yesterday): {p['day_max_f']}°F")

    if max_temps:
        # Sort by raw F value (not rounded) to find true max
        max_temps.sort(key=lambda x: x[1], reverse=True)
        best = max_temps[0]
        result["best_max_f"] = round(best[1], 1)
        result["best_max_rounded"] = best[2]
        result["best_max_source"] = best[0]
        # Check if max came from non-hourly report without T-group (ambiguous)
        if best[0] == "basic_metar":
            result["max_source_warning"] = "Max from non-hourly report (possible 5-min rounding)"
    elif result["current_temp_f"] is not None and not contract_not_started:
        # Fallback: use basic METAR temp (lower precision, whole degrees only)
        result["best_max_f"] = float(result["current_temp_f"])
        result["best_max_rounded"] = result["current_temp_f"]
        result["best_max_source"] = "basic_metar"

    # Find best min temperature
    min_temps = []
    for p in parsed_list:
        if p.get("tgroup_temp_f") is not None:
            min_temps.append(("tgroup", p["tgroup_temp_f"], p["tgroup_temp_f_rounded"]))
        if p.get("six_hr_min_f") is not None:
            min_temps.append(("synoptic_6hr", c_to_f(p["six_hr_min_c"]), p["six_hr_min_f"]))
        if p.get("day_min_f") is not None:
            min_temps.append(("synoptic_24hr", c_to_f(p["day_min_c"]), p["day_min_f"]))
    
    if min_temps:
        min_temps.sort(key=lambda x: x[1])
        best = min_temps[0]
        result["best_min_f"] = round(best[1], 1)
        result["best_min_source"] = best[0]
    elif result["current_temp_f"] is not None and not contract_not_started:
        # Fallback: use basic METAR temp
        result["best_min_f"] = float(result["current_temp_f"])
        result["best_min_source"] = "basic_metar"

    # Accumulate precipitation
    total_precip = 0.0
    rain_observed = False
    
    for p in parsed_list:
        if p.get("has_precip_weather"):
            rain_observed = True
        if p.get("precip_1hr_in") and p["precip_1hr_in"] > 0:
            total_precip += p["precip_1hr_in"]
            rain_observed = True
            result["precip_observations"].append({
                "time": p.get("obs_time"),
                "amount_in": p["precip_1hr_in"],
            })
        # Also check 6hr precip as a cross-reference
        if p.get("precip_6hr_in") and p["precip_6hr_in"] > 0:
            rain_observed = True

    result["total_precip_today"] = round(total_precip, 2)
    result["has_rained_today"] = rain_observed

    if contract_not_started:
        try:
            hours_until = (contract_midnight_local - now_local).total_seconds() / 3600
            if hours_until >= 0:
                result["hours_until_start"] = round(hours_until, 2)
        except Exception:
            pass

    return result
