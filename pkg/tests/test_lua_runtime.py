"""
Verify packaged Redis and Dragonfly Lua programs compile locally.
"""

from __future__ import annotations

from pathlib import Path

from polyad.lua.runtime import validate_server_scripts


def test_redis_scripts_compile_under_lupa_without_server_execution() -> None:
    """
    Catch syntax regressions locally while retaining atomic execution in Redis.

    Returns:
        None: Assertions validate every packaged server script.
    """
    validated = validate_server_scripts()
    lua_root = Path(__file__).parents[1] / "polyad/lua"

    # Discover packaged scripts independently so newly added Lua cannot bypass the compile check.
    expected = tuple(sorted(str(path.relative_to(lua_root)) for path in lua_root.glob("*/*.lua")))
    assert validated == expected
    assert "authentication/acquire.lua" in validated
    assert "coordination/publish.lua" in validated
    assert "events/read.lua" in validated
