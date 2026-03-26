"""
Edge calculation for Kalshi weather contracts.
Computes probability distributions and identifies profitable trades
for high temperature, daily rain, and monthly rain markets.
"""
import math
import re
import logging
import csv
import os
from datetime import datetime, date
from zoneinfo import ZoneInfo

from config import (
    CITIES,
    TRADING,
    ENABLED_MARKETS,
    bankers_round_half_up,
    PREDICTIONS_LOG_ENABLED,
    PREDICTIONS_LOG_PATH,
)
from analysis.trust_gate import evaluate_trust_gate

log = logging.getLogger(__name__)

_PREDICTION_COLUMNS = [
    "run_ts_utc",
    "city",
    "market_type",
    "contract_ticker",
    "event_ticker",
    "contract_date",
    "contract_subtitle",
    "side",
    "signal",
    "fair_prob",
    "fair_price",
    "market_price",
    "edge_cents",
    "edge_pct",
    "kelly_fraction",
    "forecast_high_f",
    "forecast_high_day_f",
    "forecast_high_day_source",
    "forecast_high_day_is_partial",
    "forecast_high_remaining_f",
    "uncertainty_f",
    "rain_probability",
    "precip_mean_in",
    "floor_strike",
    "cap_strike",
    "forecast_bias_f",
    "bias_from_today_f",
    "hard_block",
    "ghost_flags",
    "bias_flags",
    "hours_remaining",
    "model_spread_f",
    "cli_final",
    "station_used",
    "station_is_backup",
    "metar_age_min",
    "volume",
    "open_interest",
    "side_ask_cents",
    "spread_cents",
]


def _append_prediction_row(row: dict) -> None:
    """Append a single prediction row to the CSV log."""
    if not PREDICTIONS_LOG_ENABLED:
        return
    path = PREDICTIONS_LOG_PATH
    if not path:
        return
    try:
        dirpath = os.path.dirname(path)
        if dirpath:
            os.makedirs(dirpath, exist_ok=True)
        write_header = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=_PREDICTION_COLUMNS)
            if write_header:
                writer.writeheader()
            writer.writerow({k: row.get(k) for k in _PREDICTION_COLUMNS})
    except Exception as e:
        log.debug(f"Prediction log write failed: {e}")


def _to_float(val):
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    try:
        return float(str(val))
    except Exception:
        return None


def compute_source_agreement(weather_data: dict, edge: dict) -> dict:
    """
    Count independent data sources that agree with the recommended side.

    For high_temp contracts, checks whether each source's temperature reading
    falls inside (supports YES) or outside (supports NO) the contract bracket.

    Sources checked:
    1. NWS daily forecast high (from /forecast daytime period)
    2. NWS gridpoint max temp
    3. Ensemble weighted high (multi-model average)
    4. METAR observed max (if available, day in progress)
    5. CLI settlement (if final — ultimate truth)

    Returns: {
        "agreement_count": int,
        "total_sources": int,
        "agreeing_sources": list[str],
        "disagreeing_sources": list[str],
        "neutral_sources": list[str],   # sources not available
        "agreement_details": list[str], # human-readable per-source lines
    }
    """
    side = edge.get("side", "none")
    bounds = edge.get("strike_bounds") or {}
    kind = bounds.get("kind")
    market_type = edge.get("market_type", "")

    result = {
        "agreement_count": 0,
        "total_sources": 0,
        "agreeing_sources": [],
        "disagreeing_sources": [],
        "neutral_sources": [],
        "agreement_details": [],
    }

    if side == "none" or market_type != "high_temp" or not kind:
        return result

    def _temp_supports_yes(temp_f: float) -> bool:
        """Does this temperature reading fall inside the bracket (supports YES)?"""
        t_int = int(round(temp_f))
        if kind == "range":
            return bounds.get("low", -999) <= t_int <= bounds.get("high", 999)
        elif kind == "below":
            return t_int <= bounds.get("cap", 999)
        elif kind == "above":
            return t_int >= bounds.get("floor", -999)
        return False

    def _check_source(name: str, temp_f, detail_prefix: str):
        """Check a single source and record agreement/disagreement."""
        if temp_f is None:
            result["neutral_sources"].append(name)
            result["agreement_details"].append(f"  -- {detail_prefix}: not available")
            return
        try:
            temp_val = float(temp_f)
        except (TypeError, ValueError):
            result["neutral_sources"].append(name)
            result["agreement_details"].append(f"  -- {detail_prefix}: invalid value")
            return

        result["total_sources"] += 1
        supports_yes = _temp_supports_yes(temp_val)
        agrees = (supports_yes and side == "yes") or (not supports_yes and side == "no")

        if agrees:
            result["agreement_count"] += 1
            result["agreeing_sources"].append(name)
            icon = "Y"
            result["agreement_details"].append(
                f"  [{icon}] {detail_prefix}: {temp_val:.0f}F -> supports {side.upper()}"
            )
        else:
            result["disagreeing_sources"].append(name)
            icon = "N"
            other_side = "NO" if side == "yes" else "YES"
            result["agreement_details"].append(
                f"  [{icon}] {detail_prefix}: {temp_val:.0f}F -> supports {other_side}"
            )

    # 1. NWS daily forecast high
    hourly = weather_data.get("nws", {}).get("hourly", {})
    nws_daily = hourly.get("forecast_high_day", hourly.get("forecast_high_today"))
    nws_source = hourly.get("forecast_high_day_source", "hourly")
    _check_source("NWS Daily", nws_daily, f"NWS Forecast ({nws_source})")

    # 2. NWS gridpoint max
    gp = weather_data.get("nws", {}).get("gridpoint", {})
    gp_max = gp.get("max_temp_today")
    _check_source("Gridpoint Max", gp_max, "NWS Gridpoint Max")

    # 3. Ensemble — check INDIVIDUAL models, not just weighted average
    ens = weather_data.get("ensemble", {})
    ens_high = ens.get("weighted_high_f")
    ens_spread = ens.get("model_spread_f")
    model_highs = ens.get("model_highs", {})
    if model_highs:
        # Count individual models that agree with the side
        models_agree = 0
        models_disagree = 0
        disagree_names = []
        for model_name, model_temp in model_highs.items():
            try:
                supports_yes = _temp_supports_yes(float(model_temp))
                model_agrees = (supports_yes and side == "yes") or (not supports_yes and side == "no")
                if model_agrees:
                    models_agree += 1
                else:
                    models_disagree += 1
                    short_name = model_name.split("_")[0].upper()
                    disagree_names.append(f"{short_name}={model_temp:.0f}")
            except (TypeError, ValueError):
                pass
        total_models = models_agree + models_disagree
        spread_note = f", spread {ens_spread:.1f}F" if isinstance(ens_spread, (int, float)) else ""
        if models_disagree > 0:
            disagree_str = ", ".join(disagree_names)
            detail = f"Ensemble ({models_agree}/{total_models} models agree{spread_note}; DISAGREE: {disagree_str})"
        else:
            detail = f"Ensemble ({total_models} models{spread_note})"
        # Use weighted average for the agreement check but annotate disagreement
        _check_source("Ensemble", ens_high, detail)
        # Add model-level disagreement info to the result
        result["ensemble_models_agree"] = models_agree
        result["ensemble_models_total"] = total_models
        result["ensemble_models_disagree_detail"] = disagree_names
    else:
        spread_note = f", spread {ens_spread:.1f}F" if isinstance(ens_spread, (int, float)) else ""
        _check_source("Ensemble", ens_high, f"Ensemble (4 models{spread_note})")

    # 4. METAR observed max
    metar = weather_data.get("metar", {})
    if not metar.get("contract_not_started", False):
        obs_max = metar.get("best_max_f")
        obs_src = metar.get("best_max_source", "metar")
        _check_source("METAR Max", obs_max, f"Observed Max ({obs_src})")
    else:
        result["neutral_sources"].append("METAR Max")
        result["agreement_details"].append("  -- Observed Max: contract not started")

    # 5. CLI settlement
    cli = weather_data.get("nws", {}).get("cli", {})
    if cli.get("is_final") and cli.get("high_temp_f") is not None:
        _check_source("CLI", cli["high_temp_f"], "CLI Settlement (FINAL)")
    else:
        result["neutral_sources"].append("CLI")
        result["agreement_details"].append("  -- CLI Settlement: not yet available")

    return result


def _dedupe_by_event(edges: list[dict]) -> list[dict]:
    """Keep only the single strongest edge per event (mutually exclusive brackets)."""
    best = {}
    for e in edges:
        key = e.get("event_ticker") or e.get("contract_ticker")
        if not key:
            continue
        prev = best.get(key)
        if prev is None:
            best[key] = e
            continue
        # Prefer larger absolute edge; tie-breaker: higher fair_prob
        if abs(e.get("edge_cents", 0)) > abs(prev.get("edge_cents", 0)):
            best[key] = e
        elif abs(e.get("edge_cents", 0)) == abs(prev.get("edge_cents", 0)):
            if e.get("fair_prob", 0) > prev.get("fair_prob", 0):
                best[key] = e
    # Include any edges without an event key
    leftovers = [e for e in edges if not (e.get("event_ticker") or e.get("contract_ticker"))]
    return list(best.values()) + leftovers

# Month abbreviation to number for ticker parsing
_MON = {"JAN": 1, "FEB": 2, "MAR": 3, "APR": 4, "MAY": 5, "JUN": 6,
        "JUL": 7, "AUG": 8, "SEP": 9, "OCT": 10, "NOV": 11, "DEC": 12}


