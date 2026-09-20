"""
Optionally delegate bearer parsing and challenges to Flask-HTTPAuth.
"""

from __future__ import annotations

import hmac
from typing import TYPE_CHECKING

from flask import g, jsonify

if TYPE_CHECKING:
    from flask import Flask, Response

    from polyad.api.http.application import Routes
    from polyad.auth.keys import Keyring


def install_flask_auth(app: Flask | Routes, keys: Keyring | None, token: str | None) -> None:
    """
    Install the optional extension while leaving permissions and shared lanes with Polyad.

    Args:
        app (Flask | Routes): Listener receiving the credential check before authorization.
        keys (Keyring | None): Named credential registry, when enrolled.
        token (str | None): Legacy token for listeners without named keys.

    Returns:
        None: Startup fails clearly if the optional dependency is missing.
    """
    try:
        from flask_httpauth import HTTPTokenAuth
    except ImportError as error:
        raise RuntimeError("FlaskHTTPAuth requires installing polyad[flask-auth]") from error

    auth = HTTPTokenAuth(scheme="Bearer")

    def verify(supplied: str) -> bool:
        try:
            candidates = [secret for _, _, secret in keys.read()] if keys else [token or ""]
            matched = False

            # Check all candidates without stopping at the first match, including during rotation.
            for expected in candidates:
                matched |= bool(supplied) and hmac.compare_digest(supplied.encode(), expected.encode())
            return matched
        except Exception:
            g.polyad_registry_unavailable = True
            return False

    def rejected(status: int) -> tuple[Response, int]:
        if getattr(g, "polyad_registry_unavailable", False):
            return jsonify(error="credential registry unavailable"), 503
        return jsonify(error="unauthorized"), status

    auth.verify_token(verify)
    auth.error_handler(rejected)
    app.before_request(auth.login_required(lambda: None))
    app.extensions["polyad.flask_httpauth"] = auth
