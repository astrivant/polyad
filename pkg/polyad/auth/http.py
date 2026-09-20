"""
Authenticate configured inbound keys before reserving their shared request lane.
"""

from __future__ import annotations

import hmac
import os
from typing import TYPE_CHECKING

from flask import g, jsonify, request

from polyad.auth.keys import Keyring
from polyad.auth.policy import LISTENERS, endpoint_scope, public_demo
from polyad.cache import cache_url
from polyad_types.api.auth import KeyDirection

if TYPE_CHECKING:
    from collections.abc import Iterator
    from typing import Any

    from flask import Flask, Response

    from polyad.api.http.application import Routes
    from polyad.auth.lanes import Lanes
    from polyad.auth.store import CredentialStore


class Access:
    """
    Own one listener's credential reader and shared concurrency transport.
    """

    def __init__(self, keys: Keyring, lanes: Lanes, store: CredentialStore | None = None) -> None:
        """
        Share credential identities across listeners while retaining independent transports.

        Args:
            keys (Keyring): Projected policy and rotatable tokens.
            lanes (Lanes): HA-wide rate and concurrency admission.
            store (CredentialStore | None): Optional durable verification and revocation records.
        """
        self.keys, self.lanes = keys, lanes
        self.store = store
        self.endpoints = {endpoint for _, key, _ in keys.read() for endpoint in key.endpoints}

    @classmethod
    def from_environment(cls) -> Access | None:
        """
        Enable named keys only when the chart supplies a credential registry.

        Returns:
            Access | None: Listener-owned authentication runtime or existing single-token behavior.
        """
        if public_demo():
            return None
        keys = Keyring.from_environment()
        if keys is None:
            return None
        from polyad.auth.lanes import Lanes

        store = None
        if os.environ.get("POLYAD_AUTH_DATABASE_DSN_FILE"):
            from polyad.auth.store import CredentialStore

            store = CredentialStore.from_environment()
        return cls(keys, Lanes(cache_url(), os.environ.get("POLYAD_NAMESPACE", "default")), store)

    def supports(self, endpoint: str) -> bool:
        """
        Keep an enrolled endpoint protected even if its last key is removed during rotation.

        Args:
            endpoint (str): API scope being installed.

        Returns:
            bool: Whether named credentials replace the endpoint's legacy token.
        """
        return bool(LISTENERS.get(endpoint, {endpoint}) & self.endpoints)

    def close(self) -> None:
        """
        Close this listener's renewable request lanes.

        Returns:
            None: Remaining leases are released or left to expire.
        """
        self.lanes.close()
        if self.store:
            self.store.close()


def install(app: Flask | Routes, endpoint: str, token: str | None, access: Access | None = None) -> bool:
    """
    Install scoped authentication and hold streaming permits until response closure.

    Args:
        app (Flask | Routes): Listener application before routes and older rate limits are installed.
        endpoint (str): Required inbound API scope.
        token (str | None): Existing single token, or None for optional public telemetry.
        access (Access | None): Named credentials with HA-wide lane enforcement.

    Returns:
        bool: Whether this API requires authentication.
    """
    disabled = public_demo()
    managed = not disabled and access is not None and access.supports(endpoint)
    app.extensions["polyad.authentication"] = access
    if not disabled and (managed or token is not None) and os.environ.get("POLYAD_AUTH_BACKEND", "Builtin") == "FlaskHTTPAuth":
        from polyad.auth.flask_auth import install_flask_auth

        install_flask_auth(app, access.keys if managed and access else None, token)

    def authorize() -> tuple[Response, int] | None:
        if disabled:
            return None
        supplied = request.headers.get("Authorization", "")
        if not managed:
            if token is not None and not hmac.compare_digest(supplied.encode(), f"Bearer {token}".encode()):
                return jsonify(error="unauthorized"), 401
            return None
        assert access is not None
        from polyad.auth.lanes import LaneFull

        try:
            match = None
            for group, policy, secret in access.keys.read():
                if hmac.compare_digest(supplied.encode(), f"Bearer {secret}".encode()):
                    match = group, policy, secret
            if match is None:
                return jsonify(error="unauthorized"), 401
            group, policy, secret = match
            scope = endpoint_scope(endpoint, request.path)
            authorized = (
                bool(LISTENERS.get(endpoint, {endpoint}) & set(policy.endpoints))
                if request.path == "/openapi.json"
                else scope in policy.endpoints
            )
            if policy.direction == KeyDirection.OUTBOUND or not authorized:
                return jsonify(error="credential does not authorize this API"), 403
            if access.store is not None and not access.store.permitted(group, policy, secret):
                return jsonify(error="credential has been revoked"), 403

            # Authenticate and authorize first; rejected callers must not consume a valid key's lane.
            permit = access.lanes.acquire(group, policy)
            g.polyad_credential = {"group": group, "name": policy.name}
            g.polyad_key = policy
            g.polyad_permit = permit
        except LaneFull as error:
            response = jsonify(error="credential lane capacity exhausted")
            response.headers["Retry-After"] = str(error.retry_after)
            return response, 429
        except Exception:
            return jsonify(error="credential registry or lane storage unavailable"), 503
        return None

    app.before_request(authorize)

    @app.after_request
    def release(response: Response) -> Response:
        if response.status_code == 401:
            response.headers["WWW-Authenticate"] = "Bearer"
        permit = getattr(g, "polyad_permit", None)
        if permit is None or access is None:
            return response
        if not response.is_streamed:
            access.lanes.release(permit)
            return response

        # Streaming owns the permit for the entire response, not merely until headers are sent.
        original = response.response
        key = g.polyad_key
        group_name = g.polyad_credential["group"]
        authorization = request.headers.get("Authorization", "")

        # Revalidate on each chunk so rotation or revocation also stops already-open streams.
        def stream() -> Iterator[Any]:
            try:
                for chunk in original:
                    current = next(
                        (secret for group, policy, secret in access.keys.read() if group == group_name and policy == key),
                        None,
                    )
                    if current is None or not hmac.compare_digest(authorization.encode(), f"Bearer {current}".encode()):
                        raise RuntimeError("stream credential was revoked or changed")
                    if access.store is not None and not access.store.permitted(group_name, key, current):
                        raise RuntimeError("stream credential has been revoked")
                    permit.check()
                    yield chunk
            finally:
                close = getattr(original, "close", None)
                if close:
                    close()
                access.lanes.release(permit)

        response.response = stream()
        response.call_on_close(lambda: access.lanes.release(permit))
        return response

    @app.teardown_request
    def failed(error: BaseException | None) -> None:
        permit = getattr(g, "polyad_permit", None)
        if error is not None and permit is not None and access is not None:
            access.lanes.release(permit)

    return not disabled and (managed or token is not None)
