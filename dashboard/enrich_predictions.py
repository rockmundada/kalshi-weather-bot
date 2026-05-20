"""
Enrich predictions.csv with actual weather outcomes and compute trade results.

Automatically fetches actual high temps and precipitation from Iowa Mesonet
(ASOS/METAR) for any past dates found in the predictions. Caches results
so repeated runs don't re-fetch.
"""

import csv
import sqlite3
import re
import os
import json
import io
import time
import urllib.request
from datetime import datetime, date

# Station mapping: city name -> (METAR station, timezone)
CITY_STATIONS = {
    "NYC": ("KNYC", "America/New_York"),
    "Chicago": ("KMDW", "America/Chicago"),
    "Miami": ("KMIA", "America/New_York"),
    "Austin": ("KAUS", "America/Chicago"),
    "Denver": ("KDEN", "America/Denver"),
    "LA": ("KLAX", "America/Los_Angeles"),
    "Philadelphia": ("KPHL", "America/New_York"),
}


def load_cache(cache_path):
    """Load cached weather data from JSON file."""
    if os.path.exists(cache_path):
        with open(cache_path) as f:
            return json.load(f)
    return {"highs": {}, "rain": {}}


def save_cache(cache_path, cache):
    """Save weather data cache to JSON file."""
    with open(cache_path, "w") as f:
        json.dump(cache, f, indent=2)


def fetch_max_temp(station, date_str, tz):
    """Fetch max temperature for a station and date from Iowa Mesonet."""
    y, m, d = date_str.split("-")
    url = (
        f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
        f"station={station}&data=max_tmpf&year1={y}&month1={int(m)}&day1={int(d)}"
        f"&year2={y}&month2={int(m)}&day2={int(d)}"
        f"&tz={tz.replace('/', '%2F')}&format=comma&latlon=no"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KalshiBot Weather Research"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            lines = [l for l in data.strip().split("\n") if not l.startswith("#") and l.strip()]
            if len(lines) > 1:
                reader = csv.reader(io.StringIO("\n".join(lines)))
                next(reader)  # skip header
                temps = []
                for row in reader:
                    val = row[-1].strip()
                    if val and val != "M":
                        try:
                            temps.append(float(val))
                        except ValueError:
                            pass
                if temps:
                    return max(temps)
    except Exception as e:
        print(f"    WARNING: Failed to fetch temp for {station} {date_str}: {e}")
    return None


def fetch_precip(station, date_str, tz):
    """Fetch total precipitation for a station and date from Iowa Mesonet."""
    y, m, d = date_str.split("-")
    url = (
        f"https://mesonet.agron.iastate.edu/cgi-bin/request/asos.py?"
        f"station={station}&data=p01i&year1={y}&month1={int(m)}&day1={int(d)}"
        f"&year2={y}&month2={int(m)}&day2={int(d)}"
        f"&tz={tz.replace('/', '%2F')}&format=comma&latlon=no"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "KalshiBot Weather Research"})
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = resp.read().decode("utf-8")
            lines = [l for l in data.strip().split("\n") if not l.startswith("#") and l.strip()]
            if len(lines) > 1:
                reader = csv.reader(io.StringIO("\n".join(lines)))
                next(reader)  # skip header
                total = 0.0
                for row in reader:
                    val = row[-1].strip()
                    if val == "T":
                        total += 0.001  # trace
                    elif val and val != "M":
                        try:
                            total += float(val)
                        except ValueError:
                            pass
                return total > 0  # True if it rained
    except Exception as e:
        print(f"    WARNING: Failed to fetch precip for {station} {date_str}: {e}")
    return None


def fetch_all_weather(rows, cache, cache_path):
    """Fetch weather data for all city/date combos that are in the past and not cached."""
    today = date.today().isoformat()

    # Find all unique city/date pairs we need
    needed_temps = set()
    needed_rain = set()
    for row in rows:
        city = row["city"]
        dt = row["contract_date"]
        if dt >= today:
            continue  # can't get outcomes for future dates
        if city not in CITY_STATIONS:
            continue
        key = f"{city}|{dt}"
        if row["market_type"] == "daily_rain":
            if key not in cache["rain"]:
                needed_rain.add((city, dt))
        else:
            if key not in cache["highs"]:
                needed_temps.add((city, dt))

    if not needed_temps and not needed_rain:
        print("  All weather data cached, no fetches needed.")
        return

    print(f"  Fetching weather for {len(needed_temps)} temp + {len(needed_rain)} rain lookups...")

    for city, dt in sorted(needed_temps):
        station, tz = CITY_STATIONS[city]
        temp = fetch_max_temp(station, dt, tz)
        if temp is not None:
            cache["highs"][f"{city}|{dt}"] = temp
            print(f"    {city} {dt}: {temp}°F")
        else:
            print(f"    {city} {dt}: no data available")
        time.sleep(2)  # rate limit

    for city, dt in sorted(needed_rain):
        station, tz = CITY_STATIONS[city]
        rained = fetch_precip(station, dt, tz)
        if rained is not None:
            cache["rain"][f"{city}|{dt}"] = rained
            print(f"    {city} {dt} rain: {'YES' if rained else 'NO'}")
        time.sleep(2)

    save_cache(cache_path, cache)
    print(f"  Cache updated: {len(cache['highs'])} temps, {len(cache['rain'])} rain records")


