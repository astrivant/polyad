"""
Load Redis and Dragonfly Lua scripts shipped with the operator package.
"""

from __future__ import annotations

from functools import cache
from importlib.resources import files


@cache
def script(name: str) -> str:
    """
    Read a packaged Lua artifact independently of the process working directory.

    Args:
        name (str): Code-owned resource path relative to polyad.lua.

    Returns:
        str: Lua source with KEYS and ARGV placeholders preserved for server-side binding.
    """
    return files(__package__).joinpath(name).read_text(encoding="utf-8").strip()
