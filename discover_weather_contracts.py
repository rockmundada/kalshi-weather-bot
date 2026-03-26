#!/usr/bin/env python3
"""
Discover ALL daily weather contracts on Kalshi.
Uses the existing KalshiAPI class to systematically search for weather-related
event tickers across all prefixes and city codes.
"""
import sys
import os
import time

# Add project root to path
sys.path.insert(0, "/Users/rock/Downloads/kalshi bots/unified_bot")

from trading.kalshi_api import KalshiAPI

api = KalshiAPI()

# ── Phase 1: Systematic prefix + city suffix search ──────────────────

prefixes = [
    "KXHIGH", "KXLOW", "KXRAIN", "KXWIND", "KXSNOW", "KXTEMP",
    "KXWX", "KXHUMID", "KXPRECIP", "KXDEW", "KXCOLD", "KXHEAT",
    "KXFREEZE", "KXFROST", "KXSTORM", "KXSUN", "KXUV", "KXFOG",
    "KXICE", "KXHAIL", "KXTHUNDER", "KXCLOUD",
]

city_suffixes = [
    "NY", "NYC", "CHI", "MIA", "AUS", "DEN", "PHIL", "LAX", "HOU", "SEA",
    "LA", "SF", "ATL", "BOS", "DAL", "PHX", "DFW", "MSP", "DTW", "SLC",
    "PDX", "DCA", "BWI", "IAD", "ORD", "JFK", "LGA", "SFO", "OAK",
    "PHL", "MDW", "STL", "CLE", "PIT", "IND", "MCI", "MEM", "CLT",
    "TPA", "MCO", "SAN", "LAS", "SAT", "JAX", "OKC", "TUL", "BNA",
    "RDU", "CVG", "CMH", "MKE", "MSY", "ABQ", "ANC", "HNL",
]

# Also try with monthly suffix
monthly_suffixes = [s + "M" for s in city_suffixes]

found_series = {}  # series_ticker -> info dict

def check_series(series_ticker, label=""):
    """Check if a series ticker has any events on Kalshi."""
    if series_ticker in found_series:
        return  # already found
    try:
        # Use the events endpoint directly
        params = {
            "series_ticker": series_ticker,
            "limit": 5,
        }
        data = api._request("GET", "/events", params=params)
        if data and "events" in data and len(data["events"]) > 0:
            events = data["events"]
            sample = events[0]
            sample_title = sample.get("title", "N/A")
            sample_ticker = sample.get("event_ticker", "N/A")
            category = sample.get("category", "N/A")
            
            found_series[series_ticker] = {
                "count": len(events),
                "sample_title": sample_title,
                "sample_event_ticker": sample_ticker,
                "category": category,
                "label": label,
            }
            print(f"  FOUND: {series_ticker:25s} | {len(events):3d} events | {sample_title[:70]}")
            return True
    except Exception as e:
        pass
    return False


print("=" * 100)
print("PHASE 1: Systematic prefix + city suffix search")
print("=" * 100)

total_checks = 0
for prefix in prefixes:
    # Try the prefix alone first
    check_series(prefix, label=f"prefix-only")
    total_checks += 1
    
    for suffix in city_suffixes + monthly_suffixes:
        ticker = prefix + suffix
        check_series(ticker, label=f"{prefix}+{suffix}")
        total_checks += 1
        
        # Rate limit - be gentle
        if total_checks % 50 == 0:
            print(f"  ... checked {total_checks} tickers so far, found {len(found_series)} series ...")
            time.sleep(0.5)

print(f"\nPhase 1 complete: checked {total_checks} tickers, found {len(found_series)} series")

# ── Phase 2: Broader "KX" search via events endpoint ──────────────────

print("\n" + "=" * 100)
print("PHASE 2: Broad search using events endpoint with various status filters")
print("=" * 100)

# Try searching events with cursor pagination - look for anything starting with KX
for status in ["open", "closed"]:
    print(f"\n  Searching events with status={status}...")
    cursor = None
    page = 0
    while page < 10:  # max 10 pages per status
        params = {"limit": 200, "status": status}
        if cursor:
            params["cursor"] = cursor
        
        data = api._request("GET", "/events", params=params)
        if not data or "events" not in data:
            break
        
        events = data["events"]
        if not events:
            break
            
        for evt in events:
            ticker = evt.get("event_ticker", "")
            series = evt.get("series_ticker", "")
            title = evt.get("title", "")
            category = evt.get("category", "")
            
            # Look for weather-related events (KX prefix, weather category, etc.)
            if (ticker.startswith("KX") or 
                series.startswith("KX") or 
                "weather" in category.lower() or
                "temperature" in title.lower() or
                "rain" in title.lower() or
                "snow" in title.lower() or
                "wind" in title.lower() or
                "precip" in title.lower()):
                
                if series and series not in found_series:
                    found_series[series] = {
                        "count": 1,
                        "sample_title": title,
                        "sample_event_ticker": ticker,
                        "category": category,
                        "label": f"broad-search-{status}",
                    }
                    print(f"  FOUND: series={series:25s} | event={ticker:30s} | {title[:60]}")
                elif series and series in found_series:
                    # Update count
                    found_series[series]["count"] += 1
        
        cursor = data.get("cursor")
        if not cursor:
            break
        page += 1
    
    time.sleep(0.5)

# ── Phase 3: Try specific series tickers mentioned in config ──────────

print("\n" + "=" * 100)
print("PHASE 3: Check config-defined tickers and variations")
print("=" * 100)

