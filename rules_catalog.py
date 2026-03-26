"""
Contract rule summaries derived from Kalshi rule PDFs.
These are lightweight, human-readable reminders shown in output and passed to Claude.
"""

from typing import Dict


RULES_BY_TYPE: Dict[str, str] = {
    # High temperature contracts (LAXHIGH.pdf; applies to all KXHIGH*)
    "high_temp": (
        "Underlying: max temperature for the specified date from the NWS Daily Climate Report "
        "for the official station. Revisions after expiration are ignored. "
        "Brackets are inclusive (between) unless it's a tail. "
        "NWS rounding: .5 rounds up toward +inf (FMH-1 §2.6.3 / ASOS §3.1.2 / NDST: "
        "'round half up asymmetric'). CLI reports whole-integer °F. "
        "Settlement window: midnight-midnight LST (NWSI 10-1004 §4.1.2)."
    ),
    # Daily rain contracts (RAINNYC.pdf; applies to daily rain)
    "daily_rain": (
        "Underlying: inches of precipitation for the specified date in the NWS Daily Climate Report "
        "for the official station. Use the 'Yesterday' row in the next day's report if needed; "
        "'Today' row is used only if no 'Yesterday' row is available by expiration."
    ),
    # Monthly rain contracts (RAINNYCM.pdf; applies to monthly rain)
    "monthly_rain": (
        "Underlying: total monthly precipitation summed from daily NWS Daily Climate Reports "
        "for the official station. Revisions after expiration are ignored."
    ),
    # Snow contracts (SNOWOVERTIME.pdf)
    "snow": (
        "Underlying: total snowfall for the specified area/time period as reported by NWS. "
        "Exact values are rounded to two decimals. 'Between' is inclusive."
    ),
    # Global temperature contracts (GLOBALTEMPERATURE.pdf)
    "global_temp": (
        "Underlying: max/min/avg temperature for area/time period from NWS (or national weather service). "
        "Exactly is rounded to one decimal place; between is inclusive."
    ),
}


def rule_summary_for_market_type(market_type: str) -> str:
    return RULES_BY_TYPE.get(market_type, "")

