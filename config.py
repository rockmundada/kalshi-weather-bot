"""
Kalshi Weather Trading Bot - Master Configuration
All verified active markets as of Feb 2026.
"""
import os, math
from datetime import datetime, timezone

# === KALSHI API ===
# API Key ID and private key must be from the same Kalshi "Create Key".
# INCORRECT_API_KEY_SIGNATURE = key ID and key are not a pair; re-create key and update both.
# Placeholder values provided by user (edit later if needed).
KALSHI_API_KEY = os.environ.get("KALSHI_API_KEY", "YOUR_KALSHI_API_KEY")
_DEFAULT_KALSHI_PK = "YOUR_PRIVATE_KEY_PEM"
KALSHI_PRIVATE_KEY_PEM = os.environ.get("KALSHI_PRIVATE_KEY_PEM", _DEFAULT_KALSHI_PK)
KALSHI_PRIVATE_KEY_PATH = os.path.expanduser("~/kalshi_private_key.pem")
KALSHI_BASE_URL = "https://api.elections.kalshi.com/trade-api/v2"

# === CLAUDE AI ===
ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY") or "YOUR_ANTHROPIC_API_KEY"
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-opus-4-6")
CLAUDE_MAX_TOKENS = int(os.environ.get("CLAUDE_MAX_TOKENS", "16000"))
CLAUDE_THINKING_BUDGET = int(os.environ.get("CLAUDE_THINKING_BUDGET", "10000"))
# Set to 0 to disable the explicit client timeout (wait as long as needed).
CLAUDE_TIMEOUT_SECONDS = float(os.environ.get("CLAUDE_TIMEOUT_SECONDS", "0"))
CLAUDE_MAX_RETRIES = int(os.environ.get("CLAUDE_MAX_RETRIES", "0"))
# Compact prompt budgets (faster; still uses extended thinking)
CLAUDE_MAX_TOKENS_COMPACT = int(os.environ.get("CLAUDE_MAX_TOKENS_COMPACT", "4000"))
CLAUDE_THINKING_BUDGET_COMPACT = int(os.environ.get("CLAUDE_THINKING_BUDGET_COMPACT", "4000"))

# === OPENAI ===
# The OpenAI SDK reads API keys from the OPENAI_API_KEY environment variable by default.
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5")
OPENAI_MAX_OUTPUT_TOKENS = int(os.environ.get("OPENAI_MAX_OUTPUT_TOKENS", "4000"))
OPENAI_TIMEOUT_SECONDS = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "60"))
OPENAI_MAX_RETRIES = int(os.environ.get("OPENAI_MAX_RETRIES", "0"))

# === LLM PROMPT CONTROLS ===
LLM_PROMPT_MODE = os.environ.get("LLM_PROMPT_MODE", "full")  # compact|full
LLM_MAX_EDGES_IN_PROMPT = int(os.environ.get("LLM_MAX_EDGES_IN_PROMPT", "50"))
# If true, skip LLM when no filtered edges are present (per-city mode only).
LLM_RUN_ONLY_IF_EDGES = os.environ.get("LLM_RUN_ONLY_IF_EDGES", "false").lower() in ("1", "true", "yes", "y")
# LLM execution mode: per_city (default) or global_once
LLM_RUN_MODE = os.environ.get("LLM_RUN_MODE", "global_once").lower()
# If true, allow LLM to propose trades beyond statistical filters (always analyze).
LLM_ALLOW_NEW_TRADES = os.environ.get("LLM_ALLOW_NEW_TRADES", "true").lower() in ("1", "true", "yes", "y")

# === TELEGRAM ===
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "YOUR_TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "YOUR_TELEGRAM_CHAT_ID")

# === WETHR.NET (optional, paid) ===
# Leave blank unless you subscribe. If set, Wethr data is used to tighten highs and MTD precip.
WETHR_API_KEY = ""

