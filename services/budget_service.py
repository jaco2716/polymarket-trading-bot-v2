"""
Budget persistence (JSON files) and open trade tracking (DB queries).
"""
import json
import logging
import os
import sqlite3
from typing import Optional

from config import CFG, BUDGET_FILE, SHADOW_BUDGET_FILE, LIVE_BUDGET_FILE
from utils.filters import extract_event_key

log = logging.getLogger(__name__)


class BudgetService:
    """Manages budget state for all trading modes."""

    _FILE_MAP = {
        "paper":  BUDGET_FILE,
        "shadow": SHADOW_BUDGET_FILE,
        "live":   LIVE_BUDGET_FILE,
    }

    _STARTING_MAP_KEYS = {
        "paper":  "STARTING_BUDGET",
        "shadow": "SHADOW_STARTING_BUDGET",
        "live":   "STARTING_BUDGET",
    }

    def load(self, mode: str) -> float:
        """Load budget for the given mode ('paper', 'shadow', 'live')."""
        path = self._FILE_MAP[mode]
        if os.path.exists(path):
            with open(path) as f:
                return json.load(f)["budget"]
        budget = CFG[self._STARTING_MAP_KEYS[mode]]
        self.save(mode, budget)
        return budget

    def save(self, mode: str, budget: float) -> None:
        """Atomic save of budget via tmp file + rename."""
        path = self._FILE_MAP[mode]
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({"budget": round(budget, 4)}, f)
        os.replace(tmp, path)

    def open_trade_count(self, con: sqlite3.Connection, mode_filter: Optional[str] = None) -> int:
        """Count unresolved trades."""
        if mode_filter:
            return con.execute(
                "SELECT COUNT(*) FROM trades WHERE resolved=0 AND mode=?", (mode_filter,)
            ).fetchone()[0]
        return con.execute("SELECT COUNT(*) FROM trades WHERE resolved=0").fetchone()[0]

    def open_market_ids(self, con: sqlite3.Connection) -> set[str]:
        """Market IDs with open real trades."""
        rows = con.execute("SELECT market_id FROM trades WHERE resolved=0").fetchall()
        return {r[0] for r in rows}

    def open_shadow_market_ids(self, con: sqlite3.Connection) -> set[str]:
        """Market IDs with open shadow trades."""
        rows = con.execute("SELECT market_id FROM shadow_trades WHERE resolved=0").fetchall()
        return {r[0] for r in rows}

    def open_event_keys(self, con: sqlite3.Connection) -> set[str]:
        """Normalised event keys for all open real trades.

        Used to prevent entering multiple correlated positions on the same
        underlying match (e.g. Game 1, Game 2, BO3, Handicap all share a key).
        """
        rows = con.execute("SELECT market_name FROM trades WHERE resolved=0").fetchall()
        return {extract_event_key(r[0]) for r in rows}

    def get_budget_mode(self) -> str:
        """Map STRATEGY_MODE to budget mode string."""
        mode = CFG["STRATEGY_MODE"]
        if mode == "shadow":
            return "shadow"
        if mode == "live":
            return "live"
        return "paper"
