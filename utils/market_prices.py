"""
Real-time asset price context for crypto and equity index markets.

Two integrations:
  CoinGecko   — BTC, ETH, SOL, XRP, BNB etc.  Free, no API key required.
  Alpha Vantage — S&P 500, Nasdaq, Dow Jones.  Free tier (25 req/day), key required.

Prices are fetched once per TTL window and cached globally.
Per-market lookup is a local regex match — no per-market API calls.

Used by haiku_strategy to add concrete price context to the Haiku prompt, e.g.:
  "Bitcoin is currently $67,842 (+2.3% in 24h). Market asks if it will be
   above $68,000 at 4 PM ET — only $158 away."
"""

import re
import time
import logging
from typing import Optional

from config import CFG
from clients.http import SESSION

log = logging.getLogger(__name__)

_PRICE_TTL = 300  # 5 minutes — prices move but don't need sub-minute precision

# (fetched_at, {symbol: {...}})
_crypto_cache: tuple[float, dict] = (0.0, {})
_equity_cache: tuple[float, dict] = (0.0, {})

COINGECKO_URL    = "https://api.coingecko.com/api/v3"
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"

# ── Asset detection ──────────────────────────────────────────────────────────

# Maps lowercase keywords in market names → CoinGecko coin id
_CRYPTO_KEYWORDS: dict[str, str] = {
    "bitcoin":   "bitcoin",
    " btc ":     "bitcoin",
    "btc ":      "bitcoin",
    " btc":      "bitcoin",
    "ethereum":  "ethereum",
    " eth ":     "ethereum",
    "eth ":      "ethereum",
    " eth":      "ethereum",
    "solana":    "solana",
    " sol ":     "solana",
    "ripple":    "ripple",
    " xrp ":     "ripple",
    "dogecoin":  "dogecoin",
    " doge ":    "dogecoin",
    "bnb":       "binancecoin",
    "cardano":   "cardano",
    " ada ":     "cardano",
}

# Maps lowercase keywords → Alpha Vantage symbol
_EQUITY_KEYWORDS: dict[str, str] = {
    "s&p 500":  "SPY",
    "s&p500":   "SPY",
    " spx":     "SPY",
    "spx ":     "SPY",
    "(spx)":    "SPY",
    "nasdaq":   "QQQ",
    " qqq":     "QQQ",
    "dow jones": "DIA",
    " djia":    "DIA",
}


def _detect_assets(market_name: str) -> tuple[Optional[str], Optional[str]]:
    """
    Return (crypto_coin_id, equity_symbol) for the market, either may be None.
    Both could theoretically coexist (unlikely) so we return both.
    """
    lower = market_name.lower()
    crypto = next((cid for kw, cid in _CRYPTO_KEYWORDS.items() if kw in lower), None)
    equity = next((sym for kw, sym in _EQUITY_KEYWORDS.items() if kw in lower), None)
    return crypto, equity


# ── CoinGecko (crypto) ───────────────────────────────────────────────────────

