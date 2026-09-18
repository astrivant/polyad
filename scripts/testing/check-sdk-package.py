"""
Check the installed application SDK without operator or web-server dependencies.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
from pathlib import Path
from typing import TYPE_CHECKING

import polyad_sdk
from polyad_sdk import AdaptiveService, Client, ConnectionNegotiator, Delta, EventSource, Settings, ThroughputReporter
from polyad_types import ServiceEndpoint

if TYPE_CHECKING:
    from polyad_sdk import Change


class SmokeService(AdaptiveService):
    """
    Implement application adaptation using only the standalone SDK's public types.
    """

    def adapt(self, change: Change) -> None:
        """
        Store the latest change as the smoke application's local behavior.

        Args:
            change (Change): Authorized baseline or meaningful delta from the SDK.

        Returns:
            None: The application has accepted the delivered context.
        """
        self.last_change = change


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
    assert inspect.isabstract(AdaptiveService)
    for contract in (EventSource, ThroughputReporter, ConnectionNegotiator):
        assert inspect.isabstract(contract) and issubclass(Client, contract)
    assert not inspect.isabstract(Client)
    service = SmokeService(
        ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "source"),
        Client("http://localhost:8091", "reader"),
        settings=Settings(),
    )
    assert not service.view.available and service.view.candidates == ()
    assert Delta(("resources", "pods"), "changed", 2, 3).difference == 1
    print("Standalone SDK package, imports and adaptive interface passed")


if __name__ == "__main__":
    main()
