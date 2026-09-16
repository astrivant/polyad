"""
Load PostgreSQL statements shipped with the operator package.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def statement(name: str) -> str:
    """
    Read a packaged SQL artifact independently of the process working directory.

    Args:
        name (str): Code-owned resource path relative to polyad.sql.

    Returns:
        str: SQL text with driver placeholders preserved for parameter binding.
    """
    return files(__package__).joinpath(name).read_text(encoding="utf-8").strip()
