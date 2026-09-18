"""
Check the installed application SDK without operator or web-server dependencies.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path

import polyad_sdk
from polyad_sdk import AdaptiveService, Client, Delta, Settings
from polyad_types import ServiceEndpoint


def main() -> None:
    """
    Import every SDK module and construct the application interface in an isolated environment.

    Returns:
        None: Missing wheel contents or heavyweight dependencies raise an assertion.
    """
    package = Path(polyad_sdk.__file__).parent
    assert "site-packages" in package.parts, package
    assert (package / "py.typed").is_file()
    for module in pkgutil.walk_packages(polyad_sdk.__path__, "polyad_sdk."):
        importlib.import_module(module.name)
    for name in ("polyad", "kopf", "kubernetes", "redis", "flask", "numpy", "networkx"):
        assert importlib.util.find_spec(name) is None, name
    service = AdaptiveService(
        ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "source"),
        Client("http://localhost:8091", "reader"),
        settings=Settings(),
    )
    assert not service.view.available and service.view.candidates == ()
    assert Delta(("resources", "pods"), "changed", 2, 3).difference == 1
    print("Standalone SDK package, imports and adaptive interface passed")


if __name__ == "__main__":
    main()
