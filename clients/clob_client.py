"""
Wrapper around py-clob-client for all Polymarket CLOB interactions.
Singleton pattern preserved. All CLOB-specific logic lives here.
"""
import logging
import time
from typing import Optional

import requests

from config import CFG, CLOB_URL
from clients.http import get
import utils.discarded as discarded

log = logging.getLogger(__name__)

_clob_client = None
_neg_risk_cache: dict[str, bool] = {}


class PolymarketClient:
    """
    Facade around the py-clob-client singleton.
    The underlying ClobClient is a module-level singleton.
    """

    def get_client(self):
        """Lazy-init singleton for the Polymarket CLOB client."""
        global _clob_client
        if _clob_client is not None:
            return _clob_client
        if not CFG["POLYMARKET_PRIVATE_KEY"]:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY not set — cannot trade in live mode")
        from py_clob_client.client import ClobClient
        funder = CFG["POLYMARKET_FUNDER_ADDRESS"] or None
        _clob_client = ClobClient(
            CLOB_URL,
            key=CFG["POLYMARKET_PRIVATE_KEY"],
            chain_id=137,
            signature_type=1,
            funder=funder,
        )
        _clob_client.set_api_creds(_clob_client.create_or_derive_api_creds())
        log.info("CLOB client initialised (Polygon mainnet)")
        return _clob_client

    def validate_setup(self) -> None:
        """Pre-flight checks for live trading. Raises on failure."""
        if not CFG["POLYMARKET_PRIVATE_KEY"]:
            raise RuntimeError("POLYMARKET_PRIVATE_KEY is required for live trading")
        client = self.get_client()
        # Verify connectivity
        try:
            test = requests.get(f"{CLOB_URL}/markets", params={"limit": 1}, timeout=10)
            test.raise_for_status()
        except Exception as e:
            raise RuntimeError(f"Cannot reach CLOB API: {e}")
        # Ensure USDC allowance
        try:
            from py_clob_client.clob_types import BalanceAllowanceParams, AssetType
            client.update_balance_allowance(
                params=BalanceAllowanceParams(asset_type=AssetType.COLLATERAL)
            )
            log.info("USDC allowance updated")
        except Exception as e:
            log.warning(f"Could not update balance allowance: {e}")
        log.info("Live trading setup validated — CLOB API reachable")

    def resolve_token_ids(self, market: dict) -> tuple[Optional[str], Optional[str]]:
        """Return (yes_token_id, no_token_id), falling back to CLOB API if needed."""
        yes_tid = market.get("yes_token_id")
        no_tid = market.get("no_token_id")
        if yes_tid and no_tid:
            return yes_tid, no_tid
        cid = market.get("condition_id") or market.get("id")
        if not cid:
            return yes_tid, no_tid
        data = get(f"{CLOB_URL}/markets/{cid}")
        if not data or not isinstance(data, dict):
            return yes_tid, no_tid
        for t in (data.get("tokens") or []):
            outcome = str(t.get("outcome", "")).lower()
            tid = t.get("token_id")
            if outcome == "yes" and not yes_tid:
                yes_tid = tid
            elif outcome == "no" and not no_tid:
                no_tid = tid
        return yes_tid, no_tid

    def is_neg_risk(self, market: dict, direction: str = "yes") -> bool:
        """Check if a market token is neg-risk. Cached per token_id. Only checks in live mode."""
        if CFG["STRATEGY_MODE"] != "live":
            return False
        yes_tid, no_tid = self.resolve_token_ids(market)
        token_id = yes_tid if direction == "yes" else no_tid
        if not token_id:
            return False
        if token_id in _neg_risk_cache:
            return _neg_risk_cache[token_id]
        try:
            result = self.get_client().get_neg_risk(token_id)
            is_neg = bool(result)
            _neg_risk_cache[token_id] = is_neg
            if is_neg:
                market_id = market.get("id") or market.get("condition_id")
                if market_id:
                    discarded.add(market_id, "neg_risk")
            return is_neg
        except Exception:
            _neg_risk_cache[token_id] = False
            return False

    def fetch_market_by_condition_id(self, condition_id: str) -> Optional[dict]:
        """Fetch a single market by conditionId using the CLOB API."""
        data = get(f"{CLOB_URL}/markets/{condition_id}")
        if not data or not isinstance(data, dict):
            return None
        try:
            if data.get("closed") or not data.get("active", True):
                return None
            tokens = data.get("tokens") or []
            yes_token = next((t for t in tokens if t.get("outcome", "").lower() == "yes"), None)
            no_token = next((t for t in tokens if t.get("outcome", "").lower() == "no"), None)
            if not yes_token:
                return None
            yes_price = float(yes_token.get("price") or 0)
            if yes_price <= 0:
                return None
            yes_token_id = yes_token.get("token_id")
            no_token_id = no_token.get("token_id") if no_token else None
            return {
                "id":           condition_id,
                "name":         data.get("question", "Unknown"),
                "yes_price":    yes_price,
                "no_price":     round(1 - yes_price, 4),
                "liquidity":    0.0,
                "volume_24h":   0.0,
                "token_id":     yes_token_id,
                "yes_token_id": yes_token_id,
                "no_token_id":  no_token_id,
                "condition_id": condition_id,
                "end_date":     data.get("end_date_iso"),
                "slug":         data.get("market_slug", ""),
                "tags":         data.get("tags") or [],
            }
        except (TypeError, ValueError, KeyError):
            return None

    def warm_token_cache(self, token_id: str) -> None:
        """Pre-fetch tick_size and fee_rate into py_clob_client's internal cache.

        The library caches these (tick_size: 300s TTL, fee_rate: permanent) but
        the first call per token triggers HTTP requests. Calling this early moves
        that latency out of the critical order-placement path.
        """
        client = self.get_client()
        try:
            client.get_tick_size(token_id)
            client.get_fee_rate_bps(token_id)
        except Exception as e:
            log.debug(f"Cache warm failed for {token_id[:16]}…: {e}")

    def place_market_order(self, token_id: str, amount: float, direction: str, price: float = 0.0) -> dict:
        """Create and post a FAK (Fill-and-Kill / IOC) market order.

        Returns dict with order_id, status, and response.

        price should always be passed — it is the limit price for the signed
        order (typically analysis_price * (1 + slippage) for buys).  It controls
        how many shares are requested for `amount` USDC.  Without it the library
        calls calculate_market_price() internally which can walk the book in the
        wrong direction.

        FAK semantics: fills whatever portion the book can match immediately,
        cancels the rest.  This avoids the 400 "fully filled" errors that FOK
        produces on thin order books.
        """
        from py_clob_client.clob_types import MarketOrderArgs, OrderType
        from py_clob_client.order_builder.constants import BUY, SELL

        client = self.get_client()
        side = BUY if direction == "yes" else SELL

        mo = MarketOrderArgs(
            token_id=token_id,
            amount=amount,
            side=side,
            price=price if price > 0 else None,
        )
        signed_order = client.create_market_order(mo)
        resp = client.post_order(signed_order, OrderType.FAK)
        log.debug(f"post_order response: {resp}")

        order_id = None
        status = None
        if isinstance(resp, dict):
            order_id = resp.get("orderID") or resp.get("order_id") or resp.get("id")
            status = resp.get("status")
        elif hasattr(resp, "orderID"):
            order_id = resp.orderID

        return {"order_id": order_id, "status": status, "response": resp}

    def get_order_fill(self, order_id: str) -> tuple[bool, float]:
        """Check if an order was filled. Returns (was_filled, size_matched_shares).

        Always retries up to 5x with increasing waits before concluding
        the order was not filled.
        """
        attempts = 5
        wait = 1.0  # seconds between retries
        for attempt in range(attempts):
            if attempt > 0:
                log.debug(f"Retrying order fill check ({attempt}/{attempts - 1}) after {wait}s — order_id={order_id}")
                time.sleep(wait)
                wait = min(wait * 2, 4.0)
            try:
                order = self.get_client().get_order(order_id)
                if not isinstance(order, dict):
                    log.debug(f"Order {order_id} not yet in CLOB (null response), attempt {attempt + 1}")
                    continue
                size_matched = float(
                    order.get("size_matched") or
                    order.get("sizeMatched") or
                    order.get("matched_amount") or
                    order.get("matchedAmount") or 0
                )
                if size_matched > 0:
                    return True, size_matched
                status     = order.get("status") or order.get("order_status") or "unknown"
                size_total = order.get("original_size") or order.get("size") or "?"
                maker_amt  = order.get("maker_amount") or order.get("makerAmount") or "?"
                log.debug(
                    f"Order not filled — status={status}  original_size={size_total}"
                    f"  maker_amount={maker_amt}  raw={order}"
                )
                # If the order exists but has a terminal non-filled status, stop retrying
                if status in ("cancelled", "CANCELLED", "unmatched", "UNMATCHED"):
                    break
            except Exception as e:
                log.debug(f"Could not check fill status for order {order_id}: {e}")
        return False, 0.0

    def get_actual_fill_price(self, order_id: str, amount_usdc: float, fallback: float) -> float:
        """Query CLOB API for actual fill price after a FOK order executes."""
        was_filled, size_matched = self.get_order_fill(order_id)
        if was_filled:
            return round(amount_usdc / size_matched, 6)
        return fallback

    def repair_open_trade_prices(self, con) -> None:
        """Backfill actual fill prices for open live trades that have an order_id stored."""
        rows = con.execute(
            "SELECT id, order_id, amount FROM trades WHERE resolved=0 AND mode='live' AND order_id IS NOT NULL"
        ).fetchall()
        if not rows:
            return
        try:
            client = self.get_client()
        except Exception as e:
            log.debug(f"repair_open_trade_prices: could not get CLOB client: {e}")
            return
        for row_id, order_id, amount in rows:
            fill_price = self.get_actual_fill_price(order_id, amount, fallback=None)
            if fill_price:
                con.execute("UPDATE trades SET price=? WHERE id=?", (fill_price, row_id))
                log.info(f"Repaired trade #{row_id}: fill price set to {fill_price:.4f}")
        con.commit()
