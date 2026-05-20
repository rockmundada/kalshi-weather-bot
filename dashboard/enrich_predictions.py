"""
Enrich predictions.csv with actual weather outcomes and compute trade results.

Pulls actual high temps from hardcoded verified historical data (Iowa Mesonet ASOS),
determines contract outcomes, and computes P&L for each actionable signal.
"""

import csv
import sqlite3
import re
import os

ACTUAL_HIGHS = {
    ("NYC", "2026-02-10"): 36.0,
    ("NYC", "2026-02-11"): 41.0,
    ("Chicago", "2026-02-10"): 48.0,
    ("Chicago", "2026-02-11"): 43.0,
    ("Miami", "2026-02-10"): 76.0,
    ("Miami", "2026-02-11"): 77.0,
    ("Austin", "2026-02-10"): 69.0,
    ("Austin", "2026-02-11"): 75.0,
    ("Denver", "2026-02-10"): 45.0,
    ("Denver", "2026-02-11"): 57.0,
    ("LA", "2026-02-10"): 65.0,
    ("LA", "2026-02-11"): 63.0,
    ("Philadelphia", "2026-02-10"): 39.0,
    ("Philadelphia", "2026-02-11"): 46.0,
}

ACTUAL_RAIN = {
    ("NYC", "2026-02-10"): False,
    ("NYC", "2026-02-11"): True,
}

DATA_SOURCES = {
    "KNYC": "Iowa Mesonet ASOS (KNYC)",
    "KMDW": "Iowa Mesonet ASOS (KMDW)",
    "KMIA": "Iowa Mesonet ASOS (KMIA)",
    "KAUS": "Iowa Mesonet ASOS (KAUS)",
    "KDEN": "Iowa Mesonet ASOS (KDEN)",
    "KLAX": "Iowa Mesonet ASOS (KLAX)",
    "KPHL": "Iowa Mesonet ASOS (KPHL)",
}


def parse_signal(signal):
    """Parse signal string into (side, limit_price) or None if not actionable."""
    m = re.match(r"BUY (YES|NO) @ ask ≤ (\d+)¢", signal)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def determine_contract_outcome(row, actual_high):
    """Determine if contract resolved YES or NO based on actual temperature."""
    subtitle = row["contract_subtitle"]
    market_type = row["market_type"]

    if market_type == "daily_rain":
        city = row["city"]
        date = row["contract_date"]
        rained = ACTUAL_RAIN.get((city, date))
        if rained is None:
            return None
        return "YES" if rained else "NO"

    floor_strike = row.get("floor_strike", "")
    cap_strike = row.get("cap_strike", "")

    if "or above" in subtitle:
        threshold = float(floor_strike) if floor_strike else None
        if threshold is None:
            return None
        return "YES" if actual_high >= threshold else "NO"

    if "or below" in subtitle:
        threshold = float(cap_strike) if cap_strike else None
        if threshold is None:
            return None
        return "YES" if actual_high <= threshold else "NO"

    # Handle ">65°" format (above threshold, only floor_strike set)
    if floor_strike and not cap_strike:
        threshold = float(floor_strike)
        return "YES" if actual_high > threshold else "NO"

    # Handle "<51°" format (below threshold, only cap_strike set)
    if not floor_strike and cap_strike:
        threshold = float(cap_strike)
        return "YES" if actual_high < threshold else "NO"

    # Handle both "36° to 37°" and "be 67-68°" subtitle formats
    if floor_strike and cap_strike:
        low = float(floor_strike)
        high = float(cap_strike)
        return "YES" if low <= actual_high <= high else "NO"

    return None


def compute_pnl(buy_side, limit_price, contract_outcome):
    """
    Compute P&L in cents for a single contract.
    BUY YES at X cents: if YES resolves, profit = 100-X. If NO, loss = -X.
    BUY NO at X cents: if NO resolves, profit = 100-X. If YES, loss = -X.
    """
    if buy_side == "YES":
        entry_cost = limit_price
        if contract_outcome == "YES":
            return 100 - entry_cost
        else:
            return -entry_cost
    else:  # BUY NO
        entry_cost = limit_price
        if contract_outcome == "NO":
            return 100 - entry_cost
        else:
            return -entry_cost