# === CITIES WITH VERIFIED ACTIVE KALSHI MARKETS ===
CITIES = {
    "NYC": {
        "name": "New York City", "station_id": "KNYC",
        "backup_stations": ["KLGA", "KJFK"], "wfo": "OKX",
        "timezone": "America/New_York",
        "nws_grid": {"office": "OKX", "x": 34, "y": 38},
        "cli_station": "NYC", "lat": 40.7833, "lon": -73.9667,
        "kalshi_tickers": {"high_temp": "KXHIGHNY", "daily_rain": "KXRAINNYC", "monthly_rain": "KXRAINNYCM"},
    },
    "Chicago": {
        "name": "Chicago", "station_id": "KMDW",
        "backup_stations": ["KORD"], "wfo": "LOT",
        "timezone": "America/Chicago",
        "nws_grid": {"office": "LOT", "x": 72, "y": 69},
        "cli_station": "MDW", "lat": 41.7868, "lon": -87.7522,
        "kalshi_tickers": {"high_temp": "KXHIGHCHI", "monthly_rain": "KXRAINCHIM"},
    },
    "Miami": {
        "name": "Miami", "station_id": "KMIA",
        "backup_stations": ["KFLL"], "wfo": "MFL",
        "timezone": "America/New_York",
        "nws_grid": {"office": "MFL", "x": 105, "y": 51},
        "cli_station": "MIA", "lat": 25.7906, "lon": -80.3164,
        "kalshi_tickers": {"high_temp": "KXHIGHMIA", "monthly_rain": "KXRAINMIAM"},
    },
    "Austin": {
        "name": "Austin", "station_id": "KAUS",
        "backup_stations": [], "wfo": "EWX",
        "timezone": "America/Chicago",
        "nws_grid": {"office": "EWX", "x": 159, "y": 88},
        "cli_station": "AUS", "lat": 30.1945, "lon": -97.6699,
        "kalshi_tickers": {"high_temp": "KXHIGHAUS", "monthly_rain": "KXRAINAUSM"},
    },
    "Denver": {
        "name": "Denver", "station_id": "KDEN",
        "backup_stations": ["KAPA"], "wfo": "BOU",
        "timezone": "America/Denver",
        "nws_grid": {"office": "BOU", "x": 75, "y": 66},
        "cli_station": "DEN", "lat": 39.8584, "lon": -104.6670,
        "kalshi_tickers": {"high_temp": "KXHIGHDEN", "monthly_rain": "KXRAINDENM"},
    },
    "Philadelphia": {
        "name": "Philadelphia", "station_id": "KPHL",
        "backup_stations": [], "wfo": "PHI",
        "timezone": "America/New_York",
        "nws_grid": {"office": "PHI", "x": 48, "y": 72},
        "cli_station": "PHL", "lat": 39.8721, "lon": -75.2407,
        "kalshi_tickers": {"high_temp": "KXHIGHPHIL"},
    },
    "LA": {
        "name": "Los Angeles", "station_id": "KLAX",
        "backup_stations": ["KCQT"], "wfo": "LOX",
        "timezone": "America/Los_Angeles",
        "nws_grid": {"office": "LOX", "x": 149, "y": 41},
        "cli_station": "LAX", "lat": 33.9382, "lon": -118.3886,
        "kalshi_tickers": {"high_temp": "KXHIGHLAX", "monthly_rain": "KXRAINLAXM"},
    },
    "Houston": {
        "name": "Houston", "station_id": "KHOU",
        "backup_stations": ["KIAH"], "wfo": "HGX",
        "timezone": "America/Chicago",
        "nws_grid": {"office": "HGX", "x": 66, "y": 89},
        "cli_station": "HOU", "lat": 29.6454, "lon": -95.2789,
        "kalshi_tickers": {"monthly_rain": "KXRAINHOUM"},
    },
    "Seattle": {
        "name": "Seattle", "station_id": "KSEA",
        "backup_stations": [], "wfo": "SEW",
        "timezone": "America/Los_Angeles",
        "nws_grid": {"office": "SEW", "x": 124, "y": 60},
        "cli_station": "SEA", "lat": 47.4444, "lon": -122.3138,
        "kalshi_tickers": {"monthly_rain": "KXRAINSEAM"},
    },
}

