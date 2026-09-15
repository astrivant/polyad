"""
Check the built image's installed dependencies, permissions and source layout.

Run through the image's Python interpreter, passing development or production.
"""

from __future__ import annotations

import importlib.metadata
import importlib.util
import os
import sys
from pathlib import Path

import polyad.operator.runtime


def main() -> None:
    """
    Fail when a container profile leaks tools or cannot import the operator runtime.

    Returns:
        None: Raises on an invalid image and prints the checked profile on success.
    """
    profile = sys.argv[1]
    assert profile in {"development", "production"}
    assert os.getuid() == os.getgid() == 65532
    assert sys.prefix == "/opt/venv"
    for dependency in ("kopf", "kubernetes", "attrs", "cattrs", "redis", "networkx", "flask", "numpy", "waitress"):
        assert importlib.metadata.version(dependency)
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
    print(f"{profile}: non-root runtime, dependencies and source layout verified")


if __name__ == "__main__":
    main()
