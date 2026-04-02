"""
Live score fetcher for esports (Pandascore) and football/soccer (API-Football).

Fetches all running + recently finished matches once per TTL window and caches
them globally. Per-market lookups are local fuzzy name searches — no per-market
API calls. Both integrations are optional: if no API key is set, returns [] silently.

Supported sports:
  Esports — CS2, LoL, Valorant, Dota 2, R6, Rocket League  (Pandascore)
  Football/Soccer                                            (API-Football)
"""

import re
import time
import logging
from datetime import datetime, timezone, timedelta, date
from typing import Optional

from config import CFG
from clients.http import SESSION

log = logging.getLogger(__name__)

_LIVE_SCORE_TTL = 180  # 3 minutes — live data changes fast

# (fetched_at, matches_list)
_esports_cache: tuple[float, list[dict]] = (0.0, [])
_football_cache: tuple[float, list[dict]] = (0.0, [])

PANDASCORE_URL  = "https://api.pandascore.co"
API_FOOTBALL_URL = "https://v3.football.api-sports.io"

# Market-name prefix → Pandascore videogame slug (used only for logging)
_ESPORTS_PREFIXES = {
    "counter-strike", "lol", "valorant", "dota", "rocket league",
    "rainbow six", "overwatch", "starcraft", "game handicap",
    "map winner", "map handicap",
}

_FOOTBALL_KEYWORDS = [
    " fc ", " cf ", "münchen", "manchester", "barcelona", "chelsea",
    "arsenal", "liverpool", "madrid", "juventus", "premier league",
    "bundesliga", "la liga", "serie a", "ligue 1", "eredivisie",
    "champions league", "europa league",
]

# Other sports with no live score API — detected so they can be skipped
_OTHER_SPORTS_PREFIXES = {
    # Basketball
    "nba:", "wnba:", "nbl:", "euroleague:", "ncaa basketball",
    # American football
    "nfl:", "ncaa football", "cfl:",
    # Baseball
    "mlb:", "npb:",
    # Ice hockey
    "nhl:", "khl:", "shl:",
    # Tennis
    "tennis:", "atp:", "wta:",
    # Cricket
    "cricket:", "ipl:", "bbl:",
    # Rugby
    "rugby:", "nrl:", "super rugby",
    # Combat sports / racing
    "ufc:", "boxing:", "formula 1:", "f1:", "motogp:",
    # Generic matchup patterns caught by prefix
    "will ", "who wins",
}

# Suffixes / substrings that indicate a head-to-head sports match for any sport
# These fire only when the name contains " vs " (no API coverage → always skip)
_GENERIC_SPORTS_SUBSTRINGS = {
    " winner", "- game 1", "- game 2", "- map 1", "- map 2",
    "(bo1)", "(bo2)", "(bo3)", "(bo5)",
    " spread", " moneyline", "- series",
}


# ── Sport detection + name parsing ──────────────────────────────────────────

def detect_sport(market_name: str, tags: list = None) -> Optional[str]:
    """
    Return the sport category for a market, or None if not a sports market.

    Uses Polymarket tags when available (reliable), falls back to name-based
    detection when tags are empty (common for esports markets).

    Categories:
      'esports'  — live score lookup via Pandascore
      'football' — live score lookup via API-Football
      'sports'   — other sport with no live score API (will always be skipped)
    """
    # ── Tag-based detection (authoritative when present) ──────────────────
    if tags:
        tag_lower = {str(t).lower() for t in tags}
        if tag_lower & {"esports", "e-sports"}:
            return "esports"
        if tag_lower & {"soccer", "football", "bundesliga", "premier league",
                        "la liga", "serie a", "ligue 1", "champions league"}:
            return "football"
        if "sports" in tag_lower:
            # Sports tag present but not esports/football — no live score API
            return "sports"

    # ── Name-based fallback ───────────────────────────────────────────────
    lower = market_name.lower()

    # Esports: prefix-based detection
    for prefix in _ESPORTS_PREFIXES:
        if lower.startswith(prefix):
            return "esports"
        if f": {prefix}" in lower or f" {prefix} " in lower:
            return "esports"

    # Football: keyword-based (team/league names)
    if any(kw in lower for kw in _FOOTBALL_KEYWORDS):
        return "football"
    # "Will X win on YYYY-MM-DD?" — typically football
    if re.search(r"will .+ win on \d{4}-\d{2}-\d{2}", lower):
        return "football"

    # Other sports: prefix-based
    for prefix in _OTHER_SPORTS_PREFIXES:
        if lower.startswith(prefix):
            return "sports"

    # Catch-all: any "X vs Y" or "X vs. Y" (American sports) market.
    # Bare "Team A vs. Team B" with no suffix is still a sports matchup.
    has_vs = " vs " in lower or " vs. " in lower
    if has_vs and any(s in lower for s in _GENERIC_SPORTS_SUBSTRINGS):
        return "sports"
    # "vs." (dot) alone is a strong enough signal — American leagues never use
    # this format for anything other than live game matchups.
    if " vs. " in lower:
        return "sports"

    return None


