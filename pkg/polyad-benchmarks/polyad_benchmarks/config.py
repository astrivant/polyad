"""
Validate experiment bounds before scheduling any requests.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass

from polyad_sdk import Client

__all__ = (
    "RunConfig",
    "operator_client",
    "request_prefix",
)


def operator_client(timeout: float) -> Client:
    """
    Use injected graph endpoint context and an optional externally managed credential.

    Args:
        timeout (float): Per-request socket timeout in seconds.

    Returns:
        Client: Standalone client with no operator or Kubernetes imports.
    """
    return Client(os.environ["POLYAD_API_URL"], os.environ.get("POLYAD_API_TOKEN") or None, timeout=timeout)


@dataclass(frozen=True)
class RunConfig:
    """
    Bound both offered arrivals and outstanding execution observations.

    Attributes:
        rate (float): Offered requests per second, independent of completion speed.
        duration (float): Arrival window in seconds.
        max_requests (int): Maximum scheduled arrivals, including skipped arrivals.
        concurrency (int): Maximum outstanding activation observations.
        timeout (float): Per-activation observation deadline in seconds.
        request_timeout (float): Per-HTTP-request timeout in seconds.
        poll_interval (float): Delay between receipt reads in seconds.
    """

    rate: float = 1
    duration: float = 60
    max_requests: int = 60
    concurrency: int = 8
    timeout: float = 120
    request_timeout: float = 5
    poll_interval: float = 1

    def __post_init__(self) -> None:
        """
        Reject invalid or unbounded configuration before creating worker threads.

        Returns:
            None: Invalid bounds raise ValueError.
        """
        for value, maximum in ((self.rate, 1000), (self.duration, 86400), (self.timeout, 3600), (self.request_timeout, 60)):
            if isinstance(value, bool) or not math.isfinite(value) or not 0 < value <= maximum:
                raise ValueError("rate, duration and timeouts must be positive, finite and within study limits")
        if not math.isfinite(self.poll_interval) or not 0.1 <= self.poll_interval <= self.timeout:
            raise ValueError("poll interval must be between 0.1 seconds and the observation timeout")
        if self.request_timeout > self.timeout:
            raise ValueError("request timeout must not exceed the observation timeout")
        if type(self.max_requests) is not int or not 1 <= self.max_requests <= 10000:
            raise ValueError("max_requests must be an integer between 1 and 10000")
        if type(self.concurrency) is not int or not 1 <= self.concurrency <= 128:
            raise ValueError("concurrency must be an integer between 1 and 128")


def request_prefix(value: str) -> str:
    """
    Require a bounded run identity suitable for stable activation request IDs.

    Args:
        value (str): Administrator-supplied run ID or inherited activation identity.

    Returns:
        str: Validated prefix.
    """
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,95}", value):
        raise ValueError("run ID must contain 1–96 letters, digits, dots, underscores or hyphens")
    return value