def _parse_temp_subtitle(subtitle: str | None) -> dict | None:
    """Parse Kalshi temp subtitle into explicit integer bounds.

    Examples:
      "27° or below" => {"kind": "below", "cap": 27}
      "30° or above" => {"kind": "above", "floor": 30}
      "38° to 39°"   => {"kind": "range", "low": 38, "high": 39}
    """
    if not subtitle:
        return None
    s = subtitle.lower()
    s = s.replace("°", "").replace("deg", "")
    s = s.replace("–", "-")
    s = s.replace("≤", "<=").replace("≥", ">=")

    m = re.search(r"(-?\d+)\s*(?:to|through|thru|-)\s*(-?\d+)", s)
    if m:
        a, b = int(m.group(1)), int(m.group(2))
        return {"kind": "range", "low": min(a, b), "high": max(a, b)}

    m = re.search(r"(?:<=)\s*(-?\d+)", s)
    if m:
        return {"kind": "below", "cap": int(m.group(1))}

    m = re.search(r"(?:>=)\s*(-?\d+)", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1))}

    m = re.search(r"(-?\d+)\s*(?:or below|or lower|or less|or under)", s)
    if m:
        return {"kind": "below", "cap": int(m.group(1))}

    m = re.search(r"(-?\d+)\s*(?:or colder|or cooler|or below)", s)
    if m:
        return {"kind": "below", "cap": int(m.group(1))}

    m = re.search(r"(-?\d+)\s*(?:or above|or higher|or more|or over)", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1))}

    m = re.search(r"(-?\d+)\s*(?:or warmer|or hotter|or above)", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1))}

    m = re.search(r"(?:less than|below)\s*(-?\d+)", s)
    if m:
        return {"kind": "below", "cap": int(m.group(1))}

    m = re.search(r"(?:greater than|above)\s*(-?\d+)", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1))}

    m = re.search(r"(-?\d+)\s*\+", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1))}

    # "be <51" or "be >58" patterns (from Kalshi title-style subtitles)
    m = re.search(r"<\s*(-?\d+)", s)
    if m:
        return {"kind": "below", "cap": int(m.group(1)) - 1}

    m = re.search(r">\s*(-?\d+)", s)
    if m:
        return {"kind": "above", "floor": int(m.group(1)) + 1}

    return None


def _bracket_label(subtitle: str | None) -> str:
    """Generate a compact, clear bracket label from a subtitle.

    Examples:
      "36° to 37°" => "36-37°F"
      "27° or below" => "≤27°F"
      "30° or above" => "≥30°F"
    Falls back to raw subtitle if parsing fails.
    """
    bounds = _parse_temp_subtitle(subtitle)
    if not bounds:
        return subtitle or ""
    kind = bounds.get("kind")
    if kind == "range":
        return f"{bounds['low']}-{bounds['high']}°F"
    elif kind == "below":
        return f"≤{bounds['cap']}°F"
    elif kind == "above":
        return f"≥{bounds['floor']}°F"
    return subtitle or ""


def _prob_for_bounds(prob_dist: dict[int, float], bounds: dict) -> float:
    """Compute probability mass for subtitle-derived bounds (inclusive)."""
    kind = bounds.get("kind")
    if kind == "range":
        low = bounds.get("low")
        high = bounds.get("high")
        if low is None or high is None:
            return 0.0
        return sum(p for t, p in prob_dist.items() if low <= t <= high)
    if kind == "below":
        cap = bounds.get("cap")
        if cap is None:
            return 0.0
        return sum(p for t, p in prob_dist.items() if t <= cap)
    if kind == "above":
        floor = bounds.get("floor")
        if floor is None:
            return 0.0
        return sum(p for t, p in prob_dist.items() if t >= floor)
    return 0.0


def parse_contract_date_from_ticker(ticker: str) -> date | None:
    """
    Parse contract date from Kalshi ticker.
    Examples: KXHIGHNY-26FEB08-B17.5 → 2026-02-08; KXHIGHNY-26FEB09-T28 → 2026-02-09;
              KXRAINSEAM-26FEB-5 → monthly (no day), returns 2026-02-01 for month key.
    Returns None if unparseable.
    """
    if not ticker or "-" not in ticker:
        return None
    part = ticker.split("-", 2)
    if len(part) < 2:
        return None
    # part[1] = "26FEB08" or "26FEB09" or "26FEB"
    match = re.match(r"(\d{2})(JAN|FEB|MAR|APR|MAY|JUN|JUL|AUG|SEP|OCT|NOV|DEC)(\d{2})?$", part[1].upper())
    if not match:
        return None
    yy, mon_abbr, dd = match.group(1), match.group(2), match.group(3)
    year = 2000 + int(yy)
    month = _MON.get(mon_abbr)
    if month is None:
        return None
    if dd is not None:
        day = int(dd)
        try:
            return date(year, month, day)
        except ValueError:
            return None
    # Monthly contract: no day, use first of month
    return date(year, month, 1)


# ── Temperature Distribution ──────────────────────────────────────

def build_temp_distribution(forecast_high: float,
                            uncertainty: float,
                            min_temp_int: int | None = None) -> dict[int, float]:
    """
    Build probability distribution over integer temperature outcomes.
    Uses normal distribution centered on forecast_high with given uncertainty (std dev in °F).
    Returns {integer_temp: probability} for all temps with > 0.1% probability.
    """
    if uncertainty < 0.5:
        uncertainty = 0.5  # minimum uncertainty

    probs = {}
    center = int(round(forecast_high))
    # Cover ±4 standard deviations
    spread = max(int(math.ceil(uncertainty * 4)), 5)

    total = 0.0
    for t in range(center - spread, center + spread + 1):
        if min_temp_int is not None and t < min_temp_int:
            continue
        # Probability that the true temp rounds to this integer
        # Integrate normal PDF from t-0.5 to t+0.5
        z_low = (t - 0.5 - forecast_high) / uncertainty
        z_high = (t + 0.5 - forecast_high) / uncertainty
        p = _norm_cdf(z_high) - _norm_cdf(z_low)
        if p > 0.001:
            probs[t] = p
            total += p

    # Normalize
    if total > 0:
        for t in probs:
            probs[t] /= total

    return probs


def _norm_cdf(x: float) -> float:
    """Standard normal CDF approximation."""
    return 0.5 * (1.0 + math.erf(x / math.sqrt(2.0)))


def _sanitize_price(price: float | None) -> float | None:
    """Normalize a market price to a usable float in (0, 100)."""
    if price is None:
        return None
    try:
        p = float(price)
    except (TypeError, ValueError):
        return None
    if p <= 0 or p >= 100:
        return None
    return p


def _calc_binary_edge(fair_prob: float,
                      yes_price: float | None,
                      no_price: float | None) -> dict:
    """
    Compute edge for a binary YES/NO market using executable prices.

    yes_price: cost to buy YES (ask)
    no_price: cost to buy NO (ask)
    """
    fair_price = fair_prob * 100
    yes_price = _sanitize_price(yes_price)
    no_price = _sanitize_price(no_price)

    edge_yes = fair_price - yes_price if yes_price is not None else None
    edge_no = (100 - fair_price) - no_price if no_price is not None else None

    # Choose the best positive edge
    choose_yes = edge_yes is not None and edge_yes > 0
    choose_no = edge_no is not None and edge_no > 0

    if choose_yes and (not choose_no or edge_yes >= edge_no):
        odds = (100 - yes_price) / yes_price
        kelly_f = (fair_prob * odds - (1 - fair_prob)) / odds if odds > 0 else 0
        kelly_f = max(kelly_f, 0) * TRADING["kelly_fraction"]
        return {
            "side": "yes",
            "fair_prob": fair_prob,
            "fair_price": fair_price,
            "market_price": yes_price,
            "yes_price": yes_price,
            "no_price": no_price,
            "edge_cents": edge_yes,
            "edge_pct": edge_yes / max(yes_price, 1) * 100,
            "kelly_fraction": kelly_f,
        }

    if choose_no:
        no_prob = 1 - fair_prob
        odds = (100 - no_price) / no_price
        kelly_f = (no_prob * odds - (1 - no_prob)) / odds if odds > 0 else 0
        kelly_f = max(kelly_f, 0) * TRADING["kelly_fraction"]
        return {
            "side": "no",
            "fair_prob": fair_prob,
            "fair_price": fair_price,
            "market_price": no_price,
            "yes_price": yes_price,
            "no_price": no_price,
            "edge_cents": edge_no,
            "edge_pct": edge_no / max(no_price, 1) * 100,
            "kelly_fraction": kelly_f,
        }

    return {
        "side": "none",
        "fair_prob": fair_prob,
        "fair_price": fair_price,
        "market_price": yes_price if yes_price is not None else (no_price if no_price is not None else 0),
        "yes_price": yes_price,
        "no_price": no_price,
        "edge_cents": 0,
        "edge_pct": 0,
        "kelly_fraction": 0,
    }


