"""
Share OpenTelemetry traces and metrics across adaptation and process lifecycles.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from typing import TYPE_CHECKING

from opentelemetry import metrics, trace
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

if TYPE_CHECKING:
    from collections.abc import Iterator, Mapping

    from opentelemetry.context import Context
    from opentelemetry.sdk.metrics import MeterProvider
    from opentelemetry.sdk.trace import TracerProvider

    from polyad_sdk.runtime.context import WorkloadContext


class Telemetry:
    """
    Collect application metrics and trace SDK operations through shared providers.

    Construction uses application-installed providers unless explicit providers
    are supplied. Exporters and their threads are created only by otlp(). Owned
    providers are closed explicitly; injected and global providers remain owned
    by the application. Built-in records exclude payloads, argv and environment.

    Attributes:
        tracer (trace.Tracer): Application and SDK operation tracer.
        meter (metrics.Meter): Factory for application counters, histograms and gauges.
        processes (metrics.UpDownCounter): Owned workers, including startup, drain and cleanup.
    """

    tracer: trace.Tracer
    meter: metrics.Meter
    processes: metrics.UpDownCounter

    def __init__(
        self,
        *,
        tracer_provider: trace.TracerProvider | None = None,
        meter_provider: metrics.MeterProvider | None = None,
    ) -> None:
        """
        Reuse the application's providers without installing global state.

        Args:
            tracer_provider (trace.TracerProvider | None): Explicit provider; None uses the global API.
            meter_provider (metrics.MeterProvider | None): Explicit provider; None uses the global API.
        """
        self.tracer = trace.get_tracer("polyad_sdk", tracer_provider=tracer_provider)
        self.meter = metrics.get_meter("polyad_sdk", meter_provider=meter_provider)
        self._operations = self.meter.create_counter("polyad.sdk.operations", unit="{operation}")
        self._duration = self.meter.create_histogram("polyad.sdk.operation.duration", unit="s")
        self.processes = self.meter.create_up_down_counter("polyad.sdk.processes", unit="{process}")
        self._owned: list[TracerProvider | MeterProvider] = []

    @classmethod
    def otlp(
        cls,
        *,
        service_name: str,
        traces: bool = True,
        collect_metrics: bool = True,
        context: WorkloadContext | None = None,
    ) -> Telemetry:
        """
        Start explicitly owned OTLP HTTP exporters using standard OTEL environment configuration.

        Args:
            service_name (str): Stable application service name.
            traces (bool): Enable batched trace export.
            collect_metrics (bool): Enable periodic metric export.
            context (WorkloadContext | None): Optional projected workload and Pod resource identity.

        Returns:
            Telemetry: Configured telemetry; close it after services and children stop.

        Raises:
            ValueError: The name is empty or an enabled exporter selects an unsupported protocol.
        """
        if type(traces) is not bool or type(collect_metrics) is not bool:
            raise ValueError("traces and collect_metrics must be booleans")
        if not isinstance(service_name, str) or not service_name.strip():
            raise ValueError("service_name must not be empty")
        if os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true":
            traces = collect_metrics = False
        for signal, enabled in (("TRACES", traces), ("METRICS", collect_metrics)):
            protocol = os.environ.get(
                f"OTEL_EXPORTER_OTLP_{signal}_PROTOCOL", os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf")
            )
            if enabled and protocol != "http/protobuf":
                raise ValueError("SDK exporters require OTLP http/protobuf")
        tracer: trace.TracerProvider = trace.NoOpTracerProvider()
        meter: metrics.MeterProvider = metrics.NoOpMeterProvider()
        owned: list[TracerProvider | MeterProvider] = []
        try:
            if traces or collect_metrics:
                from opentelemetry.sdk.resources import Resource

                attributes: dict[str, str | int] = {"service.name": service_name, "process.pid": os.getpid()}
                if context is not None:
                    for key, value in (
                        ("k8s.pod.name", context.pod.name),
                        ("k8s.pod.uid", context.pod.uid),
                        ("k8s.namespace.name", context.pod.namespace),
                        ("k8s.node.name", context.pod.node_name),
                        ("k8s.cluster.name", context.pod.cluster),
                        ("polyad.graph.name", context.identity.graph),
                        ("polyad.node.name", context.identity.node),
                    ):
                        if value:
                            attributes[key] = value
                resource = Resource.create(attributes)
            if traces:
                from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
                from opentelemetry.sdk.trace import TracerProvider
                from opentelemetry.sdk.trace.export import BatchSpanProcessor

                tracer = TracerProvider(resource=resource, shutdown_on_exit=False)
                owned.append(tracer)
                tracer.add_span_processor(BatchSpanProcessor(OTLPSpanExporter()))
            if collect_metrics:
                from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
                from opentelemetry.sdk.metrics import MeterProvider
                from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader

                reader = PeriodicExportingMetricReader(OTLPMetricExporter())
                try:
                    meter = MeterProvider(resource=resource, metric_readers=[reader], shutdown_on_exit=False)
                except BaseException:
                    reader.shutdown()
                    raise
                owned.append(meter)
            telemetry = cls(tracer_provider=tracer, meter_provider=meter)
            telemetry._owned = owned
            return telemetry
        except BaseException:
            for provider in reversed(owned):
                provider.shutdown()
            raise

    @contextmanager
    def operation(
        self, name: str, *, attributes: Mapping[str, str | int | bool] | None = None, parent: Context | None = None
    ) -> Iterator[trace.Span]:
        """
        Record operation attempts, elapsed seconds and error types without exception payloads.

        Args:
            name (str): Stable, low-cardinality operation name.
            attributes (Mapping[str, str | int | bool] | None): Non-sensitive dimensions shared by spans and metrics.
            parent (Context | None): Explicit causal context carried from another thread; None uses the current span.

        Yields:
            trace.Span: Active span for additional explicitly chosen attributes.
        """
        labels = dict(attributes or {})
        labels["operation"] = name
        started, outcome = time.monotonic(), "success"
        with self.tracer.start_as_current_span(
            name, attributes=labels, context=parent, record_exception=False, set_status_on_exception=False
        ) as span:
            try:
                yield span
            except BaseException as error:
                outcome = "error"
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(trace.StatusCode.ERROR)
                raise
            finally:
                labels["outcome"] = outcome
                self._operations.add(1, labels)
                self._duration.record(time.monotonic() - started, labels)

    def propagation_environment(self) -> dict[str, str]:
        """
        Produce a W3C trace carrier for a newly constructed child process.

        Returns:
            dict[str, str]: TRACEPARENT and TRACESTATE when a current trace is valid; no baggage or credentials.
        """
        carrier: dict[str, str] = {}
        TraceContextTextMapPropagator().inject(carrier)
        return {name.upper(): value for name, value in carrier.items()}

    @staticmethod
    def parent_context(environ: Mapping[str, str] | None = None) -> Context:
        """
        Extract an explicitly propagated parent in the child's startup code.

        Args:
            environ (Mapping[str, str] | None): Child environment; None reads os.environ.

        Returns:
            Context: Extracted W3C parent context for the child's first span.
        """
        environment = os.environ if environ is None else environ
        return TraceContextTextMapPropagator().extract(
            {key.lower(): environment[key] for key in ("TRACEPARENT", "TRACESTATE") if key in environment}
        )

    def close(self) -> None:
        """
        Flush and shut down only providers constructed by otlp(), once.

        Returns:
            None: Repeated calls are harmless; application-owned providers remain active.
        """
        owned, self._owned = self._owned, []
        try:
            for provider in owned:
                provider.force_flush()
        finally:
            for provider in reversed(owned):
                provider.shutdown()