config_tickers = [
    "KXHIGHNY", "KXHIGHCHI", "KXHIGHMIA", "KXHIGHAUS", "KXHIGHDEN",
    "KXHIGHPHIL", "KXHIGHLAX",
    "KXRAINNYC", "KXRAINNYCM", "KXRAINCHIM", "KXRAINMIAM", "KXRAINAUSM",
    "KXRAINDENM", "KXRAINLAXM", "KXRAINHOUM", "KXRAINSEAM",
    # Low temp variations
    "KXLOWNY", "KXLOWNYC", "KXLOWCHI", "KXLOWMIA", "KXLOWAUS", "KXLOWDEN",
    "KXLOWPHIL", "KXLOWLAX", "KXLOWHOU", "KXLOWSEA",
    # Wind variations
    "KXWINDNY", "KXWINDNYC", "KXWINDCHI", "KXWINDMIA", "KXWINDAUS", "KXWINDDEN",
    # Snow variations
    "KXSNOWNY", "KXSNOWNYC", "KXSNOWCHI", "KXSNOWDEN", "KXSNOWPHIL",
    # Temperature range
    "KXTEMPNY", "KXTEMPNYC", "KXTEMPCHI", "KXTEMPMIA",
    # Dew point
    "KXDEWNY", "KXDEWNYC", "KXDEWCHI", "KXDEWMIA",
]

for ticker in config_tickers:
    check_series(ticker, label="config-check")

# ── Phase 4: Search for series directly ──────────────────────────────

print("\n" + "=" * 100)
print("PHASE 4: Search series endpoint directly")
print("=" * 100)

# Try the /series endpoint if it exists
for search_prefix in ["KX", "KXHIGH", "KXLOW", "KXRAIN", "KXWIND", "KXSNOW", "KXTEMP"]:
    data = api._request("GET", "/series", params={"limit": 200})
    if data and "series" in data:
        for s in data["series"]:
            st = s.get("ticker", "")
            if st.startswith("KX"):
                title = s.get("title", "N/A")
                category = s.get("category", "N/A")
                if st not in found_series:
                    found_series[st] = {
                        "count": 0,
                        "sample_title": title,
                        "sample_event_ticker": "N/A (series level)",
                        "category": category,
                        "label": "series-endpoint",
                    }
                    print(f"  FOUND series: {st:25s} | {title[:70]}")
        break  # only need to call once, the params don't filter by prefix
    else:
        print(f"  Series endpoint returned no data for prefix={search_prefix}")
        break

# ── Phase 5: For each found series, get more details ──────────────────

print("\n" + "=" * 100)
print("PHASE 5: Enriching found series with market details")
print("=" * 100)

for series_ticker, info in sorted(found_series.items()):
    # Get events with both open and closed to get full count
    for status in ["open"]:
        params = {"series_ticker": series_ticker, "limit": 200, "status": status}
        data = api._request("GET", "/events", params=params)
        if data and "events" in data:
            events = data["events"]
            info["open_events"] = len(events)
            
            # Get market details for the first event to understand contract structure
            if events:
                first_event = events[0]
                event_ticker = first_event.get("event_ticker", "")
                markets = api.get_event_markets(event_ticker)
                if markets:
                    sample_mkt = markets[0]
                    info["sample_market_ticker"] = sample_mkt.get("ticker", "")
                    info["sample_subtitle"] = sample_mkt.get("subtitle", "")
                    info["num_brackets"] = len(markets)
                    info["close_time"] = sample_mkt.get("close_time", "")
                    info["rules_primary"] = sample_mkt.get("rules_primary", "")[:120]
    
    time.sleep(0.3)  # gentle rate limiting

# ── Final Report ──────────────────────────────────────────────────────

print("\n" + "=" * 100)
print("FINAL REPORT: All Weather Contract Series Found on Kalshi")
print("=" * 100)

# Group by contract type
type_groups = {}
for series_ticker, info in sorted(found_series.items()):
    # Extract the prefix (everything before the city code)
    prefix = series_ticker
    for city in ["NY", "NYC", "CHI", "MIA", "AUS", "DEN", "PHIL", "LAX", "HOU", "SEA",
                 "LA", "SF", "ATL", "BOS", "DAL", "PHX", "DFW"]:
        if series_ticker.endswith(city) or series_ticker.endswith(city + "M"):
            prefix = series_ticker[:series_ticker.rfind(city)]
            break
    
    if prefix not in type_groups:
        type_groups[prefix] = []
    type_groups[prefix].append((series_ticker, info))

for prefix, tickers in sorted(type_groups.items()):
    print(f"\n{'─' * 80}")
    print(f"CONTRACT TYPE: {prefix}")
    print(f"{'─' * 80}")
    for series_ticker, info in tickers:
        open_evts = info.get("open_events", info.get("count", "?"))
        brackets = info.get("num_brackets", "?")
        sample_mkt = info.get("sample_market_ticker", "N/A")
        subtitle = info.get("sample_subtitle", "N/A")
        close = info.get("close_time", "N/A")
        rules = info.get("rules_primary", "N/A")
        
        print(f"  Series: {series_ticker}")
        print(f"    Title:          {info['sample_title'][:80]}")
        print(f"    Open events:    {open_evts}")
        print(f"    Brackets/event: {brackets}")
        print(f"    Sample market:  {sample_mkt}")
        print(f"    Subtitle:       {subtitle[:80] if subtitle else 'N/A'}")
        print(f"    Close time:     {close}")
        print(f"    Rules:          {rules[:120] if rules else 'N/A'}")
        print()

print(f"\nTOTAL: {len(found_series)} unique series found across {len(type_groups)} contract types")
