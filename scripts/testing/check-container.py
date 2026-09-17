"""
Check the built image's installed dependencies, permissions and source layout.

Run under the image's Tini process, passing development or production to Python.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import signal
import sys
import time
from pathlib import Path

import polyad.operator.runtime
from polyad.sql import statement


def main() -> None:
    """
    Fail when a container profile leaks tools or cannot import the operator runtime.

    Returns:
        None: Raises on an invalid image and prints the checked profile on success.
    """
    profile = sys.argv[1]
    assert profile in {"development", "production"}
    assert os.getuid() == os.getgid() == 65532
    assert os.getppid() == 1
    assert Path("/proc/1/comm").read_text().strip() == "tini"
    forwarded: list[int] = []
    previous = signal.signal(signal.SIGTERM, lambda signum, frame: forwarded.append(signum))
    try:
        os.kill(1, signal.SIGTERM)
        deadline = time.monotonic() + 5
        while not forwarded and time.monotonic() < deadline:
            time.sleep(0.01)
        assert forwarded == [signal.SIGTERM], "Tini did not forward SIGTERM to Python"
    finally:
        signal.signal(signal.SIGTERM, previous)
    assert sys.prefix == "/opt/venv"
    assert "CREATE TABLE" in statement("state/schema.sql")
    assert "CREATE TABLE" in statement("authentication/schema.sql")
    for dependency in (
        "kopf",
        "kubernetes",
        "attrs",
        "cattrs",
        "redis",
        "networkx",
        "flask",
        "numpy",
        "waitress",
        "psycopg",
        "psycopg-binary",
        "psycopg-pool",
        "flask-httpauth",
        "flask-limiter",
        "prometheus-client",
        "apispec",
        "opentelemetry-api",
        "opentelemetry-sdk",
        "opentelemetry-exporter-otlp-proto-http",
        "polyad-types",
    ):
        assert importlib.metadata.version(dependency)
    # Runtime dependencies are installed, but disabled optional services stay unloaded.
    from polyad.operator.lifecycle import handlers  # noqa: F401

    for module in (
        "flask",
        "flask_httpauth",
        "flask_limiter",
        "psycopg",
        "prometheus_client",
        "opentelemetry.sdk",
        "opentelemetry.exporter",
    ):
        assert module not in sys.modules, f"startup eagerly imported {module}"
    source = Path(polyad.operator.runtime.__file__)
    if profile == "production":
        assert source.is_relative_to("/opt/venv")
        assert not os.access("/opt/venv", os.W_OK)
        assert not Path("/app/pkg").exists()
        assert not Path("/opt/poetry").exists()
        for tool in ("pytest", "ruff", "mypy", "matplotlib"):
            assert importlib.util.find_spec(tool) is None
    else:
        assert source.is_relative_to("/app/pkg")
        assert importlib.metadata.version("pytest")
        assert importlib.metadata.version("ruff")
        assert Path("/opt/poetry/bin/poetry").is_file()
    print(f"{profile}: Tini PID 1, signal forwarding, non-root runtime, dependencies and source layout verified")


if __name__ == "__main__":
    main()
