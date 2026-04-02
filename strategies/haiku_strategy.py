"""
Haiku edge-detection strategy.
Fetches news -> calls Haiku -> gates on confidence -> produces TradeRequest.
"""
import logging
import time
from typing import Optional
import sqlite3

from config import CFG
from clients.anthropic_client import HaikuClient
from strategies.base import Strategy
from services.execution_service import TradeRequest
from utils.fees import price_scaled_params
from utils.news import fetch_news_headlines
from utils.live_scores import fetch_live_scores, detect_sport
from utils.market_prices import fetch_asset_context
from utils.sports_context import fetch_sports_context

log = logging.getLogger(__name__)


class HaikuStrategy(Strategy):
    """
    Strategy: ask Claude Haiku if a market has a tradeable edge.
    FETCH-BEFORE-ANALYZE: headlines are fetched fresh per market.
    """

    def __init__(self, haiku_client: HaikuClient):
        self._haiku = haiku_client

    @property
    def name(self) -> str:
        return "haiku-analyse"

    def evaluate(
        self,
        con: sqlite3.Connection,
        market: dict,
        shadow: bool = False,
    ) -> Optional[TradeRequest]:
        """Evaluate one market. Returns TradeRequest if confidence threshold met."""
        # Fetch live scores — required for any sports/esports market.
        # If the market is a sports event but no live data is found, skip it:
        # betting without knowing the current match state has no edge.
        tags = market.get("tags") or []
        live_scores = fetch_live_scores(market["name"], tags=tags)
        if detect_sport(market["name"], tags=tags) and not live_scores:
            log.debug(f"[haiku] Skipping sports market (no live score found): {market['name'][:60]}")
            return None

        # FETCH-BEFORE-ANALYZE: get fresh headlines and context right before analysis
        headlines    = fetch_news_headlines(market["name"])
        asset_ctx    = fetch_asset_context(market["name"])
        sports_ctx   = fetch_sports_context(market["name"], tags=tags)

        analysis = self._haiku.analyse(market, headlines, live_scores=live_scores,
                                       asset_context=asset_ctx, sports_context=sports_ctx,
                                       shadow=shadow)
        time.sleep(0.5)  # rate limit courtesy

        if not analysis:
            return None

        conf = float(analysis.get("confidence") or 0)
        dir_ = analysis.get("direction", "yes")
        price = market["yes_price"] if dir_ == "yes" else market["no_price"]

        if price < CFG["MIN_TRADE_PRICE"]:
            log.debug(f"[haiku] Skipping {market['name'][:50]}: price {price:.3f} < MIN_TRADE_PRICE {CFG['MIN_TRADE_PRICE']:.2f}")
            return None

        base_conf = CFG["SHADOW_HAIKU_MIN_CONF"] if shadow else CFG["HAIKU_MIN_CONF"]
        min_conf, _ = price_scaled_params(price, base_conf, 0)

        # Confidence threshold is the sole gate -- edge field is informational only.
        if conf < min_conf:
            return None

        return TradeRequest(
            market=market,
            direction=dir_,
            price=price,
            order_type=analysis.get("order_type", "maker"),
            tags=["haiku-analyse"],
            notes=analysis.get("reasoning", ""),
            confidence=conf,
            strategy="haiku-analyse",
        )
