#!/usr/bin/env python3
"""
Run morning recommendations without executing trades.
Use this to review opportunities before enabling automated trading.
"""
import sys
import os

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from main import WeatherTradingBot


def main():
    bot = WeatherTradingBot(mode="recommend")

    if "--loop" in sys.argv:
        interval = 30
        for arg in sys.argv:
            if arg.startswith("--interval="):
                interval = int(arg.split("=")[1])
        bot.run_loop(interval_minutes=interval)
    else:
        results = bot.run_single_pass()
        print(f"\nCompleted analysis for {len(results)} cities")


if __name__ == "__main__":
    main()
