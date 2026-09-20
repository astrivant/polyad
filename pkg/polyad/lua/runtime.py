"""
Validate trusted, packaged server-side Lua through a restricted runtime.
"""

from __future__ import annotations

from lupa.lua54 import LuaRuntime

from polyad.lua import script

__all__ = ("validate_server_scripts",)


_MEMORY_LIMIT = 8 * 1024 * 1024
_SERVER_SCRIPTS = (
    "authentication/acquire.lua",
    "authentication/renew.lua",
    "coordination/backlog.lua",
    "coordination/publish.lua",
    "coordination/pulse.lua",
    "events/capabilities.lua",
    "events/publish-topology.lua",
    "events/publish.lua",
    "events/read.lua",
    "events/snapshot.lua",
)


def _runtime() -> LuaRuntime:
    """
    Create an isolated Lua 5.4 runtime without Python or host-library access.

    Returns:
        LuaRuntime: Memory-bounded runtime for code-owned scripts only.
    """

    # types-lupa currently omits supported constructor keywords from its stub.
    runtime = LuaRuntime(  # type: ignore[call-arg]
        encoding="utf-8",
        register_eval=False,
        register_builtins=False,
        unpack_returned_tuples=True,
        max_memory=_MEMORY_LIMIT,
    )
    runtime.execute("python = nil; require = nil; package = nil; io = nil; os = nil; debug = nil; dofile = nil; loadfile = nil")
    return runtime


def validate_server_scripts() -> tuple[str, ...]:
    """
    Compile every Redis/Dragonfly script without executing its server calls.

    Returns:
        tuple[str, ...]: Validated package-relative script names.
    """
    runtime = _runtime()

    # Redis commands must stay server-side for atomicity; local validation only compiles their syntax.
    for name in _SERVER_SCRIPTS:
        runtime.compile(script(name), name=f"@polyad/lua/{name}", mode="t")
    return _SERVER_SCRIPTS
