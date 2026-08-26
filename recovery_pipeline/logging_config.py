"""
logging_config.py — centralized structured logging for RecoverAI.

Why this exists: print() statements don't carry severity levels, timestamps,
or module context, and can't be filtered or redirected without code changes.
Python's logging module gives us all of that for free, and is the standard
observability baseline any reviewer will look for.

Usage in any module:
    from logging_config import get_logger
    logger = get_logger(__name__)
    logger.info("Dispatching via %s", channel)
    logger.warning("Compliance rule blocked action: %s", reason)
    logger.error("LLM call failed after retries: %s", e)

Log format includes timestamp, level, module name, and message — readable
in a terminal during the demo, and parseable if piped to a file/log
aggregator later.
"""

import logging
import sys

_CONFIGURED = False


def configure_logging(level: int = logging.INFO) -> None:
    """Configure the root logger once. Safe to call multiple times —
    only the first call has effect, so every module can call this
    defensively without duplicating handlers."""
    global _CONFIGURED
    if _CONFIGURED:
        return

    handler = logging.StreamHandler(sys.stdout)
    formatter = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%H:%M:%S",
    )
    handler.setFormatter(formatter)

    root = logging.getLogger()
    root.setLevel(level)
    root.addHandler(handler)

    _CONFIGURED = True


def get_logger(name: str) -> logging.Logger:
    """Get a module-scoped logger, ensuring root config is applied first."""
    configure_logging()
    return logging.getLogger(name)
