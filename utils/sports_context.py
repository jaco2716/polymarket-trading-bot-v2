"""
Sports context fetcher — background news, recent results, injuries, roster changes.

Unlike live_scores.py (current match state), this provides pre-match context:
  - Recent team results (form)
  - Player injuries / standins
  - Head-to-head history

Sources:
  Liquipedia MediaWiki API — esports (CS2, LoL, Valorant, Dota2…)  no key required
  ESPN hidden API          — NBA, NHL injuries + recent results     no key required

All fetches are TTL-cached globally. Per-market calls are local lookups only.
"""

import re
import time
import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from clients.http import SESSION

log = logging.getLogger(__name__)

_CONTEXT_TTL = 1800  # 30 min — background context changes slowly

# (fetched_at, data)
_espn_nba_cache: tuple[float, dict] = (0.0, {})
_espn_nhl_cache: tuple[float, dict] = (0.0, {})
_liquipedia_cache: dict[str, tuple[float, list[str]]] = {}  # key → (fetched_at, lines)

ESPN_BASE = "https://site.api.espn.com/apis/site/v2/sports"
LIQUIPEDIA_BASE = "https://liquipedia.net"
_LIQUIPEDIA_HEADERS = {
    "User-Agent": "polymarket-trading-bot/2.0 (research only; contact via github)",
    "Accept-Encoding": "gzip",
}

# ── ESPN — NBA / NHL ─────────────────────────────────────────────────────────

