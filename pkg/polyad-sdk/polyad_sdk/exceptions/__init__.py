"""
Expose categorized exceptions owned by polyad_sdk.
"""

from __future__ import annotations

from polyad_sdk.exceptions.api import APIError as APIError
from polyad_sdk.exceptions.events import StreamInterrupted as StreamInterrupted

__all__ = ("APIError", "StreamInterrupted")
