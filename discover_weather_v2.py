#!/usr/bin/env python3
"""
Focused weather contract discovery - Phase 2.
We already know from Phase 1 which series exist. Now get full details
on each, and also try a few more targeted prefixes.
"""
import sys
import time

sys.path.insert(0, "/Users/rock/Downloads/kalshi bots/unified_bot")
from trading.kalshi_api import KalshiAPI

api = KalshiAPI()

# Known weather series from Phase 1
known_weather = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
    "KXHIGHPHIL", "KXHIGHLAX", "KXHIGHHOU",
    "KXRAINNY", "KXRAINNYC", "KXRAINHOU", "KXRAINSEA",
    "KXRAINNYCM", "KXRAINCHIM", "KXRAINMIAM", "KXRAINAUSM",
    "KXRAINDENM", "KXRAINLAXM", "KXRAINHOUM", "KXRAINSEAM",
    "KXRAINDALM", "KXRAINSFOM",
    "KXSNOWNY", "KXSNOWNYM",
    "KXTEMP",
]

# Additional targeted prefixes to try (things we might have missed)
extra_tries = []
for prefix in ["KXLOW", "KXHIGH", "KXRAIN", "KXSNOW", "KXWIND", "KXDEW", 
               "KXHUMID", "KXPRECIP", "KXFREEZE", "KXFROST", "KXHEAT",
               "KXCOLD", "KXUV", "KXFOG", "KXICE", "KXSTORM", "KXSUN",
               "KXCLOUD", "KXTHUNDER", "KXHAIL", "KXFLOOD", "KXTORNADO",
               "KXHURRICANE", "KXDROUGHT", "KXAQ", "KXAQI"]:
    for suffix in ["NYC", "NY", "CHI", "MIA", "AUS", "DEN", "PHIL", "LAX",
                    "HOU", "SEA", "LA", "SF", "ATL", "BOS", "DAL", "PHX",
                    "DFW", "MSP", "DCA", "SFO", "PDX", "SLC",
                    "NYCM", "CHIM", "MIAM", "AUSM", "DENM", "LAXM", "HOUM",
                    "SEAM", "DALM", "SFOM", "ATLM", "BOSM", "PHXM", "DFWM"]:
        t = prefix + suffix
        if t not in known_weather:
            extra_tries.append(t)

# Also try some specific patterns
extra_tries += [
    "KXLOWTEMP", "KXLOWNY", "KXLOWCHI", "KXLOWMIA",
    "KXWINDSPD", "KXWINDNYC", "KXWINDCHI",
    "KXSNOWCHI", "KXSNOWDEN", "KXSNOWPHIL", "KXSNOWBOS",
    "KXSNOWCHIM", "KXSNOWDENM", "KXSNOWBOSM",
    "KXRAINCHI", "KXRAINMIA", "KXRAINAUS", "KXRAINDEN",
    "KXRAINPHIL", "KXRAINLAX", "KXRAINDAL", "KXRAINSFO",
    "KXRAINATL", "KXRAINBOS", "KXRAINPHX",
    "KXHIGHSEA", "KXHIGHATL", "KXHIGHBOS", "KXHIGHDAL",
    "KXHIGHPHX", "KXHIGHDFW", "KXHIGHSF", "KXHIGHSFO",
    "KXHIGHMSP", "KXHIGHDCA", "KXHIGHSLC", "KXHIGHPDX",
    # Try without KX prefix - maybe some weather contracts use different prefix
    "HIGHTEMP", "WEATHER", "TEMP", "RAIN", "SNOW",
    # Maybe KXWX prefix
    "KXWXNY", "KXWXCHI", "KXWXMIA",
]

# Deduplicate
extra_tries = list(set(extra_tries) - set(known_weather))

print("=" * 100)
print("CHECKING ADDITIONAL PREFIXES FOR MISSED WEATHER SERIES")
print("=" * 100)

newly_found = []
checked = 0
for ticker in sorted(extra_tries):
    params = {"series_ticker": ticker, "limit": 3}
    data = api._request("GET", "/events", params=params)
    if data and "events" in data and len(data["events"]) > 0:
        events = data["events"]
        title = events[0].get("title", "N/A")
        newly_found.append(ticker)
        known_weather.append(ticker)
        print(f"  NEW FIND: {ticker:25s} | {len(events)} events | {title[:70]}")
    checked += 1
    if checked % 40 == 0:
        print(f"  ... checked {checked}/{len(extra_tries)} extra tickers ...")
        time.sleep(0.3)

print(f"\nAdditional search complete: checked {checked} extra tickers, found {len(newly_found)} new series")

# ── Now get detailed info on ALL weather series ──────────────────────

