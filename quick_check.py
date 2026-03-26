#!/usr/bin/env python3
"""
Quick check for a single city - useful for testing/debugging.

Usage:
    python quick_check.py Chicago
    python quick_check.py NYC --no-claude    # skip LLM analysis (faster)
    python quick_check.py --list             # show available cities
"""
import sys
import os
import json
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import CITIES, LLM_PROMPT_MODE, LLM_MAX_EDGES_IN_PROMPT
from data_sources.metar import get_metar_data
from data_sources.nws import get_all_nws_data
from data_sources.ensemble import get_ensemble_forecast
from trading.kalshi_api import KalshiAPI
from analysis.edge import analyze_all_contracts, estimate_uncertainty, build_temp_distribution
from analysis.llm_ai import analyze_with_llm


def quick_check(city_key: str, use_llm: bool = True):
    print(f"\n{'='*60}")
    print(f"  QUICK CHECK: {city_key}")
    print(f"{'='*60}\n")

    city = CITIES[city_key]

    # METAR
    print("📡 Fetching METAR...")
    metar = get_metar_data(city_key)
    print(f"  Current temp: {metar.get('current_temp_f', '?')}°F ({metar.get('current_temp_precision', '?')})")
    print(f"  Best max today: {metar.get('best_max_f', '?')}°F (source: {metar.get('best_max_source', '?')})")
    print(f"  Precip today: {metar.get('total_precip_today', '?')} inches")
    print(f"  Rained today: {metar.get('has_rained_today', '?')}")

    # NWS
    print("\n🌤 Fetching NWS data...")
    nws = get_all_nws_data(city_key)
    hourly = nws.get("hourly", {})
    print(f"  Forecast high (day): {hourly.get('forecast_high_day', hourly.get('forecast_high_today', '?'))}°F")
    print(f"  Temp trend: {hourly.get('temp_trend', '?')}")
    print(f"  Hours remaining: {hourly.get('hours_remaining', '?')}")
    print(f"  Precip probability: {hourly.get('precip_probability_today', '?')}%")

    gridpoint = nws.get("gridpoint", {})
    print(f"  QPF: {gridpoint.get('qpf_today_in', '?')} inches")

    afd = nws.get("afd", {})
    if afd:
        print(f"  AFD confidence: {afd.get('confidence_level', '?')}")
        if afd.get("uncertainty_notes"):
            print(f"  Uncertainty flags: {', '.join(afd['uncertainty_notes'][:3])}")

    cli = nws.get("cli", {})
    if cli and cli.get("high_temp_f") is not None:
        print(f"  CLI high: {cli['high_temp_f']}°F (final: {cli.get('is_final', False)})")

    # Ensemble
    print("\n🔮 Fetching ensemble forecasts...")
    ensemble = get_ensemble_forecast(city_key)
    for model, temp in ensemble.get("model_highs", {}).items():
        print(f"  {model}: {temp:.1f}°F")
    print(f"  Weighted: {ensemble.get('weighted_high_f', '?')}°F")
    print(f"  Spread: {ensemble.get('model_spread_f', '?')}°F")
    print(f"  Precip prob: {ensemble.get('precip_probability', '?')}%")

    # Combine data
    weather_data = {
        "metar": metar, "nws": nws, "ensemble": ensemble,
        "hours_remaining": hourly.get("hours_remaining", 12),
        "model_spread_f": ensemble.get("model_spread_f", 0),
        "afd_confidence": afd.get("confidence_level", "moderate"),
        "has_metar_obs": bool(metar.get("current_temp_f")),
    }

    # Best estimate
    estimates = []
    if metar.get("best_max_f"):
        estimates.append(metar["best_max_f"])
    if hourly.get("forecast_high_day") or hourly.get("forecast_high_today"):
        estimates.append(float(hourly.get("forecast_high_day") or hourly.get("forecast_high_today")))
    if ensemble.get("weighted_high_f"):
        estimates.append(ensemble["weighted_high_f"])

    if estimates:
        best_high = sum(estimates) / len(estimates)
        weather_data["best_forecast_high_f"] = best_high
        print(f"\n📊 Best high estimate: {best_high:.1f}°F")

        unc = estimate_uncertainty(weather_data)
        print(f"  Uncertainty: ±{unc:.1f}°F")

        dist = build_temp_distribution(best_high, unc)
        print(f"  Distribution peak: {max(dist, key=dist.get)}°F ({max(dist.values())*100:.1f}%)")

    # Rain estimate
    rain_probs = []
    if hourly.get("precip_probability_today"):
        rain_probs.append(hourly["precip_probability_today"] / 100.0)
    if ensemble.get("precip_probability"):
        rain_probs.append(ensemble["precip_probability"] / 100.0)
    if metar.get("has_rained_today"):
        rain_probs = [0.99]

    if rain_probs:
        rain_prob = sum(rain_probs) / len(rain_probs)
        weather_data["rain_probability"] = rain_prob
        print(f"  Rain probability: {rain_prob*100:.0f}%")

    weather_data["monthly_precip_forecast_in"] = ensemble.get("monthly_precip_forecast")
    weather_data["monthly_precip_uncertainty_in"] = 1.5

    # Market data
    print("\n📈 Fetching Kalshi markets...")
    api = KalshiAPI()
    market_data = {}
    tickers = city.get("kalshi_tickers", {})
    for market_type, series_ticker in tickers.items():
        contracts = api.get_active_contracts(series_ticker)
        if contracts:
            market_data[series_ticker] = contracts
            print(f"  {series_ticker}: {len(contracts)} contracts")

    if market_data:
        # Statistical edges
        today_key = datetime.now(ZoneInfo(city["timezone"])).date().isoformat()
        edges, _all_edges = analyze_all_contracts(city_key, {today_key: weather_data}, market_data, return_all=True)
        print(f"\n🎯 Statistical edges: {len(edges)} opportunities")
        for e in edges[:8]:
            side = e.get("side", "?").upper()
            ticker = e.get("contract_ticker", "?")
            edge = e.get("edge_cents", 0)
            fair = e.get("fair_price", "?")
            mkt = e.get("market_price", "?")
            print(f"  {side} {ticker} edge={edge:.1f}¢ fair={fair:.0f}¢ mkt={mkt}¢")

        # LLM analysis
        if use_llm:
            print("\n🧠 Running LLM analysis...")
            llm = analyze_with_llm(
                city_key,
                weather_data,
                market_data,
                prompt_mode=LLM_PROMPT_MODE,
                edges=edges,
                max_edges=LLM_MAX_EDGES_IN_PROMPT,
            )
            if llm:
                provider = llm.get("_provider", "?")
                print(f"  Provider: {provider}")
                print(f"  Forecast high: {llm.get('forecast_high_f', '?')}°F")
                print(f"  Confidence: {llm.get('confidence', '?')}")
                print(f"  Reasoning: {llm.get('reasoning', '?')[:200]}")
                for t in llm.get("trades", [])[:5]:
                    print(f"  → {t.get('side', '?').upper()} {t.get('contract_ticker', '?')} "
                          f"edge={t.get('edge_cents', 0):.1f}¢ [{t.get('confidence', '?')}]")
        else:
            print("\n(Skipping LLM analysis)")

    print(f"\n{'='*60}\n")


def main():
    if "--list" in sys.argv:
        print("Available cities:")
        for key, city in CITIES.items():
            tickers = list(city.get("kalshi_tickers", {}).keys())
            print(f"  {key}: {city['name']} - markets: {', '.join(tickers)}")
        return

    if len(sys.argv) < 2:
        print("Usage: python quick_check.py <city> [--no-claude] [--list]")
        print("Cities:", ", ".join(CITIES.keys()))
        return

    city_key = sys.argv[1]
    if city_key not in CITIES:
        # Try case-insensitive match
        for k in CITIES:
            if k.lower() == city_key.lower():
                city_key = k
                break
        else:
            print(f"Unknown city: {city_key}")
            print("Available:", ", ".join(CITIES.keys()))
            return

    use_llm = "--no-claude" not in sys.argv
    quick_check(city_key, use_llm)


if __name__ == "__main__":
    main()
