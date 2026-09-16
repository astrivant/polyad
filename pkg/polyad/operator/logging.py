"""
Configure consistent Python diagnostics for operator and observer entrypoints.
"""

from __future__ import annotations

import logging
import os
import time
from typing import TYPE_CHECKING

from opentelemetry import trace

if TYPE_CHECKING:
    import argparse

    from opentelemetry.sdk._logs import LoggerProvider

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL")
_provider: LoggerProvider | None = None
_handler: TelemetryHandler | None = None


class DecisionFormatter(logging.Formatter):
    """
    Keep console messages readable and attach decision identity and current trace context.

    Attributes:
        converter: Convert console timestamps to UTC.
    """

    converter = staticmethod(time.gmtime)

    def format(self, record: logging.LogRecord) -> str:
        """
        Format one line without modifying the record shared with other handlers.

        Args:
            record (logging.LogRecord): Standard Python record with optional decision attributes.

        Returns:
            str: UTC text with attributes and valid W3C trace identifiers when available.
        """
        fields = dict(getattr(record, "polyad_attributes", {}))
        if event := getattr(record, "event_name", None):
            fields = {"event": event, **fields}
        context = trace.get_current_span().get_span_context()
        if context.is_valid:
            fields.update(trace_id=f"{context.trace_id:032x}", span_id=f"{context.span_id:016x}", trace_flags=f"{context.trace_flags:02x}")
        suffix = " ".join(f"{key}={value}" for key, value in fields.items())
        return super().format(record) + (f" | {suffix}" if suffix else "")


class TelemetryHandler(logging.Handler):
    """
    Bridge Python records to the OTel log data model without exporting exception payloads.
    """

    def __init__(self, provider: LoggerProvider) -> None:
        """
        Bind the process-owned SDK provider without replacing global logging providers.

        Args:
            provider (LoggerProvider): Configured logger provider with a bounded batch processor.
        """
        super().__init__()
        self.provider = provider

    def emit(self, record: logging.LogRecord) -> None:
        """
        Export the message body, severity, event name, attributes and active trace context.

        Args:
            record (logging.LogRecord): Polyad Python log record.

        Returns:
            None: Exporter failures never change the control decision.
        """
        from opentelemetry._logs import SeverityNumber

        attributes = dict(getattr(record, "polyad_attributes", {}))
        if record.exc_info and record.exc_info[0]:
            attributes["exception.type"] = record.exc_info[0].__name__
        try:
            self.provider.get_logger(record.name).emit(
                timestamp=int(record.created * 1e9),
                observed_timestamp=time.time_ns(),
                severity_number=SeverityNumber(
                    next((number for level, number in ((50, 21), (40, 17), (30, 13), (20, 9), (10, 5)) if record.levelno >= level), 0)
                ),
                severity_text={"WARNING": "WARN", "CRITICAL": "FATAL"}.get(record.levelname, record.levelname),
                body=record.getMessage(),
                attributes=attributes,
                event_name=getattr(record, "event_name", None),
            )
        except Exception:
            # Logging must neither reject admitted work nor recursively log exporter failures.
            self.handleError(record)


def configure_log_export() -> None:
    """
    Start optional OTLP logs independently of trace sampling and trace enablement.

    Returns:
        None: Disabled processes import no log SDK or exporter and create no log export thread.
    """
    global _provider, _handler
    if _provider is not None or os.environ.get("POLYAD_LOGS_ENABLED", "false").lower() != "true":
        return
    if os.environ.get("OTEL_SDK_DISABLED", "false").lower() == "true":
        return
    protocol = os.environ.get("OTEL_EXPORTER_OTLP_LOGS_PROTOCOL", os.environ.get("OTEL_EXPORTER_OTLP_PROTOCOL", "http/protobuf"))
    if protocol != "http/protobuf":
        raise ValueError("Polyad log export requires OTLP http/protobuf")
    from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter
    from opentelemetry.sdk._logs import LoggerProvider
    from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

    from polyad.operator.tracing import telemetry_resource

    provider = LoggerProvider(resource=telemetry_resource(), shutdown_on_exit=False)
    provider.add_log_record_processor(BatchLogRecordProcessor(OTLPLogExporter()))
    _provider, _handler = provider, TelemetryHandler(provider)
    logging.getLogger("polyad").addHandler(_handler)


def shutdown_log_export() -> None:
    """
    Detach and drain the owned handler after reconciliation and HTTP threads stop.

    Returns:
        None: Repeated shutdown calls are harmless.
    """
    global _provider, _handler
    provider, handler = _provider, _handler
    _provider, _handler = None, None
    if handler is not None:
        logging.getLogger("polyad").removeHandler(handler)
        handler.close()
    if provider is not None:
        provider.shutdown()


def add_logging_options(parser: argparse.ArgumentParser) -> None:
    """
    Expose a debug shorthand and an explicit level with environment fallback.

    Args:
        parser (argparse.ArgumentParser): Entrypoint argument parser.

    Returns:
        None: Mutually exclusive CLI options are registered on the parser.
    """
    options = parser.add_mutually_exclusive_group()
    options.add_argument(
        "--log-level",
        type=str.upper,
        choices=LEVELS,
        default=os.environ.get("POLYAD_LOG_LEVEL", "INFO"),
        help="Polyad log verbosity (default: POLYAD_LOG_LEVEL or INFO)",
    )
    options.add_argument("--debug", dest="log_level", action="store_const", const="DEBUG", help="Enable Polyad debug logging")


def configure_logging(parser: argparse.ArgumentParser, level: str) -> None:
    """
    Enable Polyad diagnostics without turning on dependency transport payload logging.

    Args:
        parser (argparse.ArgumentParser): Parser used to report invalid environment defaults.
        level (str): CLI-selected or environment-provided log level.

    Returns:
        None: Python logging is initialized before the process starts its workers.
    """
    if level not in LEVELS:
        parser.error("POLYAD_LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
    handler = logging.StreamHandler()
    handler.setFormatter(
        DecisionFormatter("%(asctime)sZ %(levelname)s [%(threadName)s] %(name)s: %(message)s", datefmt="%Y-%m-%dT%H:%M:%S")
    )
    logging.basicConfig(level=logging.INFO, handlers=[handler])
    logging.getLogger("polyad").setLevel(level)
