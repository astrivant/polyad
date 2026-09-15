"""
Share composition intake quotas by logical scheduler shard across replicas.
"""

from __future__ import annotations

import os
import time
from typing import TYPE_CHECKING

from attrs import field, frozen
from flask import g, jsonify, request
from flask_limiter import Limiter
from limits.errors import StorageError
from redis.backoff import NoBackoff
from redis.exceptions import RedisError
from redis.retry import Retry

from polyad.cache import cache_url
from polyad.compiler.passes.composition import request_name
from polyad.operator.coordination import root_shard

if TYPE_CHECKING:
    from typing import Any

    from flask import Flask, Response
    from flask_limiter import RequestLimit


@frozen(kw_only=True)
class RateLimitPolicy:
    """
    Configure one shared request budget per logical shard and namespace.

    Attributes:
        namespace (str): Namespace served by every replica sharing this budget.
        storage_uri (str): Redis or Dragonfly URL, excluded from representations.
        requests_per_minute (int): Combined composition submission and audit requests per shard.
        enabled (bool): Whether this application enforces the shared quota.
    """

    namespace: str
    storage_uri: str = field(repr=False)
    requests_per_minute: int = 60
    enabled: bool = True

    def __attrs_post_init__(self) -> None:
        """
        Reject invalid budgets and process-local production storage.

        Returns:
            None: No return value.
        """
        if (
            not self.namespace
            or not isinstance(self.requests_per_minute, int)
            or self.requests_per_minute < 1
            or isinstance(self.requests_per_minute, bool)
        ):
            raise ValueError("rate limits require a namespace and a positive requests_per_minute")
        if not self.storage_uri.startswith(("redis://", "rediss://")):
            raise ValueError("rate limits require shared Redis or Dragonfly storage")

    @classmethod
    def from_environment(cls, namespace: str) -> RateLimitPolicy:
        """
        Resolve runtime limits using the operator's existing cache connection settings.

        Args:
            namespace (str): Namespace served by this operator.

        Returns:
            RateLimitPolicy: Policy with rate limiting enabled by default.
        """
        return cls(
            namespace=namespace,
            storage_uri=cache_url(),
            requests_per_minute=int(os.environ.get("POLYAD_API_REQUESTS_PER_MINUTE", "60")),
            enabled=os.environ.get("POLYAD_API_RATE_LIMIT_ENABLED", "true").lower() == "true",
        )


def install_limits(app: Flask, policy: RateLimitPolicy) -> Limiter:
    """
    Install a fail-closed shared quota after authentication and before request handlers.

    Args:
        app (Flask): Application whose authentication hook is already registered.
        policy (RateLimitPolicy): Shared storage and shard budget configuration.

    Returns:
        Limiter: Extension whose synchronous pool belongs to the API server.
    """

    def key() -> str:
        if request.endpoint in {"compose", "status", "resources"}:
            if request.endpoint == "compose":
                value = request.get_json()
                request_id = value.get("requestId") if isinstance(value, dict) else None
            else:
                request_id = (request.view_args or {}).get("request_id")
            if not isinstance(request_id, str):
                raise ValueError("requestId must be a string")
            return str(root_shard("Composition", policy.namespace, request_name(request_id)))
        # Schema discovery has a separate bounded namespace budget.
        return "discovery"

    options: dict[str, Any] = {
        "socket_connect_timeout": 5,
        "socket_timeout": 5,
        "max_connections": 32,
        "retry": Retry(NoBackoff(), 0),
    }

    def capture_headers(limit: RequestLimit) -> None:
        # Read quota metadata before invoking the durable submission callback. A
        # cache failure while calculating response headers must not follow a write.
        reset = limit.reset_at
        g.polyad_rate_headers = {
            "X-RateLimit-Limit": str(limit.limit.amount),
            "X-RateLimit-Remaining": str(limit.remaining),
            "X-RateLimit-Reset": str(reset),
            "Retry-After": str(max(0, int(reset - time.time()))),
        }

    limiter = Limiter(
        key_func=key,
        app=app,
        application_limits=[f"{policy.requests_per_minute}/minute"],
        default_limits=[],
        storage_uri=policy.storage_uri,
        storage_options=options,
        strategy="fixed-window",
        key_prefix=f"polyad:{policy.namespace}:api",
        headers_enabled=False,
        on_breach=capture_headers,
        swallow_errors=False,
        in_memory_fallback_enabled=False,
        enabled=policy.enabled,
    )

    @app.before_request
    def prepare_headers() -> None:
        if limiter.current_limit is not None:
            capture_headers(limiter.current_limit)

    @app.after_request
    def response_headers(response: Response) -> Response:
        response.headers.update(getattr(g, "polyad_rate_headers", {}))
        return response

    @app.errorhandler(StorageError)
    @app.errorhandler(RedisError)
    def cache_unavailable(error: Exception) -> tuple[Response, int]:
        return jsonify(error="rate-limit storage unavailable", retry="reuse the same requestId"), 503

    return limiter
