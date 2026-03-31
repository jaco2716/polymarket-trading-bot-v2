"""
Global configuration loaded from environment variables.
All env vars documented in .env.example.
"""
import os
from dotenv import load_dotenv

load_dotenv()

# API base URLs
GAMMA_URL: str = "https://gamma-api.polymarket.com"
DATA_URL: str = "https://data-api.polymarket.com"
CLOB_URL: str = "https://clob.polymarket.com"

# File paths (relative to working directory)
DB_FILE: str = "paper_trades.db"
BUDGET_FILE: str = "budget.json"
SHADOW_BUDGET_FILE: str = "shadow_budget.json"
LIVE_BUDGET_FILE: str = "live_budget.json"

CFG = {
    # Required
    "ANTHROPIC_API_KEY": os.getenv("ANTHROPIC_API_KEY", ""),

    # Budget
    "STARTING_BUDGET":        float(os.getenv("STARTING_BUDGET",        "500")),
    "SHADOW_STARTING_BUDGET": float(os.getenv("SHADOW_STARTING_BUDGET", os.getenv("STARTING_BUDGET", "500"))),
    "TRADE_SIZE_PCT":         float(os.getenv("TRADE_SIZE_PCT",         "0.03")),
    "MIN_TRADE_USDC":         float(os.getenv("MIN_TRADE_USDC",         "5")),

    # Market filters
    "MIN_LIQUIDITY":           float(os.getenv("MIN_LIQUIDITY",           "2000")),
    "MAX_TRADE_PRICE":         float(os.getenv("MAX_TRADE_PRICE",         "0.85")),
    "MIN_TRADE_PRICE":         float(os.getenv("MIN_TRADE_PRICE",         "0.15")),
    "MAX_RESOLVE_HOURS":       float(os.getenv("MAX_RESOLVE_HOURS",       "0")),
    "MAX_WHALE_RESOLVE_HOURS": float(os.getenv("MAX_WHALE_RESOLVE_HOURS", "0")),
    "MARKET_POOL_SIZE":        int(os.getenv("MARKET_POOL_SIZE",          "100")),
    "MARKETS_PER_SCAN":        int(os.getenv("MARKETS_PER_SCAN",          "20")),

    # Strategies
    "ENABLE_HAIKU":              os.getenv("ENABLE_HAIKU",       "true").lower() == "true",
    "ENABLE_WHALE_COPY":         os.getenv("ENABLE_WHALE_COPY",  "true").lower() == "true",
    "HAIKU_MIN_CONF":            float(os.getenv("HAIKU_MIN_CONF",        "0.65")),
    "SHADOW_HAIKU_MIN_CONF":     float(os.getenv("SHADOW_HAIKU_MIN_CONF", "0.35")),
    "CONF_FLOOR":                float(os.getenv("CONF_FLOOR",             "0.45")),
    "SIZE_FLOOR":                float(os.getenv("SIZE_FLOOR",              "0.5")),
    "HAIKU_SKIP_SPORTS":         os.getenv("HAIKU_SKIP_SPORTS", "true").lower() == "true",
    "WHALE_MIN_SIZE":            float(os.getenv("WHALE_MIN_SIZE",             "500")),
    "MAX_WHALE_TRADES_PER_SCAN": int(os.getenv("MAX_WHALE_TRADES_PER_SCAN",    "5")),
    "WHALE_LEADERBOARD_SIZE":    int(os.getenv("WHALE_LEADERBOARD_SIZE",       "30")),
    "WHALE_WALLETS_PER_SCAN":    int(os.getenv("WHALE_WALLETS_PER_SCAN",       "15")),

    # Limits
    "MAX_OPEN_TRADES": int(os.getenv("MAX_OPEN_TRADES", "10")),
    "SCAN_INTERVAL":   int(os.getenv("SCAN_INTERVAL",   "3600")),

    # Strategy mode
    "STRATEGY_MODE": os.getenv("STRATEGY_MODE", "compete"),

    # Shadow scan interval
    "SHADOW_SCAN_INTERVAL": int(os.getenv("SHADOW_SCAN_INTERVAL", os.getenv("SCAN_INTERVAL", "3600"))),

    # Low budget cooldown
    "LOW_BUDGET_COOLDOWN": int(os.getenv("LOW_BUDGET_COOLDOWN", "7200")),

    # Whale wallet addresses
    "WHALE_WALLETS": [
        w.strip() for w in os.getenv("WHALE_WALLETS", "").split(",") if w.strip()
    ],

    # Live trading
    "POLYMARKET_PRIVATE_KEY":    os.getenv("POLYMARKET_PRIVATE_KEY", ""),
    "POLYMARKET_FUNDER_ADDRESS": os.getenv("POLYMARKET_FUNDER_ADDRESS", ""),
    "MAX_LIVE_TRADE_USDC":       float(os.getenv("MAX_LIVE_TRADE_USDC", "50")),
    "LIVE_MAX_OPEN_TRADES":      int(os.getenv("LIVE_MAX_OPEN_TRADES", "5")),
    "LIVE_DRY_RUN":              os.getenv("LIVE_DRY_RUN", "true").lower() == "true",
}


def validate_config() -> list[str]:
    """Return list of config error strings. Empty list = valid."""
    errors = []
    if not (0 < CFG["TRADE_SIZE_PCT"] < 1):
        errors.append(f"TRADE_SIZE_PCT must be between 0 and 1, got {CFG['TRADE_SIZE_PCT']}")
    if not (0 < CFG["MAX_TRADE_PRICE"] < 1):
        errors.append(f"MAX_TRADE_PRICE must be between 0 and 1, got {CFG['MAX_TRADE_PRICE']}")
    if CFG["STARTING_BUDGET"] <= 0:
        errors.append(f"STARTING_BUDGET must be positive, got {CFG['STARTING_BUDGET']}")
    if CFG["SCAN_INTERVAL"] < 60:
        errors.append(f"SCAN_INTERVAL must be >= 60 seconds, got {CFG['SCAN_INTERVAL']}")
    if CFG["STRATEGY_MODE"] not in ("compete", "parallel", "shadow", "live"):
        errors.append(f"STRATEGY_MODE must be compete|parallel|shadow|live, got '{CFG['STRATEGY_MODE']}'")
    return errors
