"""
Trade resolution: checks if markets have settled, computes P&L, updates budgets.
"""
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional
import sqlite3

from config import CFG, GAMMA_URL, CLOB_URL, LIVE_BUDGET_FILE
from clients.http import get
from services.budget_service import BudgetService

log = logging.getLogger(__name__)


class ResolutionService:
    """Resolves settled trades and reconciles budgets."""

    def __init__(self, budget_service: BudgetService):
        self._budget = budget_service

    def get_market_winner(self, market_id: str) -> Optional[str]:
        """
        Return 'yes', 'no', or None (still open / unclear).
        Numeric IDs -> Gamma API. ConditionId hashes (0x...) -> CLOB API.
        """
        is_hash = str(market_id).startswith("0x")

        if is_hash:
            data = get(f"{CLOB_URL}/markets/{market_id}")
            if not data or not isinstance(data, dict):
                return None
            if not data.get("closed"):
                return None
            tokens = data.get("tokens") or []
            for token in tokens:
                if token.get("winner"):
                    outcome = str(token.get("outcome", "")).lower()
                    if outcome in ("yes", "no"):
                        return outcome
                    return "yes" if tokens.index(token) == 0 else "no"
            return None
        else:
            data = get(f"{GAMMA_URL}/markets/{market_id}")
            if not data:
                return None
            m = data[0] if isinstance(data, list) else data
            if not m.get("closed") and not m.get("resolved"):
                return None
            if m.get("winner"):
                return str(m["winner"]).lower()
            if m.get("resolvedAt") or m.get("resolution"):
                res = str(m.get("resolution") or "").lower()
                return "yes" if res in ("1", "true", "yes") else "no"
            prices = m.get("outcomePrices")
            if prices and len(prices) >= 2:
                try:
                    yes_p, no_p = float(prices[0]), float(prices[1])
                    if yes_p >= 0.99:
                        return "yes"
                    if no_p >= 0.99:
                        return "no"
                except (ValueError, TypeError):
                    pass
            condition_id = m.get("conditionId") or m.get("condition_id")
            if condition_id:
                clob_data = get(f"{CLOB_URL}/markets/{condition_id}")
                if clob_data and isinstance(clob_data, dict) and clob_data.get("closed"):
                    clob_tokens = clob_data.get("tokens") or []
                    for token in clob_tokens:
                        if token.get("winner"):
                            outcome = str(token.get("outcome", "")).lower()
                            if outcome in ("yes", "no"):
                                return outcome
                            return "yes" if clob_tokens.index(token) == 0 else "no"
            return None

    def resolve_settled_trades(self, con: sqlite3.Connection) -> float:
        """
        Check open trades against current market data.
        If a market is resolved, mark the trade and adjust budget.
        Returns total P&L from newly resolved trades.
        """
        rows = con.execute(
            "SELECT id, market_id, direction, price, amount, fee, ts, mode FROM trades WHERE resolved=0"
        ).fetchall()
        if not rows:
            return 0.0

        total_pnl = 0.0
        paper_budget = self._budget.load("paper")
        live_budget = self._budget.load("live") if os.path.exists(LIVE_BUDGET_FILE) else None
        paper_changed = False
        live_changed = False
        now = datetime.now(timezone.utc)

        for row in rows:
            row_id, market_id, direction, price, amount, fee, ts = row[:7]
            trade_mode = row[7] if len(row) > 7 else "paper"
            try:
                age = (now - datetime.fromisoformat(ts)).days
                if age > 14:
                    log.warning(f"Trade #{row_id} has been open for {age} days — may need manual review")
            except (ValueError, TypeError):
                pass

            winner = self.get_market_winner(market_id)
            if not winner:
                continue

            won = (direction == winner)
            gross = amount * (1 - price) / price if won else -amount
            pnl = gross - fee

            con.execute("""
                UPDATE trades
                SET resolved=1, outcome=?, pnl=?, close_ts=?
                WHERE id=?
            """, (winner, round(pnl, 4), datetime.now(timezone.utc).isoformat(), row_id))
            con.commit()

            if trade_mode in ("live", "live-dry"):
                if live_budget is not None:
                    live_budget += amount + fee + pnl
                    live_changed = True
            else:
                paper_budget += amount + fee + pnl
                paper_changed = True
            total_pnl += pnl

            mode_tag = " [LIVE]" if trade_mode == "live" else ""
            status = "✅ WON" if won else "❌ LOST"
            log.info(
                f"{status}{mode_tag} — resolved trade #{row_id}: {direction.upper()} "
                f"on market {market_id[:20]}… | P&L: {pnl:+.2f} USDC"
            )
            time.sleep(0.3)

        if paper_changed:
            self._budget.save("paper", paper_budget)
        if live_changed and live_budget is not None:
            self._budget.save("live", live_budget)
        return total_pnl

    def resolve_shadow_trades(self, con: sqlite3.Connection) -> None:
        """Check open shadow trades against market outcomes, record P&L, update shadow budget."""
        rows = con.execute(
            "SELECT id, market_id, direction, price, amount, fee, ts FROM shadow_trades WHERE resolved=0"
        ).fetchall()
        if not rows:
            return

        shadow_budget = self._budget.load("shadow")
        total_pnl = 0.0
        now = datetime.now(timezone.utc)

        for row_id, market_id, direction, price, amount, fee, ts in rows:
            try:
                age = (now - datetime.fromisoformat(ts)).days
                if age > 14:
                    log.warning(f"Shadow trade #{row_id} has been open for {age} days — may need manual review")
            except (ValueError, TypeError):
                pass

            winner = self.get_market_winner(market_id)
            if not winner:
                continue

            won = (direction == winner)
            pnl = (amount * (1 - price) / price if won else -amount) - (fee or 0)

            con.execute("""
                UPDATE shadow_trades SET resolved=1, outcome=?, pnl=?, close_ts=? WHERE id=?
            """, (winner, round(pnl, 4), datetime.now(timezone.utc).isoformat(), row_id))
            con.commit()

            shadow_budget += amount + (fee or 0) + pnl
            total_pnl += pnl

            log.info(
                f"👻 Shadow resolved #{row_id}: {'WON' if won else 'LOST'} "
                f"P&L: {pnl:+.2f} USDC  |  Shadow budget: ${shadow_budget:.2f}"
            )
            time.sleep(0.2)

        if total_pnl != 0.0:
            self._budget.save("shadow", shadow_budget)
