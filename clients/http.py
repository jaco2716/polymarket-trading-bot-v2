"""
Shared HTTP session with retry logic for all API calls.
"""
import json
import logging
import time
from typing import Optional

import requests

log = logging.getLogger(__name__)

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": "polymarket-paper-bot/2.0"})


def get(url: str, params: dict = None, timeout: int = 10, retries: int = 3) -> Optional[dict]:
    """GET with exponential backoff on 429/5xx. Returns parsed JSON or None."""
    for attempt in range(retries):
        try:
            r = SESSION.get(url, params=params, timeout=timeout)
            if r.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                wait = 2 ** attempt
                log.warning(f"GET {url} → {r.status_code}, retrying in {wait}s...")
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.exceptions.RequestException as e:
            if attempt < retries - 1:
                log.warning(f"GET {url} failed (attempt {attempt+1}): {e}")
                time.sleep(2 ** attempt)
                continue
            log.warning(f"GET {url} failed after {retries} attempts: {e}")
            return None
        except json.JSONDecodeError as e:
            log.warning(f"GET {url} returned invalid JSON: {e} — body: {r.text[:200]}")
            return None
    return None
