"""
Claude AI analysis using Opus 4.5 with extended thinking.
Synthesizes all weather data sources into trading recommendations.
"""
import json
import logging
from datetime import datetime
from zoneinfo import ZoneInfo

try:
    import anthropic
except ImportError:
    anthropic = None

from config import (
    ANTHROPIC_API_KEY, CLAUDE_MODEL, CLAUDE_MAX_TOKENS,
    CLAUDE_THINKING_BUDGET, CLAUDE_TIMEOUT_SECONDS, CLAUDE_MAX_RETRIES,
    CLAUDE_MAX_TOKENS_COMPACT, CLAUDE_THINKING_BUDGET_COMPACT, CITIES, TRADING, TRUST_GATES,
    LLM_MAX_EDGES_IN_PROMPT,
)
from rules_catalog import rule_summary_for_market_type

log = logging.getLogger(__name__)

_CLAUDE_DISABLED = False


def _disable_claude(reason: str) -> None:
    global _CLAUDE_DISABLED
    if not _CLAUDE_DISABLED:
        _CLAUDE_DISABLED = True
        log.warning(f"Disabling Claude analysis for this run: {reason}")


def _extract_first_json(text: str) -> dict | None:
    """Extract the first valid JSON object from arbitrary text."""
    if not text:
        return None

    stripped = text.strip()
    if stripped.startswith("{") and stripped.endswith("}"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    in_str = False
    escape = False
    depth = 0
    start = None

    for i, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == "\"":
                in_str = False
            continue

        if ch == "\"":
            in_str = True
            continue
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    candidate = text[start:i + 1]
                    try:
                        return json.loads(candidate)
                    except json.JSONDecodeError:
                        start = None
                        continue
    return None


def build_analysis_prompt(city_key: str,
                          weather_data: dict,
                          market_data: dict,
                          prompt_mode: str = "full",
                          edges: list[dict] | None = None,
                          max_edges: int = 6) -> str:
    """Build the analysis prompt with available data."""
    city = CITIES[city_key]
    tz = ZoneInfo(city["timezone"])
    now = datetime.now(tz)

    # Summarize weather data
    weather_summary = []

    # METAR observations
    metar = weather_data.get("metar", {})
    if metar:
        weather_summary.append(f"=== METAR OBSERVATIONS ===")
        if metar.get("current_temp_f") is not None:
            weather_summary.append(f"Current temp: {metar['current_temp_f']:.1f}°F (precision: {metar.get('current_temp_precision', 'basic')})")
        if metar.get("best_max_f") is not None:
            weather_summary.append(f"Best observed max today: {metar['best_max_f']:.1f}°F (source: {metar.get('best_max_source', '?')})")
        if metar.get("total_precip_today") is not None:
            weather_summary.append(f"Precipitation today: {metar['total_precip_today']:.2f} inches")
        if metar.get("has_rained_today"):
            weather_summary.append(f"Rain detected today: YES")

    # NWS forecasts
    nws = weather_data.get("nws", {})
    if nws:
        weather_summary.append(f"\n=== NWS FORECAST ===")
        hourly = nws.get("hourly", {})
        if hourly.get("forecast_high_day") is not None:
            src = hourly.get("forecast_high_day_source", "hourly")
            weather_summary.append(f"NWS day forecast high: {hourly['forecast_high_day']}°F (source: {src})")
        if hourly.get("temp_trend"):
            weather_summary.append(f"Temp trend: {hourly['temp_trend']}")
        if hourly.get("hours_remaining") is not None:
            weather_summary.append(f"Hours remaining in window: {hourly['hours_remaining']}")
        if hourly.get("precip_probability_today") is not None:
            weather_summary.append(f"Precip probability: {hourly['precip_probability_today']}%")

        gridpoint = nws.get("gridpoint", {})
        if gridpoint.get("qpf_today_in") is not None:
            weather_summary.append(f"QPF today: {gridpoint['qpf_today_in']:.2f} inches")

        afd = nws.get("afd", {})
        if afd:
            weather_summary.append(f"\n=== AREA FORECAST DISCUSSION ===")
            if afd.get("confidence_level"):
                weather_summary.append(f"Confidence level: {afd['confidence_level']}")
            if afd.get("uncertainty_notes"):
                weather_summary.append(f"Uncertainty flags: {', '.join(afd['uncertainty_notes'][:5])}")
            if prompt_mode == "full":
                if afd.get("temperature_discussion"):
                    weather_summary.append(f"Temp discussion: {afd['temperature_discussion']}")
                if afd.get("precipitation_discussion"):
                    weather_summary.append(f"Precip discussion: {afd['precipitation_discussion']}")

        cli = nws.get("cli", {})
        if cli and cli.get("is_final"):
            weather_summary.append(f"\n=== CLI REPORT (SETTLEMENT) ===")
            if cli.get("high_temp_f") is not None:
                weather_summary.append(f"Official high: {cli['high_temp_f']}°F")
            if cli.get("precip_inches") is not None:
                weather_summary.append(f"Official precip: {cli['precip_inches']} inches")

        # NWS RAW SOURCE COMPARISON
        nws_sources = {}
        if hourly.get("_nws_daily_high") is not None:
            nws_sources["daily"] = hourly["_nws_daily_high"]
        if hourly.get("_nws_hourly_max") is not None:
            nws_sources["hourly_max"] = hourly["_nws_hourly_max"]
        if hourly.get("_tabular_high") is not None:
            nws_sources["tabular"] = hourly["_tabular_high"]
        gp = nws.get("gridpoint", {})
        if gp.get("max_temp_today") is not None:
            nws_sources["gridpoint"] = gp["max_temp_today"]
        if len(nws_sources) >= 2:
            weather_summary.append(f"\n=== NWS RAW SOURCES ===")
            for src_name, val in nws_sources.items():
                weather_summary.append(f"  {src_name}: {val}°F")
            spread = max(nws_sources.values()) - min(nws_sources.values())
            if spread >= 2:
                weather_summary.append(f"  ⚠️ DISCREPANCY: {spread:.0f}°F spread!")

    # Intraday bias
    if weather_data.get("intraday_warm_bias_f") is not None:
        weather_summary.append(f"\n=== INTRADAY WARM BIAS ===")
        weather_summary.append(
            f"Current obs is {weather_data['intraday_warm_bias_f']}°F warmer than NWS hourly "
            f"(bias adjustment: +{weather_data.get('intraday_bias_adjust_f', 0)}°F)"
        )

    if weather_data.get("projected_high_f") is not None:
        weather_summary.append(f"BOT PROJECTED HIGH: {weather_data['projected_high_f']}°F")

    # Wethr.net (optional paid)
    wethr = weather_data.get("wethr", {})
    if wethr:
        weather_summary.append(f"\n=== WETHR (LST / NWS LOGIC) ===")
        if wethr.get("wethr_high_f") is not None:
            weather_summary.append(f"Wethr high so far: {wethr['wethr_high_f']}°F")
        if wethr.get("wethr_low_f") is not None:
            weather_summary.append(f"Wethr low so far: {wethr['wethr_low_f']}°F")

    wethr_precip = weather_data.get("wethr_precip", {})
    if wethr_precip:
        weather_summary.append(f"\n=== WETHR PRECIP MTD ===")
        if wethr_precip.get("total_mtd") is not None:
            weather_summary.append(f"Total MTD precip: {wethr_precip['total_mtd']} inches")
        if wethr_precip.get("today_precip") is not None:
            weather_summary.append(f"Today precip: {wethr_precip['today_precip']} inches")
        if wethr_precip.get("has_trace"):
            weather_summary.append("Trace precip observed today: YES")

    # Ensemble forecasts
    ensemble = weather_data.get("ensemble", {})
    if ensemble:
        weather_summary.append(f"\n=== ENSEMBLE MODELS ===")
        if ensemble.get("model_highs"):
            for m, t in ensemble["model_highs"].items():
                weather_summary.append(f"  {m}: {t:.1f}°F")
        if ensemble.get("weighted_high_f") is not None:
            weather_summary.append(f"Weighted ensemble high: {ensemble['weighted_high_f']:.1f}°F")
        if ensemble.get("model_spread_f") is not None:
            weather_summary.append(f"Model spread: {ensemble['model_spread_f']:.1f}°F")
        if ensemble.get("weighted_precip_in") is not None:
            weather_summary.append(f"Weighted precip: {ensemble['weighted_precip_in']:.3f} inches")
        if ensemble.get("precip_probability") is not None:
            weather_summary.append(f"Precip probability: {ensemble['precip_probability']:.0f}%")

    # Market data (full) or candidate edges (compact)
    market_summary = []
    market_types = set()
    if prompt_mode == "compact":
        market_summary.append("\n=== CANDIDATE EDGES (TOP) ===")
        if edges:
            for e in edges[:max_edges]:
                ticker = e.get("contract_ticker", "?")
                side = (e.get("side", "?") or "?").upper()
                fair = e.get("fair_price", "?")
                mkt = e.get("market_price", "?")
                edge = e.get("edge_cents", "?")
                yb = e.get("yes_bid_cents", "?")
                ya = e.get("yes_ask_cents", "?")
                nb = e.get("no_bid_cents", "?")
                na = e.get("no_ask_cents", "?")
                spread = e.get("spread_cents", "?")
                vol = e.get("volume", 0)
                oi = e.get("open_interest", 0)
                subtitle = e.get("contract_subtitle", "")
                mtype = e.get("market_type", "?")
                cdate = e.get("contract_date", "")
                market_summary.append(
                    f"  {ticker} [{mtype} {cdate}] side={side} fair={fair} mkt={mkt} edge={edge}¢ "
                    f"YES {yb}/{ya} NO {nb}/{na} spread={spread} vol={vol} oi={oi} {subtitle}".strip()
                )
                if mtype:
                    market_types.add(mtype)
        else:
            market_summary.append("  (No candidate edges provided)")
    else:
        for series_ticker, contracts in market_data.items():
            market_summary.append(f"\n=== {series_ticker} ===")
            if series_ticker.startswith("KXHIGH"):
                market_types.add("high_temp")
            elif series_ticker.startswith("KXRAIN"):
                if series_ticker.endswith("M"):
                    market_types.add("monthly_rain")
                else:
                    market_types.add("daily_rain")
            for c in contracts[:20]:  # limit to avoid token overflow
                ticker = c.get("ticker", "?")
                yes_p = c.get("yes_price", "?")
                vol = c.get("volume", 0)
                floor_s = c.get("floor_strike", "tail_low")
                cap_s = c.get("cap_strike", "tail_high")
                market_summary.append(f"  {ticker}: YES={yes_p}¢  floor={floor_s} cap={cap_s}  vol={vol}")

    # Rules summary for relevant market types
    rules_summary = []
    for mt in sorted(market_types):
        summary = rule_summary_for_market_type(mt)
        if summary:
            rules_summary.append(f"- {mt}: {summary}")

    weather_text = "\n".join(weather_summary)
    market_text = "\n".join(market_summary)

    rules_text = "\n".join(rules_summary) if rules_summary else ""

    llm_first = TRADING.get("_active_profile") == "llm_first"

    if llm_first:
        task_block = """YOU ARE THE SOLE DECISION-MAKER. All data and warnings are context, not hard blocks.

YOUR TASK:
1. Synthesize ALL data sources to estimate the most likely high temperature (to 0.1°F precision)
2. Estimate probability of rain today
3. Assess forecast uncertainty (low/moderate/high)
4. For EACH contract, estimate fair probability and identify any edge vs market price
5. Recommend only the BEST trades with genuine margin of safety
6. Consider model agreement, hours remaining, observed vs forecast gaps
7. REJECT trades where margin of safety is thin
8. If no trade is safe, return empty trades array
"""
    elif prompt_mode == "compact":
        task_block = """YOUR TASK (COMPACT):
1. Use the weather summary to estimate the most likely high temperature and uncertainty.
2. Use the provided CANDIDATE EDGES ONLY. Do NOT invent or infer missing contracts.
3. For each candidate ticker listed, estimate fair probability and edge vs market.
4. Recommend trades ONLY for the listed candidate tickers.
"""
    else:
        task_block = """YOUR TASK:
1. Synthesize ALL data sources to estimate the most likely high temperature (to 0.1°F precision)
2. Estimate probability of rain today and expected monthly total
3. Assess forecast uncertainty (low/moderate/high) with reasoning
4. For EACH contract, estimate fair probability and identify any edge vs market price
5. Recommend specific trades: which contracts to buy YES or NO, and confidence level
"""

    contract_date = weather_data.get("contract_date", "?")
    return f"""You are an expert weather derivatives trader analyzing Kalshi weather markets.

CITY: {city['name']} ({city_key})
TIME: {now.strftime('%Y-%m-%d %H:%M %Z')}
CONTRACT DATE: {contract_date}
STATION: {city['station_id']}
PROMPT_MODE: {prompt_mode}

RULES (from Kalshi PDFs):
{rules_text}

{weather_text}

{market_text}

SETTLEMENT RULES:
- High temperature: NWS CLI report, rounded using NWS rules (0.5 rounds UP)
- Daily rain: Any measurable precipitation at the official station
- Monthly rain: Total monthly precipitation from CLI reports

{task_block}

OUTPUT FORMAT (JSON):
{{
    "forecast_high_f": <best estimate>,
    "forecast_uncertainty_f": <std dev in °F>,
    "rain_probability": <0-1>,
    "confidence": "high|moderate|low",
    "reasoning": "<brief reasoning>",
    "trades": [
        {{
            "contract_ticker": "<ticker>",
            "side": "yes|no",
            "fair_price": <cents>,
            "market_price": <cents>,
            "edge_cents": <cents>,
            "confidence": "high|moderate|low",
            "margin_of_safety": "<explain why this bet is safe>",
            "reasoning": "<why this trade>"
        }}
    ]
}}

Return ONLY valid JSON. Do not include any extra text.
Be precise. Disagreements between data sources are IMPORTANT signals.
When observations conflict with forecasts, weight observations more heavily (especially late in day).
If the AFD mentions uncertainty, increase your forecast uncertainty.
"""


def build_global_prompt(analysis_bundle: list[dict],
                        prompt_mode: str = "full",
                        max_edges: int = 6) -> str:
    """Build a single prompt covering all cities at once."""
    now_utc = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    city_blocks = []
    market_types = set()
    llm_first = TRADING.get("_active_profile") == "llm_first"

    for item in analysis_bundle:
        city_key = item.get("city")
        if city_key not in CITIES:
            continue
        city = CITIES[city_key]
        weather_data = item.get("weather", {})
        market_data = item.get("market_data", {})
        edges = item.get("statistical_edges", []) or []

        # Reuse per-city summary logic (copied from build_analysis_prompt)
        weather_summary = []
        metar = weather_data.get("metar", {})
        if metar:
            weather_summary.append("=== METAR OBSERVATIONS ===")
            if metar.get("current_temp_f") is not None:
                weather_summary.append(
                    f"Current temp: {metar['current_temp_f']:.1f}°F (precision: {metar.get('current_temp_precision', 'basic')})"
                )
            if metar.get("best_max_f") is not None:
                weather_summary.append(
                    f"Best observed max today: {metar['best_max_f']:.1f}°F (source: {metar.get('best_max_source', '?')})"
                )
            if metar.get("total_precip_today") is not None:
                weather_summary.append(f"Precipitation today: {metar['total_precip_today']:.2f} inches")
            if metar.get("has_rained_today"):
                weather_summary.append("Rain detected today: YES")

        nws = weather_data.get("nws", {})
        if nws:
            weather_summary.append("\n=== NWS FORECAST ===")
            hourly = nws.get("hourly", {})
            if hourly.get("forecast_high_day") is not None:
                src = hourly.get("forecast_high_day_source", "hourly")
                weather_summary.append(f"NWS day forecast high: {hourly['forecast_high_day']}°F (source: {src})")
            if hourly.get("temp_trend"):
                weather_summary.append(f"Temp trend: {hourly['temp_trend']}")
            if hourly.get("hours_remaining") is not None:
                weather_summary.append(f"Hours remaining in window: {hourly['hours_remaining']}")
            if hourly.get("precip_probability_today") is not None:
                weather_summary.append(f"Precip probability: {hourly['precip_probability_today']}%")

            gridpoint = nws.get("gridpoint", {})
            if gridpoint.get("qpf_today_in") is not None:
                weather_summary.append(f"QPF today: {gridpoint['qpf_today_in']:.2f} inches")

            afd = nws.get("afd", {})
            if afd:
                weather_summary.append("\n=== AREA FORECAST DISCUSSION ===")
                if afd.get("confidence_level"):
                    weather_summary.append(f"Confidence level: {afd['confidence_level']}")
                if afd.get("uncertainty_notes"):
                    weather_summary.append(f"Uncertainty flags: {', '.join(afd['uncertainty_notes'][:5])}")
                if prompt_mode == "full":
                    if afd.get("temperature_discussion"):
                        weather_summary.append(f"Temp discussion: {afd['temperature_discussion']}")
                    if afd.get("precipitation_discussion"):
                        weather_summary.append(f"Precip discussion: {afd['precipitation_discussion']}")

            cli = nws.get("cli", {})
            if cli and cli.get("is_final"):
                weather_summary.append("\n=== CLI REPORT (SETTLEMENT) ===")
                if cli.get("high_temp_f") is not None:
                    weather_summary.append(f"Official high: {cli['high_temp_f']}°F")
                if cli.get("precip_inches") is not None:
                    weather_summary.append(f"Official precip: {cli['precip_inches']} inches")

            # NWS RAW SOURCE COMPARISON (for discrepancy detection)
            nws_sources = {}
            if hourly.get("_nws_daily_high") is not None:
                nws_sources["daily"] = hourly["_nws_daily_high"]
            if hourly.get("_nws_hourly_max") is not None:
                nws_sources["hourly_max"] = hourly["_nws_hourly_max"]
            if hourly.get("_tabular_high") is not None:
                nws_sources["tabular"] = hourly["_tabular_high"]
            gp = nws.get("gridpoint", {})
            if gp.get("max_temp_today") is not None:
                nws_sources["gridpoint"] = gp["max_temp_today"]
            if len(nws_sources) >= 2:
                weather_summary.append("\n=== NWS RAW SOURCES (compare for discrepancy) ===")
                for src_name, val in nws_sources.items():
                    weather_summary.append(f"  {src_name}: {val}°F")
                spread = max(nws_sources.values()) - min(nws_sources.values())
                if spread >= 2:
                    weather_summary.append(f"  ⚠️ DISCREPANCY: {spread:.0f}°F spread across NWS sources!")

        # Intraday bias info (from main.py _best_high_estimate)
        if weather_data.get("intraday_warm_bias_f") is not None:
            weather_summary.append(f"\n=== INTRADAY WARM BIAS ===")
            weather_summary.append(
                f"Current obs is {weather_data['intraday_warm_bias_f']}°F warmer than NWS hourly "
                f"(bias adjustment: +{weather_data.get('intraday_bias_adjust_f', 0)}°F to projected high)"
            )

        # Projected high from our estimation pipeline
        if weather_data.get("projected_high_f") is not None:
            weather_summary.append(f"BOT PROJECTED HIGH: {weather_data['projected_high_f']}°F")

        wethr = weather_data.get("wethr", {})
        if wethr:
            weather_summary.append("\n=== WETHR (LST / NWS LOGIC) ===")
            if wethr.get("wethr_high_f") is not None:
                weather_summary.append(f"Wethr high so far: {wethr['wethr_high_f']}°F")
            if wethr.get("wethr_low_f") is not None:
                weather_summary.append(f"Wethr low so far: {wethr['wethr_low_f']}°F")

        wethr_precip = weather_data.get("wethr_precip", {})
        if wethr_precip:
            weather_summary.append("\n=== WETHR PRECIP MTD ===")
            if wethr_precip.get("total_mtd") is not None:
                weather_summary.append(f"Total MTD precip: {wethr_precip['total_mtd']} inches")
            if wethr_precip.get("today_precip") is not None:
                weather_summary.append(f"Today precip: {wethr_precip['today_precip']} inches")
            if wethr_precip.get("has_trace"):
                weather_summary.append("Trace precip observed today: YES")

        ensemble = weather_data.get("ensemble", {})
        if ensemble:
            weather_summary.append("\n=== ENSEMBLE MODELS ===")
            if ensemble.get("model_highs"):
                for m, t in ensemble["model_highs"].items():
                    weather_summary.append(f"  {m}: {t:.1f}°F")
            if ensemble.get("weighted_high_f") is not None:
                weather_summary.append(f"Weighted ensemble high: {ensemble['weighted_high_f']:.1f}°F")
            if ensemble.get("model_spread_f") is not None:
                weather_summary.append(f"Model spread: {ensemble['model_spread_f']:.1f}°F")
            if ensemble.get("weighted_precip_in") is not None:
                weather_summary.append(f"Weighted precip: {ensemble['weighted_precip_in']:.3f} inches")
            if ensemble.get("precip_probability") is not None:
                weather_summary.append(f"Precip probability: {ensemble['precip_probability']:.0f}%")

        # Market data (full or compact)
        market_summary = []
        if prompt_mode == "compact":
            market_summary.append("\n=== CANDIDATE EDGES (TOP) ===")
            for e in edges[:max_edges]:
                ticker = e.get("contract_ticker", "?")
                side = (e.get("side", "?") or "?").upper()
                fair = e.get("fair_price", "?")
                mkt = e.get("market_price", "?")
                edge = e.get("edge_cents", "?")
                yb = e.get("yes_bid_cents", "?")
                ya = e.get("yes_ask_cents", "?")
                nb = e.get("no_bid_cents", "?")
                na = e.get("no_ask_cents", "?")
                spread = e.get("spread_cents", "?")
                vol = e.get("volume", 0)
                oi = e.get("open_interest", 0)
                subtitle = e.get("contract_subtitle", "")
                mtype = e.get("market_type", "?")
                cdate = e.get("contract_date", "")
                market_summary.append(
                    f"  {ticker} [{mtype} {cdate}] side={side} fair={fair} mkt={mkt} edge={edge}¢ "
                    f"YES {yb}/{ya} NO {nb}/{na} spread={spread} vol={vol} oi={oi} {subtitle}".strip()
                )
                if mtype:
                    market_types.add(mtype)
        else:
            for series_ticker, contracts in market_data.items():
                market_summary.append(f"\n=== {series_ticker} ===")
                if series_ticker.startswith("KXHIGH"):
                    market_types.add("high_temp")
                elif series_ticker.startswith("KXRAIN"):
                    if series_ticker.endswith("M"):
                        market_types.add("monthly_rain")
                    else:
                        market_types.add("daily_rain")
                for c in contracts:
                    ticker = c.get("ticker", "?")
                    yes_bid = c.get("yes_bid", "?")
                    yes_ask = c.get("yes_ask", "?")
                    no_bid = c.get("no_bid", "?")
                    no_ask = c.get("no_ask", "?")
                    vol = c.get("volume", 0)
                    oi = c.get("open_interest", 0)
                    floor_s = c.get("floor_strike", "tail_low")
                    cap_s = c.get("cap_strike", "tail_high")
                    subtitle = c.get("subtitle", "")
                    market_summary.append(
                        f"  {ticker}: YES {yes_bid}/{yes_ask} NO {no_bid}/{no_ask} "
                        f"floor={floor_s} cap={cap_s} vol={vol} oi={oi} {subtitle}".strip()
                    )

        # Include computed edges with risk context (llm_first mode)
        all_edges = item.get("all_edges", []) or []
        edge_context = []
        if all_edges and llm_first:
            edge_context.append("\n=== STATISTICAL ANALYSIS (all contracts) ===")
            for e in all_edges[:max_edges]:
                ticker = e.get("contract_ticker", "?")
                side = (e.get("side", "?") or "?").upper()
                blabel = e.get("bracket_label", "")
                fair = e.get("fair_price", "?")
                mkt = e.get("market_price", "?")
                edge_c = e.get("edge_cents", "?")
                edge_p = e.get("edge_pct", 0)
                mtype = e.get("market_type", "?")
                ya = e.get("yes_ask_cents", "?")
                na = e.get("no_ask_cents", "?")
                vol = e.get("volume", 0)
                oi = e.get("open_interest", 0)
                spread = e.get("spread_cents", "?")
                risks_list = e.get("risks", [])
                risks_str = "; ".join(risks_list) if risks_list else "none"
                sa = e.get("source_agreement", {})
                sa_str = ""
                if sa.get("total_sources", 0) > 0:
                    sa_str = f" sources={sa.get('agreement_count',0)}/{sa.get('total_sources',0)}"
                    ens_disagree = sa.get("ensemble_models_disagree_detail", [])
                    if ens_disagree:
                        sa_str += f" (model disagree: {', '.join(ens_disagree)})"
                edge_context.append(
                    f"  {ticker} [{blabel or mtype}] side={side} fair={fair}¢ mkt={mkt}¢ "
                    f"edge={edge_c}¢ ({edge_p:+.1f}%) YES ask={ya} NO ask={na} "
                    f"spread={spread} vol={vol} oi={oi}{sa_str}"
                )
                if risks_list:
                    edge_context.append(f"    RISKS: {risks_str}")

        contract_date = weather_data.get("contract_date", "?")
        block = (
            f"\n=== CITY: {city['name']} ({city_key}) ===\n"
            f"STATION: {city['station_id']}\n"
            f"CONTRACT DATE: {contract_date}\n"
            + "\n".join(weather_summary)
            + "\n"
            + "\n".join(market_summary)
            + ("\n" + "\n".join(edge_context) if edge_context else "")
        )
        city_blocks.append(block)

    # Rules summary across all market types
    rules_summary = []
    for mt in sorted(market_types):
        summary = rule_summary_for_market_type(mt)
        if summary:
            rules_summary.append(f"- {mt}: {summary}")
    rules_text = "\n".join(rules_summary) if rules_summary else ""

    if llm_first:
        task_block = """YOU ARE THE SOLE DECISION-MAKER for trade recommendations.
All statistical data, warnings, and risk flags are provided as context — none are hard blocks.

YOUR TASK:
1. For each city, estimate the most likely CLI high temperature using ALL data:
   - Weight OBSERVATIONS highest (T-group > synoptic max > basic METAR)
   - Then NWS forecasts (daily > gridpoint > hourly)
   - Then ensemble models (ECMWF > GFS > GEM > ICON)
   - If observed max + falling trend → high is likely locked near observed max
   - If temp still rising with hours left → add expected warming to current obs
2. Review ALL contracts across ALL cities. Estimate fair probability for each.
3. Find trades where your fair price differs meaningfully from market price.
4. Recommend your BEST 3-6 trades ranked by expected profit and confidence.
5. For daily rain: use METAR precip observations, QPF forecast, and ensemble precip probability.

EDGE FINDING APPROACH:
- Look for brackets where the market is mispricing the probability distribution
- Bracket bets near your estimated high: the market often underprices the correct bracket
- Tail/threshold NO bets are often the easiest money: if observed max is well below a threshold
  with temp falling, NO on that tail is strong and nearly free. PRIORITIZE these.
- Ensemble consensus: when 3+ of 4 models agree on a range, that's a strong signal
- NWS source disagreement of 1-3°F is NORMAL and expected — do not skip trades for this
- Only treat it as a red flag if sources disagree by ≥5°F or point in opposite directions
- Cheap contracts (under 20¢) with real probability offer the best risk/reward upside
- Expensive contracts (>90¢) with thin edges (<5¢) have poor risk/reward — deprioritize these

SAFETY CHECKS (apply to every trade):
- FLOOR CHECK: The observed max is a floor. It can only go up. Never bet that the high
  will be BELOW the already-observed max.
- WARM BIAS: If current obs exceeds what NWS predicted for this hour, expect the high to
  overshoot. Add the bias to your projected high.
- HOURS REMAINING: More hours = more uncertainty. After 3 PM local with falling temps,
  the high is nearly locked. Before noon, significant uncertainty remains.
- 5-MIN SPIKE: If the 5-min ASOS max exceeds the T-group max, the CLI may settle 1-2°F
  higher. Factor this into your estimate.

MARGIN OF SAFETY:
- For bracket bets: your estimated high should be ≥2°F inside the bracket to recommend YES,
  or ≥2°F outside to recommend NO
- For threshold/tail NO bets: observed max should be ≥3°F from the threshold, OR the
  threshold should be well above all forecasts with the temp falling
- Thin edges (<3¢) on expensive contracts (>90¢) = skip. Look for bigger edges on cheaper contracts.

OUTPUT QUALITY:
- Aim for 3-6 trades across all cities. Finding zero trades means you're being too conservative.
- Include your fair_price estimate (in cents) for every trade.
- Explain your reasoning — which specific sources support this bet?
- A good mix: some high-confidence locked-high NO bets + some moderate-confidence bracket bets with upside.

TOP BETS: Rank your best 3-5 bets in the "top_bets" field with detailed reasoning.
"""
    else:
        task_block = """YOUR TASK (GLOBAL):
1. Use the full data across ALL cities.
2. Estimate fair probabilities and edges for any contracts you find attractive.
3. Recommend specific trades across ALL cities.
4. Only recommend trades that are explicitly listed in the data above.
"""
    return f"""You are an expert weather derivatives trader analyzing Kalshi weather markets across multiple cities.
TIME: {now_utc}
PROMPT_MODE: {prompt_mode}

RULES (from Kalshi PDFs):
{rules_text}

{''.join(city_blocks)}

SETTLEMENT RULES:
- High temperature: NWS CLI report, rounded using NWS rules (0.5 rounds UP)
- Daily rain: Any measurable precipitation at the official station
- Monthly rain: Total monthly precipitation from CLI reports

{task_block}

OUTPUT FORMAT (JSON):
{{
    "confidence": "high|moderate|low",
    "reasoning": "<brief overall reasoning across all cities>",
    "trades": [
        {{
            "contract_ticker": "<ticker>",
            "side": "yes|no",
            "fair_price": <cents>,
            "market_price": <cents>,
            "edge_cents": <cents>,
            "confidence": "high|moderate|low",
            "margin_of_safety": "<explain why this bet is safe — how far is the forecast from the boundary?>",
            "reasoning": "<why this trade>"
        }}
    ],
    "top_bets": [
        {{
            "rank": 1,
            "contract_ticker": "<ticker>",
            "side": "yes|no",
            "city": "<city name>",
            "why_safe": "<detailed explanation of ALL sources aligning, margin of safety in °F>",
            "remaining_risks": "<any residual risk or 'none identified'>",
            "confidence": "high|moderate|low"
        }}
    ]
}}

Return ONLY valid JSON. Do not include any extra text.
Only output JSON. No extra text.
Be precise. Disagreements between data sources are IMPORTANT signals.
When observations conflict with forecasts, weight observations more heavily (especially late in day).
If the AFD mentions uncertainty, increase your forecast uncertainty.
"""


def analyze_with_claude(city_key: str,
                        weather_data: dict,
                        market_data: dict,
                        prompt_mode: str = "full",
                        edges: list[dict] | None = None,
                        max_edges: int = 6) -> dict | None:
    """
    Send data to Claude Opus 4.5 with extended thinking for analysis.
    Returns parsed JSON recommendations or None on failure.
    """
    if _CLAUDE_DISABLED:
        return None

    if not ANTHROPIC_API_KEY:
        _disable_claude("No ANTHROPIC_API_KEY set")
        return None

    if anthropic is None:
        _disable_claude("anthropic package not installed (pip install anthropic)")
        return None

    prompt = build_analysis_prompt(
        city_key,
        weather_data,
        market_data,
        prompt_mode=prompt_mode,
        edges=edges,
        max_edges=max_edges,
    )

    try:
        try:
            client_kwargs = {
                "api_key": ANTHROPIC_API_KEY,
                "max_retries": CLAUDE_MAX_RETRIES,
            }
            if CLAUDE_TIMEOUT_SECONDS > 0:
                client_kwargs["timeout"] = CLAUDE_TIMEOUT_SECONDS
            client = anthropic.Anthropic(**client_kwargs)
        except TypeError:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)
        use_max_tokens = CLAUDE_MAX_TOKENS if prompt_mode != "compact" else CLAUDE_MAX_TOKENS_COMPACT
        use_thinking_budget = CLAUDE_THINKING_BUDGET if prompt_mode != "compact" else CLAUDE_THINKING_BUDGET_COMPACT
        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=use_max_tokens,
            thinking={
                "type": "enabled",
                "budget_tokens": use_thinking_budget,
            },
            messages=[{"role": "user", "content": prompt}],
        )

        # Extract text from response
        result_text = ""
        thinking_text = ""
        for block in response.content:
            if block.type == "thinking":
                thinking_text = block.thinking
            elif block.type == "text":
                result_text = block.text

        log.info(f"Claude thinking length: {len(thinking_text)} chars")

        parsed = _extract_first_json(result_text)
        if parsed is not None:
            parsed["_provider"] = "claude"
            parsed["_thinking"] = thinking_text[:2000]  # store truncated thinking for debugging
            parsed["_raw_response"] = result_text[:1000]
            return parsed
        log.error(f"No JSON found in Claude response: {result_text[:500]}")
        return None

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Claude API error: {e}")
        _disable_claude(f"Claude API error: {e}")
        return None


