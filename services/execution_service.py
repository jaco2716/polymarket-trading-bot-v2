"""
Trade execution engine. Trade-type agnostic -- handles paper, live, dry-run, and shadow modes
through a unified interface. The caller never needs to know which mode is active.
"""
import json
import logging
import time
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from config import CFG
from clients.clob_client import PolymarketClient
from services.budget_service import BudgetService
from utils.fees import calc_fee, price_scaled_params
import utils.discarded as discarded

log = logging.getLogger(__name__)


class TradeRequest:
    """
    Immutable value object representing a trade to execute.
    This is what strategies produce -- the ExecutionService consumes it.
    Trade-type agnostic: works for Haiku, whale, or any future strategy.
    """
    def __init__(
        self,
        market: dict,
        direction: str,
        price: float,
        order_type: str,
        tags: list[str],
        notes: str = "",
        confidence: Optional[float] = None,
        whale_size: Optional[float] = None,
        strategy: str = "",
    ):
        self.market = market
        self.direction = direction
        self.price = price
        self.order_type = order_type
        self.tags = tags
        self.notes = notes
        self.confidence = confidence
        self.whale_size = whale_size
        self.strategy = strategy


class ExecutionService:
    """
    Unified trade execution. Dispatches to paper, live, or shadow based on mode.

    SPEED-FIRST: The execute() method is designed to be called IMMEDIATELY
    after analysis confirms a signal. No batching, no queuing.

    TRADE-TYPE AGNOSTIC: Accepts a TradeRequest -- doesn't care if the signal
    came from Haiku, whale copy, or a future strategy.
    """

    def __init__(
        self,
        polymarket_client: PolymarketClient,
        budget_service: BudgetService,
    ):
        self._poly = polymarket_client
        self._budget = budget_service

    def execute(self, con: sqlite3.Connection, trade: TradeRequest) -> Optional[float]:
        """
        Execute a trade in the current mode. Returns new budget or None if skipped.
        Single entry point for all trade execution.
        """
        mode = CFG["STRATEGY_MODE"]
        if mode == "shadow":
            self._execute_shadow(con, trade)
            return None
        elif mode == "live":
            if CFG["LIVE_DRY_RUN"]:
                return self._execute_live_dry(con, trade)
            return self._execute_live(con, trade)
        else:
            return self._execute_paper(con, trade)

    def _compute_trade_size(self, budget: float, price: float, is_shadow: bool = False) -> Optional[float]:
        """Compute position size from budget, applying price scaling. Returns None if insufficient."""
        base_conf = CFG["SHADOW_HAIKU_MIN_CONF"] if is_shadow else CFG["HAIKU_MIN_CONF"]
        base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
        _, amount = price_scaled_params(price, base_conf, base_amount)
        amount = max(amount, CFG["MIN_TRADE_USDC"])
        if not is_shadow and CFG["STRATEGY_MODE"] == "live":
            amount = min(amount, CFG["MAX_LIVE_TRADE_USDC"])
        if amount > budget * 0.95:
            return None
        return amount

    def _can_open_trade(self, con: sqlite3.Connection) -> bool:
        """Check if we're below the max open trade limit for current mode."""
        mode = CFG["STRATEGY_MODE"]
        if mode == "live":
            return self._budget.open_trade_count(con, mode_filter="live") < CFG["LIVE_MAX_OPEN_TRADES"]
        return self._budget.open_trade_count(con) < CFG["MAX_OPEN_TRADES"]

    def _execute_paper(self, con: sqlite3.Connection, trade: TradeRequest) -> Optional[float]:
        """Paper trade: deduct from budget, write to trades table."""
        if not self._can_open_trade(con):
            log.info("Max open trades reached — skipping")
            return None
        budget = self._budget.load("paper")
        base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
        amount = self._compute_trade_size(budget, trade.price)
        if amount is None:
            log.warning(f"Insufficient budget (${budget:.2f}) for trade")
            return None
        fee = calc_fee(amount, trade.price, trade.order_type)
        new_budget = budget - amount - fee

        con.execute("""
            INSERT INTO trades
                (ts, market_id, market_name, token_id, direction, price, amount,
                 fee, order_type, tags, notes, end_date, liquidity, volume_24h,
                 market_slug, market_tags, whale_size)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade.market["id"], trade.market["name"],
            trade.market.get("token_id"), trade.direction, trade.price,
            amount, fee, trade.order_type, json.dumps(trade.tags),
            trade.notes, trade.market.get("end_date"),
            trade.market.get("liquidity"), trade.market.get("volume_24h"),
            trade.market.get("slug"), json.dumps(trade.market.get("tags") or []),
            trade.whale_size,
        ))
        con.commit()
        self._budget.save("paper", new_budget)

        gross_if_win = amount * (1 - trade.price) / trade.price
        size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
        log.info(
            f"📝 Paper trade logged:\n"
            f"   Market:  {trade.market['name'][:60]}\n"
            f"   {trade.direction.upper()} @ {trade.price:.3f}  |  ${amount:.2f} staked ({size_pct}% of base)\n"
            f"   Fee est: {fee:+.4f} USDC  |  Win payoff: +${gross_if_win:.2f}\n"
            f"   Tags:    {', '.join(trade.tags)}\n"
            f"   Budget:  ${budget:.2f} → ${new_budget:.2f}"
        )
        return new_budget

    def _execute_live(self, con: sqlite3.Connection, trade: TradeRequest) -> Optional[float]:
        """Real CLOB order. Resolve token, check neg-risk, place FAK (IOC) order."""
        if not self._can_open_trade(con):
            log.info(f"Max live open trades ({CFG['LIVE_MAX_OPEN_TRADES']}) reached — skipping")
            return None
        budget = self._budget.load("live")
        base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
        amount = self._compute_trade_size(budget, trade.price)
        if amount is None:
            log.warning(f"Insufficient live budget (${budget:.2f}) for trade")
            return None

        yes_tid, no_tid = self._poly.resolve_token_ids(trade.market)
        token_id = yes_tid if trade.direction == "yes" else no_tid
        if not token_id:
            log.error(f"Cannot resolve {trade.direction} token_id for market {trade.market['id']} — skipping")
            return None

        if self._poly.is_neg_risk(trade.market, trade.direction):
            log.info(f"Skipping neg-risk market: {trade.market['name'][:50]}")
            return None

        # Pre-warm tick_size + fee_rate cache before the critical order path
        self._poly.warm_token_cache(token_id)

        # Apply slippage to limit price for buy orders
        limit_price = trade.price * (1 + CFG["SLIPPAGE_PCT"]) if trade.direction == "yes" else trade.price

        try:
            result = self._poly.place_market_order(token_id, amount, trade.direction, limit_price)
            order_id = result["order_id"]
            if not order_id:
                log.warning(f"No order_id in response for {trade.market['name'][:50]} — skipping")
                return None

            delayed = result.get("status") == "delayed"
            if delayed:
                log.debug(f"Order {order_id} is delayed — will retry fill check")
            was_filled, size_matched = self._poly.get_order_fill(order_id, delayed=delayed)
            if not was_filled:
                log.warning(
                    f"Order not filled (cancelled by exchange): {order_id}\n"
                    f"   Market: {trade.market['name'][:60]}"
                )
                return None

            # Handle partial fills: compute actual USDC spent from fill ratio
            expected_shares = amount / limit_price
            fill_ratio = min(1.0, size_matched / expected_shares) if expected_shares > 0 else 0
            actual_amount = round(fill_ratio * amount, 2)
            fill_price = round(actual_amount / size_matched, 6) if size_matched > 0 else limit_price

            fee = calc_fee(actual_amount, fill_price, trade.order_type)
            new_budget = budget - actual_amount - fee

            size_pct = round(actual_amount / base_amount * 100) if base_amount > 0 else 100
            partial_note = f"  (partial: {fill_ratio:.0%})" if fill_ratio < 0.99 else ""
            log.info(
                f"💰 LIVE trade placed!{partial_note}\n"
                f"   Market:   {trade.market['name'][:60]}\n"
                f"   {trade.direction.upper()} @ analysis {trade.price:.3f} → fill {fill_price:.3f}  |  ${actual_amount:.2f} staked ({size_pct}% of base)\n"
                f"   Order ID: {order_id}\n"
                f"   Tags:     {', '.join(trade.tags)}\n"
                f"   Budget:   ${budget:.2f} → ${new_budget:.2f}"
            )
        except Exception as e:
            err = str(e)
            if "fully filled" in err or "FOK" in err or "FAK" in err:
                log.warning(f"Order killed (thin liquidity) for {trade.market['name'][:50]} — skipping this scan")
            else:
                log.error(f"LIVE order FAILED for {trade.market['name'][:50]}: {e}")
                discarded.add(trade.market.get("id") or trade.market.get("condition_id", ""), "order_failed")
            return None

        con.execute("""
            INSERT INTO trades
                (ts, market_id, market_name, token_id, direction, price, amount,
                 fee, order_type, tags, notes, mode, order_id,
                 end_date, liquidity, volume_24h, market_slug, market_tags, whale_size)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade.market["id"], trade.market["name"], token_id,
            trade.direction, fill_price, actual_amount, fee, trade.order_type,
            json.dumps(trade.tags), trade.notes, "live", order_id,
            trade.market.get("end_date"), trade.market.get("liquidity"),
            trade.market.get("volume_24h"), trade.market.get("slug"),
            json.dumps(trade.market.get("tags") or []), trade.whale_size,
        ))
        con.commit()
        self._budget.save("live", new_budget)
        return new_budget

    def _execute_live_dry(self, con: sqlite3.Connection, trade: TradeRequest) -> Optional[float]:
        """Dry-run: log everything but skip CLOB API call."""
        if not self._can_open_trade(con):
            log.info(f"Max live open trades ({CFG['LIVE_MAX_OPEN_TRADES']}) reached — skipping")
            return None
        budget = self._budget.load("live")
        base_amount = round(budget * CFG["TRADE_SIZE_PCT"], 2)
        amount = self._compute_trade_size(budget, trade.price)
        if amount is None:
            log.warning(f"Insufficient live budget (${budget:.2f}) for trade")
            return None

        yes_tid, no_tid = self._poly.resolve_token_ids(trade.market)
        token_id = yes_tid if trade.direction == "yes" else no_tid
        if not token_id:
            log.error(f"Cannot resolve {trade.direction} token_id for market {trade.market['id']} — skipping")
            return None

        if self._poly.is_neg_risk(trade.market, trade.direction):
            log.info(f"Skipping neg-risk market: {trade.market['name'][:50]}")
            return None

        fee = calc_fee(amount, trade.price, trade.order_type)
        new_budget = budget - amount - fee

        con.execute("""
            INSERT INTO trades
                (ts, market_id, market_name, token_id, direction, price, amount,
                 fee, order_type, tags, notes, mode,
                 end_date, liquidity, volume_24h, market_slug, market_tags, whale_size)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade.market["id"], trade.market["name"], token_id,
            trade.direction, trade.price, amount, fee, trade.order_type,
            json.dumps(trade.tags), trade.notes, "live-dry",
            trade.market.get("end_date"), trade.market.get("liquidity"),
            trade.market.get("volume_24h"), trade.market.get("slug"),
            json.dumps(trade.market.get("tags") or []), trade.whale_size,
        ))
        con.commit()
        self._budget.save("live", new_budget)

        size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
        log.info(
            f"🔸 DRY RUN — would place live trade:\n"
            f"   Market:   {trade.market['name'][:60]}\n"
            f"   {trade.direction.upper()} @ {trade.price:.3f}  |  ${amount:.2f} ({size_pct}% of base)  |  token={token_id[:16]}…\n"
            f"   Budget:   ${budget:.2f} → ${new_budget:.2f}"
        )
        return new_budget

    def _execute_shadow(self, con: sqlite3.Connection, trade: TradeRequest) -> None:
        """Shadow trade: record in shadow_trades table, deduct from shadow budget."""
        shadow_budget = self._budget.load("shadow")
        base_amount = round(shadow_budget * CFG["TRADE_SIZE_PCT"], 2)
        amount = self._compute_trade_size(shadow_budget, trade.price, is_shadow=True)
        if amount is None:
            log.info(f"Shadow budget too low (${shadow_budget:.2f}) — skipping shadow trade")
            return
        fee = calc_fee(amount, trade.price)
        new_budget = shadow_budget - amount - fee

        con.execute("""
            INSERT INTO shadow_trades
                (ts, strategy, market_id, market_name, direction, price, amount,
                 fee, confidence, notes, end_date, liquidity, volume_24h,
                 market_slug, market_tags)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """, (
            datetime.now(timezone.utc).isoformat(),
            trade.strategy, trade.market["id"], trade.market["name"],
            trade.direction, trade.price, amount, fee,
            trade.confidence, trade.notes,
            trade.market.get("end_date"), trade.market.get("liquidity"),
            trade.market.get("volume_24h"), trade.market.get("slug"),
            json.dumps(trade.market.get("tags") or []),
        ))
        con.commit()
        self._budget.save("shadow", new_budget)

        size_pct = round(amount / base_amount * 100) if base_amount > 0 else 100
        conf_str = f" conf={trade.confidence:.2f}" if trade.confidence is not None else ""
        log.info(
            f"👻 Shadow [{trade.strategy}]{conf_str}: {trade.direction.upper()} on '{trade.market['name'][:55]}' @ {trade.price:.3f}"
            f"  |  ${amount:.2f} staked ({size_pct}%)  |  Shadow budget: ${shadow_budget:.2f} → ${new_budget:.2f}"
            + (f" | {trade.notes[:80]}" if trade.notes else "")
        )
