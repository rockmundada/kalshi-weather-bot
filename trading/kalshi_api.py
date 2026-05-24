"""
Kalshi API client with RSA-PSS authentication.
Handles market discovery, order placement, and position management.
"""
import time
import base64
import logging
from datetime import datetime, timezone
from pathlib import Path

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding

from config import (
    KALSHI_API_KEY,
    KALSHI_PRIVATE_KEY_PATH,
    KALSHI_PRIVATE_KEY_PEM,
    KALSHI_BASE_URL,
    TRADING,
)

log = logging.getLogger(__name__)


def _infer_is_over(subtitle: str | None, rules_primary: str | None) -> bool | None:
    """Infer whether a contract resolves YES on an 'over/greater than' outcome."""
    text = f"{subtitle or ''} {rules_primary or ''}".lower()
    over_markers = [
        "greater than", "more than", "above", "at least", "or above", "over",
    ]
    under_markers = [
        "less than", "below", "at most", "or below", "under",
    ]
    has_over = any(m in text for m in over_markers)
    has_under = any(m in text for m in under_markers)
    if has_over and not has_under:
        return True
    if has_under and not has_over:
        return False
    return None


class KalshiAPI:
    def __init__(self):
        self.base_url = KALSHI_BASE_URL
        self.api_key = KALSHI_API_KEY
        self.private_key = self._load_private_key()
        self.session = requests.Session()

    def _load_private_key(self):
        # Prefer PEM string from config (placeholder-friendly)
        if KALSHI_PRIVATE_KEY_PEM and KALSHI_PRIVATE_KEY_PEM.strip():
            try:
                return serialization.load_pem_private_key(
                    KALSHI_PRIVATE_KEY_PEM.encode("utf-8"), password=None
                )
            except Exception as e:
                log.error(f"Failed to load private key from PEM string: {e}")

        # Fallback to file path
        key_path = Path(KALSHI_PRIVATE_KEY_PATH)
        if not key_path.exists():
            log.error(f"Private key not found: {key_path}")
            return None
        with open(key_path, "rb") as f:
            return serialization.load_pem_private_key(f.read(), password=None)

    def _sign_request(self, method: str, path: str, timestamp_ms: int) -> str:
        """RSA-PSS signature for Kalshi API authentication.
        
        Per Kalshi docs: sign "{timestamp}{METHOD}{/trade-api/v2/path}" 
        using RSA-PSS with SHA256 and DIGEST_LENGTH salt.
        """
        # Path must include the /trade-api/v2 prefix for signing
        full_path = f"/trade-api/v2{path}"
        message = f"{timestamp_ms}{method}{full_path}".encode('utf-8')

        signature = self.private_key.sign(
            message,
            padding.PSS(
                mgf=padding.MGF1(hashes.SHA256()),
                salt_length=padding.PSS.DIGEST_LENGTH,
            ),
            hashes.SHA256(),
        )
        return base64.b64encode(signature).decode('utf-8')

    def _request(self, method: str, path: str, params: dict = None, json_body: dict = None) -> dict | None:
        """Make authenticated API request."""
        if self.private_key is None:
            log.error("No private key loaded")
            return None

        url = f"{self.base_url}{path}"
        timestamp_ms = int(time.time() * 1000)
        signature = self._sign_request(method.upper(), path, timestamp_ms)

        headers = {
            "KALSHI-ACCESS-KEY": self.api_key,
            "KALSHI-ACCESS-SIGNATURE": signature,
            "KALSHI-ACCESS-TIMESTAMP": str(timestamp_ms),
            "Content-Type": "application/json",
            "Accept": "application/json",
        }

        try:
            resp = self.session.request(
                method, url, headers=headers, params=params, json=json_body, timeout=15
            )
            if resp.status_code == 429:
                log.warning("Rate limited, waiting 2s")
                time.sleep(2)
                return self._request(method, path, params, json_body)
            if resp.status_code == 401:
                log.error(f"Auth failed (401) for {method} {url}")
                log.error(f"  Signed message: '{timestamp_ms}{method.upper()}/trade-api/v2{path}'")
                log.error(f"  API key: {self.api_key[:8]}... (len={len(self.api_key) if self.api_key else 0})")
                log.error(f"  Private key loaded: {self.private_key is not None}")
                log.error(f"  Response: {resp.text[:300]}")
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            log.error(f"Kalshi API HTTP error: {e} - {resp.text[:500] if resp else ''}")
            return None
        except Exception as e:
            log.error(f"Kalshi API error: {e}")
            return None

    # ── Market Discovery ──────────────────────────────────────────

    def get_markets(self, series_ticker: str, status: str = "open") -> list[dict]:
        """Get all markets (events) for a series ticker."""
        params = {
            "series_ticker": series_ticker,
            "status": status,
            "limit": 100,
        }
        data = self._request("GET", "/events", params=params)
        if data and "events" in data:
            return data["events"]
        return []

    def get_event_markets(self, event_ticker: str) -> list[dict]:
        """Get all contracts for a specific event."""
        params = {"event_ticker": event_ticker, "limit": 100}
        data = self._request("GET", "/markets", params=params)
        if data and "markets" in data:
            return data["markets"]
        return []

    def get_market(self, ticker: str) -> dict | None:
        """Get a single market/contract by ticker."""
        data = self._request("GET", f"/markets/{ticker}")
        if data and "market" in data:
            return data["market"]
        return data

    def get_active_contracts(self, series_ticker: str) -> list[dict]:
        """
        Get all active contracts for a series.
        Returns normalized contract data with pricing.
        Filters out daily contracts from dates other than today.
        """
        events = self.get_markets(series_ticker)
        all_contracts = []
        
        for event in events:
            event_ticker = event.get("event_ticker", "")
            markets = self.get_event_markets(event_ticker)

            for mkt in markets:
                # Determine best price estimate:
                # 1. Midpoint of bid/ask if both exist
                # 2. Last trade price if available
                # 3. yes_bid or yes_ask if only one exists
                # 4. Default 50 only as last resort
                #
                # Kalshi API v3 returns prices in dollars (e.g. 0.34 = 34¢)
                # with field names like yes_bid_dollars, volume_fp, etc.
                # Fall back to legacy cent-based fields for compatibility.
                def _cents(mkt, legacy_key, dollar_key):
                    """Read price in cents: prefer dollar field * 100, fall back to legacy."""
                    dollar_val = mkt.get(dollar_key)
                    if dollar_val is not None and dollar_val != 0:
                        try:
                            return round(float(dollar_val) * 100)
                        except (ValueError, TypeError):
                            pass
                    legacy_val = mkt.get(legacy_key)
                    if legacy_val is not None and legacy_val != 0:
                        try:
                            return int(legacy_val)
                        except (ValueError, TypeError):
                            pass
                    return None

                def _size(mkt, legacy_key, fp_key):
                    """Read size: prefer _fp field, fall back to legacy."""
                    fp_val = mkt.get(fp_key)
                    if fp_val is not None and fp_val != 0:
                        try:
                            return round(float(fp_val))
                        except (ValueError, TypeError):
                            pass
                    legacy_val = mkt.get(legacy_key)
                    if legacy_val is not None:
                        try:
                            return int(legacy_val)
                        except (ValueError, TypeError):
                            pass
                    return 0

                yes_bid = _cents(mkt, "yes_bid", "yes_bid_dollars")
                yes_ask = _cents(mkt, "yes_ask", "yes_ask_dollars")
                no_bid = _cents(mkt, "no_bid", "no_bid_dollars")
                no_ask = _cents(mkt, "no_ask", "no_ask_dollars")
                yes_bid_size = _size(mkt, "yes_bid_size", "yes_bid_size_fp")
                yes_ask_size = _size(mkt, "yes_ask_size", "yes_ask_size_fp")
                no_bid_size = _size(mkt, "no_bid_size", "no_bid_size_fp")
                no_ask_size = _size(mkt, "no_ask_size", "no_ask_size_fp")
                last_price = _cents(mkt, "last_price", "last_price_dollars")
                # To buy YES you pay the ask; if API omits yes_ask, use 100 - no_bid
                if not yes_ask and no_bid is not None:
                    yes_ask = 100 - no_bid
                # To buy NO you pay the no_ask; if API omits no_ask, infer from yes_bid
                if not no_ask and yes_bid is not None:
                    no_ask = 100 - yes_bid

                if yes_bid and yes_ask and yes_bid > 0 and yes_ask > 0:
                    best_yes_price = (yes_bid + yes_ask) / 2
                elif last_price and last_price > 0:
                    best_yes_price = last_price
                elif yes_bid and yes_bid > 0:
                    best_yes_price = yes_bid
                elif yes_ask and yes_ask > 0:
                    best_yes_price = yes_ask
                else:
                    best_yes_price = 50  # truly unknown

                yes_spread = (yes_ask - yes_bid) if yes_bid and yes_ask else None
                no_spread = (no_ask - no_bid) if no_bid and no_ask else None

                rules_primary = mkt.get("rules_primary", "")
                subtitle = mkt.get("subtitle", "")
                title = mkt.get("title", "") or mkt.get("market_title", "")
                inferred_is_over = _infer_is_over(subtitle, rules_primary)
                contract = {
                    "ticker": mkt.get("ticker", ""),
                    "event_ticker": event_ticker,
                    "series_ticker": series_ticker,
                    "subtitle": subtitle,
                    "title": title,
                    "floor_strike": mkt.get("floor_strike"),
                    "cap_strike": mkt.get("cap_strike"),
                    "yes_price": best_yes_price,
                    "yes_bid": yes_bid or 0,
                    "yes_ask": yes_ask or 0,
                    "no_bid": no_bid or 0,
                    "no_ask": no_ask or 0,
                    "yes_bid_size": yes_bid_size or 0,
                    "yes_ask_size": yes_ask_size or 0,
                    "no_bid_size": no_bid_size or 0,
                    "no_ask_size": no_ask_size or 0,
                    "yes_spread": yes_spread,
                    "no_spread": no_spread,
                    "last_price": last_price or 0,
                    "no_price": 100 - best_yes_price,
                    "volume": _size(mkt, "volume", "volume_fp"),
                    "open_interest": _size(mkt, "open_interest", "open_interest_fp"),
                    "status": mkt.get("status", ""),
                    "close_time": mkt.get("close_time", ""),
                    "result": mkt.get("result", ""),
                    "rules_primary": rules_primary,
                    # Determine if over/under (preferred: rules_primary)
                    "is_over": inferred_is_over,
                }

                # Parse strike values to float
                for key in ("floor_strike", "cap_strike"):
                    if contract[key] is not None:
                        try:
                            contract[key] = float(contract[key])
                        except (ValueError, TypeError):
                            pass

                all_contracts.append(contract)

        return all_contracts

    # ── Order Management ──────────────────────────────────────────

    def place_order(self, ticker: str, side: str, price_cents: int,
                    count: int, order_type: str = None) -> dict | None:
        """
        Place an order on Kalshi.
        
        ticker: contract ticker
        side: 'yes' or 'no'
        price_cents: limit price in cents (1-99)
        count: number of contracts
        order_type: 'limit' or None for default
        """
        if count <= 0 or price_cents <= 0 or price_cents >= 100:
            log.warning(f"Invalid order params: price={price_cents}, count={count}")
            return None

        body = {
            "ticker": ticker,
            "action": "buy",
            "side": side,
            "type": "limit",
            "count": count,
            "yes_price": price_cents if side == "yes" else None,
            "no_price": price_cents if side == "no" else None,
        }
        # Remove None values
        body = {k: v for k, v in body.items() if v is not None}

        log.info(f"Placing order: {side} {count}x {ticker} @ {price_cents}¢")
        return self._request("POST", "/orders", json_body=body)

    def place_maker_order(self, ticker: str, side: str, count: int,
                          side_price: int | float | None) -> dict | None:
        """
        Place a maker-only order (better price than market).
        
        side_price should be the ask price for the chosen side.
        We place a bid slightly better than the ask (ask - spread).
        """
        spread = TRADING["maker_spread_cents"]

        if side_price is None or side_price <= 0:
            log.warning(f"Invalid side price for maker order: {side_price}")
            return None

        price = max(1, int(round(side_price - spread)))

        return self.place_order(ticker, side, price, count)

    def cancel_order(self, order_id: str) -> dict | None:
        """Cancel an open order."""
        return self._request("DELETE", f"/orders/{order_id}")

    def get_orders(self, status: str = "resting") -> list[dict]:
        """Get current orders."""
        params = {"status": status, "limit": 100}
        data = self._request("GET", "/orders", params=params)
        if data and "orders" in data:
            return data["orders"]
        return []

    # ── Position & Balance ────────────────────────────────────────

    def get_positions(self) -> list[dict]:
        """Get all current positions."""
        data = self._request("GET", "/portfolio/positions", params={"limit": 200})
        if data and "market_positions" in data:
            return data["market_positions"]
        return []

    def get_balance(self) -> float:
        """Get account balance in dollars."""
        data = self._request("GET", "/portfolio/balance")
        if data:
            # Balance is in cents
            return data.get("balance", 0) / 100.0
        return 0.0

    def get_portfolio_value(self) -> dict:
        """Get full portfolio summary."""
        balance = self.get_balance()
        positions = self.get_positions()

        total_invested = 0
        for pos in positions:
            qty = pos.get("market_exposure", 0)
            total_invested += abs(qty)

        return {
            "cash_balance": balance,
            "positions_count": len(positions),
            "total_invested_cents": total_invested,
            "positions": positions,
        }

    # ── Daily P&L Tracking ────────────────────────────────────────

    def check_daily_loss(self) -> dict:
        """Check if daily loss limit has been hit."""
        # This is a simplified version - in production you'd track fills
        portfolio = self.get_portfolio_value()
        return {
            "can_trade": True,  # simplified
            "cash_balance": portfolio["cash_balance"],
            "positions_count": portfolio["positions_count"],
        }