def _extract_teams(market_name: str) -> tuple[Optional[str], Optional[str]]:
    """
    Extract (team_a, team_b) from patterns like:
      "LoL: T1 vs KT Rolster - Game 1 Winner"
      "Counter-Strike: Heroic vs BetBoom Team (BO3)"
      "Game Handicap: T1 (-1.5) vs KT Rolster (+1.5)"
    Returns (None, None) if no vs pattern found.
    """
    # Strip handicap suffixes like "(-1.5)" before parsing
    cleaned = re.sub(r'\s*\([+-]?\d+\.?\d*\)', '', market_name)
    # "Prefix: TeamA vs TeamB - ..."
    m = re.search(r'[:\-–]\s*(.+?)\s+vs\.?\s+(.+?)(?:\s*[-–(]|\s*$)', cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    # "TeamA vs TeamB" with no prefix
    m = re.search(r'^(.+?)\s+vs\.?\s+(.+?)(?:\s*[-–(]|\s*$)', cleaned, re.IGNORECASE)
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return None, None


def _extract_single_team(market_name: str) -> Optional[str]:
    """Extract team from 'Will X win on ...' patterns."""
    m = re.search(r'will (.+?) win on', market_name, re.IGNORECASE)
    return m.group(1).strip() if m else None


def _team_matches(query: str, candidate: str) -> bool:
    """Fuzzy match: True if query and candidate likely refer to the same team."""
    q = query.lower().strip()
    c = candidate.lower().strip()
    if not q or not c:
        return False
    if q == c:
        return True
    if q in c or c in q:
        return True
    # First significant word (handles "Heroic" vs "Heroic Gaming")
    q_words = [w for w in q.split() if len(w) > 2]
    c_words = [w for w in c.split() if len(w) > 2]
    if q_words and c_words and q_words[0] == c_words[0]:
        return True
    return False


def _any_team_matches(market_teams: list[str], opponent_names: list[str]) -> bool:
    return any(
        _team_matches(mt, on)
        for mt in market_teams if mt
        for on in opponent_names if on
    )


# ── Pandascore (esports) ─────────────────────────────────────────────────────

def _fetch_esports_matches() -> list[dict]:
    """Fetch running + recently finished esports matches. Cached for _LIVE_SCORE_TTL."""
    global _esports_cache
    now = time.time()
    fetched_at, cached = _esports_cache
    if now - fetched_at < _LIVE_SCORE_TTL:
        return cached

    api_key = CFG.get("PANDASCORE_API_KEY", "")
    if not api_key:
        return []

    headers = {"Authorization": f"Bearer {api_key}"}
    matches: list[dict] = []

    try:
        r = SESSION.get(
            f"{PANDASCORE_URL}/matches/running",
            headers=headers,
            params={"per_page": 100},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            matches.extend(data if isinstance(data, list) else [])
        else:
            log.debug(f"Pandascore running: HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"Pandascore running fetch error: {e}")

    # Past 3 hours — catches recently finished matches
    try:
        since = (datetime.now(timezone.utc) - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = SESSION.get(
            f"{PANDASCORE_URL}/matches/past",
            headers=headers,
            params={"per_page": 50, "range[begin_at]": since},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            matches.extend(data if isinstance(data, list) else [])
    except Exception as e:
        log.debug(f"Pandascore past fetch error: {e}")

    # Next 2 hours — catches upcoming matches (useful for "is this match about to start?")
    try:
        until = (datetime.now(timezone.utc) + timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
        r = SESSION.get(
            f"{PANDASCORE_URL}/matches/upcoming",
            headers=headers,
            params={"per_page": 30, "range[end_at]": until},
            timeout=8,
        )
        if r.status_code == 200:
            data = r.json()
            matches.extend(data if isinstance(data, list) else [])
    except Exception as e:
        log.debug(f"Pandascore upcoming fetch error: {e}")

    # Deduplicate by match id
    seen: set[int] = set()
    unique = []
    for m in matches:
        mid = m.get("id")
        if mid and mid not in seen:
            seen.add(mid)
            unique.append(m)

    _esports_cache = (now, unique)
    log.debug(f"Pandascore: cached {len(unique)} matches (running+past+upcoming)")
    return unique


def _format_esports_match(match: dict) -> list[str]:
    """Format a Pandascore match dict into readable score lines for the Haiku prompt."""
    lines = []
    status    = match.get("status", "unknown")
    league    = (match.get("league") or {}).get("name", "")
    serie     = (match.get("serie") or {}).get("full_name", "")
    context   = f"{league} — {serie}".strip(" —") or "Unknown tournament"

    opponents     = match.get("opponents") or []
    opp_names     = [(o.get("opponent") or {}).get("name", "?") for o in opponents]
    opp_ids       = [(o.get("opponent") or {}).get("id") for o in opponents]
    results       = match.get("results") or []
    score_by_id   = {r.get("team_id"): r.get("score", 0) for r in results}
    series_scores = [score_by_id.get(oid, 0) for oid in opp_ids]

    games        = match.get("games") or []
    finished_g   = [g for g in games if g.get("status") == "finished"]
    running_game = next((g for g in games if g.get("status") == "running"), None)

    a = opp_names[0] if opp_names else "?"
    b = opp_names[1] if len(opp_names) > 1 else "?"
    sa = series_scores[0] if series_scores else 0
    sb = series_scores[1] if len(series_scores) > 1 else 0

    if status == "running":
        lines.append(f"LIVE ({context}): {a} vs {b}")
        if len(series_scores) == 2:
            lines.append(f"Series score: {a} {sa} – {sb} {b}")
        if running_game:
            gnum = running_game.get("position", "?")
            lines.append(f"Game/Map {gnum} currently in progress")
        elif finished_g:
            lines.append(f"{len(finished_g)} game(s) finished, next map loading")

    elif status == "finished":
        winner = (match.get("winner") or {}).get("name", "unknown")
        lines.append(f"FINISHED ({context}): {a} vs {b}")
        lines.append(f"Winner: {winner}")
        if len(series_scores) == 2:
            lines.append(f"Final series score: {a} {sa} – {sb} {b}")

    elif status == "not_started":
        scheduled = (match.get("scheduled_at") or "")[:16]
        lines.append(f"UPCOMING ({context}): {a} vs {b} — scheduled {scheduled}")

    return lines


# ── API-Football (soccer) ────────────────────────────────────────────────────

def _fetch_football_fixtures() -> list[dict]:
    """Fetch live + today's football fixtures from API-Football. Cached."""
    global _football_cache
    now = time.time()
    fetched_at, cached = _football_cache
    if now - fetched_at < _LIVE_SCORE_TTL:
        return cached

    api_key = CFG.get("API_FOOTBALL_KEY", "")
    if not api_key:
        return []

    headers  = {"x-apisports-key": api_key}
    fixtures: list[dict] = []

    try:
        r = SESSION.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=headers,
            params={"live": "all"},
            timeout=8,
        )
        if r.status_code == 200:
            fixtures.extend(r.json().get("response", []))
        else:
            log.debug(f"API-Football live: HTTP {r.status_code}")
    except Exception as e:
        log.debug(f"API-Football live fetch error: {e}")

    # Today's scheduled + recently finished
    try:
        today = date.today().isoformat()
        r = SESSION.get(
            f"{API_FOOTBALL_URL}/fixtures",
            headers=headers,
            params={"date": today},
            timeout=8,
        )
        if r.status_code == 200:
            fixtures.extend(r.json().get("response", []))
    except Exception as e:
        log.debug(f"API-Football today fetch error: {e}")

    # Deduplicate by fixture id
    seen: set[int] = set()
    unique = []
    for f in fixtures:
        fid = (f.get("fixture") or {}).get("id")
        if fid and fid not in seen:
            seen.add(fid)
            unique.append(f)

    _football_cache = (now, unique)
    log.debug(f"API-Football: cached {len(unique)} fixtures")
    return unique


def _format_football_fixture(fixture: dict) -> list[str]:
    """Format an API-Football fixture into readable score lines."""
    lines  = []
    fix    = fixture.get("fixture") or {}
    teams  = fixture.get("teams") or {}
    goals  = fixture.get("goals") or {}
    league = (fixture.get("league") or {}).get("name", "Unknown league")

    home        = (teams.get("home") or {}).get("name", "?")
    away        = (teams.get("away") or {}).get("name", "?")
    home_goals  = goals.get("home")
    away_goals  = goals.get("away")
    status_s    = (fix.get("status") or {}).get("short", "")
    elapsed     = (fix.get("status") or {}).get("elapsed")
    score_str   = f"{home_goals} – {away_goals}" if home_goals is not None else "? – ?"

    if status_s in ("1H", "HT", "2H", "ET", "BT", "P", "SUSP", "INT", "LIVE"):
        elapsed_str = f" {elapsed}'" if elapsed else ""
        lines.append(f"LIVE ({league}): {home} vs {away}{elapsed_str}")
        lines.append(f"Current score: {home} {score_str} {away}")

    elif status_s in ("FT", "AET", "PEN"):
        lines.append(f"FINISHED ({league}): {home} vs {away}")
        lines.append(f"Final score: {home} {score_str} {away}")

    elif status_s == "NS":
        scheduled = (fix.get("date") or "")[:16]
        lines.append(f"UPCOMING ({league}): {home} vs {away} — scheduled {scheduled}")

    return lines


# ── Public API ───────────────────────────────────────────────────────────────

def fetch_live_scores(market_name: str, tags: list = None) -> list[str]:
    """
    Return live score / match-status lines for a market.
    Returns [] if the market is not a supported sport, no match is found,
    or no API key is configured.

    Called per-market during strategy evaluation. Actual HTTP fetches are
    TTL-cached globally so this is typically a fast dictionary lookup.
    """
    sport = detect_sport(market_name, tags=tags)
    if not sport:
        return []

    if sport == "esports":
        team_a, team_b = _extract_teams(market_name)
        if not team_a:
            return []
        matches = _fetch_esports_matches()
        for match in matches:
            opponents = match.get("opponents") or []
            opp_names = [(o.get("opponent") or {}).get("name", "") for o in opponents]
            if _any_team_matches([t for t in [team_a, team_b] if t], opp_names):
                lines = _format_esports_match(match)
                if lines:
                    return lines
        return []

    if sport == "football":
        team_a, team_b = _extract_teams(market_name)
        if not team_a:
            team_a = _extract_single_team(market_name)
        if not team_a:
            return []
        search_teams = [t for t in [team_a, team_b] if t]
        fixtures = _fetch_football_fixtures()
        for fixture in fixtures:
            t = fixture.get("teams") or {}
            home = (t.get("home") or {}).get("name", "")
            away = (t.get("away") or {}).get("name", "")
            if _any_team_matches(search_teams, [home, away]):
                lines = _format_football_fixture(fixture)
                if lines:
                    return lines
        return []

    return []
