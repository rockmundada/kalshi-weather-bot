"""
Trust gate evaluation for high-confidence recommendations.
Hard gates are never overridable; soft gates can be overridden by LLM if allowed.
"""
from __future__ import annotations

import re
from datetime import date, datetime
from zoneinfo import ZoneInfo

from config import CITIES, TRUST_GATES, TRADING


# ── Contract type classification ──────────────────────────────────

def classify_contract_type(edge: dict) -> str:
    """Classify a contract as 'threshold', 'bracket', or 'unknown'.

    Threshold contracts are more forgiving (e.g., "71° or above", "≤27°").
    Bracket contracts require landing in a tight range (e.g., "28° to 29°").
    """
    subtitle = (edge.get("contract_subtitle") or "").lower()

    # Check for threshold keywords
    for kw in ("or above", "or higher", "or more", "or over", "or warmer", "or hotter",
                "or below", "or lower", "or less", "or under", "or colder", "or cooler",
                "greater than", "less than", "above", "below", ">=", "<="):
        if kw in subtitle:
            return "threshold"

    # Ticker-based: -T in ticker = threshold (e.g., KXHIGHDEN-26FEB09-T71)
    ticker = edge.get("contract_ticker", "")
    parts = ticker.split("-")
    if len(parts) >= 3:
        strike_part = parts[-1]
        if strike_part.startswith("T"):
            return "threshold"
        if strike_part.startswith("B"):
            return "bracket"

    # Check floor/cap: one None = threshold
    floor_s = edge.get("floor_strike")
    cap_s = edge.get("cap_strike")
    if floor_s is None or cap_s is None:
        return "threshold"

    # If both present, it's a bracket
    if floor_s is not None and cap_s is not None:
        return "bracket"

    return "unknown"


def get_bracket_width(edge: dict) -> float | None:
    """Get the width of a bracket contract in degrees. Returns None for thresholds."""
    floor_s = edge.get("floor_strike")
    cap_s = edge.get("cap_strike")
    if floor_s is not None and cap_s is not None:
        try:
            return abs(float(cap_s) - float(floor_s))
        except (TypeError, ValueError):
            pass
    return None


def _get_side_prob(edge: dict) -> float | None:
    fair_prob = edge.get("fair_prob")
    if fair_prob is None:
        return None
    side = edge.get("side")
    if side == "yes":
        return fair_prob
    if side == "no":
        return 1 - fair_prob
    return None


def _is_today_contract(edge: dict) -> bool:
    city_key = edge.get("city")
    dkey = edge.get("contract_date")
    if not city_key or not dkey:
        return False
    try:
        tz = ZoneInfo(CITIES[city_key]["timezone"])
        today_local = datetime.now(tz).date()
        return date.fromisoformat(dkey) == today_local
    except Exception:
        return False