def estimate_uncertainty(data: dict) -> float:
    """
    Estimate forecast uncertainty in °F based on available data.
    
    data should contain:
    - hours_remaining: hours left in measurement window
    - model_spread_f: spread across ensemble models
    - afd_confidence: 'high', 'moderate', 'low' from AFD
    - has_metar_obs: whether we have actual observations today
    """
    locked_unc = data.get("locked_high_uncertainty_f")
    if locked_unc is not None:
        return max(float(locked_unc), 0.3)

    base = 1.5  # base uncertainty in °F

    hours = data.get("hours_remaining_peak")
    if hours is None:
        hours = data.get("hours_remaining", 12)
    # If contract date hasn't started, treat as full-day forecast uncertainty
    if data.get("metar", {}).get("contract_not_started"):
        hours = max(hours, 24)
    if hours <= 2:
        base = 0.5
    elif hours <= 4:
        base = 0.8
    elif hours <= 6:
        base = 1.2
    elif hours <= 8:
        base = 1.5
    else:
        base = 2.5

    spread = data.get("model_spread_f", 0)
    if spread > 5:
        base += 1.0
    elif spread > 3:
        base += 0.5

    # Bias inflation (if today's forecast was off, widen uncertainty for tomorrow)
    bias = data.get("bias_from_today_f")
    if isinstance(bias, (int, float)):
        base += min(abs(float(bias)) / 4.0, 2.0)

    confidence = data.get("afd_confidence", "moderate")
    if confidence == "low":
        base += 1.0
    elif confidence == "high":
        base -= 0.3

    if data.get("has_metar_obs") and hours <= 4:
        base *= 0.7  # observations reduce uncertainty late in day

    if data.get("temp_trend") == "falling" and hours <= 2:
        base *= 0.7

    # ── OVERNIGHT/WARM-FRONT HIGH DETECTION ──
    # If NWS hourly forecast shows temps RISING overnight (6PM-6AM),
    # the high may occur at an unusual time. Increase uncertainty.
    # Source: u/adulting_dude on r/Kalshi — "Highs can occur overnight
    # or in the evening if warm fronts are moving in."
    hourly_temps = data.get("nws", {}).get("hourly", {}).get("hourly_temps") or []
    if hourly_temps and len(hourly_temps) >= 4:
        try:
            evening_temps = []
            night_temps = []
            for t in hourly_temps:
                ts = t.get("time")
                tf = t.get("temp_f")
                if ts is None or tf is None:
                    continue
                from datetime import datetime as _dt
                try:
                    dt = _dt.fromisoformat(ts)
                    h = dt.hour
                    if 18 <= h <= 23:
                        evening_temps.append(tf)
                    elif 0 <= h <= 6:
                        night_temps.append(tf)
                except (ValueError, TypeError):
                    pass
            if evening_temps and night_temps:
                evening_max = max(evening_temps)
                night_max = max(night_temps)
                if night_max > evening_max:
                    # Warm front scenario — high may occur overnight
                    data["overnight_high_risk"] = True
                    data["overnight_high_night_f"] = night_max
                    data["overnight_high_evening_f"] = evening_max
                    base *= 1.3
        except Exception:
            pass

    return max(base, 0.3)


# ── Temperature Contract Edge ────────────────────────────────────

def calc_temp_bracket_edge(prob_dist: dict[int, float],
                           floor_strike: float | None,
                           cap_strike: float | None,
                           yes_price: float | None,
                           no_price: float | None) -> dict:
    """
    Calculate edge for a temperature bracket contract.
    
    floor_strike: lower bound (e.g., 26.5 means 27 and above included)
    cap_strike: upper bound (e.g., 27.5 means 27 and below included)
    
    For tail contracts:
    - floor_strike=None means "at or below cap_strike" 
    - cap_strike=None means "at or above floor_strike"
    
    yes_price: current YES ask in cents (0-100)
    no_price: current NO ask in cents (0-100)
    """
    # Determine which integer temps fall in this bracket
    # Kalshi bracket rules (verified from API subtitles):
    #   Regular bracket (both strikes): floor and cap are the two included integers
    #     B26.5 (floor=26, cap=27) = "26° to 27°" → includes 26 AND 27
    #   Tail LOW (cap only): covers temps BELOW cap_strike (NOT including it)  
    #     T22 (cap=22) = "21° or below" → includes ≤21, excludes 22
    #   Tail HIGH (floor only): covers temps ABOVE floor_strike (NOT including it)
    #     T29 (floor=29) = "30° or above" → includes ≥30, excludes 29
    fair_prob = 0.0
    for temp, prob in prob_dist.items():
        in_bracket = True
        if floor_strike is not None and cap_strike is not None:
            # Regular bracket: includes both endpoints
            if temp < int(floor_strike) or temp > int(cap_strike):
                in_bracket = False
        elif cap_strike is not None and floor_strike is None:
            # Tail LOW: strictly below cap_strike
            if temp >= int(cap_strike):
                in_bracket = False
        elif floor_strike is not None and cap_strike is None:
            # Tail HIGH: strictly above floor_strike
            if temp <= int(floor_strike):
                in_bracket = False
        if in_bracket:
            fair_prob += prob

    return _calc_binary_edge(fair_prob, yes_price, no_price)


def calc_temp_edge_from_subtitle(prob_dist: dict[int, float],
                                 subtitle: str | None,
                                 yes_price: float | None,
                                 no_price: float | None) -> tuple[dict, bool]:
    """Compute edge using subtitle-derived bounds. Returns (edge, parsed_ok)."""
    bounds = _parse_temp_subtitle(subtitle)
    if not bounds:
        return _calc_binary_edge(0.0, yes_price, no_price), False
    fair_prob = _prob_for_bounds(prob_dist, bounds)
    edge = _calc_binary_edge(fair_prob, yes_price, no_price)
    edge["strike_bounds"] = bounds
    return edge, True


# ── Rain Probability (Daily) ─────────────────────────────────────

def calc_rain_edge(rain_probability: float,
                   yes_price: float | None,
                   no_price: float | None) -> dict:
    """
    Calculate edge for daily rain YES/NO contract.
    
    rain_probability: our estimated probability of measurable rain (0-1)
    yes_price: current YES ask in cents
    no_price: current NO ask in cents
    """
    return _calc_binary_edge(rain_probability, yes_price, no_price)


def calc_precip_bracket_edge(total_precip_mean_in: float | None,
                             precip_prob: float | None,
                             floor_strike: float | None,
                             cap_strike: float | None,
                             yes_price: float | None,
                             no_price: float | None) -> dict:
    """Approximate precip amount distribution with a zero-inflated exponential.

    - precip_prob: probability of measurable precip (>=0.01") for the day.
    - total_precip_mean_in: expected total precip (inches) for the day.
    """
    if precip_prob is None:
        return _calc_binary_edge(0.0, yes_price, no_price)
    precip_prob = max(0.0, min(1.0, float(precip_prob)))

    # If no mean available, fall back to binary PoP for near-zero thresholds only
    if total_precip_mean_in is None:
        # Only safe tails when we don't have a mean.
        # - cap <= 0.01: "no measurable precip" ~ 1 - precip_prob
        # - floor <= 0.01 (cap None): "measurable precip" ~ precip_prob
        if cap_strike is not None and cap_strike <= 0.01 and floor_strike is None:
            return _calc_binary_edge(1.0 - precip_prob, yes_price, no_price)
        if floor_strike is not None and floor_strike <= 0.01 and cap_strike is None:
            return _calc_binary_edge(precip_prob, yes_price, no_price)
        return _calc_binary_edge(0.0, yes_price, no_price)

    mean = max(float(total_precip_mean_in), 0.0)
    if precip_prob <= 0.0:
        # No precip expected
        prob = 1.0 if (cap_strike is not None and cap_strike <= 0) else 0.0
        return _calc_binary_edge(prob, yes_price, no_price)

    # Conditional mean when it rains
    mu = mean / precip_prob if precip_prob > 0 else mean
    mu = max(mu, 0.01)

    def cdf(x: float) -> float:
        if x <= 0:
            return 1.0 - precip_prob
        return (1.0 - precip_prob) + precip_prob * (1.0 - math.exp(-x / mu))

    if floor_strike is None and cap_strike is None:
        return _calc_binary_edge(precip_prob, yes_price, no_price)

    if floor_strike is None:
        prob = cdf(cap_strike)
        return _calc_binary_edge(prob, yes_price, no_price)

    if cap_strike is None:
        prob = 1.0 - cdf(floor_strike)
        return _calc_binary_edge(prob, yes_price, no_price)

    if cap_strike < floor_strike:
        return _calc_binary_edge(0.0, yes_price, no_price)
    prob = cdf(cap_strike) - cdf(floor_strike)
    return _calc_binary_edge(prob, yes_price, no_price)


# ── Monthly Rain Threshold Edge ──────────────────────────────────

def calc_monthly_rain_edge(forecast_total_in: float,
                           uncertainty_in: float,
                           threshold_in: float,
                           is_over: bool,
                           yes_price: float | None,
                           no_price: float | None) -> dict:
    """
    Calculate edge for monthly rain threshold contract.
    Uses normal distribution over total monthly precip.
    
    forecast_total_in: expected total inches for the month
    uncertainty_in: std dev in inches
    threshold_in: contract threshold (e.g., 3.0 inches)
    is_over: True if contract is "over threshold", False for "under"
    yes_price: current YES ask in cents
    no_price: current NO ask in cents
    """
    if uncertainty_in < 0.1:
        uncertainty_in = 0.1

    z = (threshold_in - forecast_total_in) / uncertainty_in
    prob_under = _norm_cdf(z)
    prob_over = 1 - prob_under

    prob = prob_over if is_over else prob_under
    return _calc_binary_edge(prob, yes_price, no_price)


def calc_monthly_rain_bracket_edge(forecast_total_in: float,
                                   uncertainty_in: float,
                                   floor_strike: float | None,
                                   cap_strike: float | None,
                                   yes_price: float | None,
                                   no_price: float | None) -> dict:
    """
    Calculate edge for a monthly rain bracket contract (range).
    Uses normal distribution over total monthly precip.

    floor_strike: lower bound (inclusive)
    cap_strike: upper bound (inclusive)
    For tails:
      - floor_strike is None => under cap_strike
      - cap_strike is None => over floor_strike
    """
    if uncertainty_in < 0.1:
        uncertainty_in = 0.1

    if floor_strike is None and cap_strike is None:
        return _calc_binary_edge(0.0, yes_price, no_price)

    if floor_strike is None:
        # Under cap
        z = (cap_strike - forecast_total_in) / uncertainty_in
        prob = _norm_cdf(z)
        return _calc_binary_edge(prob, yes_price, no_price)

    if cap_strike is None:
        # Over floor
        z = (floor_strike - forecast_total_in) / uncertainty_in
        prob = 1 - _norm_cdf(z)
        return _calc_binary_edge(prob, yes_price, no_price)

    # Bracket range
    if cap_strike < floor_strike:
        return _calc_binary_edge(0.0, yes_price, no_price)
    z_low = (floor_strike - forecast_total_in) / uncertainty_in
    z_high = (cap_strike - forecast_total_in) / uncertainty_in
    prob = _norm_cdf(z_high) - _norm_cdf(z_low)
    return _calc_binary_edge(prob, yes_price, no_price)


