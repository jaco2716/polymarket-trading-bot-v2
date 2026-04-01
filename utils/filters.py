"""
Market name filters.
"""
import re

_SPORTS_PATTERNS = ("Spread:", " vs. ", "O/U ", ": O/U")

# Suffixes that identify a specific game/round within a match event
_EVENT_SUFFIX_RE = re.compile(
    r"""
    \s*[-–]\s*
    (?:
        game\s*\d+(?:\s+winner)?   # "- Game 1", "- Game 1 Winner"
      | map\s*\d+                  # "- Map 2"
      | bo\d+                      # "- BO3"
      | \w+\s+round.*              # "- LCK Round of 8..."
      | \w+\s+playoffs.*           # "- EMEA Playoffs..."
      | \w+\s+masters.*            # "- EMEA Masters..."
      | \w+\s+cup.*                # "- any Cup..."
      | \w+\s+stage.*              # "- Group Stage..."
      | winner$                    # trailing "- Winner"
    )
    .*$                            # anything after
    """,
    re.IGNORECASE | re.VERBOSE,
)
_PAREN_SUFFIX_RE = re.compile(r'\s*\(bo\d+\).*$', re.IGNORECASE)
_HANDICAP_PREFIX_RE = re.compile(r'^game\s+handicap:\s*', re.IGNORECASE)
_SPREAD_RE = re.compile(r'\s*\([+-][\d.]+\)', re.IGNORECASE)   # (-1.5), (+1.5)
_DATE_RE = re.compile(r'\s*\d{4}-\d{2}-\d{2}')


def is_sports_matchup(name: str) -> bool:
    """Return True if the market name looks like a live sports game or spread bet."""
    return any(p in name for p in _SPORTS_PATTERNS)


def extract_event_key(name: str) -> str:
    """Derive a normalised event key from a market name for same-event deduplication.

    Markets that are variants of the same underlying match (Game 1, Game 2, BO3,
    Handicap) all reduce to the same key so the bot only enters one position
    per match.

    Examples:
      "LoL: T1 vs KT Rolster - Game 1 Winner"      → "lol: t1 vs kt rolster"
      "LoL: T1 vs KT Rolster - Game 2 Winner"      → "lol: t1 vs kt rolster"
      "LoL: T1 vs KT Rolster (BO3) - LCK Round..." → "lol: t1 vs kt rolster"
      "Game Handicap: T1 (-1.5) vs KT Rolster"     → "t1 vs kt rolster"
    """
    key = name
    key = _HANDICAP_PREFIX_RE.sub("", key)   # strip "Game Handicap: " prefix
    key = _SPREAD_RE.sub("", key)             # strip (-1.5), (+1.5)
    key = _PAREN_SUFFIX_RE.sub("", key)       # strip (BO3) and everything after
    key = _EVENT_SUFFIX_RE.sub("", key)       # strip "- Game 1 Winner" etc.
    key = _DATE_RE.sub("", key)               # strip trailing dates
    return key.strip().lower()
