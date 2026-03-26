"""
Telegram notifications for trading alerts and daily summaries.
"""
import logging
import requests

from config import TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID

log = logging.getLogger(__name__)

_TELEGRAM_DISABLED = False

TELEGRAM_URL = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}"


def send_message(text: str, parse_mode: str = "HTML") -> bool:
    """Send a Telegram message. Falls back to plain text if HTML fails."""
    global _TELEGRAM_DISABLED
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        log.debug("Telegram not configured, skipping")
        return False
    if _TELEGRAM_DISABLED:
        log.debug("Telegram disabled for this run, skipping")
        return False

    try:
        r = requests.post(
            f"{TELEGRAM_URL}/sendMessage",
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text[:4096],
                "parse_mode": parse_mode,
            },
            timeout=10,
        )
        if r.status_code != 200:
            body = r.text.lower()
            if "chat not found" in body or "forbidden" in body:
                _TELEGRAM_DISABLED = True
                log.warning("Telegram disabled for this run: chat not found/forbidden")
                return False
            if r.status_code == 400 and parse_mode == "HTML":
                # HTML parse error - retry as plain text (strip HTML tags)
                import re
                plain = re.sub(r'<[^>]+>', '', text)
                r2 = requests.post(
                    f"{TELEGRAM_URL}/sendMessage",
                    json={
                        "chat_id": TELEGRAM_CHAT_ID,
                        "text": plain[:4096],
                    },
                    timeout=10,
                )
                if r2.status_code != 200:
                    body2 = r2.text.lower()
                    if "chat not found" in body2 or "forbidden" in body2:
                        _TELEGRAM_DISABLED = True
                        log.warning("Telegram disabled for this run: chat not found/forbidden")
                        return False
                    log.error(f"Telegram send failed (plain text fallback): {r2.status_code} {r2.text[:200]}")
                    return False
                return True
            log.error(f"Telegram send failed: {r.status_code} {r.text[:200]}")
            return False
        return True
    except Exception as e:
        log.error(f"Telegram error: {e}")
        return False


def send_trade_alert(trade: dict) -> bool:
    """Send alert for a trade execution."""
    side = trade.get("final_side", trade.get("side", "?")).upper()
    ticker = trade.get("contract_ticker", "?")
    city = trade.get("city", "?")
    edge = trade.get("final_edge_cents", trade.get("edge_cents", 0))
    confidence = trade.get("combined_confidence", trade.get("claude_confidence", "?"))
    contracts = trade.get("contracts", 0)
    cost = trade.get("cost", 0)
    market_type = trade.get("market_type", "?")

    msg = (
        f"🔔 <b>TRADE: {side} {ticker}</b>\n"
        f"City: {city} | Type: {market_type}\n"
        f"Edge: {edge:.1f}¢ | Confidence: {confidence}\n"
        f"Contracts: {contracts} | Cost: ${cost:.2f}\n"
    )

    reasoning = trade.get("claude_reasoning", "")
    if reasoning:
        msg += f"Reasoning: {reasoning[:200]}\n"

    return send_message(msg)


def send_recommendation(recs: list[dict], city_key: str, weather_summary: str = "") -> bool:
    """Send morning recommendation summary."""
    if not recs:
        return send_message(f"📊 <b>{city_key}</b>: No profitable trades found.")

    msg = f"📊 <b>{city_key} Recommendations</b>\n"
    if weather_summary:
        msg += f"\n{weather_summary}\n"

    msg += f"\n<b>Top Trades:</b>\n"
    for r in recs[:10]:
        side = r.get("final_side", r.get("side", "?")).upper()
        ticker = r.get("contract_ticker", "?")
        edge = r.get("final_edge_cents", r.get("edge_cents", 0))
        conf = r.get("combined_confidence", "?")
        fair = r.get("fair_price", "?")
        mkt = r.get("market_price", "?")
        msg += f"  {side} {ticker} edge={edge:.1f}¢ fair={fair} mkt={mkt} [{conf}]\n"

    return send_message(msg)


def send_daily_summary(results: dict) -> bool:
    """Send end-of-day P&L summary."""
    balance = results.get("balance", 0)
    trades_made = results.get("trades_made", 0)
    pnl = results.get("pnl", 0)
    wins = results.get("wins", 0)
    losses = results.get("losses", 0)

    emoji = "💰" if pnl >= 0 else "📉"
    msg = (
        f"{emoji} <b>Daily Summary</b>\n"
        f"P&L: ${pnl:+.2f}\n"
        f"Trades: {trades_made} | W/L: {wins}/{losses}\n"
        f"Balance: ${balance:.2f}\n"
    )

    cities_summary = results.get("cities", {})
    for city, data in cities_summary.items():
        actual = data.get("actual_high", "?")
        forecast = data.get("our_forecast", "?")
        msg += f"\n{city}: actual={actual}°F forecast={forecast}°F\n"

    return send_message(msg)


def send_error(error_msg: str) -> bool:
    """Send error notification."""
    return send_message(f"⚠️ <b>ERROR</b>\n{error_msg[:3000]}")