def enrich():
    base = os.path.dirname(os.path.abspath(__file__))
    input_path = os.path.join(base, "..", "data", "predictions.csv")
    output_path = os.path.join(base, "enriched_predictions.csv")
    db_path = os.path.join(base, "kalshi_analytics.db")

    with open(input_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    new_fields = [
        "actual_high_f",
        "actual_outcome",
        "is_actionable",
        "buy_side",
        "limit_price_cents",
        "prediction_correct",
        "pnl_cents",
        "outcome_data_source",
    ]
    out_fieldnames = fieldnames + new_fields

    enriched = []
    for row in rows:
        city = row["city"]
        date = row["contract_date"]
        actual_high = ACTUAL_HIGHS.get((city, date))

        row["actual_high_f"] = str(actual_high) if actual_high is not None else ""

        outcome = determine_contract_outcome(row, actual_high) if actual_high is not None else None
        row["actual_outcome"] = outcome or ""

        parsed = parse_signal(row["signal"])
        row["is_actionable"] = "1" if parsed else "0"

        if parsed and outcome:
            buy_side, limit_price = parsed
            row["buy_side"] = buy_side
            row["limit_price_cents"] = str(limit_price)
            row["prediction_correct"] = "1" if (
                (buy_side == "YES" and outcome == "YES") or
                (buy_side == "NO" and outcome == "NO")
            ) else "0"
            row["pnl_cents"] = str(compute_pnl(buy_side, limit_price, outcome))
        else:
            row["buy_side"] = ""
            row["limit_price_cents"] = ""
            row["prediction_correct"] = ""
            row["pnl_cents"] = ""

        station = row.get("station_used", "")
        row["outcome_data_source"] = DATA_SOURCES.get(station, "")

        enriched.append(row)

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(enriched)

    # Load into SQLite
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS predictions")
    conn.execute("DROP TABLE IF EXISTS actual_weather")
    conn.execute("DROP TABLE IF EXISTS trade_summary")

    cols_sql = ", ".join(f'"{c}" TEXT' for c in out_fieldnames)
    conn.execute(f"CREATE TABLE predictions ({cols_sql})")

    placeholders = ", ".join("?" for _ in out_fieldnames)
    for row in enriched:
        vals = [row.get(c, "") for c in out_fieldnames]
        conn.execute(f"INSERT INTO predictions VALUES ({placeholders})", vals)

    conn.execute("""
        CREATE TABLE actual_weather (
            city TEXT, contract_date TEXT, actual_high_f REAL,
            data_source TEXT,
            PRIMARY KEY (city, contract_date)
        )
    """)
    for (city, date), temp in ACTUAL_HIGHS.items():
        conn.execute(
            "INSERT INTO actual_weather VALUES (?, ?, ?, ?)",
            (city, date, temp, "Iowa Mesonet ASOS"),
        )

    conn.commit()
    conn.close()

    total = len(enriched)
    actionable = sum(1 for r in enriched if r["is_actionable"] == "1")
    with_outcome = sum(1 for r in enriched if r["pnl_cents"])
    correct = sum(1 for r in enriched if r["prediction_correct"] == "1")
    total_pnl = sum(int(r["pnl_cents"]) for r in enriched if r["pnl_cents"])

    print(f"Enriched {total} predictions -> {output_path}")
    print(f"  Actionable signals: {actionable}")
    print(f"  With outcomes: {with_outcome}")
    print(f"  Correct trades: {correct}/{with_outcome} ({100*correct/with_outcome:.1f}%)")
    print(f"  Total P&L: {total_pnl} cents (${total_pnl/100:.2f})")
    print(f"  SQLite DB: {db_path}")


if __name__ == "__main__":
    enrich()