# ── Position Sizing ──────────────────────────────────────────────

def size_position(edge_result: dict, bankroll: float) -> dict:
    """
    Determine position size from edge calculation.
    
    Returns dict with:
    - contracts: number of contracts to trade
    - cost: total cost in dollars
    - expected_profit: expected profit in dollars
    """
    if edge_result["side"] == "none" or edge_result["kelly_fraction"] <= 0:
        return {"contracts": 0, "cost": 0, "expected_profit": 0}

    kelly_f = edge_result["kelly_fraction"]
    price = edge_result.get("market_price", 0)

    # Kelly says bet this fraction of bankroll
    bet_amount = bankroll * kelly_f
    cost_per_contract = price / 100.0  # dollars

    if cost_per_contract <= 0:
        return {"contracts": 0, "cost": 0, "expected_profit": 0}

    contracts = int(bet_amount / cost_per_contract)
    max_pos = TRADING.get("max_position_per_contract", 0)
    if max_pos and max_pos > 0:
        contracts = min(contracts, max_pos)
    contracts = max(contracts, 0)

    total_cost = contracts * cost_per_contract
    expected_profit = contracts * (edge_result["edge_cents"] / 100.0)

    return {
        "contracts": contracts,
        "cost": round(total_cost, 2),
        "expected_profit": round(expected_profit, 2),
    }


# ── Agent-style output (risks, signal, confidence, why) per prior chat rules ──

