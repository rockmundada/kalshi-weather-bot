"""
Main orchestrator for the Kalshi Weather Trading Bot.
Coordinates data collection, analysis, and trading across all cities and market types.
"""
import logging
import sys
import time
import argparse
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from config import (
    CITIES, TRADING, DATA_SOURCES, ENABLED_MARKETS,
    LLM_PROMPT_MODE, LLM_MAX_EDGES_IN_PROMPT, LLM_RUN_ONLY_IF_EDGES, LLM_RUN_MODE,
    LLM_ALLOW_NEW_TRADES,
)
from data_sources.metar import get_metar_data, fetch_awc_metars_multi
from data_sources.nws import get_all_nws_data, fetch_5min_max
from data_sources.iem import get_cli_iem
from data_sources.wethr import get_wethr_high_low, get_wethr_precip_mtd, get_wethr_nws_forecast
from data_sources.ensemble import get_ensemble_forecast
from analysis.edge import (
    analyze_all_contracts, build_temp_distribution, estimate_uncertainty, size_position,
    parse_contract_date_from_ticker,
)
from analysis.claude_ai import merge_analysis
from analysis.llm_ai import analyze_with_llm, analyze_with_llm_global
from analysis.validation import compute_validation_stats, get_calibration_badge, compute_nws_bias
from rules_catalog import rule_summary_for_market_type
from trading.kalshi_api import KalshiAPI
from alerts.telegram import send_trade_alert, send_recommendation, send_error, send_daily_summary

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", mode="a"),
    ],
)
log = logging.getLogger("main")


def _compute_month_anchors(weather_by_date: dict) -> dict:
    """Pick a single anchor date per month (earliest available date)."""
    month_to_anchor: dict = {}
    for dkey in sorted(weather_by_date.keys()):
        month = dkey[:7]
        if month not in month_to_anchor:
            month_to_anchor[month] = dkey
    return month_to_anchor


def _filter_market_data_for_date(market_data: dict, date_key: str, month_anchor_by_month: dict) -> dict:
    """Filter market data to only contracts matching a given contract date.

    Monthly contracts are included only on the month anchor date.
    """
    try:
        target_date = date.fromisoformat(date_key)
    except Exception:
        return {}
    month_str = f"{target_date.year:04d}-{target_date.month:02d}"
    is_month_anchor = month_anchor_by_month.get(month_str) == date_key

    filtered = {}
    for series_ticker, contracts in market_data.items():
        is_monthly_series = series_ticker.endswith("M")
        kept = []
        for c in contracts:
            ticker = c.get("ticker", "")
            cdate = parse_contract_date_from_ticker(ticker)
            if cdate is None:
                continue
            if is_monthly_series:
                if is_month_anchor and cdate.year == target_date.year and cdate.month == target_date.month:
                    kept.append(c)
            else:
                if cdate == target_date:
                    kept.append(c)
        if kept:
            filtered[series_ticker] = kept
    return filtered


def _group_edges_by_date(stat_edges: list, weather_by_date: dict, month_anchor_by_month: dict) -> dict:
    """Group edges by contract date for display/LLM alignment."""
    edges_by_date: dict = {}
    fallback_date = next(iter(weather_by_date.keys()), None)
    for e in stat_edges:
        date_key = e.get("contract_date")
        if e.get("market_type") == "monthly_rain":
            month = e.get("contract_month") or (date_key[:7] if date_key else None)
            if month and month_anchor_by_month.get(month):
                date_key = month_anchor_by_month[month]
        if not date_key:
            date_key = fallback_date
        if not date_key:
            continue
        edges_by_date.setdefault(date_key, []).append(e)
    return edges_by_date