def _fetch_crypto_prices() -> dict:
    """Fetch prices for all tracked coins in one request. Cached for _PRICE_TTL."""
    global _crypto_cache
    now = time.time()
    fetched_at, cached = _crypto_cache
    if now - fetched_at < _PRICE_TTL:
        return cached

    coin_ids = ",".join(set(_CRYPTO_KEYWORDS.values()))
    try:
        r = SESSION.get(
            f"{COINGECKO_URL}/simple/price",
            params={
                "ids":             coin_ids,
                "vs_currencies":   "usd",
                "include_24hr_change": "true",
                "include_24hr_vol": "true",
            },
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            _crypto_cache = (now, data)
            log.debug(f"CoinGecko: fetched prices for {list(data.keys())}")
            return data
        elif r.status_code == 429:
            log.debug("CoinGecko rate limited — using cached prices")
        else:
            log.debug(f"CoinGecko: HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"CoinGecko fetch error: {e}")

    return cached


def _format_crypto_context(coin_id: str, market_name: str) -> list[str]:
    """Build context lines for a crypto market."""
    prices = _fetch_crypto_prices()
    coin = prices.get(coin_id)
    if not coin:
        return []

    price_usd  = coin.get("usd")
    change_24h = coin.get("usd_24h_change")
    if price_usd is None:
        return []

    name = coin_id.capitalize()
    lines = [f"{name} current price: ${price_usd:,.2f} USD"]

    if change_24h is not None:
        direction = "up" if change_24h >= 0 else "down"
        lines.append(f"24h change: {direction} {abs(change_24h):.2f}%")

    # Extract target price from market name and compute distance
    # Patterns: "above $68,000", "below $66,000", "between $64,000 and $66,000"
    targets = re.findall(r'\$(\d[\d,]+)', market_name)
    if targets:
        parsed = [float(t.replace(",", "")) for t in targets]
        if len(parsed) == 1:
            target = parsed[0]
            diff = target - price_usd
            pct  = (diff / price_usd) * 100
            direction = "above" if diff > 0 else "below"
            lines.append(
                f"Target ${target:,.0f} is ${abs(diff):,.0f} ({abs(pct):.1f}%) "
                f"{direction} current price"
            )
        elif len(parsed) == 2:
            lo, hi = sorted(parsed)
            if lo <= price_usd <= hi:
                lines.append(
                    f"Currently INSIDE target range ${lo:,.0f}–${hi:,.0f} "
                    f"(${price_usd - lo:,.0f} above floor, ${hi - price_usd:,.0f} below ceiling)"
                )
            else:
                nearest = lo if price_usd < lo else hi
                diff = abs(price_usd - nearest)
                side = "below" if price_usd < lo else "above"
                lines.append(
                    f"Currently OUTSIDE range ${lo:,.0f}–${hi:,.0f} "
                    f"(${diff:,.0f} {side} range)"
                )

    return lines


# ── Alpha Vantage (equities) ─────────────────────────────────────────────────

def _fetch_equity_quote(symbol: str) -> Optional[dict]:
    """Fetch a single equity quote. Cached per symbol for _PRICE_TTL."""
    global _equity_cache
    now = time.time()
    fetched_at, cached = _equity_cache

    if now - fetched_at < _PRICE_TTL and symbol in cached:
        return cached.get(symbol)

    api_key = CFG.get("ALPHA_VANTAGE_KEY", "")
    if not api_key:
        return None

    try:
        r = SESSION.get(
            ALPHA_VANTAGE_URL,
            params={
                "function": "GLOBAL_QUOTE",
                "symbol":   symbol,
                "apikey":   api_key,
            },
            timeout=8,
        )
        if r.status_code == 200:
            quote = r.json().get("Global Quote") or {}
            if quote:
                # Merge into existing cache entries
                new_cache = dict(cached)
                new_cache[symbol] = quote
                _equity_cache = (now, new_cache)
                log.debug(f"Alpha Vantage: fetched quote for {symbol}")
                return quote
        log.debug(f"Alpha Vantage: HTTP {r.status_code} for {symbol}")
    except Exception as e:
        log.debug(f"Alpha Vantage fetch error for {symbol}: {e}")

    return cached.get(symbol)


def _format_equity_context(symbol: str, market_name: str) -> list[str]:
    """Build context lines for an equity index market."""
    quote = _fetch_equity_quote(symbol)
    if not quote:
        return []

    price_str  = quote.get("05. price") or quote.get("price")
    change_pct = quote.get("10. change percent") or quote.get("change percent", "")
    prev_close = quote.get("08. previous close") or quote.get("previous close")

    if not price_str:
        return []

    try:
        price = float(price_str)
    except (ValueError, TypeError):
        return []

    label = {"SPY": "S&P 500 (SPY ETF)", "QQQ": "Nasdaq (QQQ ETF)", "DIA": "Dow Jones (DIA ETF)"}.get(symbol, symbol)
    lines = [f"{label} current price: ${price:,.2f}"]

    if change_pct:
        pct_clean = change_pct.replace("%", "").strip()
        try:
            pct = float(pct_clean)
            direction = "up" if pct >= 0 else "down"
            lines.append(f"Today's move: {direction} {abs(pct):.2f}% from previous close")
        except (ValueError, TypeError):
            pass

    if prev_close:
        try:
            lines.append(f"Previous close: ${float(prev_close):,.2f}")
        except (ValueError, TypeError):
            pass

    return lines


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_asset_context(market_name: str) -> list[str]:
    """
    Return real-time price context lines for crypto or equity index markets.
    Returns [] if the market is not a recognised asset market or data is unavailable.

    CoinGecko requires no API key. Alpha Vantage requires ALPHA_VANTAGE_KEY in config.
    Both are TTL-cached globally — typically zero HTTP calls after first fetch per scan.
    """
    crypto_id, equity_symbol = _detect_assets(market_name)
    lines: list[str] = []

    if crypto_id:
        lines.extend(_format_crypto_context(crypto_id, market_name))

    if equity_symbol:
        lines.extend(_format_equity_context(equity_symbol, market_name))

    return lines
