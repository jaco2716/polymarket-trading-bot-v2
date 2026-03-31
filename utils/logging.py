"""
Logging configuration: rotating file handler + stdout.
"""
import logging
import logging.handlers
import sys


def setup_logging() -> logging.Logger:
    """Configure root logger with rotating file and stdout handlers."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(levelname)-7s  %(message)s",
        handlers=[
            logging.handlers.RotatingFileHandler(
                "bot.log", maxBytes=5_000_000, backupCount=3, encoding="utf-8"
            ),
            logging.StreamHandler(sys.stdout),
        ],
    )
    return logging.getLogger("polymarket_bot")