def _enrich_edge_agent(o: dict, weather_data: dict) -> None:
    """
    Add risks, signal, confidence, why to each opportunity.
    Rules: Never assume provisional = final CLI; >60% prob edge to recommend buy;
    <40% = HOLD; ghost flags = high risk; pessimistic default.
    Sanity: strict ghost gap → NO TRADE; fair 99¢ vs mkt 5¢ → NO TRADE (date/strike bug).
    """
    llm_first = TRADING.get("_active_profile") == "llm_first"
    min_buy = TRADING.get("min_fair_prob_to_recommend_buy", 0.60)
    max_hold = TRADING.get("max_fair_prob_hold_threshold", 0.40)
    max_spread = TRADING.get("max_spread_cents", 0)
    min_vol = TRADING.get("min_volume", 0)
    min_oi = TRADING.get("min_open_interest", 0)
    min_side_book = TRADING.get("min_side_book_size", 0)
    ghost_gap_no_trade = TRADING.get("ghost_gap_no_trade_f", 10.0)
    suspicious_fair = TRADING.get("suspicious_fair_cents", 95)
    suspicious_mkt = TRADING.get("suspicious_market_cents", 15)
    illiquid_price = TRADING.get("illiquid_price_cents", 0)
    require_subtitle_parse = TRADING.get("require_subtitle_parse", False)
    max_uncertainty = TRADING.get("max_uncertainty_f", 0)
    max_model_spread = TRADING.get("max_model_spread_f", 0)
    allow_same_day_locked = TRADING.get("allow_same_day_locked_trades", False)
    monthly_min_day = TRADING.get("monthly_rain_min_day", 0)
    monthly_min_price = TRADING.get("monthly_rain_min_price_cents", 0)
    monthly_min_volume = TRADING.get("monthly_rain_min_volume", 0)
    monthly_min_oi = TRADING.get("monthly_rain_min_open_interest", 0)
    ghost_flags = list(weather_data.get("ghost_flags") or [])
    bias_flags = list(weather_data.get("bias_flags") or [])
    hours_left = weather_data.get("hours_remaining") is not None and weather_data.get("hours_remaining", 0) or 0
    cli = weather_data.get("nws", {}).get("cli", {})
    cli_final = bool(cli.get("is_final"))

    # Identify if this is today's contract date (local)
    is_today_contract = False
    try:
        city_key = weather_data.get("city")
        cdate_str = weather_data.get("contract_date")
        if city_key and cdate_str:
            tz = ZoneInfo(CITIES[city_key]["timezone"])
            today_local = datetime.now(tz).date()
            is_today_contract = date.fromisoformat(cdate_str) == today_local
    except Exception:
        pass

    risks = list(ghost_flags) + list(bias_flags)
    if is_today_contract and not cli_final and hours_left > 0 and o.get("market_type") == "high_temp":
        risks.append("Provisional data only (CLI not final for today)")
    # Overnight high warning
    if weather_data.get("overnight_high_risk") and o.get("market_type") == "high_temp":
        night_f = weather_data.get("overnight_high_night_f", "?")
        eve_f = weather_data.get("overnight_high_evening_f", "?")
        risks.append(f"Overnight high possible (night {night_f}°F > evening {eve_f}°F)")
    if isinstance(hours_left, (int, float)) and hours_left > 4 and o.get("market_type") == "high_temp":
        risks.append(f"{int(hours_left)} hours left in high-temp window")

    # 5-min ASOS spike detection — if intraday obs show higher max than T-group,
    # the CLI may settle higher. Brackets near the boundary are especially risky.
    fivemin_div = weather_data.get("5min_tgroup_divergence_f") or 0
    if fivemin_div >= 1.0 and o.get("market_type") == "high_temp":
        fivemin_max = weather_data.get("max_5min_f")
        tgroup_max = weather_data.get("observed_max_f")
        # Check if this contract's strike is near the uncertain boundary
        strike_lo = o.get("floor_strike") or o.get("strike")
        strike_hi = o.get("ceil_strike")
        if strike_lo is not None and tgroup_max is not None and fivemin_max is not None:
            try:
                strike_lo_f = float(strike_lo)
                # If the T-group max falls in this bracket but the 5-min max falls
                # in a different bracket, this trade has extra settlement risk
                if strike_hi is not None:
                    strike_hi_f = float(strike_hi)
                    tgroup_in_bracket = (strike_lo_f <= tgroup_max <= strike_hi_f)
                    fivemin_in_bracket = (strike_lo_f <= fivemin_max <= strike_hi_f)
                    if tgroup_in_bracket and not fivemin_in_bracket:
                        risks.append(
                            f"⚠️ SETTLEMENT RISK: T-group max ({tgroup_max:.1f}°F) in this bracket "
                            f"but 5-min ASOS ({fivemin_max}°F ±1°F) suggests CLI may settle in adjacent bracket"
                        )
                    elif not tgroup_in_bracket and fivemin_in_bracket:
                        risks.append(
                            f"⚠️ SETTLEMENT RISK: 5-min ASOS ({fivemin_max}°F ±1°F) in this bracket "
                            f"but T-group ({tgroup_max:.1f}°F) outside — uncertain which bracket CLI settles in"
                        )
            except (TypeError, ValueError):
                pass

    o["risks"] = risks

    # Strike mapping confidence - warn but allow fallback to numeric strikes
    if require_subtitle_parse and o.get("market_type") == "high_temp" and not o.get("strike_parse_ok", False):
        risks.append("Strike mapping from subtitle failed (using numeric strikes)")

    # Bias guard for tomorrow if today's forecast was way off
    if weather_data.get("bias_block_trade") and o.get("market_type") == "high_temp":
        if llm_first:
            risks.append("⚠️ FORECAST BIAS: Today's forecast bias exceeded safety threshold")
        else:
            o["signal"] = "NO TRADE - forecast bias"
            o["confidence"] = "Low"
            o["hard_block"] = True
            o["why"] = [
                "Today's forecast bias exceeded safety threshold.",
                "Skip trades until model bias stabilizes.",
            ]
        return

    # ── OVERPRICED BRACKET FADER (KevinLuWX strategy) ──
    # Day-before brackets priced >50¢ are systematically overpriced.
    # Even elite forecasters have 1-2°F MAE — no single 2°F bracket
    # deserves >50% confidence 24h out. Boost NO confidence on these.
    fade_threshold = TRADING.get("overpriced_bracket_fade_threshold", 0)
    is_tomorrow = weather_data.get("metar", {}).get("contract_not_started", False)
    contract_type = o.get("contract_type", "")
    bracket_width = o.get("bracket_width")
    if not contract_type:
        # classify early if not yet done
        from analysis.trust_gate import classify_contract_type, get_bracket_width
        contract_type = classify_contract_type(o)
        bracket_width = get_bracket_width(o)
        o["contract_type"] = contract_type
        o["bracket_width"] = bracket_width
    if (fade_threshold and is_tomorrow
            and contract_type == "bracket"
            and bracket_width is not None and bracket_width <= 2.0
            and side == "no"):
        yes_ask = o.get("yes_ask_cents") or o.get("market_price", 0)
        if yes_ask >= fade_threshold:
            o["overpriced_bracket_fade"] = True
            risks.append(
                f"Overpriced day-before bracket: YES ask {yes_ask}¢ ≥ {fade_threshold}¢ on {bracket_width:.0f}°F bracket "
                f"(KevinLuWX strategy — fade with NO)"
            )
            log.info(f"  🎯 Bracket fader triggered: {o.get('contract_ticker')} YES@{yes_ask}¢ → fade NO")

    # Uncertainty / spread: add as risk warnings but don't block
    if o.get("market_type") == "high_temp" and not weather_data.get("locked_high"):
        unc = o.get("uncertainty_f")
        if max_uncertainty and isinstance(unc, (int, float)) and unc > max_uncertainty:
            risks.append(f"High uncertainty ±{unc:.1f}°F")
        spread = weather_data.get("model_spread_f")
        if max_model_spread and isinstance(spread, (int, float)) and spread > max_model_spread:
            risks.append(f"Model spread {spread:.1f}°F (models disagree)")

    # Same-day pre-CLI trading: allow with risk warnings instead of blocking
    if is_today_contract and o.get("market_type") in ("high_temp", "daily_rain"):
        if not cli_final:
            locked_high = bool(weather_data.get("locked_high"))
            has_rained = bool(weather_data.get("metar", {}).get("has_rained_today"))
            if o.get("market_type") == "high_temp" and locked_high:
                risks.append("Same-day trade (locked high; CLI not final)")
            elif o.get("market_type") == "daily_rain" and has_rained:
                risks.append("Same-day trade (precip observed; CLI not final)")
            else:
                risks.append("Pre-CLI trade: settlement data not yet final")
        if ghost_flags:
            risks.append("Ghost flags present - data may be inconsistent")

    side = o.get("side", "none")
    fair_prob = o.get("fair_prob", 0)
    fair_price = o.get("fair_price", 0)
    fair_no_price = (100 - fair_price) if fair_price is not None else None
    side_prob = fair_prob if side == "yes" else (1 - fair_prob)
    fair_side_price = fair_price if side == "yes" else fair_no_price
    mkt = o.get("market_price", 50)
    edge_pct = o.get("edge_pct", 0)
    yes_ask = o.get("yes_ask_cents")
    no_ask = o.get("no_ask_cents")
    side_ask = o.get("side_ask_cents") or (yes_ask if side == "yes" else no_ask)
    side_bid = o.get("side_bid_cents")
    spread = o.get("spread_cents")
    side_ask_size = o.get("side_ask_size")
    volume = o.get("volume", 0) or 0
    open_interest = o.get("open_interest", 0) or 0

    if side == "none":
        o["signal"] = "NO TRADE"
        o["confidence"] = "Low"
        o["why"] = ["No edge after risks.", "Fair value too close to market."]
        return

    # ── TEMPERATURE AMBIGUITY WARNING ──
    # If the observed max came from a basic METAR (no T-group), the F→C→F
    # conversion can introduce ±1°F error. Warn when on a bracket boundary.
    if o.get("market_type") == "high_temp":
        metar = weather_data.get("metar", {})
        max_warning = metar.get("max_source_warning")
        if max_warning:
            risks.append(max_warning)
        # Check if any METAR observation has ambiguity spanning our bracket
        for p in metar.get("all_parsed", [])[:5]:
            amb = p.get("temp_ambiguity")
            if amb and amb.get("ambiguous"):
                bounds = o.get("strike_bounds") or {}
                if bounds.get("kind") == "range":
                    low_bound = bounds.get("low")
                    high_bound = bounds.get("high")
                    if low_bound is not None and high_bound is not None:
                        # Check if ambiguity range spans a bracket boundary
                        f_low = amb.get("f_low", 0)
                        f_high = amb.get("f_high", 0)
                        if f_low <= high_bound and f_high >= low_bound and f_low != f_high:
                            risks.append(
                                f"5-min temp ambiguity: {amb['reported_c']}°C → "
                                f"{f_low}-{f_high}°F spans bracket"
                            )
                            break  # one warning is enough

    # Liquidity checks - warn but don't block (we use maker orders)
    if side_ask is None or side_ask <= 0:
        # Still need SOME price to work with
        o["signal"] = "NO TRADE - no ask available"
        o["confidence"] = "Low"
        o["why"] = ["No executable ask for this side."]
        return
    if max_spread and spread is not None and spread > max_spread:
        risks.append(f"Wide spread {spread:.0f}¢")
    if min_vol and volume < min_vol:
        risks.append(f"Low volume ({volume})")
    if min_oi and open_interest < min_oi:
        risks.append(f"Low open interest ({open_interest})")
    if min_side_book and isinstance(side_ask_size, (int, float)) and side_ask_size > 0 and side_ask_size < min_side_book:
        risks.append(f"Thin book (size {int(side_ask_size)})")

    # Monthly rain liquidity guardrails (avoid very thin books)
    if o.get("market_type") == "monthly_rain":
        if monthly_min_volume and volume < monthly_min_volume:
            if llm_first:
                risks.append(f"⚠️ Monthly volume {volume} < {monthly_min_volume} (thin book)")
            else:
                o["signal"] = "NO TRADE - monthly low volume"
                o["confidence"] = "Low"
                o["hard_block"] = True
                o["why"] = [f"Monthly volume {volume} < {monthly_min_volume}.", "Thin book; edge likely noisy."]
                return
        if monthly_min_oi and open_interest < monthly_min_oi:
            if llm_first:
                risks.append(f"⚠️ Monthly OI {open_interest} < {monthly_min_oi} (thin book)")
            else:
                o["signal"] = "NO TRADE - monthly low open interest"
                o["confidence"] = "Low"
                o["hard_block"] = True
                o["why"] = [f"Monthly OI {open_interest} < {monthly_min_oi}.", "Thin book; edge likely noisy."]
                return

    # Monthly rain: skip very early in the month (too much uncertainty)
    if o.get("market_type") == "monthly_rain" and monthly_min_day:
        try:
            from datetime import datetime as _dt
            d_str = weather_data.get("contract_date")
            if d_str:
                day = _dt.fromisoformat(d_str).date().day
                if day < monthly_min_day:
                    if llm_first:
                        risks.append(f"⚠️ Early month (day {day} < {monthly_min_day})")
                    else:
                        o["signal"] = "NO TRADE - early month"
                        o["confidence"] = "Low"
                        o["hard_block"] = True
                        o["why"] = [f"Day {day} < {monthly_min_day} of month.", "Monthly totals too uncertain this early."]
                        return
        except Exception:
            pass

    # Illiquid microprice guard - warn only
    if illiquid_price and mkt <= illiquid_price:
        risks.append(f"Low market price {mkt:.0f}¢ (potentially illiquid)")

    # Monthly rain microprice guard (slightly higher)
    if o.get("market_type") == "monthly_rain" and monthly_min_price and mkt <= monthly_min_price:
        if llm_first:
            risks.append(f"⚠️ Monthly rain price {mkt:.0f}¢ <= {monthly_min_price}¢ (illiquid)")
        else:
            o["signal"] = "NO TRADE - illiquid monthly"
            o["confidence"] = "Low"
            o["hard_block"] = True
            o["why"] = [f"Monthly rain price {mkt:.0f}¢ <= {monthly_min_price}¢.", "Illiquid early-month monthly contracts are unreliable."]
            return

    # Strict ghost: large obs vs forecast gap → do not recommend (data unreliable)
    if ghost_flags and ghost_gap_no_trade:
        for g in ghost_flags:
            m = re.search(r"(\d+\.?\d*)°F", g)
            if m and float(m.group(1)) >= ghost_gap_no_trade:
                if llm_first:
                    risks.append(f"⚠️ Ghost gap ≥{ghost_gap_no_trade}°F (obs vs forecast gap)")
                    break
                else:
                    o["signal"] = "NO TRADE - ghost gap too large"
                    o["confidence"] = "Low"
                    o["hard_block"] = True
                    o["why"] = [f"Obs vs forecast gap ≥ {ghost_gap_no_trade}°F.", "Data unreliable for this contract."]
                    return

    # Station mismatch / stale METAR - warn instead of blocking
    if any("station mismatch" in g.lower() for g in ghost_flags):
        risks.append("Using backup station (settlement station mismatch risk)")
    if any("metar stale" in g.lower() for g in ghost_flags):
        risks.append("METAR observations are stale")

    # Suspect date/strike bug: fair ~100¢ vs market ~5¢ — warn but allow
    if fair_side_price is not None and fair_side_price >= suspicious_fair and mkt <= suspicious_mkt and not cli_final:
        risks.append(f"Large fair/market gap ({fair_side_price:.0f}¢ vs {mkt:.0f}¢) - verify strike mapping")

    # Monthly rain: huge fair vs low market often means wrong band/strike mapping
    if o.get("market_type") == "monthly_rain" and fair_side_price is not None and fair_side_price >= 95 and mkt <= 20:
        if llm_first:
            risks.append("⚠️ Fair ≥95¢ vs market ≤20¢ on monthly rain (possible band mapping error)")
        else:
            o["signal"] = "NO TRADE - monthly rain band mapping uncertain"
            o["confidence"] = "Low"
            o["hard_block"] = True
            o["why"] = ["Fair ≥95¢ vs market ≤20¢ on monthly rain.", "Verify Kalshi band thresholds (e.g. 5–6 in)."]
            return

    # Fee/slippage guard: require a minimum net edge after estimated fees
    min_edge_net = TRADING.get("min_edge_after_fees_cents", 0)
    fee_cents = TRADING.get("estimated_fee_cents", 0)
    if min_edge_net:
        try:
            net_edge = float(o.get("edge_cents", 0)) - float(fee_cents or 0)
        except (TypeError, ValueError):
            net_edge = 0
        if net_edge < min_edge_net:
            if llm_first:
                risks.append(f"⚠️ Net edge {net_edge:+.1f}¢ < {min_edge_net:.1f}¢ after fees")
            else:
                o["signal"] = "NO TRADE - edge too small after fees"
                o["confidence"] = "Low"
                o["why"] = [
                    f"Net edge {net_edge:+.1f}¢ < {min_edge_net:.1f}¢ after fees.",
                    "Too little margin for fees/slippage.",
                ]
                return

    if side_prob <= max_hold and not llm_first:
        o["signal"] = "HOLD"
        o["confidence"] = "Low"
        o["why"] = [f"Fair prob {side_prob*100:.0f}% <= 40%; pessimistic default.", "Insufficient edge to recommend buy."]
        return

    if side_prob < min_buy and not llm_first:
        o["signal"] = "NO TRADE - fair prob below threshold"
        o["confidence"] = "Low"
        o["why"] = [f"Fair prob {side_prob*100:.0f}% < {min_buy*100:.0f}% threshold."]
        return

    if ghost_flags and abs(edge_pct) < 10:
        risks.append("Ghost flags with small edge - data gap present")

    # Compute source agreement (for evidence scorecard / margin_of_safety filtering)
    source_agreement = compute_source_agreement(weather_data, o)
    o["source_agreement"] = source_agreement

    # Margin of safety: require minimum source agreement
    min_agreement = TRADING.get("min_source_agreement", 0)
    if min_agreement and o.get("market_type") == "high_temp":
        agree_count = source_agreement.get("agreement_count", 0)
        total_count = source_agreement.get("total_sources", 0)
        if total_count > 0 and agree_count < min_agreement:
            if llm_first:
                disagree_str = ', '.join(source_agreement.get('disagreeing_sources', []))
                risks.append(f"⚠️ Source agreement: {agree_count}/{total_count} (disagree: {disagree_str})")
            else:
                o["signal"] = "NO TRADE - insufficient source agreement"
                o["confidence"] = "Low"
                o["why"] = [
                    f"Only {agree_count}/{total_count} sources agree (need {min_agreement}+).",
                    f"Disagreeing: {', '.join(source_agreement.get('disagreeing_sources', []))}.",
                    "Multiple data sources must converge before betting.",
                ]
                return

    # Late-day preference info (for display, not a hard block)
    preferred_hours = TRADING.get("preferred_max_hours_remaining")
    if preferred_hours and isinstance(hours_left, (int, float)) and hours_left > preferred_hours:
        if o.get("market_type") == "high_temp":
            risks.append(f"{int(hours_left)}h remaining (prefer ≤{preferred_hours}h for higher certainty)")

    if side_prob >= min_buy:
        o["signal"] = f"BUY {side.upper()} @ ask ≤ {side_ask}¢"
        o["confidence"] = "High" if side_prob >= 0.55 else "Med"
        o["why"] = [
            f"Fair value {fair_side_price:.0f}¢ vs market {mkt:.0f}¢.",
            f"Fair prob {side_prob*100:.0f}% | Edge {edge_pct:+.1f}%.",
        ]
        if source_agreement.get("total_sources", 0) > 0:
            sa_line = f"Sources: {source_agreement['agreement_count']}/{source_agreement['total_sources']} agree."
            # Warn if individual ensemble models disagree (even if weighted avg agrees)
            ens_disagree = source_agreement.get("ensemble_models_disagree_detail", [])
            if ens_disagree:
                ens_agree = source_agreement.get("ensemble_models_agree", 0)
                ens_total = source_agreement.get("ensemble_models_total", 0)
                sa_line += f" ⚠️ Models: {ens_agree}/{ens_total} agree ({', '.join(ens_disagree)} disagree)."
            o["why"].append(sa_line)
        if ghost_flags:
            o["why"].append("Ghost flags present - size conservatively.")
        if hours_left <= 4 and o.get("market_type") == "high_temp":
            o["why"].append("Near end of high-temp window.")
        if not cli_final and is_today_contract:
            o["why"].append("Pre-CLI prediction trade.")
        return

    o["signal"] = f"BUY {side.upper()} @ ask ≤ {side_ask}¢"
    o["confidence"] = "Med"
    o["why"] = [f"Fair {fair_side_price:.0f}¢ vs mkt {mkt:.0f}¢.", f"Edge {edge_pct:+.1f}%."]
    if ghost_flags:
        o["why"].append("Ghost flags present - size conservatively.")