# === TRADING PARAMETERS ===
# NOTE: Set limits to 0/None to disable. Use maker-only orders.
TRADING = {
    "kelly_fraction": 0.25,  # quarter Kelly (reduced risk)
    "max_position_per_contract": 0,  # 0 = no cap
    "max_daily_loss": 0.0,  # 0 = no cap
    "max_open_positions": 0,  # 0 = no cap
    "order_type": "maker_only",
    "maker_spread_cents": 1,
    "order_timeout_seconds": 300,
    "min_edge_percent": 2.0,
    "min_edge_cents": 2.5,
    # Fee/slippage guard - lowered for more aggressive trading
    "estimated_fee_cents": 1.0,
    "min_edge_after_fees_cents": 2.0,
    "min_contract_price": 0,
    "max_contract_price": 100,
    "max_trusted_edge_pct": 150.0,
    # Liquidity guards - opened up for thin Kalshi weather markets
    "max_spread_cents": 50,  # allow wide spreads - weather markets are thin
    "min_volume": 0,  # allow zero volume (new/thin markets)
    "min_open_interest": 0,  # allow zero OI
    "min_side_book_size": 0,  # allow empty book - we post maker orders
    # Allow penny markets - that's where big edges live
    "illiquid_price_cents": 0,
    # Subtitle parsing for strike bounds - disabled to allow numeric strike fallback
    "require_subtitle_parse": False,
    # Forecast reliability guards - loosened to allow pre-CLI trading
    "max_uncertainty_f": 0,  # disabled - let the model decide
    "max_model_spread_f": 0,  # disabled - model disagreement is info, not a blocker
    "max_metar_age_min": 180,  # 3 hours - observations can lag
    "forecast_bias_warn_f": 5.0,
    "forecast_bias_no_trade_f": 15.0,  # only block on extreme bias
    "allow_same_day_locked_trades": True,
    # If true, keep only the single strongest edge per event (mutually exclusive brackets)
    "dedupe_by_event": True,
    # Monthly rain guardrails (early month is high uncertainty)
    # Require forecast coverage for the full remaining month + MTD data before pricing monthly rain
    "monthly_rain_require_full_coverage": True,
    "monthly_rain_min_day": 7,  # day-of-month before which monthly rain is NO TRADE
    "monthly_rain_min_price_cents": 5,
    # Extra liquidity guardrails for monthly rain (thin books are noisy)
    "monthly_rain_min_volume": 500,
    "monthly_rain_min_open_interest": 500,
    # Require forecast coverage for at least this many days before pricing monthly rain
    "monthly_rain_min_forecast_days": 7,
    # Edge agent guardrails - lowered to allow more trades through
    "min_fair_prob_to_recommend_buy": 0.15,  # allow bets with 15%+ probability edge
    # If our fair prob <= this, output HOLD / NO TRADE
    "max_fair_prob_hold_threshold": 0.10,  # only hold if <10% fair prob on our side
    # Ghost flag: API vs T-group/synoptic gap above this (°F) = warn
    "ghost_gap_threshold_f": 8.0,
    # Only block on extreme ghost gaps
    "ghost_gap_no_trade_f": 20.0,  # effectively disabled
    # Suspect strike/date bug: fair >= this and market <= this → NO TRADE unless CLI final
    "suspicious_fair_cents": 95,
    "suspicious_market_cents": 15,
}

# === MARKET SCOPE ===
# Control which market types are analyzed/recommended.
ENABLED_MARKETS = {
    "high_temp": True,
    "daily_rain": True,
    "monthly_rain": False,
}

# === TRUST GATES (recommendations-only) ===
# Hard gates cannot be overridden (even by LLM).
# Soft gates can be overridden by LLM if confidence is high and trust score is sufficient.
TRUST_GATES = {
    "same_day_allowed": True,
    "same_day_max_hours_remaining": 24,  # allow trading all day
    "same_day_max_metar_age_min": 180,  # 3 hours
    "require_subtitle_parse": False,  # fall back to strike-based if needed
    "require_station_match": False,  # allow backup stations
    "allow_backup_station": True,
    "require_executable_ask": True,
    "min_volume": 0,  # allow zero volume
    "min_open_interest": 0,  # allow zero OI
    "max_spread_cents": 50,  # allow wide spreads
    "min_side_book_size": 0,  # allow empty book
    "min_edge_after_fees_cents": 3,  # lowered
    "min_fair_prob_to_recommend_buy": 0.15,  # allow lower prob bets
    "trust_score_min_for_llm_override": 30,  # let LLM override more easily
    "calibration_warn_mae_f": 6.0,
    "calibration_warn_brier": 0.30,
}

# === DATA SOURCES ===
DATA_SOURCES = {
    "temp_priority": ["cli", "t_group", "synoptic", "metar_temp", "forecast", "ensemble"],
    "awc_cache_url": "https://aviationweather.gov/api/data/metar",
    # Pull enough history to capture day-to-date max (we filter by contract date).
    "awc_cache_hours": 24,
    "nws_api_base": "https://api.weather.gov",
    "nws_user_agent": "KalshiWeatherBot/2.0 (weather-trading@example.com)",
    "open_meteo_url": "https://api.open-meteo.com/v1/forecast",
    "ensemble_models": ["gfs_seamless", "ecmwf_ifs025", "gem_seamless", "icon_seamless"],
    "ensemble_forecast_days": 10,
    "wethr_api_base": "https://wethr.net/api/v2",
}

# === PREDICTION LOGGING ===
PREDICTIONS_LOG_ENABLED = os.environ.get("PREDICTIONS_LOG_ENABLED", "true").lower() in ("1", "true", "yes", "y")
PREDICTIONS_LOG_PATH = os.environ.get("PREDICTIONS_LOG_PATH", "data/predictions.csv")

