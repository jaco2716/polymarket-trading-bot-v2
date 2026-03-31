"""
Abstract base for trading strategies.
"""
from abc import ABC, abstractmethod
from typing import Optional
import sqlite3

from services.execution_service import TradeRequest


class Strategy(ABC):
    """
    Interface that all trading strategies implement.

    A strategy's job:
      1. Receive market data
      2. Decide if there's a signal
      3. Return a TradeRequest (or None)

    Strategies do NOT execute trades -- they produce TradeRequests
    that the ExecutionService handles.
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Strategy identifier, e.g. 'haiku-analyse', 'whale-copy'."""
        ...

    @abstractmethod
    def evaluate(
        self,
        con: sqlite3.Connection,
        market: dict,
        shadow: bool = False,
    ) -> Optional[TradeRequest]:
        """
        Evaluate a single market for a trading signal.
        Returns a TradeRequest if a signal is found, None otherwise.
        """
        ...
