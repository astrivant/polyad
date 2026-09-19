"""
Decode bounded WebSocket observations without importing the transport for SSE clients.
"""

from __future__ import annotations

import json
import socket
from typing import TYPE_CHECKING

from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, InvalidStatus
from websockets.sync.client import reconnect

from polyad_sdk.transport.http import APIError
from polyad_types.events.envelope import Event, EventTooLarge

if TYPE_CHECKING:
    from collections.abc import Iterator
    from threading import Event as StopEvent


class _NoRedirect(reconnect):
    def process_redirect(self, exc: Exception) -> Exception:
        return exc


def events(
    uri: str,
    headers: dict[str, str],
    timeout: float,
    max_bytes: int,
    *,
    endpoint: tuple[str, int] | None = None,
    stop_event: StopEvent | None = None,
) -> Iterator[Event]:
    """
    Consume one read-only subscription, leaving reconnection and checkpointing to the caller.

    Args:
        uri (str): WS(S) event endpoint with no embedded credential.
        headers (dict[str, str]): Fresh authorization, identity and optional replay cursor.
        timeout (float): Maximum wait for handshake or next event/heartbeat.
        max_bytes (int): Validated application receive budget for one complete JSON frame.
        endpoint (tuple[str, int] | None): Validated discovery target; keeps the URI's authority and TLS server name.
        stop_event (StopEvent | None): Optional subscription cancellation, checked on each heartbeat or frame.

    Yields:
        Event: Validated observation or recovery control, excluding transport heartbeats.
    """
    connection_socket = None
    try:
        if endpoint is not None:
            connection_socket = socket.create_connection(endpoint, timeout=timeout)
        with _NoRedirect(
            uri,
            sock=connection_socket,
            proxy=None if connection_socket is not None else True,
            additional_headers=headers,
            open_timeout=timeout,
            close_timeout=5,
            max_size=max_bytes,
            max_queue=1,
            compression=None,
        ) as connection:
            while stop_event is None or not stop_event.is_set():
                try:
                    raw = connection.recv(timeout=timeout)
                except ConnectionClosedOK:
                    return
                if not isinstance(raw, str):
                    raise ValueError("expected a text WebSocket event")
                if len(raw.encode("utf-8")) > max_bytes:
                    raise EventTooLarge(f"event exceeds {max_bytes} bytes")
                value = json.loads(raw)
                if (
                    not isinstance(value, dict)
                    or not isinstance(value.get("id"), str)
                    or not isinstance(value.get("event"), str)
                    or not isinstance(value.get("data"), dict)
                ):
                    raise ValueError("expected a WebSocket event with id, event and data")
                if value["event"] != "heartbeat":
                    yield Event(value["id"], value["event"], value["data"])
    except ConnectionClosedError as error:
        if any(close and close.code == 1009 for close in (error.sent, error.rcvd)):
            raise EventTooLarge(f"event exceeds {max_bytes} bytes") from None
        raise
    except InvalidStatus as error:
        try:
            body = json.loads(error.response.body[: 1024 * 1024])
        except (ValueError, UnicodeDecodeError):
            body = {"error": "non-JSON error response"}
        raise APIError(error.response.status_code, body if isinstance(body, dict) else {"error": body}) from None
    finally:
        if connection_socket is not None:
            connection_socket.close()