# ── Master Edge Analysis ─────────────────────────────────────────

def analyze_all_contracts(city_key: str, weather_by_date: dict, market_data: dict, return_all: bool = False):
    """
    Analyze all contracts for a city and return ranked trade opportunities.
    
    weather_by_date: { "YYYY-MM-DD": weather_data } so we use the correct forecast
    for each contract's date (today vs tomorrow). Prevents applying Feb 8 weather to Feb 9 contracts.
    market_data: {series_ticker: [{contract_ticker, floor_strike, cap_strike, yes_price, ...}]}
    
    Returns list of trade opportunities sorted by edge.
    """
    city = CITIES[city_key]
    tickers = city.get("kalshi_tickers", {})
    opportunities = []

    # ── High Temperature Contracts (per contract date) ──
    if ENABLED_MARKETS.get("high_temp", True) and "high_temp" in tickers and tickers["high_temp"] in market_data:
        for contract in market_data[tickers["high_temp"]]:
            ticker = contract.get("ticker", "")
            contract_date = parse_contract_date_from_ticker(ticker)
            if contract_date is None:
                log.debug(f"  Skip {ticker}: could not parse contract date")
                continue
            date_key = contract_date.isoformat()
            weather_data = weather_by_date.get(date_key)
            if weather_data is None:
                log.debug(f"  Skip {ticker}: no weather for date {date_key}")
                continue
            forecast_high = weather_data.get("best_forecast_high_f")
            if forecast_high is None:
                continue
            unc = estimate_uncertainty(weather_data)
            metar = weather_data.get("metar", {})
            observed_max = metar.get("best_max_f") if not metar.get("contract_not_started", False) else None
            min_temp_int = bankers_round_half_up(observed_max) if observed_max is not None else None
            dist = build_temp_distribution(forecast_high, unc, min_temp_int=min_temp_int)
            floor_s = contract.get("floor_strike")
            cap_s = contract.get("cap_strike")
            yes_price = contract.get("yes_ask") or contract.get("yes_price", 50)
            no_price = contract.get("no_ask")
            if not no_price and contract.get("yes_bid"):
                no_price = 100 - contract.get("yes_bid")
            if not no_price and contract.get("yes_price"):
                no_price = 100 - contract.get("yes_price")
            # Prefer subtitle-based parsing to avoid strike mapping bugs
            subtitle = (
                contract.get("subtitle")
                or contract.get("title")
                or contract.get("market_title")
                or contract.get("rules_primary")
                or ""
            )
            edge, parsed_ok = calc_temp_edge_from_subtitle(dist, subtitle, yes_price, no_price)
            if not parsed_ok:
                # Fallback to strike-based mapping (lower confidence)
                edge = calc_temp_bracket_edge(dist, floor_s, cap_s, yes_price, no_price)
            edge["strike_parse_ok"] = parsed_ok
            edge["contract_ticker"] = ticker
            edge["event_ticker"] = contract.get("event_ticker")
            edge["contract_subtitle"] = subtitle
            edge["bracket_label"] = _bracket_label(subtitle)
            edge["city"] = city_key
            edge["market_type"] = "high_temp"
            edge["contract_date"] = date_key
            edge["uncertainty_f"] = unc
            edge["forecast_high_f"] = forecast_high
            edge["bracket"] = f"{floor_s}-{cap_s}"
            edge["floor_strike"] = floor_s
            edge["cap_strike"] = cap_s
            edge["yes_ask_cents"] = contract.get("yes_ask") or 0
            edge["no_ask_cents"] = contract.get("no_ask") or 0
            edge["yes_bid_cents"] = contract.get("yes_bid") or 0
            edge["no_bid_cents"] = contract.get("no_bid") or 0
            edge["yes_spread_cents"] = contract.get("yes_spread")
            edge["no_spread_cents"] = contract.get("no_spread")
            edge["yes_bid_size"] = contract.get("yes_bid_size") or 0
            edge["yes_ask_size"] = contract.get("yes_ask_size") or 0
            edge["no_bid_size"] = contract.get("no_bid_size") or 0
            edge["no_ask_size"] = contract.get("no_ask_size") or 0
            edge["volume"] = contract.get("volume", 0)
            edge["open_interest"] = contract.get("open_interest", 0)
            # Side-specific liquidity
            if edge.get("side") == "yes":
                edge["side_bid_cents"] = edge["yes_bid_cents"] or None
                edge["side_ask_cents"] = edge["yes_ask_cents"] or None
                edge["spread_cents"] = edge.get("yes_spread_cents")
                edge["side_bid_size"] = edge.get("yes_bid_size")
                edge["side_ask_size"] = edge.get("yes_ask_size")
            elif edge.get("side") == "no":
                edge["side_bid_cents"] = edge["no_bid_cents"] or None
                edge["side_ask_cents"] = edge["no_ask_cents"] or None
                edge["spread_cents"] = edge.get("no_spread_cents")
                edge["side_bid_size"] = edge.get("no_bid_size")
                edge["side_ask_size"] = edge.get("no_ask_size")
            edge["_weather_data"] = weather_data
            opportunities.append(edge)

    # ── Daily Rain Contracts (per contract date) ──
    if ENABLED_MARKETS.get("daily_rain", True) and "daily_rain" in tickers and tickers["daily_rain"] in market_data:
        for contract in market_data[tickers["daily_rain"]]:
            ticker = contract.get("ticker", "")
            contract_date = parse_contract_date_from_ticker(ticker)
            if contract_date is None:
                continue
            date_key = contract_date.isoformat()
            weather_data = weather_by_date.get(date_key)
            if weather_data is None:
                continue
            rain_prob = weather_data.get("rain_probability")
            if rain_prob is None:
                continue
            # Use QPF / ensemble precip for bracketed precip contracts
            precip_mean = (
                _to_float(weather_data.get("nws", {}).get("gridpoint", {}).get("qpf_today_in"))
                or _to_float(weather_data.get("ensemble", {}).get("weighted_precip_in"))
            )
            yes_price = contract.get("yes_ask") or contract.get("yes_price", 50)
            no_price = contract.get("no_ask")
            if not no_price and contract.get("yes_bid"):
                no_price = 100 - contract.get("yes_bid")
            if not no_price and contract.get("yes_price"):
                no_price = 100 - contract.get("yes_price")
            floor_s = contract.get("floor_strike")
            cap_s = contract.get("cap_strike")
            if floor_s is not None or cap_s is not None:
                # If we lack a precip mean, only price "no measurable precip" or "measurable precip" tails.
                if precip_mean is None:
                    is_zero_tail = (floor_s is None and cap_s is not None and cap_s <= 0.01)
                    is_measurable_tail = (cap_s is None and floor_s is not None and floor_s <= 0.01)
                    if not (is_zero_tail or is_measurable_tail):
                        continue
                edge = calc_precip_bracket_edge(precip_mean, rain_prob, floor_s, cap_s, yes_price, no_price)
                edge["precip_model"] = "zero_inflated_exponential"
                edge["precip_mean_in"] = precip_mean
            else:
                edge = calc_rain_edge(rain_prob, yes_price, no_price)
            edge["contract_ticker"] = ticker
            edge["event_ticker"] = contract.get("event_ticker")
            edge["contract_subtitle"] = (
                contract.get("subtitle")
                or contract.get("title")
                or contract.get("market_title")
                or contract.get("rules_primary")
                or ""
            )
            edge["city"] = city_key
            edge["market_type"] = "daily_rain"
            edge["contract_date"] = date_key
            edge["rain_probability"] = rain_prob
            edge["floor_strike"] = floor_s
            edge["cap_strike"] = cap_s
            edge["yes_ask_cents"] = contract.get("yes_ask") or 0
            edge["no_ask_cents"] = contract.get("no_ask") or 0
            edge["yes_bid_cents"] = contract.get("yes_bid") or 0
            edge["no_bid_cents"] = contract.get("no_bid") or 0
            edge["yes_spread_cents"] = contract.get("yes_spread")
            edge["no_spread_cents"] = contract.get("no_spread")
            edge["yes_bid_size"] = contract.get("yes_bid_size") or 0
            edge["yes_ask_size"] = contract.get("yes_ask_size") or 0
            edge["no_bid_size"] = contract.get("no_bid_size") or 0
            edge["no_ask_size"] = contract.get("no_ask_size") or 0
            edge["volume"] = contract.get("volume", 0)
            edge["open_interest"] = contract.get("open_interest", 0)
            # Side-specific liquidity
            if edge.get("side") == "yes":
                edge["side_bid_cents"] = edge["yes_bid_cents"] or None
                edge["side_ask_cents"] = edge["yes_ask_cents"] or None
                edge["spread_cents"] = edge.get("yes_spread_cents")
                edge["side_bid_size"] = edge.get("yes_bid_size")
                edge["side_ask_size"] = edge.get("yes_ask_size")
            elif edge.get("side") == "no":
                edge["side_bid_cents"] = edge["no_bid_cents"] or None
                edge["side_ask_cents"] = edge["no_ask_cents"] or None
                edge["spread_cents"] = edge.get("no_spread_cents")
                edge["side_bid_size"] = edge.get("no_bid_size")
                edge["side_ask_size"] = edge.get("no_ask_size")
            edge["_weather_data"] = weather_data
            opportunities.append(edge)

    # ── Monthly Rain Contracts (use any same-month weather; prefer today) ──
    if ENABLED_MARKETS.get("monthly_rain", True) and "monthly_rain" in tickers and tickers["monthly_rain"] in market_data:
        # Use first available weather for this month (e.g. today's data for February)
        weather_data = None
        for dkey, w in weather_by_date.items():
            if w.get("monthly_precip_forecast_in") is not None or w.get("nws", {}).get("cli", {}).get("month_to_date_precip_in") is not None:
                weather_data = w
                break
        if weather_data is None:
            weather_data = next(iter(weather_by_date.values()), {})
        monthly_forecast = _to_float(weather_data.get("monthly_precip_forecast_in"))
        monthly_unc = _to_float(weather_data.get("monthly_precip_uncertainty_in", 1.0)) or 1.0
        cli = weather_data.get("nws", {}).get("cli", {})
        mtd_precip = _to_float(weather_data.get("monthly_precip_mtd_in") or cli.get("month_to_date_precip_in"))
        forecast_days = _to_float(weather_data.get("monthly_precip_forecast_days"))
        min_forecast_days = TRADING.get("monthly_rain_min_forecast_days", 0)
        require_full = TRADING.get("monthly_rain_require_full_coverage", False)
        contract_date_str = weather_data.get("contract_date")
        remaining_days = None
        required_days = None
        # Inflate uncertainty based on remaining days in month (we only have a few forecast days)
        try:
            from calendar import monthrange
            if contract_date_str:
                d = datetime.fromisoformat(contract_date_str).date()
                days_in_month = monthrange(d.year, d.month)[1]
                remaining_days = max(0, days_in_month - d.day)
                # Conservative uncertainty floor: 0.25" per remaining day (caps extreme early-month edges)
                monthly_unc = max(monthly_unc, remaining_days * 0.25)
                required_days = remaining_days + 1
        except Exception:
            pass
        if require_full and monthly_forecast is not None:
            if mtd_precip is None or required_days is None or forecast_days is None or forecast_days < required_days:
                log.warning(
                    f"  Monthly rain: insufficient coverage "
                    f"(forecast_days={forecast_days or 0} < required={required_days or '?'} "
                    f"or missing MTD). SKIPPING monthly rain contracts."
                )
                monthly_forecast = None
        if monthly_forecast is not None:
            # Guard: if we don't have enough forecast days, monthly pricing is too unstable
            if min_forecast_days and (forecast_days is None or forecast_days < min_forecast_days):
                log.warning(
                    f"  Monthly rain: only {forecast_days or 0} forecast days (<{min_forecast_days}). "
                    f"SKIPPING monthly rain contracts."
                )
                monthly_forecast = None

        if monthly_forecast is not None:
            # CRITICAL: Open-Meteo only gives 3 forecast days, NOT the full month.
            # Without month-to-date observations, the monthly total is wildly inaccurate.
            # We need actual observed MTD precip + remaining forecast to get a real estimate.
            
            if mtd_precip is not None:
                # Have month-to-date: add forecast remaining days
                adjusted_monthly = mtd_precip + monthly_forecast
                log.info(f"  Monthly rain: MTD={mtd_precip:.2f}in + forecast={monthly_forecast:.2f}in = {adjusted_monthly:.2f}in")
            else:
                # NO month-to-date data - only have 3 forecast days out of ~28.
                # We CANNOT meaningfully price a full-month contract with 3 days of data.
                # The 3-day forecast total (e.g., 0.01in) says nothing about the other 25 days.
                # Skip these contracts entirely to avoid phantom edges.
                log.warning(f"  Monthly rain: NO MTD data, 3-day forecast only ({monthly_forecast:.2f}in). "
                           f"SKIPPING monthly rain contracts (cannot price month with 3 days of data)")
                adjusted_monthly = None  # Signal to skip
            
            if adjusted_monthly is not None:
                for contract in market_data[tickers["monthly_rain"]]:
                    floor_s = contract.get("floor_strike")
                    cap_s = contract.get("cap_strike")
                    is_over = contract.get("is_over")
                    yes_price = contract.get("yes_ask") or contract.get("yes_price", 50)
                    no_price = contract.get("no_ask")
                    if not no_price and contract.get("yes_bid"):
                        no_price = 100 - contract.get("yes_bid")
                    if not no_price and contract.get("yes_price"):
                        no_price = 100 - contract.get("yes_price")

                    if floor_s is not None and cap_s is not None:
                        # Bracketed monthly range
                        edge = calc_monthly_rain_bracket_edge(
                            adjusted_monthly, monthly_unc, floor_s, cap_s, yes_price, no_price
                        )
                        edge["bracket"] = f"{floor_s}-{cap_s}"
                        edge["threshold_in"] = None
                    else:
                        # Over/under (tail) monthly contracts
                        if is_over is None:
                            if floor_s is not None and cap_s is None:
                                is_over = True
                            elif cap_s is not None and floor_s is None:
                                is_over = False
                        if is_over is None:
                            log.warning(f"  Monthly rain: cannot infer over/under for {contract.get('ticker','?')} - skipping")
                            continue
                        threshold = floor_s if is_over else (cap_s if cap_s is not None else floor_s)
                        if threshold is None:
                            continue
                        edge = calc_monthly_rain_edge(
                            adjusted_monthly, monthly_unc, threshold, is_over, yes_price, no_price
                        )
                        edge["threshold_in"] = threshold

                    edge["contract_ticker"] = contract.get("ticker", "")
                    edge["event_ticker"] = contract.get("event_ticker")
                    edge["contract_subtitle"] = contract.get("subtitle", "")
                    edge["city"] = city_key
                    edge["market_type"] = "monthly_rain"
                    # Monthly contracts don't have a specific day; use month start for grouping
                    try:
                        d = None
                        if contract_date_str:
                            d = datetime.fromisoformat(contract_date_str).date()
                        else:
                            d = parse_contract_date_from_ticker(edge["contract_ticker"])
                        if d:
                            edge["contract_month"] = f"{d.year:04d}-{d.month:02d}"
                            edge["contract_date"] = f"{d.year:04d}-{d.month:02d}-01"
                    except Exception:
                        pass
                    edge["monthly_forecast_in"] = adjusted_monthly
                    edge["forecast_days"] = forecast_days
                    edge["has_mtd_data"] = mtd_precip is not None
                    edge["yes_ask_cents"] = contract.get("yes_ask") or 0
                    edge["no_ask_cents"] = contract.get("no_ask") or 0
                    edge["yes_bid_cents"] = contract.get("yes_bid") or 0
                    edge["no_bid_cents"] = contract.get("no_bid") or 0
                    edge["yes_spread_cents"] = contract.get("yes_spread")
                    edge["no_spread_cents"] = contract.get("no_spread")
                    edge["yes_bid_size"] = contract.get("yes_bid_size") or 0
                    edge["yes_ask_size"] = contract.get("yes_ask_size") or 0
                    edge["no_bid_size"] = contract.get("no_bid_size") or 0
                    edge["no_ask_size"] = contract.get("no_ask_size") or 0
                    edge["volume"] = contract.get("volume", 0)
                    edge["open_interest"] = contract.get("open_interest", 0)
                    # Side-specific liquidity
                    if edge.get("side") == "yes":
                        edge["side_bid_cents"] = edge["yes_bid_cents"] or None
                        edge["side_ask_cents"] = edge["yes_ask_cents"] or None
                        edge["spread_cents"] = edge.get("yes_spread_cents")
                        edge["side_bid_size"] = edge.get("yes_bid_size")
                        edge["side_ask_size"] = edge.get("yes_ask_size")
                    elif edge.get("side") == "no":
                        edge["side_bid_cents"] = edge["no_bid_cents"] or None
                        edge["side_ask_cents"] = edge["no_ask_cents"] or None
                        edge["spread_cents"] = edge.get("no_spread_cents")
                        edge["side_bid_size"] = edge.get("no_bid_size")
                        edge["side_ask_size"] = edge.get("no_ask_size")
                    edge["_weather_data"] = weather_data
                    opportunities.append(edge)

    # Enrich each opportunity with agent-style risks, signal, confidence, why (from prior chat rules)
    run_ts = datetime.utcnow().isoformat()
    for o in opportunities:
        w = o.pop("_weather_data", None) or (next(iter(weather_by_date.values()), {}) if weather_by_date else {})
        _enrich_edge_agent(o, w)
        gate = evaluate_trust_gate(o, w)
        o.update(gate)
        _llm_first_mode = TRADING.get("_active_profile") == "llm_first"
        if _llm_first_mode:
            # In llm_first mode, trust gates become warnings, not blocks
            if gate.get("hard_block"):
                hard_reasons = gate.get("hard_reasons", [])
                for reason in hard_reasons:
                    if "no ask" not in reason.lower():  # keep "no ask" as real block
                        o.setdefault("risks", []).append(f"⚠️ Trust gate: {reason}")
                # Only block if it's truly no-ask
                if any("no ask" in r.lower() for r in hard_reasons):
                    o["signal"] = "NO TRADE - no ask available"
                    o["confidence"] = "Low"
        else:
            if gate.get("hard_block") and not (o.get("signal") or "").startswith("NO TRADE"):
                o["signal"] = "NO TRADE - trust gate"
                o["confidence"] = "Low"
            # Soft blocks are warnings only - don't override BUY signals
            elif gate.get("soft_block") and not (o.get("signal") or "").startswith(("NO TRADE", "BUY", "HOLD")):
                o["signal"] = "NO TRADE - soft gate"
                o["confidence"] = "Low"
        _append_prediction_row({
            "run_ts_utc": run_ts,
            "city": o.get("city"),
            "market_type": o.get("market_type"),
            "contract_ticker": o.get("contract_ticker"),
            "event_ticker": o.get("event_ticker"),
            "contract_date": o.get("contract_date"),
            "contract_subtitle": o.get("contract_subtitle"),
            "side": o.get("side"),
            "signal": o.get("signal"),
            "fair_prob": o.get("fair_prob"),
            "fair_price": o.get("fair_price"),
            "market_price": o.get("market_price"),
            "edge_cents": o.get("edge_cents"),
            "edge_pct": o.get("edge_pct"),
            "kelly_fraction": o.get("kelly_fraction"),
            "forecast_high_f": o.get("forecast_high_f") or w.get("best_forecast_high_f"),
            "forecast_high_day_f": w.get("forecast_high_day_f"),
            "forecast_high_day_source": w.get("forecast_high_day_source"),
            "forecast_high_day_is_partial": w.get("forecast_high_day_is_partial"),
            "forecast_high_remaining_f": w.get("forecast_high_remaining_f"),
            "uncertainty_f": o.get("uncertainty_f"),
            "rain_probability": o.get("rain_probability") or w.get("rain_probability"),
            "precip_mean_in": o.get("precip_mean_in"),
            "floor_strike": o.get("floor_strike"),
            "cap_strike": o.get("cap_strike"),
            "forecast_bias_f": w.get("forecast_bias_f"),
            "bias_from_today_f": w.get("bias_from_today_f"),
            "hard_block": bool(o.get("hard_block")),
            "ghost_flags": "; ".join(w.get("ghost_flags") or []),
            "bias_flags": "; ".join(w.get("bias_flags") or []),
            "hours_remaining": w.get("hours_remaining"),
            "model_spread_f": w.get("model_spread_f"),
            "cli_final": bool(w.get("nws", {}).get("cli", {}).get("is_final")),
            "station_used": w.get("station_used"),
            "station_is_backup": w.get("station_is_backup"),
            "metar_age_min": w.get("metar_age_min"),
            "volume": o.get("volume"),
            "open_interest": o.get("open_interest"),
            "side_ask_cents": o.get("side_ask_cents"),
            "spread_cents": o.get("spread_cents"),
        })

    # Sort: in SAFE mode, prefer threshold contracts over brackets.
    # Otherwise, sort by absolute edge.
    safe_mode = TRADING.get("_active_profile") == "safe"
    if safe_mode:
        def _safe_sort_key(x):
            ctype = x.get("contract_type", "unknown")
            # Threshold first (0), then bracket (1), then unknown (2)
            type_rank = 0 if ctype == "threshold" else (1 if ctype == "bracket" else 2)
            return (type_rank, -abs(x.get("edge_cents", 0)))
        opportunities.sort(key=_safe_sort_key)
    else:
        opportunities.sort(key=lambda x: abs(x.get("edge_cents", 0)), reverse=True)

    # Log ALL opportunities for diagnostics (before filtering). Use full ticker.
    min_edge = TRADING["min_edge_percent"]
    min_edge_cents = TRADING.get("min_edge_cents", 0)
    max_display_pct = TRADING.get("max_trusted_edge_pct", 150)
    if opportunities:
        log.info(f"  All edges (before filtering, min_edge_pct={min_edge}%, min_edge_cents={min_edge_cents}¢):")
        for o in opportunities[:15]:
            side = o.get("side", "?")
            ticker = o.get("contract_ticker", "?")
            edge_c = o.get("edge_cents", 0)
            edge_p = o.get("edge_pct", 0)
            # Cap displayed % so 8000% doesn't obscure the list; real value used for sorting
            display_pct = edge_p if abs(edge_p) <= max_display_pct else (max_display_pct if edge_p > 0 else -max_display_pct)
            fair = o.get("fair_price", 0)
            if side == "no" and fair is not None:
                fair = 100 - fair
            mkt = o.get("market_price", 0)
            sig = o.get("signal", "")
            _llm_log = TRADING.get("_active_profile") == "llm_first"
            will_pass = (
                (_llm_log or abs(edge_p) >= min_edge)
                and side != "none"
                and (_llm_log or not (sig or "").startswith("NO TRADE"))
            )
            pass_filter = "✅" if will_pass else "❌"
            blabel = o.get("bracket_label", "")
            blabel_str = f"  [{blabel}]" if blabel else ""
            log.info(
                f"    {pass_filter} {side.upper():3s} {ticker:28s}{blabel_str} "
                f"edge={edge_c:+6.1f}¢ ({display_pct:+5.1f}%) fair={fair:5.1f}¢ mkt={mkt:5.1f}¢ "
                f"signal={sig}"
            )
    else:
        log.warning("  No opportunities calculated at all")

    drop_reasons = {"edge_pct": 0, "edge_cents": 0, "side_none": 0, "no_trade": 0}
    filtered = []
    min_edge_cents = TRADING.get("min_edge_cents", 0)
    _llm_first = TRADING.get("_active_profile") == "llm_first"
    for o in opportunities:
        edge_p = abs(o.get("edge_pct", 0))
        edge_c = abs(o.get("edge_cents", 0))
        if not _llm_first and edge_p < min_edge:
            drop_reasons["edge_pct"] += 1
            continue
        if not _llm_first and min_edge_cents and edge_c < min_edge_cents:
            drop_reasons["edge_cents"] += 1
            continue
        if o.get("side") == "none":
            drop_reasons["side_none"] += 1
            continue
        if not _llm_first and (o.get("signal", "").startswith("NO TRADE")):
            drop_reasons["no_trade"] += 1
            continue
        filtered.append(o)
    if any(v > 0 for v in drop_reasons.values()):
        log.info(
            f"  Drops: edge<{min_edge}%={drop_reasons['edge_pct']}, "
            f"edge<{min_edge_cents}¢={drop_reasons['edge_cents']}, "
            f"side=none={drop_reasons['side_none']}, no_trade={drop_reasons['no_trade']}"
        )

    # Also filter out contracts under minimum price (favorite-longshot bias)
    min_price = TRADING.get("min_contract_price", 0)
    if min_price > 0:
        before_count = len(filtered)
        filtered = [o for o in filtered if o.get("market_price") and o["market_price"] >= min_price]
        if len(filtered) < before_count:
            log.info(f"  Price filter: removed {before_count - len(filtered)} contracts under {min_price}¢")

    # Illiquid microprice guard (1–2¢ markets are often stale)
    illiquid_price = TRADING.get("illiquid_price_cents", 0)
    if illiquid_price > 0:
        before_count = len(filtered)
        filtered = [o for o in filtered if o.get("market_price") and o["market_price"] > illiquid_price]
        if len(filtered) < before_count:
            log.info(f"  Illiquid filter: removed {before_count - len(filtered)} contracts at/under {illiquid_price}¢")

    # Monthly rain microprice guard (slightly higher threshold)
    monthly_min_price = TRADING.get("monthly_rain_min_price_cents", 0)
    if monthly_min_price > 0:
        before_count = len(filtered)
        filtered = [
            o for o in filtered
            if not (o.get("market_type") == "monthly_rain" and o.get("market_price") and o["market_price"] <= monthly_min_price)
        ]
        if len(filtered) < before_count:
            log.info(f"  Monthly price filter: removed {before_count - len(filtered)} monthly contracts ≤ {monthly_min_price}¢")

    # De-dupe mutually exclusive brackets per event
    if TRADING.get("dedupe_by_event", False) and filtered:
        filtered = _dedupe_by_event(filtered)

    log.info(f"  After filtering: {len(filtered)}/{len(opportunities)} pass {min_edge}% edge threshold")
    return (filtered, opportunities) if return_all else filtered
