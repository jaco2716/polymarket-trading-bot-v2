"""
Google News RSS headline fetcher with TTL cache.
"""
import logging
import time
import urllib.parse
import xml.etree.ElementTree as ET

from clients.http import SESSION

log = logging.getLogger(__name__)

_news_cache: dict[str, tuple[float, list[str]]] = {}
NEWS_CACHE_TTL = 3600


def fetch_news_headlines(query: str, max_items: int = 5) -> list[str]:
    """Fetch recent headlines from Google News RSS. Cached for 1 hour."""
    now = time.time()
    # Evict expired entries to prevent unbounded growth
    expired = [k for k, (ts, _) in _news_cache.items() if now - ts >= NEWS_CACHE_TTL]
    for k in expired:
        del _news_cache[k]
    if query in _news_cache:
        cached_at, headlines = _news_cache[query]
        if now - cached_at < NEWS_CACHE_TTL:
            return headlines

    encoded = urllib.parse.quote(query)
    url = f"https://news.google.com/rss/search?q={encoded}&hl=en-US&gl=US&ceid=US:en"
    try:
        r = SESSION.get(url, timeout=8)
        r.raise_for_status()
        root = ET.fromstring(r.text)
        headlines = [
            item.findtext("title", "")
            for item in root.findall("./channel/item")[:max_items]
            if item.findtext("title", "")
        ]
        _news_cache[query] = (now, headlines)
        return headlines
    except Exception as e:
        log.debug(f"News fetch failed for '{query[:40]}': {e}")
        return []