def _fetch_espn(sport: str, league: str) -> dict:
    """Fetch and cache ESPN injuries + recent scoreboard for a league."""
    cache = _espn_nba_cache if league == "nba" else _espn_nhl_cache
    now = time.time()
    if now - cache[0] < _CONTEXT_TTL:
        return cache[1]

    result: dict = {"injuries": {}, "recent": {}}

    # Injuries
    try:
        r = SESSION.get(f"{ESPN_BASE}/{sport}/{league}/injuries", timeout=8)
        if r.status_code == 200:
            for team_block in r.json().get("injuries") or []:
                team_name = team_block.get("displayName", "")
                team_injuries = []
                for inj in team_block.get("injuries") or []:
                    athlete = inj.get("athlete", {})
                    name    = athlete.get("displayName", "?")
                    status  = inj.get("status", "")
                    comment = inj.get("shortComment") or inj.get("longComment") or ""
                    pos     = athlete.get("position", {})
                    pos_abbr = pos.get("abbreviation", "") if isinstance(pos, dict) else str(pos)
                    if status and status.upper() not in ("ACTIVE", ""):
                        team_injuries.append(f"{name} ({pos_abbr}): {status}"
                                             + (f" — {comment}" if comment else ""))
                if team_injuries:
                    result["injuries"][team_name.lower()] = team_injuries
        else:
            log.debug(f"ESPN {league} injuries: HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"ESPN {league} injuries fetch error: {e}")

    # Recent scoreboard (yesterday + today)
    try:
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y%m%d")
        r = SESSION.get(
            f"{ESPN_BASE}/{sport}/{league}/scoreboard",
            params={"dates": yesterday},
            timeout=8,
        )
        if r.status_code == 200:
            for event in r.json().get("events") or []:
                comps = event.get("competitions") or []
                if not comps:
                    continue
                comp = comps[0]
                competitors = comp.get("competitors") or []
                if len(competitors) < 2:
                    continue
                names  = [c.get("displayName", "?") for c in competitors]
                scores = [c.get("score", "?") for c in competitors]
                status = (event.get("status") or {})
                state  = status.get("type", {}).get("description", "")
                if state in ("Final", "Final/OT", "Final/SO"):
                    line = f"Recent result: {names[0]} {scores[0]} – {scores[1]} {names[1]}"
                    for name in names:
                        result["recent"].setdefault(name.lower(), []).append(line)
    except Exception as e:
        log.debug(f"ESPN {league} scoreboard fetch error: {e}")

    if league == "nba":
        global _espn_nba_cache
        _espn_nba_cache = (now, result)
    else:
        global _espn_nhl_cache
        _espn_nhl_cache = (now, result)

    log.debug(f"ESPN {league}: cached {len(result['injuries'])} injury teams, "
              f"{len(result['recent'])} recent results")
    return result


def _espn_context_for_teams(sport: str, league: str, team_names: list[str]) -> list[str]:
    """Return injury + recent result lines for the given team names."""
    data = _fetch_espn(sport, league)
    lines: list[str] = []

    for query in team_names:
        q = query.lower()
        # Fuzzy match against stored keys
        inj_key = next((k for k in data["injuries"] if q in k or k in q
                        or q.split()[-1] in k), None)
        rec_key = next((k for k in data["recent"]   if q in k or k in q
                        or q.split()[-1] in k), None)

        if inj_key:
            lines.extend(data["injuries"][inj_key][:3])
        if rec_key:
            lines.extend(data["recent"][rec_key][:2])

    # Deduplicate while preserving order
    seen: set[str] = set()
    unique = []
    for l in lines:
        if l not in seen:
            seen.add(l)
            unique.append(l)
    return unique


# ── Liquipedia — esports ─────────────────────────────────────────────────────

# Maps esports game prefix → Liquipedia wiki slug
_ESPORTS_WIKI: dict[str, str] = {
    "counter-strike": "counterstrike",
    "cs2":            "counterstrike",
    "lol":            "leagueoflegends",
    "league of legends": "leagueoflegends",
    "valorant":       "valorant",
    "dota":           "dota2",
    "rocket league":  "rocketleague",
    "rainbow six":    "rainbowsix",
    "overwatch":      "overwatch",
    "starcraft":      "starcraft2",
}


def _detect_esports_wiki(market_name: str) -> Optional[str]:
    lower = market_name.lower()
    for prefix, wiki in _ESPORTS_WIKI.items():
        if lower.startswith(prefix) or lower.startswith(f"{prefix}:"):
            return wiki
        if f": {prefix}" in lower:
            return wiki
    return None


def _fetch_liquipedia_team(wiki: str, team_name: str) -> list[str]:
    """Fetch recent match results + roster notes for a team from Liquipedia."""
    cache_key = f"{wiki}:{team_name.lower()}"
    now = time.time()
    if cache_key in _liquipedia_cache:
        cached_at, lines = _liquipedia_cache[cache_key]
        if now - cached_at < _CONTEXT_TTL:
            return lines

    lines: list[str] = []

    # Use the MediaWiki parse endpoint to get the team page summary
    try:
        r = SESSION.get(
            f"{LIQUIPEDIA_BASE}/{wiki}/api.php",
            params={
                "action":   "query",
                "format":   "json",
                "prop":     "extracts",
                "exintro":  "1",
                "exsentences": "5",
                "explaintext": "1",
                "redirects": "1",
                "titles":   team_name,
            },
            headers=_LIQUIPEDIA_HEADERS,
            timeout=10,
        )
        if r.status_code == 200:
            pages = r.json().get("query", {}).get("pages", {})
            for page in pages.values():
                extract = page.get("extract", "")
                if extract and len(extract) > 50:
                    # First 2 sentences are usually enough for context
                    sentences = [s.strip() for s in extract.split(".") if len(s.strip()) > 20]
                    lines.extend(f"Liquipedia: {s}." for s in sentences[:2])
        elif r.status_code == 429:
            log.debug(f"Liquipedia rate limited for {team_name}")
        else:
            log.debug(f"Liquipedia {wiki}/{team_name}: HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"Liquipedia fetch error for {team_name}: {e}")

    _liquipedia_cache[cache_key] = (now, lines)
    # Respect Liquipedia's 1 req/2s guideline
    time.sleep(2.0)
    return lines


def _liquipedia_context(market_name: str, team_a: Optional[str], team_b: Optional[str]) -> list[str]:
    """Fetch Liquipedia context for both teams in a match."""
    wiki = _detect_esports_wiki(market_name)
    if not wiki:
        return []

    lines: list[str] = []
    for team in [t for t in [team_a, team_b] if t]:
        team_lines = _fetch_liquipedia_team(wiki, team)
        lines.extend(team_lines)
    return lines


# ── Team extraction helper ───────────────────────────────────────────────────

def _extract_teams(market_name: str) -> tuple[Optional[str], Optional[str]]:
    """Extract (team_a, team_b) from 'X vs Y' or 'X vs. Y' patterns."""
    cleaned = re.sub(r'\s*\([+-]?\d+\.?\d*\)', '', market_name)
    m = re.search(r'[:\-–]\s*(.+?)\s+vs\.?\s+(.+?)(?:\s*[-–(]|\s*$)', cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    m = re.search(r'^(.+?)\s+vs\.?\s+(.+?)(?:\s*[-–(]|\s*$)', cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_sports_context(market_name: str, tags: list = None) -> list[str]:
    """
    Return background context lines (injuries, recent form, roster notes) for a market.
    Returns [] if the market is not a recognised sports event or no data is available.

    All HTTP fetches are TTL-cached (30 min). Per-market calls are local lookups only
    after the first fetch per cache window.
    """
    lower = market_name.lower()
    tag_lower = {str(t).lower() for t in (tags or [])}
    team_a, team_b = _extract_teams(market_name)

    # ── Esports: Liquipedia ───────────────────────────────────────────────
    if _detect_esports_wiki(market_name):
        return _liquipedia_context(market_name, team_a, team_b)

    # ── NBA ───────────────────────────────────────────────────────────────
    is_nba = (
        "basketball" in tag_lower or "nba" in tag_lower or
        "nba:" in lower or " nba " in lower or
        (team_a and any(w in lower for w in ["lakers", "celtics", "warriors", "nets",
                                              "knicks", "bulls", "heat", "thunder",
                                              "nuggets", "suns", "clippers", "bucks",
                                              "sixers", "raptors", "hawks", "jazz",
                                              "spurs", "rockets", "pelicans", "grizzlies",
                                              "timberwolves", "trail blazers", "kings",
                                              "pacers", "cavaliers", "pistons", "wizards",
                                              "hornets", "magic", "mavericks"]))
    )
    if is_nba and (team_a or team_b):
        return _espn_context_for_teams("basketball", "nba",
                                       [t for t in [team_a, team_b] if t])

    # ── NHL ───────────────────────────────────────────────────────────────
    is_nhl = (
        "hockey" in tag_lower or "nhl" in tag_lower or
        "nhl:" in lower or " nhl " in lower or
        (team_a and any(w in lower for w in ["bruins", "canadiens", "maple leafs",
                                              "senators", "sabres", "rangers", "islanders",
                                              "flyers", "penguins", "capitals", "hurricanes",
                                              "lightning", "panthers", "red wings", "blackhawks",
                                              "blues", "predators", "jets", "wild",
                                              "avalanche", "coyotes", "stars", "ducks",
                                              "kings", "sharks", "golden knights", "kraken",
                                              "canucks", "oilers", "flames", "devils"]))
    )
    if is_nhl and (team_a or team_b):
        return _espn_context_for_teams("hockey", "nhl",
                                       [t for t in [team_a, team_b] if t])

    return []