def analyze_with_claude_global(analysis_bundle: list[dict],
                               prompt_mode: str = "full",
                               max_edges: int = 6) -> dict | None:
    """
    Send all-city data to Claude in a single request.
    Returns parsed JSON recommendations or None on failure.
    """
    if _CLAUDE_DISABLED:
        return None
    if not ANTHROPIC_API_KEY:
        _disable_claude("No ANTHROPIC_API_KEY set")
        return None
    if anthropic is None:
        _disable_claude("anthropic package not installed (pip install anthropic)")
        return None

    prompt = build_global_prompt(analysis_bundle, prompt_mode=prompt_mode, max_edges=max_edges)

    try:
        try:
            client_kwargs = {
                "api_key": ANTHROPIC_API_KEY,
                "max_retries": CLAUDE_MAX_RETRIES,
            }
            if CLAUDE_TIMEOUT_SECONDS > 0:
                client_kwargs["timeout"] = CLAUDE_TIMEOUT_SECONDS
            client = anthropic.Anthropic(**client_kwargs)
        except TypeError:
            client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

        use_max_tokens = CLAUDE_MAX_TOKENS if prompt_mode != "compact" else CLAUDE_MAX_TOKENS_COMPACT
        use_thinking_budget = CLAUDE_THINKING_BUDGET if prompt_mode != "compact" else CLAUDE_THINKING_BUDGET_COMPACT

        response = client.messages.create(
            model=CLAUDE_MODEL,
            max_tokens=use_max_tokens,
            thinking={
                "type": "enabled",
                "budget_tokens": use_thinking_budget,
            },
            messages=[{"role": "user", "content": prompt}],
        )

        result_text = ""
        thinking_text = ""
        for block in response.content:
            if block.type == "thinking":
                thinking_text = block.thinking
            elif block.type == "text":
                result_text = block.text

        log.info(f"Claude thinking length (global): {len(thinking_text)} chars")

        parsed = _extract_first_json(result_text)
        if parsed is not None:
            parsed["_provider"] = "claude"
            parsed["_thinking"] = thinking_text[:2000]
            parsed["_raw_response"] = result_text[:1000]
            return parsed
        log.error(f"No JSON found in Claude response: {result_text[:500]}")
        return None

    except json.JSONDecodeError as e:
        log.error(f"Failed to parse Claude JSON: {e}")
        return None
    except Exception as e:
        log.error(f"Claude API error: {e}")
        _disable_claude(f"Claude API error: {e}")
        return None