# === ENSEMBLE WEIGHTS (inverse-MAE) ===
MODEL_WEIGHTS = {
    "gfs_seamless": {"mae": 2.0}, "ecmwf_ifs025": {"mae": 1.5},
    "gem_seamless": {"mae": 2.2}, "icon_seamless": {"mae": 1.8},
}
_total = sum(1.0 / m["mae"] for m in MODEL_WEIGHTS.values())
for _k in MODEL_WEIGHTS:
    MODEL_WEIGHTS[_k]["weight"] = (1.0 / MODEL_WEIGHTS[_k]["mae"]) / _total

# === TRADING PROFILES ===
# "safe" = threshold-preferred, NWS-aligned, conservative sizing
# "aggressive" = original loose settings (bracket bets, higher risk)
TRADING_PROFILE_DEFAULT = "llm_first"

SAFE_TRADING_OVERRIDES = {
    "min_edge_cents": 5.0,           # require bigger edge
    "min_edge_after_fees_cents": 4.0, # bigger net edge after fees
    "min_fair_prob_to_recommend_buy": 0.55,  # higher confidence needed
    "max_spread_cents": 15,          # tighter spread requirement
    "max_contract_price": 85,        # avoid overpaying
    "min_contract_price": 5,         # avoid penny traps
    "kelly_fraction": 0.15,          # smaller bet sizing
    "_active_profile": "safe",       # internal marker
}

SAFE_TRUST_GATES_OVERRIDES = {
    "penny_trap_max_price_cents": 5,      # hard-block ≤5¢ bracket asks
    "require_nws_daily_consistency": True, # NWS daily must agree with bracket
    "min_edge_after_fees_cents": 4.0,
    "min_fair_prob_to_recommend_buy": 0.55,
    "max_spread_cents": 15,
}

AGGRESSIVE_TRADING_OVERRIDES = {
    "_active_profile": "aggressive",
}

AGGRESSIVE_TRUST_GATES_OVERRIDES = {
    "penny_trap_max_price_cents": 0,       # no penny trap gate
    "require_nws_daily_consistency": False, # no NWS consistency check
}

# === MARGIN OF SAFETY PROFILE ===
# Philosophy: Benjamin Graham / Buffett value investing for weather contracts.
# High win rate (~90%), consistent ~30% returns, proof-backed, never go to zero.
# Prefer threshold contracts (forgiving), late-day trades (low uncertainty),
# and only bet when multiple data sources converge.
MARGIN_OF_SAFETY_TRADING_OVERRIDES = {
    "min_edge_cents": 4.0,              # Need real edge, not noise
    "min_edge_after_fees_cents": 3.0,   # Must survive fees
    "min_fair_prob_to_recommend_buy": 0.65,  # Only bet when 65%+ confident on our side
    "max_fair_prob_hold_threshold": 0.40,    # Standard hold threshold
    "max_spread_cents": 12,             # Liquid markets only — tight spreads
    "max_contract_price": 88,           # Don't overpay (room for profit)
    "min_contract_price": 8,            # Avoid penny traps (wider than safe)
    "kelly_fraction": 0.10,             # Tenth-Kelly — very conservative sizing
    "min_volume": 0,                    # Weather markets are thin, allow zero
    "min_open_interest": 0,             # Same
    "illiquid_price_cents": 0,          # Handled by min_contract_price
    "max_trusted_edge_pct": 100.0,      # Cap displayed edge
    # Source agreement: require ALL independent sources to agree
    # (Reddit/KevinLuWX wisdom: only bet when everything converges)
    "min_source_agreement": 4,
    # Prefer late-day (lower uncertainty) but don't hard-block morning trades
    "preferred_max_hours_remaining": 6,
    # Overpriced bracket fader (KevinLuWX strategy):
    # Day-before brackets priced >50¢ are systematically overpriced.
    # Even elite forecasters have 1-2°F MAE — no single 2°F bracket
    # deserves >50% confidence 24h out. Fade them with NO.
    "overpriced_bracket_fade_threshold": 50,
    "_active_profile": "margin_of_safety",
}

MARGIN_OF_SAFETY_TRUST_GATES_OVERRIDES = {
    "penny_trap_max_price_cents": 8,          # Block asks ≤8¢ on unlocked brackets
    "require_nws_daily_consistency": True,     # NWS must agree with bracket
    "min_edge_after_fees_cents": 3.0,
    "min_fair_prob_to_recommend_buy": 0.65,
    "max_spread_cents": 12,
    # Require forecast margin to exceed historical MAE (margin of safety)
    "require_edge_exceeds_mae": True,
    # Hard-block NO bets when NWS forecast rounds INTO the bracket.
    # If NWS says 45°F and bracket is 44-45°, don't bet NO — no margin of safety.
    "min_forecast_buffer_f": 1.0,
}


