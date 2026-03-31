"""
Logging configuration: rotating file handler + stdout.
"""
import logging
import logging.handlers
import os
import sys


def setup_logging() -> logging.Logger:
    """Configure root logger with rotating file and stdout handlers."""
    level = getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO)
    logging.basicConfig(
        level=level,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("polymarket_bot")
