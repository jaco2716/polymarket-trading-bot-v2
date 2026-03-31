"""
Market name filters.
"""

_SPORTS_PATTERNS = ("Spread:", " vs. ", "O/U ", ": O/U")


def is_sports_matchup(name: str) -> bool:
    """Return True if the market name looks like a live sports game or spread bet."""
    return any(p in name for p in _SPORTS_PATTERNS)