def merge_analysis(statistical_edges: list[dict], claude_analysis: dict | None) -> list[dict]:
    """
    Merge statistical edge calculations with Claude's analysis.
    Claude's recommendations override statistical when they disagree.

    Returns final list of trade recommendations.
    """
    llm_first = TRADING.get("_active_profile") == "llm_first"

    if claude_analysis is None:
        # No Claude analysis - set final fields from statistical data
        for edge in statistical_edges:
            edge["final_side"] = edge.get("side", "none")
            edge["final_edge_cents"] = edge.get("edge_cents", 0)
            edge["combined_confidence"] = "statistical_only"
        return statistical_edges

    claude_trades = {t.get("contract_ticker"): t for t in claude_analysis.get("trades", []) if t.get("contract_ticker")}
    final = []
    min_edge_pct = TRADING.get("min_edge_percent", 2.0)
    min_buy_prob = TRUST_GATES.get("min_fair_prob_to_recommend_buy", TRADING.get("min_fair_prob_to_recommend_buy", 0.60))
    min_trust_score = TRUST_GATES.get("trust_score_min_for_llm_override", 0)
    min_edge_net = TRUST_GATES.get("min_edge_after_fees_cents", TRADING.get("min_edge_after_fees_cents", 0))

    def _compute_side_metrics(edge: dict, side: str) -> dict | None:
        fair_prob = edge.get("fair_prob")
        if fair_prob is None:
            return None
        yes_ask = edge.get("yes_ask_cents")
        no_ask = edge.get("no_ask_cents")
        yes_bid = edge.get("yes_bid_cents")
        no_bid = edge.get("no_bid_cents")
        yes_spread = edge.get("yes_spread_cents")
        no_spread = edge.get("no_spread_cents")
        yes_bid_size = edge.get("yes_bid_size")
        yes_ask_size = edge.get("yes_ask_size")
        no_bid_size = edge.get("no_bid_size")
        no_ask_size = edge.get("no_ask_size")

        if side == "yes":
            side_ask = yes_ask
            side_bid = yes_bid
            spread = yes_spread
            side_bid_size = yes_bid_size
            side_ask_size = yes_ask_size
            side_prob = fair_prob
            fair_side_price = fair_prob * 100
        elif side == "no":
            side_ask = no_ask
            side_bid = no_bid
            spread = no_spread
            side_bid_size = no_bid_size
            side_ask_size = no_ask_size
            side_prob = 1 - fair_prob
            fair_side_price = (1 - fair_prob) * 100
        else:
            return None

        if side_ask is None or side_ask <= 0:
            return None

        edge_cents = fair_side_price - side_ask
        edge_pct = edge_cents / max(side_ask, 1) * 100
        return {
            "side": side,
            "side_prob": side_prob,
            "fair_side_price": fair_side_price,
            "edge_cents": edge_cents,
            "edge_pct": edge_pct,
            "side_ask": side_ask,
            "side_bid": side_bid,
            "spread": spread,
            "side_bid_size": side_bid_size,
            "side_ask_size": side_ask_size,
        }

    def _llm_can_override_soft(merged: dict, ct: dict) -> bool:
        if not merged.get("soft_block"):
            return True
        conf = (ct.get("confidence") or "").lower()
        if conf not in ("high", "moderate"):
            return False
        if merged.get("trust_score", 0) < min_trust_score:
            return False
        return True

    def _apply_llm_override(merged: dict, ct: dict) -> None:
        """Apply LLM override only when it aligns with positive, executable edge."""
        if not llm_first:
            if merged.get("hard_block"):
                merged["combined_confidence"] = "low"
                merged["final_side"] = merged.get("side", "none")
                merged["final_edge_cents"] = merged.get("edge_cents", 0)
                merged["override_reason"] = "LLM blocked by hard gate"
                return
            if not _llm_can_override_soft(merged, ct):
                merged["combined_confidence"] = "low"
                merged["final_side"] = merged.get("side", "none")
                merged["final_edge_cents"] = merged.get("edge_cents", 0)
                merged["override_reason"] = "LLM blocked by soft gate"
                return
        side = (ct.get("side") or "none").lower()
        merged["claude_side"] = side
        merged["claude_confidence"] = ct.get("confidence", "low")
        merged["claude_reasoning"] = ct.get("reasoning", "")
        # If LLM provides a fair price, use it to compute side metrics for override.
        fair_price = ct.get("fair_price")
        if fair_price is not None:
            try:
                fair_price = float(fair_price)
                if 0 <= fair_price <= 100:
                    merged["fair_price"] = fair_price
                    merged["fair_prob"] = max(0.0, min(1.0, fair_price / 100.0))
            except (TypeError, ValueError):
                pass
        metrics = _compute_side_metrics(merged, side)
        if metrics is None:
            merged["combined_confidence"] = "low"
            merged["final_side"] = merged.get("side", "none")
            merged["final_edge_cents"] = merged.get("edge_cents", 0)
            return

        # Require real positive edge and minimum probability threshold (skip in llm_first)
        if not llm_first:
            if metrics["edge_cents"] <= 0 or abs(metrics["edge_pct"]) < min_edge_pct or metrics["side_prob"] < min_buy_prob:
                merged["combined_confidence"] = "low"
                merged["final_side"] = merged.get("side", "none")
                merged["final_edge_cents"] = merged.get("edge_cents", 0)
                merged["override_reason"] = "LLM override failed minimum edge/probability"
                return
            fee_cents = TRADING.get("estimated_fee_cents", 0) or 0
            if min_edge_net:
                net_edge = metrics["edge_cents"] - fee_cents
                if net_edge < min_edge_net:
                    merged["combined_confidence"] = "low"
                    merged["final_side"] = merged.get("side", "none")
                    merged["final_edge_cents"] = merged.get("edge_cents", 0)
                    merged["override_reason"] = "LLM override failed net edge after fees"
                    return

        merged["final_side"] = side
        merged["final_edge_cents"] = metrics["edge_cents"]
        merged["market_price"] = metrics["side_ask"]
        merged["side_bid_cents"] = metrics["side_bid"]
        merged["side_ask_cents"] = metrics["side_ask"]
        merged["spread_cents"] = metrics["spread"]
        merged["side_bid_size"] = metrics["side_bid_size"]
        merged["side_ask_size"] = metrics["side_ask_size"]

        merged["signal"] = f"BUY {side.upper()} @ ask ≤ {metrics['side_ask']}¢"
        conf = merged.get("claude_confidence", "low")
        if conf == "high":
            merged["confidence"] = "High"
        elif conf == "moderate":
            merged["confidence"] = "Med"
        else:
            merged["confidence"] = "Low"
        merged["combined_confidence"] = conf

        reasoning = ct.get("reasoning")
        if reasoning:
            merged["why"] = [reasoning]

    # Track which claude trades matched a statistical edge
    matched_claude_tickers = set()

    for edge in statistical_edges:
        ticker = edge.get("contract_ticker", "")
        if edge.get("hard_block") and not llm_first:
            edge["combined_confidence"] = "blocked"
            edge["final_side"] = "none"
            edge["final_edge_cents"] = 0
            final.append(edge)
            continue
        if ticker in claude_trades:
            matched_claude_tickers.add(ticker)
            ct = claude_trades[ticker]
            # Claude override
            merged = {**edge}
            merged["claude_side"] = ct.get("side", "none")
            merged["claude_fair_price"] = ct.get("fair_price")
            merged["claude_edge_cents"] = ct.get("edge_cents", 0)
            merged["claude_confidence"] = ct.get("confidence", "low")
            merged["claude_reasoning"] = ct.get("reasoning", "")
            merged["margin_of_safety"] = ct.get("margin_of_safety", "")

            if llm_first:
                # In llm_first mode, Claude is the decision-maker — trust its judgment
                side = (ct.get("side") or "none").lower()
                conf = (ct.get("confidence") or "low").lower()
                if side != "none":
                    # Use Claude's fair price if provided, else fall back to statistical
                    fair_price = ct.get("fair_price")
                    if fair_price is not None:
                        try:
                            fair_price = float(fair_price)
                            if 0 <= fair_price <= 100:
                                merged["fair_price"] = fair_price
                                merged["fair_prob"] = max(0.0, min(1.0, fair_price / 100.0))
                        except (TypeError, ValueError):
                            pass
                    metrics = _compute_side_metrics(merged, side)
                    if metrics:
                        merged["final_side"] = side
                        merged["final_edge_cents"] = metrics["edge_cents"]
                        merged["market_price"] = metrics["side_ask"]
                        merged["side_bid_cents"] = metrics["side_bid"]
                        merged["side_ask_cents"] = metrics["side_ask"]
                        merged["spread_cents"] = metrics["spread"]
                        merged["side_bid_size"] = metrics["side_bid_size"]
                        merged["side_ask_size"] = metrics["side_ask_size"]
                        merged["signal"] = f"BUY {side.upper()} @ ask ≤ {metrics['side_ask']}¢"
                    else:
                        merged["final_side"] = side
                        merged["final_edge_cents"] = ct.get("edge_cents", edge.get("edge_cents", 0))
                        ask = edge.get(f"{side}_ask_cents", edge.get("side_ask_cents", 0))
                        merged["signal"] = f"BUY {side.upper()} @ ask ≤ {ask}¢"
                    merged["combined_confidence"] = conf
                    if conf == "high":
                        merged["confidence"] = "High"
                    elif conf == "moderate":
                        merged["confidence"] = "Med"
                    else:
                        merged["confidence"] = "Low"
                    reasoning = ct.get("reasoning")
                    if reasoning:
                        merged["why"] = [reasoning]
                else:
                    # Claude says skip this one
                    merged["combined_confidence"] = "claude_skip"
                    merged["final_side"] = edge.get("side", "none")
                    merged["final_edge_cents"] = edge.get("edge_cents", 0)
            else:
                # Original gated merge logic
                # If Claude and stats agree on direction, boost confidence (keep stats pricing)
                if ct.get("side") == edge.get("side") and edge.get("side") != "none":
                    if _llm_can_override_soft(merged, ct):
                        merged["combined_confidence"] = "high"
                        merged["final_side"] = edge["side"]
                        merged["final_edge_cents"] = edge.get("edge_cents", 0)
                    else:
                        merged["combined_confidence"] = "low"
                        merged["final_side"] = "none"
                        merged["final_edge_cents"] = 0
                        merged["override_reason"] = "LLM agreement blocked by soft gate"
                elif ct.get("side") != "none" and ct.get("confidence") in ("high", "moderate"):
                    # Claude disagrees - only override if positive, executable edge
                    _apply_llm_override(merged, ct)
                else:
                    # Weak signal from both - keep statistical only
                    merged["combined_confidence"] = "statistical_only"
                    if merged.get("soft_block"):
                        merged["final_side"] = "none"
                        merged["final_edge_cents"] = 0
                    else:
                        merged["final_side"] = edge.get("side", "none")
                        merged["final_edge_cents"] = edge.get("edge_cents", 0)

            final.append(merged)
        else:
            # No Claude opinion - use statistical only with lower confidence
            edge["combined_confidence"] = "statistical_only"
            if edge.get("soft_block") and not llm_first:
                edge["final_side"] = "none"
                edge["final_edge_cents"] = 0
            else:
                edge["final_side"] = edge.get("side", "none")
                edge["final_edge_cents"] = edge.get("edge_cents", 0)
            final.append(edge)

    # In llm_first mode, also add Claude trades that didn't match any statistical edge
    # (e.g., Claude found a trade the stats pipeline missed)
    if llm_first:
        for ticker, ct in claude_trades.items():
            if ticker in matched_claude_tickers:
                continue
            side = (ct.get("side") or "none").lower()
            if side == "none":
                continue
            # Build a synthetic edge from Claude's recommendation
            synth = {
                "contract_ticker": ticker,
                "claude_side": side,
                "claude_fair_price": ct.get("fair_price"),
                "claude_edge_cents": ct.get("edge_cents", 0),
                "claude_confidence": ct.get("confidence", "low"),
                "claude_reasoning": ct.get("reasoning", ""),
                "margin_of_safety": ct.get("margin_of_safety", ""),
                "final_side": side,
                "final_edge_cents": ct.get("edge_cents", 0),
                "combined_confidence": (ct.get("confidence") or "low").lower(),
                "confidence": "High" if (ct.get("confidence") or "").lower() == "high" else ("Med" if (ct.get("confidence") or "").lower() == "moderate" else "Low"),
                "signal": f"BUY {side.upper()} (claude-only)",
                "why": [ct.get("reasoning", "Claude-only trade")],
                "source": "claude_only",
            }
            final.append(synth)

    # Sort by absolute final edge and return only actionable (side != none)
    final.sort(key=lambda x: abs(x.get("final_edge_cents", 0)), reverse=True)
    return [r for r in final if r.get("final_side") != "none"]
