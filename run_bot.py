#!/usr/bin/env python3
"""
Automated trading mode - analyzes markets and executes trades.
Use with caution. Default interval: 30 minutes.

Usage:
    python run_bot.py                    # single pass, live trading (conservative)
    python run_bot.py --dry-run          # single pass, no real trades (conservative)
    python run_bot.py --dry-run --both   # dry run, today + tomorrow
    python run_bot.py --loop             # continuous, 30min interval
    python run_bot.py --loop --interval=15   # continuous, 15min interval
    python run_bot.py --loop --dry-run   # continuous dry run
    python run_bot.py --profile=llm_first    # use LLM-first profile (old behavior)
    python run_bot.py --profile=conservative # use conservative profile (default)
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import apply_trading_profile, TRADING_PROFILE_DEFAULT


def main():
    dry_run = "--dry-run" in sys.argv
    mode = "recommend" if dry_run else "trade"

    # Parse profile
    profile = TRADING_PROFILE_DEFAULT
    for arg in sys.argv:
        if arg.startswith("--profile="):
            profile = arg.split("=", 1)[1]

    # Apply profile BEFORE creating the bot
    apply_trading_profile(profile)

    from main import WeatherTradingBot

    include_today = True
    include_tomorrow = False
    if "--both" in sys.argv:
        include_today = True
        include_tomorrow = True
    elif "--tomorrow" in sys.argv:
        include_today = False
        include_tomorrow = True
    bot = WeatherTradingBot(mode=mode, include_today=include_today, include_tomorrow=include_tomorrow)

    if dry_run:
        import logging
        logging.getLogger().warning(f"DRY RUN MODE ENABLED [{profile.upper()}] - no real trades")

    profile_desc = {
        "conservative": "BUY NO only, edge cap 20¢, strict gates",
        "llm_first": "LLM decides everything, no gates",
        "margin_of_safety": "Benjamin Graham approach, high win rate",
        "safe": "Threshold-preferred, NWS-aligned",
        "aggressive": "Original loose settings",
    }
    print(f"\n{'='*60}")
    print(f"  KALSHI WEATHER BOT [{profile.upper()}]")
    print(f"  {profile_desc.get(profile, profile)}")
    print(f"  Mode: {'DRY RUN' if dry_run else 'LIVE TRADING'}")
    print(f"{'='*60}\n")

    if "--loop" in sys.argv:
        interval = 30
        for arg in sys.argv:
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
        bot.run_loop(interval_minutes=interval)
    else:
        results = bot.run_single_pass()
        print(f"\nCompleted {'dry run' if dry_run else 'trading'} pass for {len(results)} cities")


if __name__ == "__main__":
    main()