def parse_signal(signal):
    """Parse signal string into (side, limit_price) or None if not actionable."""
    m = re.match(r"BUY (YES|NO) @ ask ≤ (\d+)¢", signal)
    if not m:
        return None
    return m.group(1), int(m.group(2))


def determine_contract_outcome(row, actual_high, rain_cache):
    """Determine if contract resolved YES or NO based on actual weather."""
    subtitle = row["contract_subtitle"]
    market_type = row["market_type"]

    if market_type == "daily_rain":
        key = f"{row['city']}|{row['contract_date']}"
        rained = rain_cache.get(key)
        if rained is None:
            return None
        return "YES" if rained else "NO"

    if actual_high is None:
        return None

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

    # Handle ">X°" format (above threshold, only floor_strike set)
    if floor_strike and not cap_strike:
        threshold = float(floor_strike)
        return "YES" if actual_high > threshold else "NO"

    # Handle "<X°" format (below threshold, only cap_strike set)
    if not floor_strike and cap_strike:
        threshold = float(cap_strike)
        return "YES" if actual_high < threshold else "NO"

    # Handle "X° to Y°" and "be X-Y°" formats
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
        return (100 - limit_price) if contract_outcome == "YES" else -limit_price
    else:  # BUY NO
        return (100 - limit_price) if contract_outcome == "NO" else -limit_price


def enrich():
    base = os.path.dirname(os.path.abspath(__file__))
    input_path = os.path.join(base, "..", "data", "predictions.csv")
    output_path = os.path.join(base, "enriched_predictions.csv")
    db_path = os.path.join(base, "kalshi_analytics.db")
    cache_path = os.path.join(base, "weather_cache.json")

    print(f"Loading predictions from {input_path}...")
    with open(input_path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
        fieldnames = reader.fieldnames

    print(f"  Found {len(rows)} predictions")

    # Load cache and fetch any missing weather data
    cache = load_cache(cache_path)
    fetch_all_weather(rows, cache, cache_path)

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
        dt = row["contract_date"]
        key = f"{city}|{dt}"

        actual_high = cache["highs"].get(key)
        row["actual_high_f"] = str(actual_high) if actual_high is not None else ""

        outcome = determine_contract_outcome(row, actual_high, cache["rain"])
        row["actual_outcome"] = outcome or ""

        parsed = parse_signal(row["signal"])
        row["is_actionable"] = "1" if parsed else "0"

        if parsed and outcome:
            buy_side, limit_price = parsed
            row["buy_side"] = buy_side
            row["limit_price_cents"] = str(limit_price)
            row["prediction_correct"] = "1" if (
                (buy_side == "YES" and outcome == "YES")
                or (buy_side == "NO" and outcome == "NO")
            ) else "0"
            row["pnl_cents"] = str(compute_pnl(buy_side, limit_price, outcome))
        else:
            row["buy_side"] = ""
            row["limit_price_cents"] = ""
            row["prediction_correct"] = ""
            row["pnl_cents"] = ""

        station = row.get("station_used", "")
        if station in CITY_STATIONS.values():
            row["outcome_data_source"] = f"Iowa Mesonet ASOS ({station})"
        elif any(station == s for s, _ in CITY_STATIONS.values()):
            row["outcome_data_source"] = f"Iowa Mesonet ASOS ({station})"
        else:
            row["outcome_data_source"] = ""

        enriched.append(row)

    # Write enriched CSV
    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames)
        writer.writeheader()
        writer.writerows(enriched)

    # Load into SQLite
    conn = sqlite3.connect(db_path)
    conn.execute("DROP TABLE IF EXISTS predictions")
    conn.execute("DROP TABLE IF EXISTS actual_weather")

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
    for key, temp in cache["highs"].items():
        city, dt = key.split("|")
        conn.execute(
            "INSERT OR REPLACE INTO actual_weather VALUES (?, ?, ?, ?)",
            (city, dt, temp, "Iowa Mesonet ASOS"),
        )

    conn.commit()
    conn.close()

    # Summary
    total = len(enriched)
    actionable = sum(1 for r in enriched if r["is_actionable"] == "1")
    with_outcome = sum(1 for r in enriched if r["pnl_cents"])
    correct = sum(1 for r in enriched if r["prediction_correct"] == "1")
    total_pnl = sum(int(r["pnl_cents"]) for r in enriched if r["pnl_cents"])

    # Count dates
    all_dates = sorted(set(r["contract_date"] for r in enriched))
    past_dates = [d for d in all_dates if d < date.today().isoformat()]
    future_dates = [d for d in all_dates if d >= date.today().isoformat()]

    print(f"\nEnriched {total} predictions -> {output_path}")
    print(f"  Dates: {len(past_dates)} past ({', '.join(past_dates)})")
    if future_dates:
        print(f"         {len(future_dates)} future ({', '.join(future_dates)}) — awaiting outcomes")
    print(f"  Actionable signals: {actionable}")
    if with_outcome:
        print(f"  With outcomes: {with_outcome}")
        print(f"  Correct trades: {correct}/{with_outcome} ({100*correct/with_outcome:.1f}%)")
        print(f"  Total P&L: {total_pnl} cents (${total_pnl/100:.2f})")
    else:
        print(f"  No outcomes available yet (all dates are in the future)")
    print(f"  SQLite DB: {db_path}")


if __name__ == "__main__":
    enrich()
