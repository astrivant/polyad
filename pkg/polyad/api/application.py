"""
Register endpoint families on one Flask application with socket-scoped routing.
"""

from __future__ import annotations

from functools import wraps
from typing import TYPE_CHECKING

from flask import Blueprint, Flask, request
from opentelemetry.trace import SpanKind, StatusCode
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from polyad.operator.tracing import span

if TYPE_CHECKING:
    from typing import Any

    from flask import Request, Response
    from flask.sansio.scaffold import T_before_request
    from werkzeug.routing import MapAdapter


class Application(Flask):
    """
    Select an endpoint family using the actual listener port, never a client-supplied Host header.
    """

    def full_dispatch_request(self) -> Response:
        """
        Trace request dispatch on the shared application using route templates.

        Returns:
            Response: Original response; streaming bodies run after dispatch completes.
        """
        if request.path in {"/metrics", "/healthz", "/readyz"}:
            return super().full_dispatch_request()
        method = request.method if request.method in {"GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"} else "_OTHER"
        route = request.url_rule.rule if request.url_rule else "unmatched"
        parent = TraceContextTextMapPropagator().extract({"traceparent": request.headers.get("traceparent", "")})
        with span(f"{method} {route}", kind=SpanKind.SERVER, context=parent) as active:
            active.set_attribute("http.request.method", method)
            active.set_attribute("http.route", route)
            active.set_attribute("polyad.api.family", request.blueprint or "unmatched")
            response = super().full_dispatch_request()
            active.set_attribute("http.response.status_code", response.status_code)
            if response.status_code >= 500:
                active.set_status(StatusCode.ERROR)
            return response

    def create_url_adapter(self, request: Request | None) -> MapAdapter | None:
        """
        Bind blueprint hosts to trusted WSGI socket identity without rewriting public paths.

        Args:
            request (Request | None): Incoming request, or an application-only context.

        Returns:
            MapAdapter | None: Adapter for the enabled endpoint family.
        """
        if request is None:
            return super().create_url_adapter(request)
        ports = self.extensions.get("polyad.ports")
        if ports is None:
            # Standalone builders used by embedders and tests register one family.
            domain = next(iter(self.blueprints), "unavailable") if len(self.blueprints) == 1 else "unavailable"
        else:
            domain = ports.get(request.environ.get("SERVER_PORT"), "unavailable")
        environ = {**request.environ, "HTTP_HOST": domain, "SERVER_NAME": domain}
        return self.url_map.bind_to_environ(environ)


def create_application() -> Flask:
    """
    Create the sole HTTP application owned by a server runtime.

    Returns:
        Flask: Empty application awaiting enabled route families.
    """
    app = Application(__name__, host_matching=True, static_folder=None)
    # Reject malformed paths instead of redirecting to an internal routing host.
    app.url_map.merge_slashes = False
    app.config["MAX_CONTENT_LENGTH"] = 1024 * 1024
    app.extensions["polyad.routes"] = {}
    return app


class Routes(Blueprint):
    """
    Keep family-specific hooks, errors and credentials inside one application.
    """

    def __init__(self, name: str, application: Flask | None = None, *, max_body: int = 1024 * 1024) -> None:
        """
        Prepare a blueprint and authenticate before its shared-storage quota hooks run.

        Args:
            name (str): Endpoint family and internal routing host.
            application (Flask | None): Existing process application, or a standalone application.
            max_body (int): Maximum request size for this family.
        """
        super().__init__(name, __name__)
        self.application = application if application is not None else create_application()
        if name in self.application.extensions["polyad.routes"]:
            raise ValueError(f"API family already registered: {name}")
        self.extensions: dict[str, Any] = {}
        self.application.extensions["polyad.routes"][name] = self

        @self.before_request
        def body_limit() -> None:
            request.max_content_length = max_body

    def add_url_rule(
        self, rule: str, endpoint: str | None = None, view_func: Any = None, provide_automatic_options: bool | None = None, **options: Any
    ) -> None:
        """
        Scope every route, including OpenAPI, to its socket-selected family.

        Args:
            rule (str): Public HTTP path.
            endpoint (str | None): Blueprint-local endpoint identity.
            view_func (Any): Flask view callback.
            provide_automatic_options (bool | None): Whether Flask supplies OPTIONS handling.
            **options (Any): Standard Flask route options.

        Returns:
            None: Route registration is deferred until the blueprint is installed.
        """
        super().add_url_rule(rule, endpoint, view_func, provide_automatic_options, host=self.name, **options)

    def before_request(self, function: T_before_request) -> T_before_request:
        """
        Preserve authentication-before-quota ordering with guarded application hooks.

        Args:
            function (T_before_request): Family-specific request hook.

        Returns:
            T_before_request: Original callback for decorator use.
        """

        @wraps(function)
        def scoped() -> Any:
            return function() if request.blueprint == self.name else None

        self.application.before_request(scoped)
        return function

    def finish(self) -> Flask:
        """
        Install this family without constructing another Flask application.

        Returns:
            Flask: Shared application with the blueprint registered.
        """
        self.application.register_blueprint(self)
        self.application.extensions.update(self.extensions)
        return self.application
