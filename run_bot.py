#!/usr/bin/env python3
"""
Automated trading mode - analyzes markets and executes trades.
Use with caution. Default interval: 30 minutes.

Usage:
    python run_bot.py                    # single pass, live trading
    python run_bot.py --dry-run          # single pass, no real trades
    python run_bot.py --loop             # continuous, 30min interval
    python run_bot.py --loop --interval=15   # continuous, 15min interval
    python run_bot.py --loop --dry-run   # continuous dry run
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import WeatherTradingBot


def main():
    dry_run = "--dry-run" in sys.argv
    mode = "recommend" if dry_run else "trade"
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
        logging.getLogger().warning("DRY RUN MODE ENABLED - no real trades")

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
