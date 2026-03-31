"""
Whale copy-trading strategy.
Receives a pre-generated whale signal and wraps it as a TradeRequest.
"""
import logging
from typing import Optional
import sqlite3

from strategies.base import Strategy
from services.execution_service import TradeRequest

log = logging.getLogger(__name__)


class WhaleStrategy(Strategy):
    """
    Strategy: copy trades from profitable whale wallets.
    No Haiku confirmation -- whales trade on their own merit.
    Always uses taker orders.
    """

    @property
    def name(self) -> str:
        return "whale-copy"

    def evaluate(
        self,
        con: sqlite3.Connection,
        market: dict,
        shadow: bool = False,
    ) -> Optional[TradeRequest]:
        """Not used directly -- whale signals come pre-built from CopyTradingService."""
        raise NotImplementedError("Use from_signal() instead")

    def from_signal(self, signal: dict) -> TradeRequest:
        """Convert a whale signal dict (from CopyTradingService) into a TradeRequest."""
        m = signal["market"]
        return TradeRequest(
            market=m,
            direction=signal["direction"],
            price=signal["price"],
            order_type="taker",
            tags=["whale-copy"],
            notes=signal["notes"],
            whale_size=signal.get("whale_size"),
            strategy="whale-copy",
        )