def evaluate_trust_gate(edge: dict, weather_data: dict, trust_config: dict | None = None) -> dict:
    """
    Evaluate hard/soft trust gates for a single edge.
    Returns a dict with hard_block, soft_block, trust_score, gate_reasons,
    contract_type, bracket_width.
    """
    trust = trust_config or TRUST_GATES
    reasons: list[str] = []
    hard_reasons: list[str] = []
    soft_reasons: list[str] = []

    # Classify contract type early (used by several gates)
    contract_type = classify_contract_type(edge)
    bracket_width = get_bracket_width(edge)
    edge["contract_type"] = contract_type
    edge["bracket_width"] = bracket_width

    # Respect any prior hard block
    if edge.get("hard_block"):
        hard_reasons.append("Prior hard block")

    # Subtitle parsing (hard gate)
    require_subtitle = trust.get("require_subtitle_parse", TRADING.get("require_subtitle_parse", False))
    if require_subtitle and not edge.get("strike_parse_ok", False):
        hard_reasons.append("Subtitle parse failed")

    # Executable ask (hard gate)
    if trust.get("require_executable_ask", True):
        side_ask = edge.get("side_ask_cents") or 0
        if side_ask <= 0:
            hard_reasons.append("No executable ask")

    # Station match (hard gate)
    if trust.get("require_station_match", False):
        station_is_backup = bool(weather_data.get("station_is_backup"))
        if station_is_backup and not trust.get("allow_backup_station", False):
            hard_reasons.append("Station mismatch (backup station)")

    # Determine if this is a today contract (used by multiple gates below)
    is_today = _is_today_contract(edge)

    # ── OBSERVED MAX THRESHOLD LOCK (hard) ──
    # If the observed max (ASOS or T-group) is already at or near a tail threshold,
    # block NO bets on that tail. Once the station hits it, it's a floor.
    # Example: Philly ASOS shows 45°F → block NO on "≥45°F" because it already happened.
    if edge.get("market_type") == "high_temp":
        obs_max = weather_data.get("max_tgroup_f") or weather_data.get("metar", {}).get("best_max_f")
        obs_5min = weather_data.get("max_5min_f") or (weather_data.get("obs_5min") or {}).get("max_5min_f")
        # Use the higher of T-group and 5-min ASOS as the "worst case" observed max
        effective_obs = None
        if obs_max is not None and obs_5min is not None:
            effective_obs = max(obs_max, obs_5min)
        elif obs_max is not None:
            effective_obs = obs_max
        elif obs_5min is not None:
            effective_obs = obs_5min

        if effective_obs is not None and is_today:
            side = edge.get("side", "")
            floor_s = edge.get("floor_strike")
            cap_s = edge.get("cap_strike")

            if contract_type == "threshold":
                # For "≥X" tails (NO side): if obs >= X-0.5 (rounding margin), block NO
                if side == "no" and floor_s is not None:
                    try:
                        threshold = float(floor_s)
                        if effective_obs >= threshold - 0.5:
                            hard_reasons.append(
                                f"THRESHOLD LOCK: Observed max {effective_obs:.1f}°F already at/near "
                                f"≥{threshold:.0f}°F threshold (rounding margin). "
                                f"Cannot bet NO — temp floor is locked."
                            )
                    except (TypeError, ValueError):
                        pass
                # For "≤X" tails (NO side): if obs already above X+0.5, the tail is dead
                if side == "no" and cap_s is not None and floor_s is None:
                    try:
                        threshold = float(cap_s)
                        if effective_obs > threshold + 0.5:
                            hard_reasons.append(
                                f"THRESHOLD LOCK: Observed max {effective_obs:.1f}°F already above "
                                f"≤{threshold:.0f}°F threshold. Tail is dead."
                            )
                    except (TypeError, ValueError):
                        pass

            if contract_type == "bracket":
                # For NO on a bracket: if obs already rounds INTO the bracket, block NO
                if side == "no" and floor_s is not None and cap_s is not None:
                    try:
                        from config import bankers_round_half_up
                        fl = float(floor_s)
                        cp = float(cap_s)
                        obs_rounded = bankers_round_half_up(effective_obs)
                        if fl <= obs_rounded <= cp:
                            hard_reasons.append(
                                f"THRESHOLD LOCK: Observed max {effective_obs:.1f}°F (→{obs_rounded}°F) "
                                f"already in bracket {fl:.0f}-{cp:.0f}°F. Cannot bet NO."
                            )
                    except (TypeError, ValueError):
                        pass

    # ── TAIL PROXIMITY PENALTY (hard) ──
    # If the warmest model is within 1°F of a tail threshold AND hours > 4 remaining,
    # block NO bets on that tail. This catches the Philly scenario where GFS=44.5
    # was within 0.5°F of the 45°F threshold but the bot still recommended NO.
    if edge.get("market_type") == "high_temp" and is_today:
        side = edge.get("side", "")
        model_highs = weather_data.get("ensemble", {}).get("model_highs") or {}
        hours_left_val = weather_data.get("hours_remaining") or weather_data.get("nws", {}).get("hourly", {}).get("hours_remaining", 0)

        if side == "no" and model_highs and hours_left_val and hours_left_val > 4:
            warmest_model = max(model_highs.values()) if model_highs else None
            if warmest_model is not None:
                if contract_type == "threshold" and edge.get("floor_strike") is not None:
                    try:
                        threshold = float(edge["floor_strike"])
                        if warmest_model >= threshold - 1.0:
                            model_name = max(model_highs, key=model_highs.get)
                            hard_reasons.append(
                                f"TAIL PROXIMITY: Warmest model {model_name}={warmest_model:.1f}°F "
                                f"within 1°F of ≥{threshold:.0f}°F threshold with {hours_left_val:.0f}h left. "
                                f"Too risky for NO bet."
                            )
                    except (TypeError, ValueError):
                        pass
                elif contract_type == "bracket" and edge.get("floor_strike") is not None and edge.get("cap_strike") is not None:
                    try:
                        fl = float(edge["floor_strike"])
                        cp = float(edge["cap_strike"])
                        # If warmest model rounds into or near the bracket
                        if warmest_model >= fl - 1.0 and warmest_model <= cp + 1.0:
                            model_name = max(model_highs, key=model_highs.get)
                            hard_reasons.append(
                                f"TAIL PROXIMITY: Warmest model {model_name}={warmest_model:.1f}°F "
                                f"within 1°F of bracket {fl:.0f}-{cp:.0f}°F with {hours_left_val:.0f}h left. "
                                f"Too risky for NO bet."
                            )
                    except (TypeError, ValueError):
                        pass

    # ── NWS SOURCE DISCREPANCY GATE (hard for NO bets) ──
    # If NWS daily high vs tabular high vs hourly max disagree by ≥2°F AND the
    # higher value rounds into the losing zone, block the bet.
    # This catches the Miami scenario: daily=78, tabular=75, but daily is at KMIA.
    if edge.get("market_type") == "high_temp":
        hourly = weather_data.get("nws", {}).get("hourly", {})
        nws_daily = hourly.get("_nws_daily_high")
        nws_hourly_max = hourly.get("_nws_hourly_max")
        tab_high = hourly.get("_tabular_high")
        side = edge.get("side", "")

        # Collect all available NWS source values
        nws_sources = {}
        if nws_daily is not None:
            nws_sources["daily"] = float(nws_daily)
        if nws_hourly_max is not None:
            nws_sources["hourly"] = float(nws_hourly_max)
        if tab_high is not None:
            nws_sources["tabular"] = float(tab_high)

        if len(nws_sources) >= 2:
            max_nws = max(nws_sources.values())
            min_nws = min(nws_sources.values())
            discrepancy = max_nws - min_nws

            if discrepancy >= 2.0 and side == "no":
                # Check if the highest NWS source value threatens the bet
                if contract_type == "bracket" and edge.get("floor_strike") is not None and edge.get("cap_strike") is not None:
                    try:
                        from config import bankers_round_half_up
                        fl = float(edge["floor_strike"])
                        cp = float(edge["cap_strike"])
                        max_rounded = bankers_round_half_up(max_nws)
                        if fl <= max_rounded <= cp:
                            sources_str = ", ".join(f"{k}={v:.0f}" for k, v in nws_sources.items())
                            hard_reasons.append(
                                f"NWS DISCREPANCY: Sources disagree by {discrepancy:.0f}°F "
                                f"({sources_str}). Highest ({max_nws:.0f}°F→{max_rounded}°F) "
                                f"lands IN bracket {fl:.0f}-{cp:.0f}°F. Not safe for NO."
                            )
                    except (TypeError, ValueError):
                        pass
                elif contract_type == "threshold" and edge.get("floor_strike") is not None:
                    try:
                        threshold = float(edge["floor_strike"])
                        if max_nws >= threshold - 0.5:
                            sources_str = ", ".join(f"{k}={v:.0f}" for k, v in nws_sources.items())
                            hard_reasons.append(
                                f"NWS DISCREPANCY: Sources disagree by {discrepancy:.0f}°F "
                                f"({sources_str}). Highest ({max_nws:.0f}°F) near/above "
                                f"≥{threshold:.0f}°F threshold. Not safe for NO."
                            )
                    except (TypeError, ValueError):
                        pass

    # ── PENNY PRICE TRAP GATE (hard) ──
    # If ask ≤ 5¢ on a tight bracket and the high is NOT locked → almost certainly
    # the bot is mis-estimating. These "2¢ free money" trades are traps.
    side_ask = edge.get("side_ask_cents") or 0
    penny_threshold = trust.get("penny_trap_max_price_cents", 5)
    if (side_ask > 0 and side_ask <= penny_threshold
            and edge.get("market_type") == "high_temp"
            and contract_type == "bracket"
            and not weather_data.get("locked_high")):
        hours_to_peak = weather_data.get("hours_remaining_peak")
        if hours_to_peak is None or hours_to_peak > 1.0:
            hard_reasons.append(
                f"Penny trap: {side_ask}¢ ask on unlocked bracket "
                f"(high not locked, peak not passed)"
            )

    # ── NWS DAILY CONSISTENCY GATE (hard for brackets, soft for thresholds) ──
    # If NWS daily forecast high (rounded) is outside the bracket by >1°F → block.
    # This catches the "bot says 29°F, NWS says 31°F" type of disagreement.
    if edge.get("market_type") == "high_temp" and trust.get("require_nws_daily_consistency", True):
        from config import bankers_round_half_up
        nws_daily_high = weather_data.get("forecast_high_day_f")
        if nws_daily_high is not None:
            nws_rounded = bankers_round_half_up(nws_daily_high)
            floor_s = edge.get("floor_strike")
            cap_s = edge.get("cap_strike")
            if contract_type == "bracket" and floor_s is not None and cap_s is not None:
                try:
                    fl = float(floor_s)
                    cp = float(cap_s)
                    side = edge.get("side", "")
                    # NWS rounded high is outside bracket by more than 1°F
                    if nws_rounded < fl - 1 or nws_rounded > cp + 1:
                        if side == "yes":
                            hard_reasons.append(
                                f"NWS daily high {nws_daily_high:.0f}°F (→{nws_rounded}) "
                                f"outside bracket {fl:.0f}-{cp:.0f}°F"
                            )
                        else:
                            soft_reasons.append(
                                f"NWS daily high {nws_daily_high:.0f}°F near bracket edge"
                            )
                    # NWS forecast lands IN bracket — hard-block NO bets
                    # (don't bet against the NWS forecast landing in the bracket)
                    if side == "no" and fl <= nws_rounded <= cp:
                        min_buffer = trust.get("min_forecast_buffer_f", 0)
                        if min_buffer > 0:
                            hard_reasons.append(
                                f"NWS forecast {nws_daily_high:.0f}°F (→{nws_rounded}) "
                                f"lands IN bracket {fl:.0f}-{cp:.0f}°F — "
                                f"no margin of safety for NO bet"
                            )
                except (TypeError, ValueError):
                    pass

    # ── TABULAR CROSS-CHECK GATE (catches API vs website discrepancy) ──
    # The NWS tabular (website) forecast can show a HIGHER peak than the API.
    # If the tabular high rounds INTO the bracket and we're betting NO, block it.
    # This catches the Miami scenario: API=75°F but tabular shows 76°F at 3pm.
    if edge.get("market_type") == "high_temp" and trust.get("min_forecast_buffer_f", 0) > 0:
        from config import bankers_round_half_up
        tab_high = weather_data.get("tabular_high_f")
        if tab_high is not None:
            floor_s = edge.get("floor_strike")
            cap_s = edge.get("cap_strike")
            side = edge.get("side", "")
            if contract_type == "bracket" and floor_s is not None and cap_s is not None:
                try:
                    fl = float(floor_s)
                    cp = float(cap_s)
                    tab_rounded = bankers_round_half_up(tab_high)
                    if side == "no" and fl <= tab_rounded <= cp:
                        hard_reasons.append(
                            f"NWS tabular high {tab_high}°F (→{tab_rounded}) "
                            f"lands IN bracket {fl:.0f}-{cp:.0f}°F — "
                            f"website disagrees with API, no margin of safety"
                        )
                except (TypeError, ValueError):
                    pass

    # Ghost flags (soft gate - warn but don't block)
    ghost_flags = weather_data.get("ghost_flags") or []
    if ghost_flags:
        soft_reasons.append("Ghost flags present")

    # Liquidity soft gates (warnings only)
    max_spread = trust.get("max_spread_cents")
    spread = edge.get("spread_cents")
    if max_spread and max_spread > 0:
        if spread is not None and spread > max_spread:
            soft_reasons.append(f"Spread {spread}¢ (wide)")

    min_volume = trust.get("min_volume")
    if min_volume and min_volume > 0:
        volume = edge.get("volume", 0) or 0
        if volume < min_volume:
            soft_reasons.append(f"Volume {volume} < {min_volume}")

    min_oi = trust.get("min_open_interest")
    if min_oi and min_oi > 0:
        oi = edge.get("open_interest", 0) or 0
        if oi < min_oi:
            soft_reasons.append(f"Open interest {oi} < {min_oi}")

    min_side_size = trust.get("min_side_book_size")
    if min_side_size and min_side_size > 0:
        side_size = edge.get("side_ask_size")
        if side_size is not None and side_size < min_side_size:
            soft_reasons.append(f"Top-of-book size {side_size or 0} < {min_side_size}")

    # Same-day gate - softened for pre-CLI prediction trading
    if is_today:
        if not trust.get("same_day_allowed", False):
            hard_reasons.append("Same-day trades disabled")
        else:
            # These are now soft warnings, not hard blocks
            if not weather_data.get("locked_high"):
                soft_reasons.append("High not yet locked (pre-CLI)")

            max_age = trust.get("same_day_max_metar_age_min", 180)
            metar_age = weather_data.get("metar_age_min")
            if metar_age is None:
                metar_age = weather_data.get("metar", {}).get("latest_obs_age_min")
            if metar_age is not None and metar_age > max_age:
                soft_reasons.append(f"METAR age {metar_age}m (observations may be stale)")

    # Soft gates
    min_edge_net = trust.get("min_edge_after_fees_cents", TRADING.get("min_edge_after_fees_cents", 0))
    fee_cents = TRADING.get("estimated_fee_cents", 0) or 0
    edge_cents = float(edge.get("edge_cents", 0) or 0)
    if min_edge_net and (edge_cents - fee_cents) < min_edge_net:
        soft_reasons.append(f"Net edge {(edge_cents - fee_cents):+.1f}¢ < {min_edge_net:.1f}¢")

    min_prob = trust.get("min_fair_prob_to_recommend_buy", TRADING.get("min_fair_prob_to_recommend_buy", 0.6))
    side_prob = _get_side_prob(edge)
    if side_prob is not None and side_prob < min_prob:
        soft_reasons.append(f"Fair prob {side_prob*100:.0f}% < {min_prob*100:.0f}%")

    # Future-date uncertainty/spread gates (soft)
    if not is_today:
        max_unc = TRADING.get("max_uncertainty_f", 0)
        unc = edge.get("uncertainty_f")
        if max_unc and isinstance(unc, (int, float)) and unc > max_unc:
            soft_reasons.append(f"Uncertainty {unc:.1f}°F > {max_unc:.1f}°F")

        max_model_spread = TRADING.get("max_model_spread_f", 0)
        model_spread = weather_data.get("model_spread_f")
        if max_model_spread and isinstance(model_spread, (int, float)) and model_spread > max_model_spread:
            soft_reasons.append(f"Model spread {model_spread:.1f}°F > {max_model_spread:.1f}°F")

    # ── MARGIN OF SAFETY: Edge must exceed historical MAE ──
    # If our bot's forecast MAE is 1.5°F and the edge is based on only 1°F of
    # forecast advantage, the edge is within noise — not a real edge.
    if trust.get("require_edge_exceeds_mae", False) and edge.get("market_type") == "high_temp":
        calibration = weather_data.get("calibration") or {}
        mae = calibration.get("mae_f")
        if mae and isinstance(mae, (int, float)) and mae > 0:
            forecast_high = weather_data.get("best_forecast_high_f")
            if forecast_high is not None:
                bounds = edge.get("strike_bounds") or {}
                margin_f = None
                if bounds.get("kind") == "above":
                    floor_val = bounds.get("floor")
                    if floor_val is not None:
                        margin_f = forecast_high - floor_val
                elif bounds.get("kind") == "below":
                    cap_val = bounds.get("cap")
                    if cap_val is not None:
                        margin_f = cap_val - forecast_high
                elif bounds.get("kind") == "range":
                    low_val = bounds.get("low")
                    high_val = bounds.get("high")
                    if low_val is not None and high_val is not None:
                        # Distance from forecast to nearest bracket edge
                        if forecast_high < low_val:
                            margin_f = -(low_val - forecast_high)
                        elif forecast_high > high_val:
                            margin_f = -(forecast_high - high_val)
                        else:
                            margin_f = min(forecast_high - low_val, high_val - forecast_high)

                if margin_f is not None and abs(margin_f) < mae * 0.8:
                    soft_reasons.append(
                        f"MoS: forecast margin {abs(margin_f):.1f}F < MAE {mae:.1f}F "
                        f"(edge may be within forecast noise)"
                    )

    # ── MARGIN OF SAFETY MODE GATES ──
    mos_mode = TRADING.get("_active_profile") == "margin_of_safety"
    if mos_mode and edge.get("market_type") == "high_temp":
        # In MoS mode, only allow brackets if the high is locked or bracket is wide (≥3°F).
        # SOFTENED: When >12h remaining (early day / morning trading), downgrade from
        # hard block to soft warning. This allows placing limit (maker) orders early
        # that fill as the day progresses and uncertainty drops.
        # Source: Bot output analysis — at midnight, this gate blocked ALL bracket trades,
        # which is too restrictive for a $143 bankroll that needs to trade every day.
        if contract_type == "bracket":
            if not weather_data.get("locked_high"):
                if bracket_width is not None and bracket_width < 3:
                    hours_left = weather_data.get("hours_remaining", 0) or 0
                    if hours_left > 12:
                        soft_reasons.append(
                            f"MoS: Tight bracket ({bracket_width:.0f}F) with unlocked high "
                            f"(early day — consider maker order)"
                        )
                    else:
                        hard_reasons.append(
                            f"MoS: Tight bracket ({bracket_width:.0f}F) with unlocked high"
                        )
        # Require NWS forecast to be available
        if weather_data.get("forecast_high_day_f") is None:
            soft_reasons.append("MoS: No NWS daily forecast available")
        # Require spread ≤ configured max
        mos_max_spread = trust.get("max_spread_cents", 12)
        if spread is not None and mos_max_spread and spread > mos_max_spread:
            soft_reasons.append(f"MoS: Spread {spread}c > {mos_max_spread}c limit")

    # ── SAFE MODE GATES ──
    # When TRADING_PROFILE is "safe", apply stricter filters
    safe_mode = TRADING.get("_active_profile") == "safe"
    if safe_mode and edge.get("market_type") == "high_temp":
        # In safe mode, only allow brackets if the high is locked or bracket is wide (≥3°F)
        if contract_type == "bracket":
            if not weather_data.get("locked_high"):
                if bracket_width is not None and bracket_width < 3:
                    hard_reasons.append(
                        f"SAFE: Tight bracket ({bracket_width:.0f}°F) with unlocked high"
                    )
        # In safe mode, require NWS forecast to be available
        if weather_data.get("forecast_high_day_f") is None:
            soft_reasons.append("SAFE: No NWS daily forecast available")
        # In safe mode, require spread ≤ 15¢
        if spread is not None and spread > 15:
            soft_reasons.append(f"SAFE: Spread {spread}¢ > 15¢ limit")

    # Calibration-based trust adjustments (no hard block)
    calibration = weather_data.get("calibration") or {}
    calibration_warn = False
    mae_warn = trust.get("calibration_warn_mae_f", 0)
    brier_warn = trust.get("calibration_warn_brier", 0)
    mae = calibration.get("mae_f")
    brier = calibration.get("brier")
    if isinstance(mae, (int, float)) and mae_warn and mae > mae_warn:
        calibration_warn = True
    if isinstance(brier, (int, float)) and brier_warn and brier > brier_warn:
        calibration_warn = True

    # Compute trust score
    trust_score = 100
    trust_score -= 25 * len(hard_reasons)
    trust_score -= 10 * len(soft_reasons)
    if calibration_warn:
        trust_score -= 10
    trust_score = max(0, min(100, trust_score))

    hard_block = len(hard_reasons) > 0
    soft_block = len(soft_reasons) > 0

    reasons.extend([f"H: {r}" for r in hard_reasons])
    reasons.extend([f"S: {r}" for r in soft_reasons])
    if calibration_warn and calibration.get("badge"):
        reasons.append(f"C: {calibration.get('badge')}")

    return {
        "hard_block": hard_block,
        "soft_block": soft_block,
        "trust_score": trust_score,
        "gate_reasons": reasons,
        "contract_type": contract_type,
        "bracket_width": bracket_width,
    }