print("\n" + "=" * 100)
print("DETAILED INFO ON ALL WEATHER SERIES")
print("=" * 100)

all_weather = sorted(set(known_weather))
results = {}

for series_ticker in all_weather:
    print(f"\n{'─' * 90}")
    print(f"SERIES: {series_ticker}")
    print(f"{'─' * 90}")
    
    # Get open events
    params = {"series_ticker": series_ticker, "limit": 200, "status": "open"}
    data = api._request("GET", "/events", params=params)
    open_events = data.get("events", []) if data else []
    
    # Also check closed events for historical context
    params2 = {"series_ticker": series_ticker, "limit": 5, "status": "closed"}
    data2 = api._request("GET", "/events", params=params2)
    closed_events = data2.get("events", []) if data2 else []
    
    all_events = open_events + closed_events
    if not all_events:
        print("  No events found (may be inactive)")
        continue
    
    sample_event = open_events[0] if open_events else closed_events[0]
    event_ticker = sample_event.get("event_ticker", "")
    event_title = sample_event.get("title", "")
    category = sample_event.get("category", "")
    
    print(f"  Title:           {event_title}")
    print(f"  Category:        {category}")
    print(f"  Open events:     {len(open_events)}")
    print(f"  Sample event:    {event_ticker}")
    
    # List all open event tickers (to see date pattern)
    if open_events:
        print(f"  Open event tickers:")
        for evt in open_events[:8]:
            print(f"    - {evt.get('event_ticker', '')}  |  {evt.get('title', '')[:60]}")
        if len(open_events) > 8:
            print(f"    ... and {len(open_events) - 8} more")
    
    # Get markets for sample event
    markets = api.get_event_markets(event_ticker)
    if markets:
        print(f"  Brackets/markets in sample event: {len(markets)}")
        print(f"  Market details:")
        for mkt in markets[:10]:
            ticker = mkt.get("ticker", "")
            subtitle = mkt.get("subtitle", "")
            yes_bid = mkt.get("yes_bid", 0)
            yes_ask = mkt.get("yes_ask", 0)
            volume = mkt.get("volume", 0)
            status = mkt.get("status", "")
            floor = mkt.get("floor_strike")
            cap = mkt.get("cap_strike")
            close_time = mkt.get("close_time", "")
            
            print(f"    {ticker:40s} | floor={floor} cap={cap} | bid={yes_bid} ask={yes_ask} | vol={volume} | {status}")
            if subtitle:
                print(f"      subtitle: {subtitle[:80]}")
        
        if len(markets) > 10:
            print(f"    ... and {len(markets) - 10} more brackets")
        
        # Show close time and rules from first market
        first_mkt = markets[0]
        print(f"  Close time:      {first_mkt.get('close_time', 'N/A')}")
        rules = first_mkt.get("rules_primary", "")
        if rules:
            print(f"  Rules (first 300 chars):")
            print(f"    {rules[:300]}")
    
    results[series_ticker] = {
        "title": event_title,
        "category": category,
        "open_events": len(open_events),
        "brackets": len(markets) if markets else 0,
        "sample_event": event_ticker,
    }
    
    time.sleep(0.3)

# ── Summary Table ──────────────────────────────────────────────────────

print("\n\n" + "=" * 100)
print("SUMMARY TABLE: ALL KALSHI WEATHER CONTRACT TYPES")
print("=" * 100)

# Group by type
groups = {}
for s in sorted(results.keys()):
    # Determine type prefix
    if "HIGH" in s:
        gtype = "KXHIGH (Daily High Temp)"
    elif "LOW" in s:
        gtype = "KXLOW (Daily Low Temp)"
    elif "SNOW" in s and s.endswith("M"):
        gtype = "KXSNOW__M (Monthly Snow)"
    elif "SNOW" in s:
        gtype = "KXSNOW (Daily Snow)"
    elif "RAIN" in s and s.endswith("M"):
        gtype = "KXRAIN__M (Monthly Rain)"
    elif "RAIN" in s:
        gtype = "KXRAIN (Daily Rain)"
    elif "WIND" in s:
        gtype = "KXWIND (Wind)"
    elif "TEMP" in s:
        gtype = "KXTEMP (Temperature)"
    else:
        gtype = "OTHER"
    
    if gtype not in groups:
        groups[gtype] = []
    groups[gtype].append(s)

for gtype, tickers in sorted(groups.items()):
    print(f"\n  {gtype}:")
    for t in tickers:
        r = results[t]
        print(f"    {t:25s} | {r['open_events']:3d} open events | {r['brackets']:2d} brackets | {r['title'][:50]}")

print(f"\n{'=' * 100}")
print(f"GRAND TOTAL: {len(results)} active weather series across {len(groups)} contract types")
print(f"{'=' * 100}")
