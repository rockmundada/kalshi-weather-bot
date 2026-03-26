"""
Backtest/evaluate logged predictions against settled CLI data (IEM).

Usage:
  python backtest.py --since 2026-02-01 --until 2026-02-08
  python backtest.py --path data/predictions.csv
"""
import argparse
import csv
from datetime import date, datetime
from typing import Optional

from config import CITIES, PREDICTIONS_LOG_PATH
from data_sources.iem import get_cli_iem
from analysis.edge import _parse_temp_subtitle


def _parse_date(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        return date.fromisoformat(s)
    except Exception:
        return None


def _temp_outcome(actual_high: float, subtitle: str) -> Optional[int]:
    bounds = _parse_temp_subtitle(subtitle)
    if not bounds:
        return None
    kind = bounds.get("kind")
    if kind == "range":
        return 1 if bounds.get("low") <= actual_high <= bounds.get("high") else 0
    if kind == "below":
        return 1 if actual_high <= bounds.get("cap") else 0
    if kind == "above":
        return 1 if actual_high >= bounds.get("floor") else 0
    return None


def _precip_outcome(actual_precip: float, floor: Optional[float], cap: Optional[float]) -> Optional[int]:
    if floor is None and cap is None:
        return 1 if actual_precip >= 0.01 else 0
    if floor is None and cap is not None:
        return 1 if actual_precip <= cap else 0
    if floor is not None and cap is None:
        return 1 if actual_precip >= floor else 0
    if cap is not None and floor is not None:
        if cap < floor:
            return None
        return 1 if floor <= actual_precip <= cap else 0
    return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--path", default=PREDICTIONS_LOG_PATH, help="Predictions CSV path")
    parser.add_argument("--since", default=None, help="YYYY-MM-DD inclusive")
    parser.add_argument("--until", default=None, help="YYYY-MM-DD inclusive")
    args = parser.parse_args()

    since = _parse_date(args.since)
    until = _parse_date(args.until)

    totals = {
        "high_temp": {"n": 0, "mae": 0.0, "brier": 0.0, "brier_n": 0, "buy_n": 0, "buy_hit": 0},
        "daily_rain": {"n": 0, "brier": 0.0, "brier_n": 0, "buy_n": 0, "buy_hit": 0},
    }

    with open(args.path, newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            city = row.get("city")
            if not city or city not in CITIES:
                continue
            contract_date = _parse_date(row.get("contract_date"))
            if not contract_date:
                continue
            if since and contract_date < since:
                continue
            if until and contract_date > until:
                continue

            cli = get_cli_iem(city, contract_date)
            if not cli or not cli.get("is_settled"):
                continue

            market_type = row.get("market_type")
            fair_prob = row.get("fair_prob")
            try:
                fair_prob = float(fair_prob)
            except Exception:
                fair_prob = None

            signal = row.get("signal", "")
            side = row.get("side", "")

            if market_type == "high_temp":
                actual_high = cli.get("high_temp_f")
                if actual_high is None:
                    continue
                totals["high_temp"]["n"] += 1
                forecast_high = row.get("forecast_high_f")
                try:
                    forecast_high = float(forecast_high)
                except Exception:
                    forecast_high = None
                if forecast_high is not None:
                    totals["high_temp"]["mae"] += abs(forecast_high - actual_high)
                subtitle = row.get("contract_subtitle", "")
                outcome = _temp_outcome(actual_high, subtitle)
                if outcome is not None and fair_prob is not None:
                    totals["high_temp"]["brier"] += (fair_prob - outcome) ** 2
                    totals["high_temp"]["brier_n"] += 1
                if signal.startswith("BUY"):
                    totals["high_temp"]["buy_n"] += 1
                    if side == "yes" and outcome == 1:
                        totals["high_temp"]["buy_hit"] += 1
                    if side == "no" and outcome == 0:
                        totals["high_temp"]["buy_hit"] += 1

            elif market_type == "daily_rain":
                actual_precip = cli.get("precip_inches")
                if actual_precip is None:
                    continue
                totals["daily_rain"]["n"] += 1
                try:
                    floor = float(row.get("floor_strike")) if row.get("floor_strike") not in (None, "") else None
                except Exception:
                    floor = None
                try:
                    cap = float(row.get("cap_strike")) if row.get("cap_strike") not in (None, "") else None
                except Exception:
                    cap = None
                outcome = _precip_outcome(actual_precip, floor, cap)
                if outcome is not None and fair_prob is not None:
                    totals["daily_rain"]["brier"] += (fair_prob - outcome) ** 2
                    totals["daily_rain"]["brier_n"] += 1
                if signal.startswith("BUY"):
                    totals["daily_rain"]["buy_n"] += 1
                    if side == "yes" and outcome == 1:
                        totals["daily_rain"]["buy_hit"] += 1
                    if side == "no" and outcome == 0:
                        totals["daily_rain"]["buy_hit"] += 1

    # Summary
    print("Backtest summary")
    for mt, stats in totals.items():
        print(f"\n{mt}:")
        if stats["n"] == 0:
            print("  No settled rows.")
            continue
        if mt == "high_temp":
            mae = stats["mae"] / max(stats["n"], 1)
            print(f"  Mean abs error (forecast high): {mae:.2f}°F")
        if stats["brier_n"]:
            brier = stats["brier"] / stats["brier_n"]
            print(f"  Brier score (fair_prob vs outcome): {brier:.4f} over {stats['brier_n']} rows")
        if stats["buy_n"]:
            hit = stats["buy_hit"] / stats["buy_n"]
            print(f"  BUY hit rate: {hit*100:.1f}% ({stats['buy_hit']}/{stats['buy_n']})")
        else:
            print("  BUY hit rate: n/a")


if __name__ == "__main__":
    main()