# === LLM-FIRST PROFILE ===
# Philosophy: Remove all statistical gates. Scrape everything, pass it all to the LLM,
# and let the LLM be the sole intelligent decision-maker. All warnings/risk flags
# are provided as context (not hard blocks) so the LLM can evaluate holistically.
LLM_FIRST_TRADING_OVERRIDES = {
    "min_edge_percent": 0,                    # No minimum — LLM decides
    "min_edge_cents": 0,                      # No minimum — LLM decides
    "min_edge_after_fees_cents": 0,           # LLM accounts for fees itself
    "min_fair_prob_to_recommend_buy": 0,      # No probability floor
    "max_fair_prob_hold_threshold": 0,        # No HOLD threshold
    "max_spread_cents": 100,                  # Allow any spread
    "max_contract_price": 100,                # Allow any price
    "min_contract_price": 0,                  # Allow penny contracts
    "min_source_agreement": 0,                # No source agreement gate
    "kelly_fraction": 0.10,                   # Keep conservative sizing
    "illiquid_price_cents": 0,
    "min_volume": 0,
    "min_open_interest": 0,
    "forecast_bias_no_trade_f": 999,          # Effectively disable
    "ghost_gap_no_trade_f": 999,              # Effectively disable
    "dedupe_by_event": False,                 # Show all contracts to LLM
    "_active_profile": "llm_first",
}

LLM_FIRST_TRUST_GATES_OVERRIDES = {
    "penny_trap_max_price_cents": 0,          # Disable penny trap gate
    "require_nws_daily_consistency": False,    # Disable NWS consistency gate
    "require_edge_exceeds_mae": False,         # Disable MAE margin gate
    "min_edge_after_fees_cents": 0,
    "min_fair_prob_to_recommend_buy": 0,
    "max_spread_cents": 100,
    "min_forecast_buffer_f": 0,               # Disable forecast buffer gate
}


def apply_trading_profile(profile: str = "llm_first"):
    """Apply a trading profile by merging overrides into TRADING and TRUST_GATES."""
    if profile == "safe":
        TRADING.update(SAFE_TRADING_OVERRIDES)
        TRUST_GATES.update(SAFE_TRUST_GATES_OVERRIDES)
    elif profile == "aggressive":
        TRADING.update(AGGRESSIVE_TRADING_OVERRIDES)
        TRUST_GATES.update(AGGRESSIVE_TRUST_GATES_OVERRIDES)
    elif profile == "margin_of_safety":
        TRADING.update(MARGIN_OF_SAFETY_TRADING_OVERRIDES)
        TRUST_GATES.update(MARGIN_OF_SAFETY_TRUST_GATES_OVERRIDES)
    elif profile == "llm_first":
        TRADING.update(LLM_FIRST_TRADING_OVERRIDES)
        TRUST_GATES.update(LLM_FIRST_TRUST_GATES_OVERRIDES)
    # else: no overrides (use raw config)


# === UTILITIES ===
def bankers_round_half_up(value: float) -> int:
    """NWS rounding: 'round half up asymmetric' (toward +infinity).

    Positive: 0.5→1, 70.5→71, 3.5→4
    Negative: -1.5→-1, -3.5→-3, -3.6→-4, -2.6→-3

    VERIFIED against three official sources:
      • FMH-1 (2019) §2.6.3: "fractional part of positive ≥ 0.5 → increase;
        fractional part of negative > 0.5 → decrease; else unchanged."
        Examples given: 1.5→2, -1.5→-1, 1.3→1, -2.6→-3.
      • ASOS User's Guide (1998) §3.1.2: "All mid-point values rounded up.
        +3.5°F→+4°F; -3.5°F→-3°F; -3.6°F→-4°F."
      • NOAA NDST Rounding Advice: recommends 'round half up asymmetric'.

    NOT Python's built-in round() (which is 'round half to even').
    Function name kept for backward compat; behavior is correct.
    """
    frac = value - math.floor(value)
    if abs(frac - 0.5) < 1e-9:
        return math.ceil(value)
    return round(value)

def c_to_f(celsius: float) -> float:
    return celsius * 9.0 / 5.0 + 32.0

def is_dst(city_key: str) -> bool:
    from zoneinfo import ZoneInfo
    tz = ZoneInfo(CITIES[city_key]["timezone"])
    return bool(datetime.now(tz).dst())
