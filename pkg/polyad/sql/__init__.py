"""
Load PostgreSQL statements shipped with the operator package.
"""

from __future__ import annotations

import os
from functools import cache
from importlib.resources import files
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from polyad.sql.encryption import RecordCipher

__all__ = (
    "record_cipher",
    "statement",
)


def record_cipher() -> RecordCipher | None:
    """
    Import encryption primitives only for an explicitly enabled database writer.

    Returns:
        RecordCipher | None: Validated encryption configuration, or None when disabled.
    """
    enabled = os.environ.get("POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED", "false").lower()
    if enabled not in {"true", "false"}:
        raise ValueError("record encryption enablement must be true or false")
    if enabled == "false":
        return None
    from polyad.sql.encryption import RecordCipher

    return RecordCipher.from_environment()


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
