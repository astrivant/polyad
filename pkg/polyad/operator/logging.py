"""
Configure consistent Python diagnostics for operator and observer entrypoints.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import argparse

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")


def add_logging_options(parser: argparse.ArgumentParser) -> None:
    """
    Expose a debug shorthand and an explicit level with environment fallback.

    Args:
        parser (argparse.ArgumentParser): Entrypoint argument parser.

    Returns:
        None: Mutually exclusive CLI options are registered on the parser.
    """
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--log-level",
        type=str.upper,
        choices=LEVELS,
        default=os.environ.get("POLYAD_LOG_LEVEL", "INFO"),
        help="Polyad log verbosity (default: POLYAD_LOG_LEVEL or INFO)",
    )
    options.add_argument("--debug", dest="log_level", action="store_const", const="DEBUG", help="Enable Polyad debug logging")


def configure_logging(parser: argparse.ArgumentParser, level: str) -> None:
    """
    Enable Polyad diagnostics without turning on dependency transport payload logging.

    Args:
        parser (argparse.ArgumentParser): Parser used to report invalid environment defaults.
        level (str): CLI-selected or environment-provided log level.

    Returns:
        None: Python logging is initialized before the process starts its workers.
    """
    if level not in LEVELS:
        parser.error("POLYAD_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s [%(threadName)s] %(name)s: %(message)s")
    logging.getLogger("polyad").setLevel(level)
