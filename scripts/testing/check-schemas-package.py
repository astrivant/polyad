"""
Verify a standalone schema distribution without operator, types or validator dependencies.
"""

from __future__ import annotations

import importlib.util
import pkgutil
from importlib.metadata import requires
from importlib.resources import files
from pathlib import Path

import polyad_schemas
from polyad_schemas import available_schemas, load_schema
from polyad_schemas.events import event_schema
from polyad_schemas.helm import values_schema
from polyad_schemas.models import schema_for
from polyad_schemas.resources import resource_schema


def main() -> None:
    """
    Check categorized artifacts, license notices and dependency isolation in an installed wheel.

    Returns:
        None: Invalid distributions raise an assertion.
    """
    package = Path(polyad_schemas.__file__).parent
    assert "site-packages" in package.parts, package
    assert (package / "py.typed").is_file()
    assert not requires("polyad-schemas")
    assert importlib.import_module("polyad_schemas.exceptions").__all__ == ()
    modules = ("polyad_schemas", *(info.name for info in pkgutil.walk_packages(polyad_schemas.__path__, "polyad_schemas.")))
    for name in modules:
        module = importlib.import_module(name)
        public: dict[str, object] = {}
        exec(f"from {name} import *", public)
        assert set(public) - {"__builtins__"} == set(module.__all__)
    for name in ("polyad", "polyad_types", "polyad_sdk", "jsonschema", "attrs", "yaml"):
        assert importlib.util.find_spec(name) is None, name
    for name in available_schemas():
        assert "$schema" in load_schema(name), name
    assert schema_for("polyad_types.graphs.policies.Cheeger")["$ref"].endswith(".Cheeger")
    assert resource_schema("Graph")["properties"]["kind"]["const"] == "Graph"
    assert resource_schema("Gateway", "v1", group="networking.istio.io")["properties"]["kind"]["const"] == "Gateway"
    assert "TopologyEvent" in event_schema()["$defs"]
    assert values_schema(partial=True)["properties"]["ha"]["$ref"]
    for name in ("gateway-api", "istio", "keda", "external-secrets", "dragonfly-operator"):
        assert "Apache License" in files("polyad_schemas.resources").joinpath(f"{name}-LICENSE").read_text()
    print("Standalone schema modules, artifacts, licenses and dependency isolation passed")


if __name__ == "__main__":
    main()
