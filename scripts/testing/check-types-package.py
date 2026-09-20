"""
Check a standalone installed types distribution in an isolated consumer environment.
"""

from __future__ import annotations

import importlib
import importlib.util
import pkgutil
from pathlib import Path

import polyad_types
from polyad_types import (
    Cheeger,
    Graph,
    ObjectMeta,
    from_dict,
    from_document,
    to_dict,
    to_document,
)


def main() -> None:
    """
    Require a marked installed package, working codecs and no operator dependencies.

    Returns:
        None: Invalid installation boundaries raise an assertion.
    """
    package = Path(polyad_types.__file__).parent
    assert "site-packages" in package.parts, package
    assert (package / "py.typed").is_file()
    for namespace, names in {
        "api": ("APIKey", "ServiceEndpoint", "CompositionRequest", "ThroughputSample"),
        "graphs": ("Cheeger", "Topology", "Replication", "CapacityPlan"),
        "networking": ("NetworkPort", "NetworkAccess", "TrafficRoute"),
        "events": ("Event", "EventAST", "decode_event", "EventStreamSettings"),
        "exceptions": ("EventTooLarge",),
        "exceptions.events": ("EventTooLarge",),
        "resources": ("Graph", "Daemon", "Deployment", "ObjectMeta"),
    }.items():
        module = importlib.import_module(f"polyad_types.{namespace}")
        assert all(getattr(module, name) is getattr(polyad_types, name) for name in names)
    modules = ("polyad_types", *(info.name for info in pkgutil.walk_packages(polyad_types.__path__, "polyad_types.")))
    for name in modules:
        module = importlib.import_module(name)
        public: dict[str, object] = {}
        exec(f"from {name} import *", public)
        assert set(public) - {"__builtins__"} == set(module.__all__)
    for name in ("polyad", "polyad_sdk", "kopf", "kubernetes", "redis", "flask", "numpy", "networkx"):
        assert importlib.util.find_spec(name) is None, name
    rule = from_dict({"minimum": 1}, Cheeger)
    assert to_dict(rule) == {"minimum": 1.0, "maximum": None}
    graph = Graph(metadata=ObjectMeta(name="pipeline"), spec={"nodes": []})
    assert from_document(to_document(graph)) == graph
    assert importlib.util.find_spec("polyad_schemas") is None
    assert not list(package.rglob("*.schema.json"))
    print("Standalone types package, imports and serialization passed")


if __name__ == "__main__":
    main()
