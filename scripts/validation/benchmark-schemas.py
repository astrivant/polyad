"""
Prepare disposable validation schemas directly from the locked monitoring chart's CRDs.
"""

from __future__ import annotations

import json
import tarfile
from pathlib import Path
from typing import TYPE_CHECKING

import yaml

if TYPE_CHECKING:
    from typing import Any

ROOT = Path(__file__).resolve().parents[2]


class CRDLoader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):
    """
    Read upstream enums containing the YAML 1.1 value marker as ordinary strings.
    """


CRDLoader.add_constructor("tag:yaml.org,2002:value", CRDLoader.construct_scalar)


def normalize(value: Any) -> Any:
    """
    Translate OpenAPI nullable and numeric bounds for the Draft 7 validator.

    Args:
        value (Any): CRD schema subtree from the dependency archive.

    Returns:
        Any: JSON Schema retaining all Kubernetes validation annotations.
    """
    if isinstance(value, list):
        return [normalize(item) for item in value]
    if not isinstance(value, dict):
        return value
    result = {key: normalize(item) for key, item in value.items() if key != "nullable"}
    for bound in ("Minimum", "Maximum"):
        key = "exclusive" + bound
        if isinstance(result.get(key), bool):
            if result.pop(key):
                result[key] = result.pop(bound.lower())
    if result.get("x-kubernetes-int-or-string") and "anyOf" not in result:
        result["anyOf"] = [{"type": "integer"}, {"type": "string"}]
    return {"anyOf": [result, {"type": "null"}]} if value.get("nullable") else result


def main() -> None:
    """
    Derive local validator inputs without maintaining a second copy of dependency CRDs.

    Returns:
        None: Write schemas under the ignored cache, using only already-built dependencies.
    """
    chart = ROOT / "charts/polyad-benchmarks"
    metadata = yaml.safe_load((chart / "Chart.lock").read_text())
    version = next(item["version"] for item in metadata["dependencies"] if item["name"] == "kube-prometheus-stack")
    output = ROOT / ".cache/benchmarks/schemas"
    output.mkdir(parents=True, exist_ok=True)
    schemas = {}
    with tarfile.open(chart / "charts" / f"kube-prometheus-stack-{version}.tgz") as archive:
        for member in archive.getmembers():
            if "/charts/crds/crds/" not in member.name or not member.name.endswith(".yaml") or not member.isfile():
                continue
            stream = archive.extractfile(member)
            assert stream is not None
            for resource in yaml.load_all(stream, Loader=CRDLoader):
                if not resource or resource.get("kind") != "CustomResourceDefinition":
                    continue
                spec = resource["spec"]
                for api in spec["versions"]:
                    if not api["served"]:
                        continue
                    schema = normalize(api["schema"]["openAPIV3Schema"])
                    schema["$schema"] = "http://json-schema.org/draft-07/schema#"
                    properties = schema.setdefault("properties", {})
                    properties["apiVersion"] = {"const": f"{spec['group']}/{api['name']}"}
                    properties["kind"] = {"const": spec["names"]["kind"]}
                    properties.setdefault("metadata", {"type": "object"})
                    filename = f"{spec['names']['kind'].lower()}-{spec['group'].split('.')[0]}-{api['name']}.json"
                    schemas[filename] = schema
    if not schemas:
        raise ValueError("locked monitoring dependency contains no served CRD schemas")
    for path in output.glob("*.json"):
        if path.name not in schemas:
            path.unlink()
    for name, schema in schemas.items():
        (output / name).write_text(json.dumps(schema) + "\n")
    print(f"Prepared {len(schemas)} schemas from kube-prometheus-stack {version}")


if __name__ == "__main__":
    main()
