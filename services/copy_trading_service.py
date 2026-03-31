"""
Whale copy-trading: wallet discovery, position scanning, signal generation.
"""
import logging
import re
import time
from datetime import datetime, timezone
from typing import Optional

from config import CFG, DATA_URL
from clients.http import get
from clients.clob_client import PolymarketClient

log = logging.getLogger(__name__)

_WALLET_RE = re.compile(r'^0x[0-9a-fA-F]{40}$')
_leaderboard_cache: tuple[float, list[str]] = (0.0, [])
LEADERBOARD_CACHE_TTL = 3600


class CopyTradingService:
    """Discovers profitable wallets and generates copy-trade signals."""

    def __init__(self, polymarket_client: PolymarketClient):
        self._poly = polymarket_client

    def fetch_whale_positions(self, wallet: str, min_size: float) -> list[dict]:
        """Get current open positions for a wallet above min_size USDC."""
        data = get(f"{DATA_URL}/positions", params={
            "user":          wallet,
            "sizeThreshold": 1,
            "limit":         100,
            "sortBy":        "CURRENT",
            "sortDirection": "DESC",
        })
        if not data:
            return []

        positions = data if isinstance(data, list) else data.get("data", [])
        result = []
        for p in positions:
            try:
                if p.get("redeemable") or float(p.get("curPrice") or 0) <= 0:
                    continue
                current_value = float(p.get("currentValue") or 0)
                if current_value < min_size:
                    continue
                direction = "yes" if p.get("outcomeIndex") == 0 else "no"
                result.append({
                    "market_id":   p.get("conditionId") or "",
                    "market_name": p.get("title") or "",
                    "direction":   direction,
                    "price":       float(p.get("curPrice") or 0.5),
                    "size":        current_value,
                    "wallet":      wallet,
                })
            except (TypeError, ValueError):
                continue
        return result

    def fetch_profitable_wallet_list(self, top_n: int = 30) -> list[str]:
        """Return profitable wallet addresses ordered by weekly PNL, cached for 1 hour."""
        global _leaderboard_cache
        now = time.time()
        cached_at, cached_list = _leaderboard_cache
        if now - cached_at < LEADERBOARD_CACHE_TTL and cached_list:
            return cached_list

        data = get(f"{DATA_URL}/v1/leaderboard", params={
            "limit":      top_n,
            "category":   "OVERALL",
            "timePeriod": "WEEK",
            "orderBy":    "PNL",
        })
        if not data:
            return cached_list

        entries = data if isinstance(data, list) else data.get("data", [])
        wallets = []
        seen = set()
        for e in entries:
            addr = e.get("proxyWallet") or e.get("address") or e.get("user", "")
            if addr and _WALLET_RE.match(addr) and addr not in seen:
                wallets.append(addr)
                seen.add(addr)

        _leaderboard_cache = (now, wallets)
        log.info(f"Leaderboard refreshed: {len(wallets)} profitable wallets cached (ordered by PNL)")
        return wallets

    def get_whale_signals(self, markets: list[dict]) -> list[dict]:
        """
        Collect all valid whale signals from open positions of profitable wallets.
        Returns a list of signal dicts (may be empty). Deduplicates by market_id.
        Path A: manually configured WHALE_WALLETS (override).
        Path B: top weekly profitable wallets from leaderboard (auto-discover).
        """
        market_ids = {m["id"]: m for m in markets}
        signals: list[dict] = []
        seen_markets: set[str] = set()

        def _make_signal(m, pos, wallet) -> Optional[dict]:
            price = m["yes_price"] if pos["direction"] == "yes" else m["no_price"]
            if price > CFG["MAX_TRADE_PRICE"]:
                log.debug(f"🐋 Skipping whale position (price {price:.3f} > MAX_TRADE_PRICE): {m['name'][:50]}")
                return None
            if price < CFG["MIN_TRADE_PRICE"]:
                log.debug(f"🐋 Skipping whale position (price {price:.3f} < MIN_TRADE_PRICE): {m['name'][:50]}")
                return None
            if CFG["MAX_WHALE_RESOLVE_HOURS"] > 0:
                end_date_str = m.get("end_date") or m.get("endDate") or m.get("endDateIso")
                if end_date_str:
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left > CFG["MAX_WHALE_RESOLVE_HOURS"] or hours_left < 0:
                            log.debug(f"🐋 Skipping whale position ({hours_left:.0f}h left > MAX_WHALE_RESOLVE_HOURS): {m['name'][:50]}")
                            return None
                    except ValueError:
                        pass
            return {
                "market":     m,
                "direction":  pos["direction"],
                "price":      price,
                "signal":     "whale-copy",
                "whale":      wallet,
                "whale_size": pos["size"],
                "notes":      f"Whale {wallet[:10]}… holds ${pos['size']:.0f}",
            }

        # Path A: configured wallets (manual override)
        if CFG["WHALE_WALLETS"]:
            for wallet in CFG["WHALE_WALLETS"][:15]:
                for pos in self.fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"]):
                    if pos["market_id"] in market_ids and pos["market_id"] not in seen_markets:
                        m = market_ids[pos["market_id"]]
                        sig = _make_signal(m, pos, wallet)
                        if sig:
                            log.info(
                                f"🐋 Whale position (manual): {wallet[:10]}… "
                                f"{pos['direction'].upper()} on '{m['name'][:50]}' holding ${pos['size']:.0f}"
                            )
                            signals.append(sig)
                            seen_markets.add(pos["market_id"])
                time.sleep(0.15)
            return signals

        # Path B: positions-based whale discovery
        profitable = self.fetch_profitable_wallet_list(top_n=CFG["WHALE_LEADERBOARD_SIZE"])
        if not profitable:
            log.info("Leaderboard unavailable — skipping whale strategy this scan")
            return signals

        log.info(f"Scanning positions for {len(profitable)} top weekly wallets (min size=${CFG['WHALE_MIN_SIZE']:.0f})...")
        wallets = list(profitable)[:CFG["WHALE_WALLETS_PER_SCAN"]]
        total_positions = 0
        for wallet in wallets:
            positions = self.fetch_whale_positions(wallet, CFG["WHALE_MIN_SIZE"])
            total_positions += len(positions)
            for pos in positions:
                if pos["market_id"] in seen_markets:
                    continue
                m = market_ids.get(pos["market_id"])
                if m is None:
                    m = self._poly.fetch_market_by_condition_id(pos["market_id"])
                    time.sleep(0.3)
                    if m is None:
                        continue
                sig = _make_signal(m, pos, wallet)
                if sig:
                    log.info(
                        f"🐋 Whale position: {wallet[:10]}… "
                        f"{pos['direction'].upper()} on '{m['name'][:50]}' @ {sig['price']:.3f} (holding ${pos['size']:.0f})"
                    )
                    signals.append(sig)
                    seen_markets.add(pos["market_id"])
            time.sleep(0.15)
        log.info(f"Whale scan: {total_positions} open positions checked across {len(wallets)} wallets — {len(signals)} signal(s) found")
        return signals
