"""
Market discovery and ranking via the Gamma API.
"""
import json
import logging
import random
from datetime import datetime, timezone

from config import CFG, GAMMA_URL
from clients.http import get

log = logging.getLogger(__name__)


class MarketService:
    """Fetches, filters, scores, and ranks markets."""

    def fetch_markets(self, randomize: bool = False) -> list[dict]:
        """Fetch a large pool of active markets, rank by interest score, return top N.

        Scoring (higher = more worth analysing):
          - 70% closeness to 50/50: markets near fair value are most likely to be mispriced
          - 30% normalised volume:  liquid markets are easier to act on

        In shadow mode (randomize=True) shuffles the scored pool so different markets
        get coverage across scans instead of always seeing the same top-N.
        """
        data = get(f"{GAMMA_URL}/markets", params={
            "active":    "true",
            "closed":    "false",
            "limit":     CFG["MARKET_POOL_SIZE"],
            "order":     "volume24hr",
            "ascending": "false",
        })
        if not data:
            return []

        markets = data if isinstance(data, list) else data.get("markets", [])
        pool = []

        for m in markets:
            try:
                liquidity = float(m.get("liquidity") or 0)
                if liquidity < CFG["MIN_LIQUIDITY"]:
                    continue

                prices_raw = m.get("outcomePrices") or m.get("bestBid")
                if prices_raw is None:
                    continue

                if isinstance(prices_raw, str):
                    prices = json.loads(prices_raw)
                    yes_price = float(prices[0]) if prices else None
                elif isinstance(prices_raw, list):
                    yes_price = float(prices_raw[0])
                else:
                    yes_price = float(prices_raw)

                if yes_price is None:
                    continue

                no_price = round(1 - yes_price, 4)
                if max(yes_price, no_price) > CFG["MAX_TRADE_PRICE"]:
                    continue

                if CFG["MAX_RESOLVE_HOURS"] > 0:
                    end_date_str = m.get("endDate") or m.get("endDateIso")
                    if not end_date_str:
                        continue
                    try:
                        end_dt = datetime.fromisoformat(end_date_str.replace("Z", "+00:00"))
                        hours_left = (end_dt - datetime.now(timezone.utc)).total_seconds() / 3600
                        if hours_left > CFG["MAX_RESOLVE_HOURS"] or hours_left < 0:
                            continue
                    except ValueError:
                        continue

                tokens = m.get("tokens") or m.get("clobTokenIds") or []
                if isinstance(tokens, str):
                    try:
                        tokens = json.loads(tokens)
                    except (ValueError, TypeError):
                        tokens = []
                yes_token_id = None
                no_token_id = None
                if tokens:
                    if isinstance(tokens[0], dict):
                        for t in tokens:
                            outcome = str(t.get("outcome", "")).lower()
                            tid = t.get("token_id") or t.get("id", "")
                            if outcome == "yes":
                                yes_token_id = tid
                            elif outcome == "no":
                                no_token_id = tid
                    elif isinstance(tokens[0], str):
                        yes_token_id = tokens[0]
                        no_token_id = tokens[1] if len(tokens) > 1 else None
                token_id = yes_token_id or m.get("conditionId")
                volume = float(m.get("volume24hr") or m.get("volume") or 0)

                pool.append({
                    "id":           m.get("id") or m.get("conditionId", ""),
                    "name":         m.get("question") or m.get("title", "Unknown"),
                    "yes_price":    yes_price,
                    "no_price":     round(1 - yes_price, 4),
                    "liquidity":    liquidity,
                    "volume_24h":   volume,
                    "token_id":     token_id,
                    "yes_token_id": yes_token_id,
                    "no_token_id":  no_token_id,
                    "condition_id": m.get("conditionId") or m.get("condition_id"),
                    "end_date":     m.get("endDate") or m.get("endDateIso"),
                    "slug":         m.get("slug", ""),
                    "tags":         m.get("tags") or [],
                })
            except (TypeError, ValueError, KeyError):
                continue

        if not pool:
            return []

        # Score each market
        max_vol = max(m["volume_24h"] for m in pool) or 1
        for m in pool:
            closeness = 1.0 - abs(m["yes_price"] - 0.5) * 2
            vol_norm = m["volume_24h"] / max_vol
            m["_score"] = closeness * 0.7 + vol_norm * 0.3

        if randomize:
            random.shuffle(pool)
            result = pool[:CFG["MARKETS_PER_SCAN"]]
        else:
            pool.sort(key=lambda m: -m["_score"])
            result = pool[:CFG["MARKETS_PER_SCAN"]]

        selection_method = "randomly sampled" if randomize else "top by interest score"
        log.info(f"Fetched {len(pool)} qualifying markets → {selection_method} {len(result)}")
        return result
