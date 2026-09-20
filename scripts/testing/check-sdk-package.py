"""
Check the installed application SDK without operator or web-server dependencies.
"""

from __future__ import annotations

import importlib
import importlib.util
import inspect
import pkgutil
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import polyad_sdk
from polyad_sdk import (
    AdaptationStrategy,
    AdaptiveService,
    Client,
    ConnectionNegotiator,
    ConstraintStrategy,
    Delta,
    EventSource,
    FreshnessStrategy,
    Settings,
    ThroughputReporter,
    WorkloadContext,
    env,
)
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
    assert "polyad_sdk.transport.websocket" not in sys.modules
    assert "opentelemetry.sdk" not in sys.modules
    assert "opentelemetry.exporter" not in sys.modules
    for namespace, names in {
        "api": ("Client", "APIError", "ConnectionNegotiator", "ThroughputReporter"),
        "events": ("EventSource", "Filter", "Subscription", "StreamInterrupted", "Event"),
        "exceptions": ("APIError", "StreamInterrupted"),
        "exceptions.api": ("APIError",),
        "exceptions.events": ("StreamInterrupted",),
        "runtime": ("env", "refresh_environment", "WorkloadContext", "PodContext", "ContainerResources"),
        "observability": ("Telemetry",),
        "processes": ("ProcessSpec", "ProcessPlan", "ProcessSupervisor", "ManagedProcess", "PlanResult"),
        "symbiosis": ("AdaptiveService", "Change", "Delta", "Environment", "Settings"),
        "connections": ("WorkloadClient", "WorkloadEndpoint"),
        "symbiosis.strategies": (
            "AdaptationStrategy",
            "ConstraintStrategy",
            "FreshnessStrategy",
            "PeerAvailabilityStrategy",
            "ConnectionPermissionStrategy",
            "ResourceBudgetStrategy",
            "ContainerBudgetStrategy",
            "DecisionGuardStrategy",
            "ThresholdStrategy",
            "ObserveStrategy",
            "CallbackStrategy",
            "TopologyStrategy",
            "ResourceStrategy",
            "DecisionStrategy",
            "ConstraintAssessment",
        ),
    }.items():
        module = importlib.import_module(f"polyad_sdk.{namespace}")
        assert all(getattr(module, name) is getattr(polyad_sdk, name) for name in names)
    modules = ("polyad_sdk", *(info.name for info in pkgutil.walk_packages(polyad_sdk.__path__, "polyad_sdk.")))
    for name in modules:
        module = importlib.import_module(name)
        public: dict[str, object] = {}
        exec(f"from {name} import *", public)
        assert set(public) - {"__builtins__"} == set(module.__all__)
    for name in ("polyad", "kopf", "kubernetes", "redis", "grpc", "pika", "flask", "numpy", "networkx"):
        assert importlib.util.find_spec(name) is None, name
    assert inspect.isabstract(AdaptiveService)
    assert inspect.isabstract(AdaptationStrategy) and inspect.isabstract(ConstraintStrategy)
    for contract in (EventSource, ThroughputReporter, ConnectionNegotiator):
        assert inspect.isabstract(contract) and issubclass(Client, contract)
    assert not inspect.isabstract(Client)
    service = SmokeService(
        ServiceEndpoint("", "test", "Graph", "pipeline", "uid-pipeline", "source"),
        Client("http://localhost:8091", "reader"),
        strategies=[FreshnessStrategy("admission", lambda result: None)],
        settings=Settings(),
    )
    assert not service.view.available and service.view.candidates == ()
    assert isinstance(env, dict) and isinstance(service.context, WorkloadContext)
    assert len(service.strategies) == 1
    assert Delta(("resources", "pods"), "changed", 2, 3).difference == 1
    print("Standalone SDK package, imports and adaptive interface passed")


if __name__ == "__main__":
    main()
