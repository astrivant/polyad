"""
Own opt-in, batched OpenTelemetry traces without collecting request payloads.
"""

from __future__ import annotations

import os
from contextlib import contextmanager
from functools import wraps
from typing import TYPE_CHECKING, ParamSpec, TypeVar

from opentelemetry import trace

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable, Iterator

    from opentelemetry.context import Context
    from opentelemetry.sdk.trace import TracerProvider

_provider: TracerProvider | None = None
_noop = trace.NoOpTracerProvider()
P = ParamSpec("P")
R = TypeVar("R")


def configure_tracing() -> None:
    """
    Initialize one process-owned provider before starting operator threads.

    Returns:
        None: Disabled processes create no exporter or background export thread.
    """
    global _provider
    if _provider is not None or os.environ.get("POLYAD_TRACING_ENABLED", "false").lower() != "true":
        return
    if os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true":
        return
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_TRACES_PROTOCOL", os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
    if protocol != "http/protobuf":
        raise ValueError("Polyad tracing requires OTLP http/protobuf")
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    resource = Resource.create({"service.name": os.environ.get("OTEL_SERVICE_NAME", "polyad-operator")})
    # The SDK reads OTEL_TRACES_SAMPLER, OTEL_RESOURCE_ATTRIBUTES and OTEL_BSP_*.
    # The exporter reads standard OTEL_EXPORTER_OTLP[_TRACES]_* configuration.
    exporter = OTLPSpanExporter()
    provider = TracerProvider(resource=resource, shutdown_on_exit=False)
    provider.add_span_processor(BatchSpanProcessor(exporter))
    _provider = provider


def shutdown_tracing() -> None:
    """
    Drain the batch processor after operator and HTTP workers have stopped.

    Returns:
        None: Repeated shutdown calls are harmless.
    """
    global _provider
    provider, _provider = _provider, None
    if provider is not None:
        provider.shutdown()


@contextmanager
def span(name: str, *, kind: trace.SpanKind = trace.SpanKind.INTERNAL, context: Context | None = None) -> Iterator[trace.Span]:
    """
    Trace an operation, recording error types without exception text or stacks.

    Args:
        name (str): Stable operation name without user-supplied payloads.
        kind (trace.SpanKind): Internal, server or client operation role.
        context (Context | None): Extracted parent context, or the current context.

    Yields:
        trace.Span: Current span for explicit, non-sensitive attributes.
    """
    tracer = (_provider or _noop).get_tracer("polyad")
    with tracer.start_as_current_span(name, context=context, kind=kind, record_exception=False, set_status_on_exception=False) as active:
        try:
            yield active
        except Exception as error:
            active.set_attribute("error.type", type(error).__name__)
            active.set_status(trace.StatusCode.ERROR)
            raise


def traced(  # noqa: UP047 -- pydocstyle cannot parse PEP 695 function type parameters.
    name: str, *, kind: trace.SpanKind = trace.SpanKind.INTERNAL
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    """
    Trace the awaited lifetime of an asynchronous operator operation.

    Args:
        name (str): Stable span name.
        kind (trace.SpanKind): Span role within the trace.

    Returns:
        Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]: Decorator preserving arguments and results.
    """

    def decorate(function: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @wraps(function)
        async def invoke(*args: P.args, **kwargs: P.kwargs) -> R:
            with span(name, kind=kind):
                return await function(*args, **kwargs)

        return invoke

    return decorate
