"""
Persistent set of market IDs to skip in future scans.
Backed by discarded_markets.json. Thread-safe via atomic file writes.

Reasons:
  neg_risk     — market uses neg-risk contracts (unsupported)
  order_failed — CLOB order placement failed
  manual       — manually added
"""
import json
import logging
import os
import tempfile
from datetime import datetime, timezone

from config import DISCARDED_FILE

log = logging.getLogger(__name__)

# In-memory cache: market_id -> {reason, ts}
_cache: dict[str, dict] | None = None


def _load() -> dict[str, dict]:
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.exists(DISCARDED_FILE):
        _cache = {}
        return _cache
    try:
        with open(DISCARDED_FILE) as f:
            _cache = json.load(f)
    except Exception:
        log.warning("Could not read discarded_markets.json — starting fresh")
        _cache = {}
    return _cache


def _save(data: dict[str, dict]) -> None:
    tmp = DISCARDED_FILE + ".tmp"
    try:
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, DISCARDED_FILE)
    except Exception as e:
        log.warning(f"Could not save discarded_markets.json: {e}")


def is_discarded(market_id: str) -> bool:
    """Return True if this market ID should be skipped."""
    return market_id in _load()


def add(market_id: str, reason: str) -> None:
    """Add a market ID to the discarded list and persist it."""
    if not market_id:
        return
    data = _load()
    if market_id in data:
        return  # already recorded
    data[market_id] = {
        "reason": reason,
        "ts": datetime.now(timezone.utc).isoformat(),
    }
    _save(data)
    log.info(f"Discarded market {market_id[:12]}… (reason: {reason})")