class WeatherTradingBot:
    def __init__(self, mode: str = "recommend", include_today: bool = True, include_tomorrow: bool = False):
        """
        mode: 'recommend' for recommendations only, 'trade' for automated execution
        """
        self.mode = mode
        self.include_today = include_today
        self.include_tomorrow = include_tomorrow
        self.api = KalshiAPI()
        self.daily_trades = []
        self.daily_pnl = 0.0

    # ── Data Collection ───────────────────────────────────────────

    def collect_weather_data(self,
                             city_key: str,
                             target_date: date | None = None,
                             prefetched_metars: dict | None = None) -> dict:
        """Collect all weather data for a city for a given contract date.
        If target_date is None, uses today in the city's local timezone.
        """
        city = CITIES[city_key]
        tz = ZoneInfo(city["timezone"])
        now = datetime.now(tz)
        contract_date = target_date if target_date is not None else now.date()
        label = "today" if contract_date == now.date() else "tomorrow"
        log.info(f"Collecting weather data for {city_key} ({label} {contract_date}) at {now.strftime('%H:%M %Z')}")

        data = {"city": city_key, "timestamp": now.isoformat(), "contract_date": str(contract_date)}

        # METAR observations (for today only; for tomorrow no observed max yet)
        try:
            data["metar"] = get_metar_data(city_key, contract_date=contract_date, prefetched_raw_metars=prefetched_metars)
            log.info(f"  METAR: temp={data['metar'].get('current_temp_f', '?')}°F max={data['metar'].get('best_max_f', '?')}°F")
        except Exception as e:
            log.error(f"  METAR failed: {e}")
            data["metar"] = {}

        # NWS forecasts for this contract date
        try:
            data["nws"] = get_all_nws_data(city_key, contract_date=contract_date)
            hourly = data["nws"].get("hourly", {})
            fh = hourly.get("forecast_high_day", hourly.get("forecast_high_today", '?'))
            src = hourly.get("forecast_high_day_source", hourly.get("forecast_high_source", "hourly"))
            partial = hourly.get("forecast_high_day_is_partial")
            partial_tag = " (remaining-only)" if partial else ""
            log.info(f"  NWS: forecast_high={fh}°F (source={src}{partial_tag}) hours_left={hourly.get('hours_remaining', '?')}")
            # Log raw NWS source values for cross-checking
            raw_daily = hourly.get("_nws_daily_high")
            raw_gp = hourly.get("_nws_gridpoint_max")
            raw_hourly = hourly.get("_nws_hourly_max")
            hrs = hourly.get("_nws_hourly_hours_covered", 0)
            if raw_daily or raw_gp or raw_hourly:
                log.info(f"  NWS raw: daily={raw_daily}°F gp={raw_gp}°F hourly={raw_hourly}°F ({hrs}h coverage)")
            # Log tabular (website) cross-check
            tab_high = hourly.get("_tabular_high")
            tab_gap = hourly.get("_tabular_gap_f")
            tab_hrs = hourly.get("_tabular", {}).get("hours_covered", 0)
            if tab_high is not None:
                gap_str = f" gap={tab_gap}°F" if tab_gap is not None else ""
                log.info(f"  NWS tabular (website): {tab_high}°F ({tab_hrs}h coverage){gap_str}")
            else:
                tab_err = hourly.get("_tabular", {}).get("error")
                if tab_err:
                    log.warning(f"  NWS tabular unavailable: {tab_err}")
        except Exception as e:
            log.error(f"  NWS failed: {e}")
            data["nws"] = {}

        # 5-minute ASOS observations (intraday spike detection)
        # WARNING SIGNAL ONLY — not ground truth. Whole-°C precision has ±1°F rounding.
        try:
            data["obs_5min"] = fetch_5min_max(
                city["station_id"],
                contract_date=contract_date,
                tz_name=city["timezone"],
            )
            fivemin = data["obs_5min"]
            if fivemin.get("max_5min_f") is not None:
                tgroup_max = data.get("metar", {}).get("best_max_f")
                if tgroup_max is not None:
                    divergence = fivemin["max_5min_f"] - tgroup_max
                    fivemin["tgroup_divergence_f"] = round(divergence, 1)
                    if divergence >= 1.0:
                        log.warning(
                            f"  ⚠️ 5-min ASOS max: {fivemin['max_5min_f']}°F vs "
                            f"T-group max: {tgroup_max:.1f}°F — "
                            f"CLI may settle {divergence:.0f}°F higher (±1°F uncertainty)"
                        )
                    else:
                        log.info(f"  5-min ASOS max: {fivemin['max_5min_f']}°F (agrees with T-group {tgroup_max:.1f}°F)")
                else:
                    log.info(f"  5-min ASOS max: {fivemin['max_5min_f']}°F (no T-group to compare)")
            elif fivemin.get("error"):
                log.debug(f"  5-min ASOS: {fivemin['error']}")
        except Exception as e:
            log.debug(f"  5-min ASOS fetch failed: {e}")
            data["obs_5min"] = {}

        # IEM CLI (optional fallback for final/MTD values)
        try:
            data["iem_cli"] = get_cli_iem(city_key, contract_date=contract_date) or {}
        except Exception as e:
            log.debug(f"  IEM CLI failed: {e}")
            data["iem_cli"] = {}

        # Wethr.net (optional paid) — tighter highs + MTD precip
        try:
            data["wethr"] = get_wethr_high_low(city_key) or {}
            if data["wethr"].get("wethr_high_f") is not None:
                log.info(f"  Wethr: high={data['wethr'].get('wethr_high_f')}°F low={data['wethr'].get('wethr_low_f')}")
        except Exception as e:
            log.debug(f"  Wethr high/low failed: {e}")
            data["wethr"] = {}

        try:
            data["wethr_precip"] = get_wethr_precip_mtd(city_key) or {}
            if data["wethr_precip"].get("total_mtd") is not None:
                log.info(f"  Wethr precip MTD={data['wethr_precip'].get('total_mtd')} in (today {data['wethr_precip'].get('today_precip')} in)")
        except Exception as e:
            log.debug(f"  Wethr precip failed: {e}")
            data["wethr_precip"] = {}

        # If Wethr indicates precip today, mark rain detected
        try:
            if data.get("wethr_precip"):
                today_precip = data["wethr_precip"].get("today_precip")
                has_trace = data["wethr_precip"].get("has_trace")
                if (isinstance(today_precip, (int, float)) and today_precip > 0) or has_trace:
                    if "metar" in data and isinstance(data["metar"], dict):
                        data["metar"]["has_rained_today"] = True
        except Exception:
            pass

        try:
            data["wethr_nws"] = get_wethr_nws_forecast(city_key, contract_date=contract_date) or {}
        except Exception as e:
            log.debug(f"  Wethr NWS forecast failed: {e}")
            data["wethr_nws"] = {}
        # Fallback: if NWS hourly forecast missing, use Wethr NWS forecast
        if data.get("wethr_nws"):
            hourly = data.get("nws", {}).get("hourly", {})
            if hourly.get("forecast_high_day") is None and data["wethr_nws"].get("high") is not None:
                hourly["forecast_high_day"] = data["wethr_nws"].get("high")
                hourly["forecast_high_day_source"] = "wethr_nws"
                hourly["forecast_high_today"] = hourly.get("forecast_high_day")
            if hourly.get("forecast_low_today") is None and data["wethr_nws"].get("low") is not None:
                hourly["forecast_low_today"] = data["wethr_nws"].get("low")
            if hourly.get("hourly_temps") in (None, []) and data["wethr_nws"].get("hourly_temps"):
                hourly["hourly_temps"] = data["wethr_nws"].get("hourly_temps")

        # Normalize CLI "final" based on contract date (only settled for past dates).
        cli = data.get("nws", {}).get("cli", {})
        try:
            report_iso = cli.get("report_date_iso")
            if report_iso:
                report_date = date.fromisoformat(report_iso)
                today_local = datetime.now(ZoneInfo(city["timezone"])).date()
                cli["is_for_contract_date"] = (report_date == contract_date)
                cli["is_settled"] = (report_date < today_local)
                cli["is_final"] = bool(cli.get("is_for_contract_date") and cli.get("is_settled"))
        except Exception:
            pass

        # Ensemble models for this contract date
        try:
            data["ensemble"] = get_ensemble_forecast(city_key, target_date=contract_date)
            log.info(f"  Ensemble: weighted={data['ensemble'].get('weighted_high_f', '?')}°F spread={data['ensemble'].get('model_spread_f', '?')}°F")
        except Exception as e:
            log.error(f"  Ensemble failed: {e}")
            data["ensemble"] = {}

        # Build combined best estimates
        data["best_forecast_high_f"] = self._best_high_estimate(data)
        data["rain_probability"] = self._best_rain_estimate(data)
        data["monthly_precip_forecast_in"] = self._best_monthly_precip(data)
        data["monthly_precip_forecast_days"] = data.get("ensemble", {}).get("forecast_days")
        data["monthly_precip_uncertainty_in"] = 1.5  # default uncertainty

        # ── NWS Forecast Bias Correction (DailyDewPoint concept) ──
        # If we have enough historical data, correct for systematic NWS bias.
        # Source: u/hediwinn on r/Kalshi — bias-corrected nowcasting.
        try:
            nws_bias = compute_nws_bias(city_key)
            data["nws_bias"] = nws_bias
            if (nws_bias["n_samples"] >= 10
                    and nws_bias["confidence"] in ("medium", "high")
                    and nws_bias["bias_f"] is not None
                    and abs(nws_bias["bias_f"]) >= 0.3):
                correction = nws_bias["bias_f"]
                old_est = data["best_forecast_high_f"]
                if old_est is not None:
                    data["best_forecast_high_f"] = round(old_est - correction, 1)
                    data["nws_bias_correction_applied_f"] = round(correction, 2)
                    log.info(
                        f"  NWS bias correction: {correction:+.1f}°F "
                        f"(n={nws_bias['n_samples']}, conf={nws_bias['confidence']}) "
                        f"forecast {old_est:.1f}→{data['best_forecast_high_f']:.1f}°F"
                    )
        except Exception as e:
            log.debug(f"  NWS bias computation failed: {e}")

        # Copy useful fields to top level for edge calculations
        hourly = data.get("nws", {}).get("hourly", {})
        data["hours_remaining"] = hourly.get("hours_remaining", 12)
        data["hours_remaining_peak"] = hourly.get("hours_to_peak")
        data["forecast_high_remaining_f"] = hourly.get("forecast_high_remaining")
        data["forecast_high_day_f"] = hourly.get("forecast_high_day", hourly.get("forecast_high_today"))
        data["forecast_high_day_source"] = hourly.get("forecast_high_day_source", hourly.get("forecast_high_source"))
        data["forecast_high_day_is_partial"] = hourly.get("forecast_high_day_is_partial")
        data["tabular_high_f"] = hourly.get("_tabular_high")
        data["temp_trend"] = hourly.get("temp_trend")
        
        # Override hours_remaining if contract date hasn't started locally
        metar = data.get("metar", {})
        if metar.get("contract_not_started", False):
            log.info(f"  Contract date hasn't started locally - using forecast-only hours_left=24")
            data["hours_remaining"] = 24
        data["hours_until_start"] = metar.get("hours_until_start")
        
        data["model_spread_f"] = data.get("ensemble", {}).get("model_spread_f", 0)
        afd = data.get("nws", {}).get("afd", {})
        data["afd_confidence"] = afd.get("confidence_level", "moderate")
        data["has_metar_obs"] = bool(data.get("metar", {}).get("current_temp_f"))
        # Observed max for conditioning
        observed_max = metar.get("best_max_f")
        wethr_high = data.get("wethr", {}).get("wethr_high_f")
        if wethr_high is not None:
            try:
                observed_max = max(observed_max or -1e9, float(wethr_high))
            except (TypeError, ValueError):
                pass
        data["observed_max_f"] = observed_max

        # 5-min ASOS spike detection — copy key fields to top level
        obs_5min = data.get("obs_5min", {})
        data["max_5min_f"] = obs_5min.get("max_5min_f")
        data["5min_tgroup_divergence_f"] = obs_5min.get("tgroup_divergence_f", 0)
        data["5min_obs_count"] = obs_5min.get("obs_count", 0)
        # If 5-min data shows a higher max, flag uncertainty for edge calcs
        divergence = data.get("5min_tgroup_divergence_f") or 0
        if divergence >= 1.0:
            data["5min_spike_warning"] = (
                f"⚠️ 5-min ASOS obs max ({obs_5min.get('max_5min_f')}°F) exceeds "
                f"T-group max ({metar.get('best_max_f'):.1f}°F) by {divergence:.0f}°F — "
                f"CLI may settle higher (±1°F rounding uncertainty)"
            )
        else:
            data["5min_spike_warning"] = None

        # Heuristic: if forecast peak has passed and remaining forecast doesn't exceed observed max,
        # treat the high as effectively locked (tighten uncertainty).
        forecast_remaining = hourly.get("forecast_high_remaining")
        hours_to_peak = hourly.get("hours_to_peak")
        locked_high = False
        if observed_max is not None and forecast_remaining is not None:
            if hours_to_peak is not None and hours_to_peak <= 0.5 and forecast_remaining <= observed_max + 0.4:
                locked_high = True
            elif hourly.get("temp_trend") == "falling" and forecast_remaining <= observed_max + 0.4:
                locked_high = True
        if locked_high:
            data["locked_high"] = True
            data["locked_high_uncertainty_f"] = 0.6

        # Data priority per chat: CLI > T-group > Synoptic > API. Build summary and ghost flags.
        metar = data.get("metar", {})
        hourly = data.get("nws", {}).get("hourly", {})
        cli = data.get("nws", {}).get("cli", {})
        iem_cli = data.get("iem_cli", {})
        if cli.get("month_to_date_precip_in") is None and iem_cli.get("month_to_date_precip_in") is not None:
            try:
                cli["month_to_date_precip_in"] = float(iem_cli.get("month_to_date_precip_in"))
            except (TypeError, ValueError):
                pass
        # Wethr precip MTD overrides missing MTD (more timely than CLI/IEM)
        wethr_precip = data.get("wethr_precip", {})
        if cli.get("month_to_date_precip_in") is None and wethr_precip.get("total_mtd") is not None:
            try:
                cli["month_to_date_precip_in"] = float(wethr_precip.get("total_mtd"))
            except (TypeError, ValueError):
                pass
        data["monthly_precip_mtd_in"] = cli.get("month_to_date_precip_in")
        data["max_tgroup_f"] = metar.get("best_max_f") if metar.get("best_max_source") == "tgroup" else None
        data["max_synoptic_f"] = metar.get("best_max_f") if str(metar.get("best_max_source", "")).startswith("synoptic") else None
        data["api_display_f"] = metar.get("current_temp_f")  # rounded/display temp (least reliable)
        data["cli_high_f"] = cli.get("high_temp_f") if cli.get("is_final") else None
        data["cli_report_date"] = cli.get("report_date")  # e.g. "January 28 2026" for verification
        data["station_used"] = metar.get("station")
        data["station_is_backup"] = bool(metar.get("station_is_backup"))
        data["metar_age_min"] = metar.get("latest_obs_age_min")
        ghost_gap = TRADING.get("ghost_gap_threshold_f", 5.0)
        data["ghost_flags"] = []
        data["bias_flags"] = []
        data["forecast_bias_f"] = None
        if not metar.get("contract_not_started", False):
            is_today = (contract_date == now.date())
            # Current temp vs nearest NWS hourly forecast temp (today only)
            current_temp = metar.get("current_temp_f")
            hourly_temps = hourly.get("hourly_temps") or []
            if is_today and current_temp is not None and hourly_temps:
                nearest_temp = None
                nearest_delta = None
                for t in hourly_temps:
                    ts = t.get("time")
                    tf = t.get("temp_f")
                    if ts is None or tf is None:
                        continue
                    try:
                        dt = datetime.fromisoformat(ts)
                    except (ValueError, TypeError):
                        continue
                    delta = abs((dt - now).total_seconds())
                    if nearest_delta is None or delta < nearest_delta:
                        nearest_delta = delta
                        nearest_temp = tf
                if nearest_temp is not None:
                    gap = abs(current_temp - nearest_temp)
                    if gap >= ghost_gap:
                        data["ghost_flags"].append(f"Current vs NWS hourly gap {gap:.1f}°F")

            # Max-source disagreement (tgroup vs synoptic vs wethr vs CLI)
            max_sources = {}
            if data.get("max_tgroup_f") is not None:
                max_sources["tgroup"] = data["max_tgroup_f"]
            if data.get("max_synoptic_f") is not None:
                max_sources["synoptic"] = data["max_synoptic_f"]
            wethr_high = data.get("wethr", {}).get("wethr_high_f")
            if wethr_high is not None:
                try:
                    max_sources["wethr"] = float(wethr_high)
                except (TypeError, ValueError):
                    pass
            if data.get("cli_high_f") is not None:
                max_sources["cli"] = data["cli_high_f"]
            if len(max_sources) >= 2:
                max_gap = max(max_sources.values()) - min(max_sources.values())
                if max_gap >= ghost_gap:
                    sources = "/".join(sorted(max_sources.keys()))
                    data["ghost_flags"].append(f"Max source disagreement {max_gap:.1f}°F (sources: {sources})")
            # Station mismatch risk (backup station vs official settlement station)
            try:
                official_station = CITIES[city_key].get("station_id")
                used_station = metar.get("station")
                if official_station and used_station and used_station != official_station:
                    data["ghost_flags"].append(
                        f"Station mismatch: using {used_station} (official {official_station})"
                    )
            except Exception:
                pass

            # METAR staleness risk
            max_age_min = TRADING.get("max_metar_age_min", 120)
            age_min = metar.get("latest_obs_age_min")
            if isinstance(age_min, (int, float)) and max_age_min and age_min > max_age_min:
                data["ghost_flags"].append(f"METAR stale: {age_min:.0f} min old")

            # NWS forecast cross-check warnings (daily vs hourly vs gridpoint + tabular)
            nws_warnings = data.get("nws", {}).get("hourly", {}).get("_nws_cross_check_warnings", [])
            for w in nws_warnings:
                data["ghost_flags"].append(f"NWS cross-check: {w}")

            # Specific tabular cross-check ghost_flag (high priority - different data pipeline)
            tab_warning = data.get("nws", {}).get("hourly", {}).get("_tabular_cross_check_warning")
            if tab_warning:
                data["ghost_flags"].append(
                    f"⚠️ NWS API vs WEBSITE DISAGREE: {tab_warning} — website tabular may be more current"
                )

            # 5-min ASOS spike warning — intraday max may be higher than T-group
            spike_warn = data.get("5min_spike_warning")
            if spike_warn:
                data["ghost_flags"].append(spike_warn)

            # Forecast bias (observed max vs full-day NWS forecast) — not a ghost, but a reliability flag
            if is_today and metar.get("best_max_f") is not None and data.get("forecast_high_day_f") is not None:
                # Skip bias when NWS "day" forecast is actually remaining-hours only
                if data.get("forecast_high_day_is_partial"):
                    data["bias_flags"].append("NWS forecast is remaining-only; bias skipped")
                    data["forecast_bias_reliable"] = False
                else:
                    fgap = metar["best_max_f"] - data["forecast_high_day_f"]
                    data["forecast_bias_f"] = round(float(fgap), 1)
                    data["forecast_bias_reliable"] = True
                    bias_warn = TRADING.get("forecast_bias_warn_f", 5.0)
                    if bias_warn and abs(fgap) >= bias_warn:
                        data["bias_flags"].append(f"Observed vs NWS day forecast gap {fgap:+.1f}°F")
        log.info(f"  Best estimate: high={data['best_forecast_high_f']}°F rain_prob={data['rain_probability']}")
        return data

    def _best_high_estimate(self, data: dict) -> float | None:
        """Determine best high temperature estimate from all sources.

        PRIORITY (non-contrarian, NWS-anchored):
          1. CLI settlement value (final truth)
          2. IEM CLI (settled past dates only)
          3. If high is locked (day basically done): observed max
          4. NWS daily forecast high (/forecast daytime period) — authoritative
          5. Gridpoint max temp — second-best NWS product
          6. NWS hourly remaining max — only for intraday floor
          7. Ensemble: small ±1°F nudge on top of NWS, never the driver

        The result is always floored by observed max (can't go below what already happened).
        """
        # 1. CLI (settlement truth)
        cli = data.get("nws", {}).get("cli", {})
        if cli.get("is_final") and cli.get("high_temp_f") is not None:
            log.info(f"  Using CLI settlement value: {cli['high_temp_f']}°F")
            return float(cli["high_temp_f"])

        # 2. IEM CLI fallback (ONLY when date is settled; never same-day)
        iem_cli = data.get("iem_cli", {})
        if iem_cli.get("is_settled") and iem_cli.get("high_temp_f") is not None:
            log.info(f"  Using IEM CLI settlement value: {iem_cli['high_temp_f']}°F")
            return float(iem_cli["high_temp_f"])

        metar = data.get("metar", {})
        contract_not_started = metar.get("contract_not_started", False)
        hours_left = data.get("nws", {}).get("hourly", {}).get("hours_remaining", 12)
        if contract_not_started:
            hours_left = 24

        # Observed max so far (METAR + Wethr)
        observed_max = None if contract_not_started else metar.get("best_max_f")
        wethr_high = data.get("wethr", {}).get("wethr_high_f")
        if not contract_not_started and wethr_high is not None:
            try:
                observed_max = max(observed_max or -1e9, float(wethr_high))
            except (TypeError, ValueError):
                pass

        # 3. Day is over or high is locked → observed max IS the high
        if hours_left == 0 and observed_max is not None:
            log.info(f"  Day complete (hours_left=0), using observed high: {observed_max}°F")
            return float(observed_max)
        if data.get("locked_high") and observed_max is not None:
            log.info(f"  High locked, using observed max: {observed_max}°F")
            return float(observed_max)

        # 4. NWS daily forecast high (from /forecast daytime period) — ANCHOR
        hourly = data.get("nws", {}).get("hourly", {})
        nws_daily_high = None
        nws_source = hourly.get("forecast_high_day_source")
        if nws_source in ("nws_daily_forecast", "gridpoint_max"):
            nws_daily_high = hourly.get("forecast_high_day")
        elif not hourly.get("forecast_high_day_is_partial", True):
            # Full-day hourly coverage (rare but possible)
            nws_daily_high = hourly.get("forecast_high_day")

        # 5. Fallback: gridpoint max if daily forecast unavailable
        if nws_daily_high is None:
            gp = data.get("nws", {}).get("gridpoint", {})
            gp_max = gp.get("max_temp_today")
            if gp_max is not None:
                nws_daily_high = gp_max
                nws_source = "gridpoint_max"

        # 6. Last resort: hourly remaining max (known to underestimate)
        if nws_daily_high is None:
            nws_daily_high = hourly.get("forecast_high_remaining") or hourly.get("forecast_high_day")
            nws_source = "hourly_remaining"

        if nws_daily_high is None:
            # No NWS data at all — fall back to ensemble only
            ens = data.get("ensemble", {})
            ens_high = ens.get("weighted_high_f")
            if ens_high is not None:
                result = float(ens_high)
                if observed_max is not None:
                    result = max(result, observed_max)
                log.info(f"  No NWS data, using ensemble only: {result}°F")
                return result
            return float(observed_max) if observed_max is not None else None

        # 7. Ensemble nudge: allow ±1°F adjustment, clamped
        ens = data.get("ensemble", {})
        ens_high = ens.get("weighted_high_f")
        estimate = float(nws_daily_high)
        clamped_nudge = 0
        if ens_high is not None:
            nudge = float(ens_high) - estimate
            clamped_nudge = max(-1.0, min(1.0, nudge))
            estimate += clamped_nudge

        # 8. Intraday bias correction: if the current observed temp is warmer than
        # what NWS hourly predicted for this hour, carry that warm bias forward.
        # This catches scenarios like Philly where obs was already 42°F at 9am
        # while NWS hourly only showed 38°F → warm bias of +4°F means the high
        # will likely overshoot the forecast.
        if not contract_not_started and observed_max is not None:
            hourly_temps = hourly.get("hourly_temps") or []
            current_temp_f = data.get("metar", {}).get("current_temp_f")
            if current_temp_f is not None and hourly_temps:
                # Find the NWS hourly temp closest to current time
                city = CITIES[data.get("city", "")]
                tz = ZoneInfo(city["timezone"])
                now = datetime.now(tz)
                current_hour = now.hour
                # hourly_temps is a list of hourly temp values; find current hour's value
                nws_current_hour_temp = None
                for ht in hourly_temps:
                    if isinstance(ht, dict) and ht.get("hour") is not None:
                        if ht["hour"] == current_hour:
                            nws_current_hour_temp = ht.get("temp_f")
                            break
                    elif isinstance(ht, (int, float)):
                        # Simple list - skip, can't match to hour
                        break

                if nws_current_hour_temp is not None:
                    intraday_bias = current_temp_f - nws_current_hour_temp
                    if intraday_bias > 1.5:
                        # Warm bias detected - adjust estimate upward (decay the bias by 50%)
                        bias_adjust = intraday_bias * 0.5
                        old_estimate = estimate
                        estimate += bias_adjust
                        data["intraday_warm_bias_f"] = round(intraday_bias, 1)
                        data["intraday_bias_adjust_f"] = round(bias_adjust, 1)
                        log.warning(
                            f"  ⚠️ WARM BIAS: Current {current_temp_f:.1f}°F vs NWS hourly {nws_current_hour_temp:.0f}°F "
                            f"(bias +{intraday_bias:.1f}°F). Adjusting estimate {old_estimate:.1f}→{estimate:.1f}°F"
                        )

        # Floor: never below observed max
        if observed_max is not None:
            estimate = max(estimate, float(observed_max))

        log.info(f"  Best estimate: {estimate:.1f}°F (NWS {nws_source}={nws_daily_high}, ens_nudge={clamped_nudge:.1f})")

        # Store the projected high and all model highs for trust gate use
        data["projected_high_f"] = round(estimate, 1)

        return estimate

    def _best_rain_estimate(self, data: dict) -> float | None:
        """Estimate probability of rain today."""
        probs = []

        # Already rained?
        metar = data.get("metar", {})
        if metar.get("has_rained_today"):
            return 0.99  # near certain

        # NWS hourly precip probability
        hourly = data.get("nws", {}).get("hourly", {})
        if hourly.get("precip_probability_today") is not None:
            probs.append(hourly["precip_probability_today"] / 100.0)

        # Ensemble precip probability
        ens = data.get("ensemble", {})
        if ens.get("precip_probability") is not None:
            probs.append(ens["precip_probability"] / 100.0)

        # NWS QPF-based estimate
        gridpoint = data.get("nws", {}).get("gridpoint", {})
        if gridpoint.get("qpf_today_in") is not None:
            qpf = gridpoint["qpf_today_in"]
            if qpf > 0.1:
                probs.append(0.8)
            elif qpf > 0.01:
                probs.append(0.5)
            else:
                probs.append(0.1)

        if not probs:
            return None

        return sum(probs) / len(probs)

    def _best_monthly_precip(self, data: dict) -> float | None:
        """Estimate total monthly precipitation."""
        # Only return forecast for remaining days (not MTD) to avoid double-counting.
        ens = data.get("ensemble", {})
        return ens.get("monthly_precip_remaining")

    # ── Market Data Collection ────────────────────────────────────

    def collect_market_data(self, city_key: str) -> dict:
        """Collect all active market data for a city."""
        city = CITIES[city_key]
        tickers = city.get("kalshi_tickers", {})
        market_data = {}

        for market_type, series_ticker in tickers.items():
            if not ENABLED_MARKETS.get(market_type, True):
                continue
            try:
                contracts = self.api.get_active_contracts(series_ticker)
                if contracts:
                    market_data[series_ticker] = contracts
                    log.info(f"  {series_ticker}: {len(contracts)} contracts")
                else:
                    log.warning(f"  {series_ticker}: no active contracts found")
            except Exception as e:
                log.error(f"  {series_ticker} fetch failed: {e}")

        return market_data

    # ── Analysis Pipeline ─────────────────────────────────────────

    def analyze_city(self, city_key: str) -> dict:
        """Full analysis pipeline for one city."""
        log.info(f"\n{'='*60}")
        log.info(f"ANALYZING {city_key}")
        log.info(f"{'='*60}")

        city = CITIES[city_key]
        tz = ZoneInfo(city["timezone"])
        today_local = datetime.now(tz).date()
        tomorrow_local = today_local + timedelta(days=1)

        stations = [city["station_id"]] + city.get("backup_stations", [])
        prefetched = fetch_awc_metars_multi(stations, hours=DATA_SOURCES.get("awc_cache_hours", 24))

        weather_by_date = {}
        weather_data = None
        if self.include_today:
            weather_today = self.collect_weather_data(city_key, target_date=today_local, prefetched_metars=prefetched)
            weather_by_date[today_local.isoformat()] = weather_today
            weather_data = weather_today
        if self.include_tomorrow:
            weather_tomorrow = self.collect_weather_data(city_key, target_date=tomorrow_local, prefetched_metars=prefetched)
            weather_by_date[tomorrow_local.isoformat()] = weather_tomorrow
            if weather_data is None:
                weather_data = weather_tomorrow

        # Propagate today's forecast bias to tomorrow (skeptical guard)
        if self.include_today and self.include_tomorrow:
            today_key = today_local.isoformat()
            tomorrow_key = tomorrow_local.isoformat()
            today_w = weather_by_date.get(today_key, {})
            tomorrow_w = weather_by_date.get(tomorrow_key, {})
            bias = today_w.get("forecast_bias_f")
            if bias is not None and today_w.get("forecast_bias_reliable", True):
                try:
                    bias_val = float(bias)
                    bias_warn = TRADING.get("forecast_bias_warn_f", 5.0)
                    bias_block = TRADING.get("forecast_bias_no_trade_f", 7.0)
                    if abs(bias_val) >= bias_warn:
                        tomorrow_w["bias_from_today_f"] = bias_val
                        tomorrow_w.setdefault("bias_flags", []).append(
                            f"Today forecast bias {bias_val:+.1f}°F (apply skepticism)"
                        )
                    if abs(bias_val) >= bias_block:
                        tomorrow_w["bias_block_trade"] = True
                except (TypeError, ValueError):
                    pass
        # Attach calibration badge to all dates for this city
        calibration = get_calibration_badge(city_key)
        if calibration:
            for w in weather_by_date.values():
                w["calibration"] = calibration
        if weather_data is None:
            return {"city": city_key, "recommendations": [], "weather": {}}

        market_data = self.collect_market_data(city_key)

        if not market_data:
            log.warning(f"No market data for {city_key}, skipping")
            return {"city": city_key, "recommendations": [], "weather": weather_data}

        month_anchor_by_month = _compute_month_anchors(weather_by_date)

        # Statistical edge analysis (each contract uses weather for its contract date)
        stat_edges, all_edges = analyze_all_contracts(city_key, weather_by_date, market_data, return_all=True)
        log.info(f"Statistical analysis found {len(stat_edges)} opportunities")

        # Group edges per contract date (monthly goes to month anchor)
        stat_edges_by_date = _group_edges_by_date(stat_edges, weather_by_date, month_anchor_by_month)
        all_edges_by_date = _group_edges_by_date(all_edges, weather_by_date, month_anchor_by_month)

        # Statistical recommendations per date (fallback if LLM fails or skipped)
        stat_recommendations_by_date = {}
        for dkey, edges in stat_edges_by_date.items():
            merged = merge_analysis(edges, None)
            stat_recommendations_by_date[dkey] = [
                r for r in merged if (r.get("signal") or "").strip().startswith("BUY")
            ]

        # LLM analysis (per-date to avoid today/tomorrow mixing)
        recommendations_by_date: dict = {}
        final_recs: list = []
        llm_result = None
        llm_meta_by_date: dict = {}
        if LLM_RUN_MODE != "global_once":
            for dkey in sorted(weather_by_date.keys()):
                edges_for_date = stat_edges_by_date.get(dkey, [])
                if LLM_ALLOW_NEW_TRADES:
                    edges_for_date = []
                if LLM_RUN_ONLY_IF_EDGES and not edges_for_date and not LLM_ALLOW_NEW_TRADES:
                    log.info(f"LLM skipped (no filtered edges) for {city_key} {dkey}")
                    merged = merge_analysis(edges_for_date, None)
                    recs = [r for r in merged if (r.get("signal") or "").strip().startswith("BUY")]
                    recommendations_by_date[dkey] = recs
                    final_recs.extend(recs)
                    continue

                market_filtered = _filter_market_data_for_date(market_data, dkey, month_anchor_by_month)
                llm_result = analyze_with_llm(
                    city_key,
                    weather_by_date[dkey],
                    market_filtered,
                    prompt_mode=LLM_PROMPT_MODE,
                    edges=edges_for_date,
                    max_edges=LLM_MAX_EDGES_IN_PROMPT,
                )
                if llm_result:
                    provider = llm_result.get("_provider", "?")
                    log.info(
                        f"LLM analysis (provider={provider}) {city_key} {dkey}: "
                        f"confidence={llm_result.get('confidence', '?')}, "
                        f"trades={len(llm_result.get('trades', []))}"
                    )
                    llm_meta_by_date[dkey] = {
                        "provider": provider,
                        "confidence": llm_result.get("confidence"),
                        "reasoning": llm_result.get("reasoning"),
                        "trades": llm_result.get("trades", []) or [],
                    }
                else:
                    log.warning(f"LLM analysis failed for {city_key} {dkey}, using statistical only")
                    llm_meta_by_date[dkey] = {"provider": None, "trades": []}

                edges_for_merge = all_edges_by_date.get(dkey, []) if LLM_ALLOW_NEW_TRADES else stat_edges_by_date.get(dkey, [])
                merged = merge_analysis(edges_for_merge, llm_result)
                recs = [r for r in merged if (r.get("signal") or "").strip().startswith("BUY")]
                recommendations_by_date[dkey] = recs
                final_recs.extend(recs)

            log.info(f"Final recommendations (BUY only): {len(final_recs)} trades")
            for r in final_recs[:5]:
                log.info(
                    f"  {r.get('final_side', '?').upper()} {r.get('contract_ticker', '?')} "
                    f"edge={r.get('final_edge_cents', 0):.1f}¢ [{r.get('combined_confidence', '?')}]"
                )
        else:
            log.info("LLM deferred (global_once mode) - using statistical recommendations for now")
            # Use statistical recommendations per date until global LLM merges
            recommendations_by_date = stat_recommendations_by_date.copy()
            final_recs = [r for recs in recommendations_by_date.values() for r in recs]

        return {
            "city": city_key,
            "weather": weather_data,
            "weather_by_date": weather_by_date,
            "month_anchor_by_month": month_anchor_by_month,
            "market_data": market_data,
            "statistical_edges": stat_edges,
            "statistical_edges_by_date": stat_edges_by_date,
            "all_edges_by_date": all_edges_by_date,
            "stat_recommendations_by_date": stat_recommendations_by_date,
            "recommendations_by_date": recommendations_by_date,
            "claude_analysis": llm_result,
            "llm_meta_by_date": llm_meta_by_date,
            "recommendations": final_recs,
        }

    # ── Trade Execution ───────────────────────────────────────────

    def execute_trades(self, analysis: dict) -> list[dict]:
        """Execute recommended trades."""
        if self.mode != "trade":
            log.info("Recommendation mode - not executing trades")
            return []

        recs = analysis.get("recommendations", [])
        balance = self.api.get_balance()
        log.info(f"Account balance: ${balance:.2f}")

        # Check daily loss limit
        loss_check = self.api.check_daily_loss()
        if not loss_check.get("can_trade", True):
            log.warning("Daily loss limit reached, stopping")
            send_error("Daily loss limit reached - trading paused")
            return []

        executed = []
        for rec in recs:
            if rec.get("final_side") == "none":
                continue

            # Minimum edge filter (in cents)
            edge = abs(rec.get("final_edge_cents", 0))
            min_edge_cents = TRADING.get("min_edge_cents", 0)
            if min_edge_cents and edge < min_edge_cents:
                log.debug(f"Skipping {rec.get('contract_ticker', '?')}: edge {edge:.1f}¢ < min {min_edge_cents}¢")
                continue

            # Size the position
            sizing = size_position(rec, balance)
            if sizing["contracts"] <= 0:
                continue

            ticker = rec["contract_ticker"]
            side = rec["final_side"]
            side_price = rec.get("market_price", 50)
            if not side_price:
                log.debug(f"Skipping {ticker}: missing side ask price")
                continue

            # Re-quote market just before placing order to avoid stale edges
            try:
                latest = self.api.get_market(ticker)
            except Exception:
                latest = None
            if latest:
                yes_ask = latest.get("yes_ask")
                no_ask = latest.get("no_ask")
                if side == "yes":
                    side_price = yes_ask or side_price
                else:
                    side_price = no_ask or side_price

                # Skip if no executable ask
                if not side_price or side_price <= 0:
                    log.debug(f"Skipping {ticker}: no executable ask on re-quote")
                    continue

                # Check price moved beyond recommendation
                max_ask = rec.get("side_ask_cents")
                if isinstance(max_ask, (int, float)) and side_price > max_ask:
                    log.debug(f"Skipping {ticker}: ask {side_price}¢ > recommended {max_ask}¢")
                    continue

                # Ensure edge still meets minimum at current price
                fair_yes = rec.get("fair_price")
                if fair_yes is not None:
                    fair_no = 100 - fair_yes
                    fair_side = fair_yes if side == "yes" else fair_no
                    current_edge = fair_side - side_price
                    if min_edge_cents and current_edge < min_edge_cents:
                        log.debug(f"Skipping {ticker}: current edge {current_edge:.1f}¢ < min {min_edge_cents}¢")
                        continue

            # Place maker order
            result = self.api.place_maker_order(ticker, side, sizing["contracts"], side_price)

            if result:
                trade = {**rec, **sizing, "order_result": result}
                executed.append(trade)
                self.daily_trades.append(trade)
                send_trade_alert(trade)
                log.info(f"EXECUTED: {side.upper()} {sizing['contracts']}x {ticker} @ ~{side_price}¢")

                # Update balance estimate
                balance -= sizing["cost"]
            else:
                log.error(f"Order failed for {ticker}")

            time.sleep(0.5)  # rate limiting

        return executed

    # ── Main Loop ─────────────────────────────────────────────────

    def run_single_pass(self, city_keys: list[str] | None = None) -> dict:
        """Run one analysis pass across all cities."""
        try:
            compute_validation_stats()
        except Exception as e:
            log.debug(f"Validation stats update failed: {e}")
        results = {}
        keys = city_keys or list(CITIES.keys())
        for city_key in keys:
            try:
                analysis = self.analyze_city(city_key)
                results[city_key] = analysis

                if LLM_RUN_MODE != "global_once":
                    # Send recommendations per contract date
                    recs_by_date = analysis.get("recommendations_by_date", {})
                    weather_by_date = analysis.get("weather_by_date", {})
                    for dkey in sorted(recs_by_date.keys()):
                        recs = recs_by_date.get(dkey, [])
                        weather = weather_by_date.get(dkey, {})
                        weather_str = (
                            f"{dkey} | High forecast: {weather.get('best_forecast_high_f', '?')}°F | "
                            f"Rain prob: {weather.get('rain_probability', '?')}"
                        )
                        send_recommendation(recs, f"{city_key} {dkey}", weather_str)

                    # Execute if in trading mode
                    if self.mode == "trade":
                        self.execute_trades(analysis)

            except Exception as e:
                log.error(f"Error processing {city_key}: {e}", exc_info=True)
                send_error(f"Error processing {city_key}: {e}")

            time.sleep(1)  # rate limiting between cities

        if LLM_RUN_MODE == "global_once":
            # Build global analysis bundle
            analysis_bundle = []
            for city_key, analysis in results.items():
                weather_by_date = analysis.get("weather_by_date", {})
                month_anchor_by_month = analysis.get("month_anchor_by_month", {})
                market_data = analysis.get("market_data", {})
                stat_edges_by_date = analysis.get("statistical_edges_by_date", {})
                all_edges_by_date = analysis.get("all_edges_by_date", {})
                for dkey, w in weather_by_date.items():
                    market_filtered = _filter_market_data_for_date(market_data, dkey, month_anchor_by_month)
                    analysis_bundle.append({
                        "city": city_key,
                        "contract_date": dkey,
                        "weather": w,
                        "market_data": market_filtered,
                        "statistical_edges": [] if LLM_ALLOW_NEW_TRADES else stat_edges_by_date.get(dkey, []),
                        "all_edges": all_edges_by_date.get(dkey, []),
                    })

            llm_global = None
            if not LLM_ALLOW_NEW_TRADES:
                log.info("LLM skipped (LLM_ALLOW_NEW_TRADES disabled)")
            else:
                # Always run when LLM is enabled (ignore LLM_RUN_ONLY_IF_EDGES)
                llm_global = analyze_with_llm_global(
                    analysis_bundle,
                    prompt_mode=LLM_PROMPT_MODE,
                    max_edges=LLM_MAX_EDGES_IN_PROMPT,
                )

            # Store global LLM result for TOP BETS display
            self._llm_global = llm_global

            if llm_global:
                provider = llm_global.get("_provider", "?")
                log.info(f"Global LLM analysis (provider={provider}) trades={len(llm_global.get('trades', []))}")
            else:
                log.warning("Global LLM analysis failed, using statistical only")

            # Build ticker -> contract index
            ticker_index = {}
            for city_key, analysis in results.items():
                month_anchor_by_month = analysis.get("month_anchor_by_month", {})
                for series_ticker, contracts in analysis.get("market_data", {}).items():
                    for c in contracts:
                        t = c.get("ticker")
                        if t:
                            cdate = parse_contract_date_from_ticker(t)
                            if cdate is None:
                                continue
                            if series_ticker.endswith("M"):
                                month_str = f"{cdate.year:04d}-{cdate.month:02d}"
                                date_key = month_anchor_by_month.get(month_str)
                            else:
                                date_key = cdate.isoformat()
                            ticker_index[t] = {
                                "city": city_key,
                                "date_key": date_key,
                                "contract": c,
                                "series_ticker": series_ticker,
                            }
            # Build ticker sets per city/date (filtered or full depending on LLM mode)
            tickers_by_city_date = {}
            for city_key, analysis in results.items():
                edges_by_date = analysis.get("all_edges_by_date", {}) if LLM_ALLOW_NEW_TRADES else analysis.get("statistical_edges_by_date", {})
                for dkey, edges in edges_by_date.items():
                    tickers_by_city_date[(city_key, dkey)] = {e.get("contract_ticker") for e in edges}

            # Group global LLM trades by city/date
            trades_by_city_date = {}
            if llm_global:
                for t in llm_global.get("trades", []) or []:
                    ticker = t.get("contract_ticker")
                    info = ticker_index.get(ticker)
                    if not info:
                        log.warning(f"LLM trade ticker not found in market data: {ticker}")
                        continue
                    city_key = info["city"]
                    date_key = info.get("date_key")
                    if ticker not in tickers_by_city_date.get((city_key, date_key), set()):
                        log.info(f"LLM trade ignored (not in eligible edges): {ticker}")
                        continue
                    trades_by_city_date.setdefault((city_key, date_key), []).append(t)

            # Apply per-city/date results and send recommendations
            for city_key, analysis in results.items():
                recommendations_by_date = {}
                weather_by_date = analysis.get("weather_by_date", {})
                stat_edges_by_date = analysis.get("statistical_edges_by_date", {})
                all_edges_by_date = analysis.get("all_edges_by_date", {})
                stat_recs_by_date = analysis.get("stat_recommendations_by_date", {})
                for dkey in sorted(weather_by_date.keys()):
                    stat_edges = all_edges_by_date.get(dkey, []) if LLM_ALLOW_NEW_TRADES else stat_edges_by_date.get(dkey, [])
                    llm_trades = trades_by_city_date.get((city_key, dkey), [])
                    if llm_global and llm_trades:
                        llm_city = {"trades": llm_trades}
                        merged = merge_analysis(stat_edges, llm_city)
                        # Allow Claude-only trades through (don't filter them out)
                        # merged = [r for r in merged if r.get("combined_confidence") != "claude_only"]
                        merged.sort(key=lambda x: abs(x.get("final_edge_cents", 0)), reverse=True)
                        final_recs = [r for r in merged if (r.get("signal") or "").strip().startswith("BUY")]
                        recommendations_by_date[dkey] = final_recs
                    else:
                        recommendations_by_date[dkey] = stat_recs_by_date.get(dkey, [])

                analysis["claude_analysis"] = None
                llm_meta_by_date = {}
                provider = llm_global.get("_provider", "?") if llm_global else None
                analysis["recommendations_by_date"] = recommendations_by_date
                analysis["recommendations"] = [r for recs in recommendations_by_date.values() for r in recs]
                for dkey in weather_by_date.keys():
                    llm_trades = trades_by_city_date.get((city_key, dkey), []) if llm_global else []
                    llm_meta_by_date[dkey] = {
                        "provider": provider,
                        "confidence": llm_global.get("confidence") if llm_global else None,
                        "reasoning": llm_global.get("reasoning") if llm_global else None,
                        "trades": llm_trades or [],
                    }
                analysis["llm_meta_by_date"] = llm_meta_by_date

                for dkey, recs in recommendations_by_date.items():
                    weather = weather_by_date.get(dkey, {})
                    weather_str = (
                        f"{dkey} | High forecast: {weather.get('best_forecast_high_f', '?')}°F | "
                        f"Rain prob: {weather.get('rain_probability', '?')}"
                    )
                    send_recommendation(recs, f"{city_key} {dkey}", weather_str)

                if self.mode == "trade":
                    self.execute_trades(analysis)

        return results

    def run_loop(self, interval_minutes: int = 30):
        """Run continuously with specified interval."""
        log.info(f"Starting bot in {self.mode} mode, interval={interval_minutes}min")
        send_error(f"🤖 Bot started in {self.mode} mode")  # startup notification

        while True:
            try:
                log.info(f"\n{'#'*60}")
                log.info(f"NEW PASS at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
                log.info(f"{'#'*60}")

                self.run_single_pass()

            except KeyboardInterrupt:
                log.info("Bot stopped by user")
                break
            except Exception as e:
                log.error(f"Loop error: {e}", exc_info=True)
                send_error(f"Loop error: {e}")

            log.info(f"Sleeping {interval_minutes} minutes...")
            time.sleep(interval_minutes * 60)


# ── Evidence Scorecard ────────────────────────────────────────

def display_evidence_scorecard(rec: dict, weather: dict):
    """Display a detailed evidence scorecard for a BUY recommendation.

    Shows every data source, whether it agrees, the math, and risk/reward.
    This is the 'proof not vibes' output — lets you trust the recommendation.
    """
    ticker = rec.get("contract_ticker", "?")
    subtitle = rec.get("contract_subtitle", "")
    signal = rec.get("signal", "")
    confidence = rec.get("confidence", "?")
    side = (rec.get("final_side") or rec.get("side") or "?").upper()
    fair_yes = rec.get("fair_price")
    fair_no = (100 - fair_yes) if fair_yes is not None else None
    fair_prob = rec.get("fair_prob", 0)
    side_prob = fair_prob if side == "YES" else (1 - fair_prob)
    side_ask = rec.get("side_ask_cents") or rec.get("market_price", 0)
    edge_cents = rec.get("final_edge_cents") or rec.get("edge_cents", 0)
    edge_pct = rec.get("edge_pct", 0)
    unc = rec.get("uncertainty_f") or weather.get("model_spread_f", 1.5)
    hours_left = weather.get("hours_remaining", "?")
    contract_type = rec.get("contract_type", "?")

    # Source agreement data
    sa = rec.get("source_agreement") or {}
    agree = sa.get("agreement_count", 0)
    total = sa.get("total_sources", 0)
    details = sa.get("agreement_details", [])

    # Calibration
    cal = weather.get("calibration") or {}
    mae = cal.get("mae_f")

    W = 60  # box width

    def _line(text="", pad=" "):
        return f"   |{pad}{text:<{W-2}}{pad}|"

    def _sep():
        return f"   +{'-'*(W)}+"

    blabel = rec.get("bracket_label", "")
    print(f"\n{_sep()}")
    print(_line(f"EVIDENCE SCORECARD - {ticker}"))
    if blabel:
        print(_line(f"Contract: {blabel}"))
    elif subtitle:
        print(_line(f"Contract: {subtitle}"))
    print(_line(f"Verdict: {signal}   Confidence: {confidence}"))
    print(_sep())

    # Source details
    if details:
        for d in details:
            # Trim leading spaces since _line adds padding
            d_trimmed = d.strip()
            icon = ""
            if d_trimmed.startswith("[Y]"):
                icon = "Y "
                d_trimmed = d_trimmed[3:].strip()
            elif d_trimmed.startswith("[N]"):
                icon = "N "
                d_trimmed = d_trimmed[3:].strip()
            elif d_trimmed.startswith("--"):
                icon = "- "
                d_trimmed = d_trimmed[2:].strip()
            line_text = f"{icon}{d_trimmed}"
            print(_line(line_text))
    else:
        print(_line("(Source agreement data not available)"))

    print(_sep())

    # Summary stats
    if total > 0:
        pct = agree / total * 100
        print(_line(f"Sources Agreeing:  {agree}/{total} ({pct:.0f}%)"))
    else:
        print(_line("Sources Agreeing:  N/A"))

    unc_label = "low" if isinstance(unc, (int, float)) and unc <= 1.0 else ("med" if isinstance(unc, (int, float)) and unc <= 2.0 else "high")
    hours_str = f"{hours_left:.0f}h remaining" if isinstance(hours_left, (int, float)) else str(hours_left)
    if isinstance(unc, (int, float)):
        print(_line(f"Uncertainty:       +/-{unc:.1f}F ({unc_label} - {hours_str})"))

    print(_line(f"Fair Probability:  {side_prob*100:.0f}%  |  Market Price: {side_ask}c"))
    print(_line(f"Edge:              {edge_cents:+.1f}c ({edge_pct:+.1f}%)"))

    if mae and isinstance(mae, (int, float)):
        forecast_high = weather.get("best_forecast_high_f")
        bounds = (rec.get("strike_bounds") or {})
        margin_f = None
        if forecast_high is not None:
            if bounds.get("kind") == "above":
                floor_val = bounds.get("floor")
                if floor_val is not None:
                    margin_f = abs(forecast_high - floor_val)
            elif bounds.get("kind") == "below":
                cap_val = bounds.get("cap")
                if cap_val is not None:
                    margin_f = abs(cap_val - forecast_high)
        if margin_f is not None:
            ok = "PASS" if margin_f >= mae * 0.8 else "WARN"
            print(_line(f"Margin vs MAE:     {margin_f:.1f}F margin vs {mae:.1f}F MAE [{ok}]"))

    print(_line(f"Contract Type:     {contract_type.upper()}"))

    print(_sep())

    # Risk/reward breakdown
    if side_ask and isinstance(side_ask, (int, float)) and side_ask > 0:
        cost = side_ask / 100.0
        profit_if_win = (100 - side_ask) / 100.0
        ev = side_prob * profit_if_win - (1 - side_prob) * cost
        ret_pct = profit_if_win / cost * 100 if cost > 0 else 0
        print(_line(f"Position: 1 contract @ {side_ask}c = ${cost:.2f} risk"))
        print(_line(f"If correct: +${profit_if_win:.2f} profit ({ret_pct:.0f}% return)"))
        print(_line(f"If wrong:   -${cost:.2f} loss"))
        print(_line(f"Expected Value: ${ev:+.2f} per contract"))

        # Bankroll sizing (assume $100 display)
        try:
            sizing = size_position(rec, 100.0)
            if sizing["contracts"] > 0:
                print(_line(f"Sizing ($100): {sizing['contracts']} contracts = ${sizing['cost']:.2f}"))
        except Exception:
            pass

    print(_sep())
    print()


# ── Console Display ───────────────────────────────────────────

def display_city_analysis(city_key: str, analysis: dict):
    """Pretty-print analysis for a city."""
    weather_by_date = analysis.get("weather_by_date") or {}
    if not weather_by_date:
        weather = analysis.get("weather", {})
        if weather:
            dkey = weather.get("contract_date", "unknown")
            weather_by_date = {dkey: weather}

    stat_edges_by_date = analysis.get("statistical_edges_by_date", {})
    all_edges_by_date = analysis.get("all_edges_by_date", {})
    recs_by_date = analysis.get("recommendations_by_date", {})
    llm_meta_by_date = analysis.get("llm_meta_by_date", {}) or {}
    market_data = analysis.get("market_data", {})
    month_anchor_by_month = analysis.get("month_anchor_by_month") or _compute_month_anchors(weather_by_date)

    tz = ZoneInfo(CITIES[city_key]["timezone"])
    today_local = datetime.now(tz).date()

    for dkey in sorted(weather_by_date.keys()):
        weather = weather_by_date.get(dkey, {})
        metar = weather.get("metar", {})
        ens = weather.get("ensemble", {})
        hours_left = weather.get("hours_remaining", "?")
        contract_not_started = metar.get("contract_not_started", False)

        label = ""
        try:
            d = date.fromisoformat(dkey)
            if d == today_local:
                label = "today"
            elif d == (today_local + timedelta(days=1)):
                label = "tomorrow"
        except Exception:
            pass

        print(f"\n{'='*50}")
        header = f"  {city_key} — {dkey}"
        if label:
            header += f" ({label})"
        print(header)
        print(f"{'='*50}")

        if contract_not_started:
            h_until = metar.get("hours_until_start")
            if h_until is not None:
                print(f"📍 Contract date not started locally — forecast-only (starts in {h_until:.1f}h).")
            else:
                print("📍 Contract date not started locally — forecast-only.")
            bias_flags = weather.get("bias_flags") or []
            if bias_flags:
                print(f"   ⚠️ Bias: {'; '.join(bias_flags)}")
        else:
            # Current Data Summary (T-group > Synoptic > API per chat; ghost flags)
            tgrp = weather.get("max_tgroup_f")
            syn = weather.get("max_synoptic_f")
            api = weather.get("api_display_f")
            ghosts = weather.get("ghost_flags") or []
            bias_flags = weather.get("bias_flags") or []
            cli_date = weather.get("cli_report_date")
            cli_high = weather.get("cli_high_f")
            station_used = metar.get("station")
            official_station = CITIES[city_key].get("station_id")
            cli_station = CITIES[city_key].get("cli_station")
            if station_used:
                station_note = "official" if station_used == official_station else "backup"
                print(f"📍 Station: {station_used} ({station_note}) | CLI station: {cli_station}")
            print(f"📍 Current: {metar.get('current_temp_f', '?')}°F ({metar.get('current_temp_precision', 'standard')})")
            if metar.get("best_max_f") is not None:
                max_src = metar.get("best_max_source", "?")
                max_line = f"   Max observed today: {metar['best_max_f']}°F ({max_src})"
                # Show 5-min spike warning alongside max
                fivemin_max = weather.get("max_5min_f")
                fivemin_div = weather.get("5min_tgroup_divergence_f") or 0
                if fivemin_max is not None and fivemin_div >= 1.0:
                    max_line += f" | ⚠️ 5-min ASOS: {fivemin_max}°F (±1°F)"
                print(max_line)
            elif weather.get("max_5min_f") is not None:
                print(f"   Max observed today: {weather['max_5min_f']}°F (5-min ASOS, ±1°F)")
            age_min = metar.get("latest_obs_age_min")
            age_str = f"{age_min:.0f}m" if isinstance(age_min, (int, float)) else "—"
            print(f"   Data: T-group={tgrp or '—'}°F | Synoptic={syn or '—'}°F | API={api or '—'}°F | METAR age={age_str} | Time left LST: {hours_left}h")
            if cli_date or cli_high is not None:
                print(f"   CLI: {cli_date or '—'} → high={cli_high or '—'}°F")
            if ghosts:
                print(f"   ⚠️ Ghosts: {'; '.join(ghosts)}")
            if bias_flags:
                print(f"   ⚠️ Bias: {'; '.join(bias_flags)}")

        # Forecast
        high = weather.get("best_forecast_high_f")
        rain = weather.get("rain_probability")
        if high is not None:
            try:
                unc = estimate_uncertainty(weather)
            except Exception:
                unc = weather.get("model_spread_f", 1.5)
            print(f"🔮 Forecast high: {high:.1f}°F ±{unc:.1f}°F")
        if rain is not None:
            print(f"🌧  Rain probability: {rain*100:.0f}%")

        # Ensemble breakdown
        model_highs = ens.get("model_highs", {})
        if model_highs:
            models_str = ", ".join(f"{m}={t:.0f}" for m, t in model_highs.items())
            print(f"📊 Models: {models_str}")

        # NWS Raw Source Comparison (transparency for discrepancy detection)
        nws_data = weather.get("nws", {})
        hourly_data = nws_data.get("hourly", {})
        nws_raw = {}
        if hourly_data.get("_nws_daily_high") is not None:
            nws_raw["daily"] = hourly_data["_nws_daily_high"]
        if hourly_data.get("_nws_hourly_max") is not None:
            nws_raw["hourly"] = hourly_data["_nws_hourly_max"]
        if hourly_data.get("_tabular_high") is not None:
            nws_raw["tabular"] = hourly_data["_tabular_high"]
        gp_data = nws_data.get("gridpoint", {})
        if gp_data.get("max_temp_today") is not None:
            nws_raw["gridpoint"] = gp_data["max_temp_today"]
        if nws_raw:
            raw_str = ", ".join(f"{k}={v:.0f}" for k, v in nws_raw.items())
            nws_spread = max(nws_raw.values()) - min(nws_raw.values()) if len(nws_raw) >= 2 else 0
            disc_flag = f" ⚠️ SPREAD={nws_spread:.0f}°F" if nws_spread >= 2 else ""
            print(f"🌡️  NWS Sources: {raw_str}{disc_flag}")

        # Intraday warm bias warning
        if weather.get("intraday_warm_bias_f") is not None:
            print(f"⚠️  WARM BIAS: Current obs +{weather['intraday_warm_bias_f']}°F vs NWS hourly "
                  f"(projected high adjusted +{weather.get('intraday_bias_adjust_f', 0)}°F)")

        cal = weather.get("calibration") or {}
        if cal.get("badge"):
            n_mae = cal.get("n_mae", 0)
            n_brier = cal.get("n_brier", 0)
            print(f"🔎 Trust & Calibration: {cal['badge']} (n_mae={n_mae}, n_brier={n_brier})")

        # Market edges (filtered to contract date)
        market_filtered = _filter_market_data_for_date(market_data, dkey, month_anchor_by_month)
        total_contracts = sum(len(c) for c in market_filtered.values())
        print(f"\n📈 Markets: {total_contracts} contracts found")

        # Rules summary for relevant market types
        market_types = set()
        for series_ticker in market_filtered.keys():
            if series_ticker.startswith("KXHIGH"):
                if ENABLED_MARKETS.get("high_temp", True):
                    market_types.add("high_temp")
            elif series_ticker.startswith("KXRAIN"):
                if series_ticker.endswith("M"):
                    if ENABLED_MARKETS.get("monthly_rain", True):
                        market_types.add("monthly_rain")
                else:
                    if ENABLED_MARKETS.get("daily_rain", True):
                        market_types.add("daily_rain")
        if market_types:
            print("📜 Rules:")
            for mt in sorted(market_types):
                summary = rule_summary_for_market_type(mt)
                if summary:
                    print(f"   • {mt}: {summary}")

        stat_edges = stat_edges_by_date.get(dkey, [])
        if stat_edges:
            print(f"🎯 Edges passing filter: {len(stat_edges)}")
            max_ep = TRADING.get("max_trusted_edge_pct", 150)
            has_extreme = any(abs(e.get("edge_pct", 0)) > max_ep for e in stat_edges[:8])
            if has_extreme:
                print(f"   (Edges capped at ±{max_ep:.0f}% for display; verify liquidity on very high edges.)")
            for e in stat_edges[:8]:
                side = e.get("side", "?").upper()
                ticker = e.get("contract_ticker", "?")
                subtitle = e.get("contract_subtitle", "")
                edge_c = e.get("edge_cents", 0)
                edge_p = e.get("edge_pct", 0)
                display_pct = edge_p if abs(edge_p) <= max_ep else (max_ep if edge_p > 0 else -max_ep)
                fair = e.get("fair_price", 0)
                if side.strip().upper() == "NO" and fair is not None:
                    fair = 100 - fair
                mkt = e.get("market_price", 0)
                kelly = e.get("kelly_fraction", 0)
                sig = e.get("signal", "")
                blabel = e.get("bracket_label", "")
                sub = f"  [{blabel}]" if blabel else (f" ({subtitle})" if subtitle else "")
                print(f"   {side:3s} {ticker:28s}{sub} edge={edge_c:+.1f}¢ ({display_pct:+.1f}%) fair={fair:.0f} mkt={mkt:.0f} kelly={kelly:.2f}  {sig}")
                yes_bid = e.get("yes_bid_cents")
                yes_ask = e.get("yes_ask_cents")
                no_bid = e.get("no_bid_cents")
                no_ask = e.get("no_ask_cents")
                spread = e.get("spread_cents")
                vol = e.get("volume", 0)
                oi = e.get("open_interest", 0)
                spread_disp = f"{spread:.0f}¢" if isinstance(spread, (int, float)) else "?"
                print(f"      book YES {yes_bid}/{yes_ask}  NO {no_bid}/{no_ask}  spread={spread_disp}  vol={vol} oi={oi}")
        else:
            print("   ❌ No edges passed minimum threshold")

        # Show global LLM reasoning if available
        llm_meta_dkey = llm_meta_by_date.get(dkey, {})
        llm_reasoning = llm_meta_dkey.get("reasoning")
        if llm_reasoning and TRADING.get("_active_profile") == "llm_first":
            print(f"\n🤖 CLAUDE ANALYSIS:")
            for line in str(llm_reasoning)[:600].split('\n'):
                print(f"   {line}")

        # Final recommendations: only BUY signals in RECOMMENDATIONS; HOLD/NO TRADE listed separately
        recs = recs_by_date.get(dkey, [])
        actionable = [r for r in recs if r.get("final_side") != "none"]
        strong_recs = [r for r in actionable if (r.get("signal") or "").strip().startswith("BUY")]

        if strong_recs:
            print(f"\n💰 RECOMMENDATIONS (BUY): {len(strong_recs)} trades")
            for r in strong_recs[:5]:
                signal = r.get("signal", "")
                signal_side = None
                if signal.strip().upper().startswith("BUY NO"):
                    signal_side = "NO"
                elif signal.strip().upper().startswith("BUY YES"):
                    signal_side = "YES"
                side = (r.get("final_side") or r.get("side") or "?").upper()
                if signal_side and side != signal_side:
                    side = signal_side
                ticker = r.get("contract_ticker", "?")
                subtitle = r.get("contract_subtitle", "")
                edge = r.get("final_edge_cents")
                if edge is None:
                    edge = r.get("edge_cents", 0)
                conf = r.get("combined_confidence", "?")
                confidence = r.get("confidence", "?")
                fair_yes = r.get("fair_price", None)
                fair_no = None
                if fair_yes is not None:
                    fair_no = 100 - fair_yes
                fair_val = None
                if side == "YES":
                    fair_val = fair_yes
                elif side == "NO":
                    fair_val = fair_no
                risks = r.get("risks", [])
                why = r.get("why", [])
                blabel = r.get("bracket_label", "")
                sub = f" [{blabel}]" if blabel else (f" ({subtitle})" if subtitle else "")

                # Contract type label
                ctype = r.get("contract_type", "")
                ctype_label = f"[{ctype.upper()}]" if ctype else ""

                print(f"   → {side} {ticker}{sub} edge={edge:.1f}¢ [{conf}] {ctype_label}")
                print(f"      Signal: {signal}  Confidence: {confidence}")

                # NWS forecast alignment info
                nws_day_high = weather.get("forecast_high_day_f")
                nws_source = weather.get("forecast_high_day_source", "")
                if nws_day_high is not None:
                    print(f"      NWS daily high: {nws_day_high:.0f}°F (source: {nws_source})")

                if fair_yes is not None and fair_no is not None:
                    print(f"      Fair (YES/NO): {fair_yes:.0f}¢ / {fair_no:.0f}¢")
                elif fair_val is not None:
                    print(f"      Fair value (side): {fair_val:.0f}¢")
                bid = r.get("side_bid_cents")
                ask = r.get("side_ask_cents")
                spread = r.get("spread_cents")
                vol = r.get("volume", 0)
                oi = r.get("open_interest", 0)
                spread_disp = f"{spread:.0f}¢" if isinstance(spread, (int, float)) else "?"
                if bid or ask:
                    print(f"      Book: bid {bid} / ask {ask} (spread {spread_disp})  vol {vol} oi {oi}")
                if fair_val is not None and ask:
                    edge_calc = fair_val - ask
                    print(f"      Edge (side): {edge_calc:+.1f}¢")

                # Bankroll sizing recommendation
                try:
                    sizing = size_position(r, 100.0)  # assume $100 bankroll for display
                    if sizing["contracts"] > 0:
                        print(f"      Sizing ($100 bankroll): {sizing['contracts']} contracts "
                              f"= ${sizing['cost']:.2f} risk → ${sizing['expected_profit']:.2f} expected profit")
                except Exception:
                    pass

                # Claude LLM analysis details
                claude_reasoning = r.get("claude_reasoning", "")
                margin_safety = r.get("margin_of_safety", "")
                if claude_reasoning:
                    print(f"      🤖 Claude: {claude_reasoning[:200]}")
                if margin_safety:
                    print(f"      🛡️  Margin of safety: {margin_safety}")

                if risks:
                    print(f"      Risks: {'; '.join(risks[:3])}")
                for w in why[:4]:
                    print(f"      • {w}")
                trust_score = r.get("trust_score")
                if isinstance(trust_score, (int, float)):
                    print(f"      Trust score: {int(trust_score)}/100")

                # Pre-trade forecast freshness check
                if r.get("market_type") == "high_temp" and TRADING.get("_active_profile") == "margin_of_safety":
                    try:
                        from data_sources.nws import quick_forecast_recheck
                        city_key = r.get("city")
                        original_high = r.get("forecast_high_f") or weather.get("forecast_high_day_f")
                        if city_key and original_high is not None:
                            fresh_high = quick_forecast_recheck(city_key)
                            if fresh_high is not None:
                                drift = abs(fresh_high - original_high)
                                if drift >= 1.0:
                                    print(f"      ⚠️  FORECAST SHIFTED: was {original_high:.0f}°F → now {fresh_high:.0f}°F (drift {drift:.0f}°F) — RECHECK BEFORE TRADING")
                                else:
                                    print(f"      ✅ Forecast stable: {fresh_high:.0f}°F (drift {drift:.1f}°F)")
                    except Exception:
                        pass

                # Evidence scorecard (detailed proof for margin_of_safety profile)
                if TRADING.get("_active_profile") == "margin_of_safety":
                    display_evidence_scorecard(r, weather)

        llm_meta = llm_meta_by_date.get(dkey, {})
        llm_trades = llm_meta.get("trades") or []
        llm_provider = llm_meta.get("provider") or "llm"
        buy_tickers = {r.get("contract_ticker") for r in strong_recs}
        if llm_trades:
            edge_by_ticker = {e.get("contract_ticker"): e for e in all_edges_by_date.get(dkey, [])}
            non_actionable = []
            for t in llm_trades:
                ticker = t.get("contract_ticker") or "?"
                if ticker in buy_tickers:
                    continue
                edge = edge_by_ticker.get(ticker)
                reasons = []
                if edge:
                    if edge.get("hard_block"):
                        reasons.extend(edge.get("gate_reasons") or [])
                    elif edge.get("soft_block"):
                        # Soft blocks are just warnings, show them but don't label as blocked
                        soft_gates = [r for r in (edge.get("gate_reasons") or []) if r.startswith("S:")]
                        if soft_gates:
                            reasons.extend(soft_gates)
                    else:
                        reasons.append("Not recommended after gating")
                else:
                    reasons.append("Not in eligible edges")
                non_actionable.append((t, reasons))
            if non_actionable:
                print(f"\n🤖 {str(llm_provider).upper()} IDEAS (NON-ACTIONABLE): {len(non_actionable)}")
                for t, reasons in non_actionable[:5]:
                    side = (t.get("side") or "?").upper()
                    ticker = t.get("contract_ticker") or "?"
                    fair = t.get("fair_price")
                    mkt = t.get("market_price")
                    edge = t.get("edge_cents")
                    conf = t.get("confidence", "?")
                    reason = t.get("reasoning", "")
                    fair_str = f"{fair:.0f}¢" if isinstance(fair, (int, float)) else "?"
                    mkt_str = f"{mkt:.0f}¢" if isinstance(mkt, (int, float)) else "?"
                    edge_str = f"{edge:+.1f}¢" if isinstance(edge, (int, float)) else "?"
                    print(f"   {side:3s} {ticker:28s} fair={fair_str} mkt={mkt_str} edge={edge_str} conf={conf}")
                    if reasons:
                        print(f"      Gate: {'; '.join(reasons[:3])}")
                    if reason:
                        print(f"      Reason: {reason}")
        hold_no_trade = [
            r for r in stat_edges
            if (r.get("contract_ticker") not in buy_tickers)
            and not (r.get("signal") or "").strip().startswith("BUY")
        ]
        if hold_no_trade:
            tickers = [(r.get("contract_ticker") or "?")[:24] for r in hold_no_trade[:5]]
            print(f"\n   ⏸ HOLD / NO TRADE ({len(hold_no_trade)}): {', '.join(tickers)}")
        if not actionable:
            print("\n   No actionable trade recommendations at this time.")


def display_top_bets(llm_global: dict | None):
    """Display Claude's TOP BETS ranking across all cities."""
    if not llm_global:
        return
    top_bets = llm_global.get("top_bets") or []
    if not top_bets:
        print(f"\n{'='*60}")
        print("  🏆 CLAUDE'S TOP BETS: None — no bet passes all safety checks")
        print(f"{'='*60}")
        return

    print(f"\n{'='*60}")
    print("  🏆 CLAUDE'S TOP BETS (ranked best to worst)")
    print(f"{'='*60}")
    for bet in top_bets:
        rank = bet.get("rank", "?")
        ticker = bet.get("contract_ticker", "?")
        side = (bet.get("side") or "?").upper()
        city = bet.get("city", "?")
        conf = bet.get("confidence", "?")
        why_safe = bet.get("why_safe", "")
        risks = bet.get("remaining_risks", "")

        print(f"\n  #{rank}  {side} {ticker}  [{city}]  confidence={conf}")
        if why_safe:
            # Wrap long lines
            for i in range(0, len(why_safe), 80):
                prefix = "      ✅ " if i == 0 else "         "
                print(f"{prefix}{why_safe[i:i+80]}")
        if risks:
            print(f"      ⚠️  Risks: {risks}")
    print(f"\n{'='*60}")


# ── Main Entry Point ──────────────────────────────────────────

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Kalshi Weather Bot")
    parser.add_argument("--city", action="append", help="City key to analyze (repeatable)")
    parser.add_argument("--tomorrow", action="store_true", help="Analyze tomorrow only")
    parser.add_argument("--both", action="store_true", help="Analyze today and tomorrow")
    parser.add_argument("--trade", action="store_true", help="Execute trades (live)")
    parser.add_argument("--profile", choices=["llm_first", "margin_of_safety", "safe", "aggressive"],
                        default="llm_first",
                        help="Trading profile: 'llm_first' (default) = no gates, LLM decides everything; "
                             "'margin_of_safety' = high win-rate, proof-backed, Benjamin Graham approach; "
                             "'safe' = threshold-preferred, NWS-aligned; "
                             "'aggressive' = original loose settings")
    args = parser.parse_args()

    # Apply trading profile BEFORE anything else
    from config import apply_trading_profile
    apply_trading_profile(args.profile)

    print(f"""
======================================================================
  KALSHI WEATHER BOT - RECOMMENDATION MODE [{args.profile.upper()}]
  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
======================================================================
""")

    # Set up console logging
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    )
    try:
        compute_validation_stats()
    except Exception as e:
        log.debug(f"Validation stats update failed: {e}")

    include_today = True
    include_tomorrow = False
    if args.both:
        include_today = True
        include_tomorrow = True
    elif args.tomorrow:
        include_today = False
        include_tomorrow = True

    bot = WeatherTradingBot(mode="trade" if args.trade else "recommend",
                            include_today=include_today,
                            include_tomorrow=include_tomorrow)

    # Allow filtering to specific cities
    if args.city:
        target_cities = [c for c in args.city if c in CITIES]
        if not target_cities:
            target_cities = list(CITIES.keys())
    else:
        target_cities = list(CITIES.keys())

    if LLM_RUN_MODE == "global_once":
        try:
            results = bot.run_single_pass(city_keys=target_cities)
            for city_key in target_cities:
                analysis = results.get(city_key)
                if analysis:
                    display_city_analysis(city_key, analysis)
            # Display TOP BETS summary from Claude's global analysis
            display_top_bets(getattr(bot, '_llm_global', None))
        except Exception as e:
            print(f"\n❌ Error during global analysis: {e}")
            import traceback
            traceback.print_exc()
    else:
        for city_key in target_cities:
            try:
                analysis = bot.analyze_city(city_key)
                display_city_analysis(city_key, analysis)
                if args.trade:
                    bot.execute_trades(analysis)
            except Exception as e:
                print(f"\n❌ Error analyzing {city_key}: {e}")
                import traceback
                traceback.print_exc()

    # Show balance
    try:
        balance = bot.api.get_balance()
        print(f"\n💰 Account Balance: ${balance:.2f}")
    except Exception:
        pass

    print(f"\n{'='*50}")
    print("Done.")
