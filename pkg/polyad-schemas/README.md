# Polyad schemas

Generated JSON Schemas and typed loaders for Polyad's shared models, Kubernetes
resources, event streams and Helm values. Python 3.11–3.14 is supported.
This package has no runtime dependencies and does not install the operator,
client, shared Python types or a JSON Schema validator.

## Table of contents

- [Installation](#installation)
- [Modules](#modules)
- [Example](#example)
- [Sources and releases](#sources-and-releases)

## Installation

```sh
pip install polyad-schemas
```

To install the operator with schema support:

```sh
pip install 'polyad[schemas]'
```

From a checkout, use `pip install ./pkg/polyad-schemas`, or
`poetry install --all-extras` when developing the complete project.

## Modules

| Module | Loader | Artifacts |
| --- | --- | --- |
| `polyad_schemas.models` | `schema_for(Model)` or `schema_for("fully.qualified.Model")` | Shared model definitions |
| `polyad_schemas.resources` | `resource_schema(kind, version, group=...)` | Polyad and pinned integration CRDs, with upstream license notices |
| `polyad_schemas.events` | `event_schema()` | Event envelopes, typed payload shapes and stream settings |
| `polyad_schemas.helm` | `values_schema(partial=False, chart="polyad")` | Merged chart values or partial administrator overlays |

The package root also exports these helpers, `available_schemas()` and
`load_schema(name)`. Each call returns an independent dictionary. References
resolve inside the returned document without network access. Ordinary JSON
artifacts live beside each submodule and are available through
`importlib.resources.files("polyad_schemas.resources")`, for example.

## Example

```python
from polyad_schemas.events import event_schema
from polyad_schemas.helm import values_schema
from polyad_schemas.models import schema_for
from polyad_schemas.resources import resource_schema

request = schema_for("polyad_types.api.requests.ConnectionRequest")
graph = resource_schema("Graph")
istio = resource_schema("Gateway", "v1", group="networking.istio.io")
events = event_schema()
overlay = values_schema(partial=True)
```

Install `jsonschema` separately to validate documents, or use any validator for
the artifact's declared dialect. Models, manifests and events use Draft 2020-12;
Helm contracts use Draft 7. Constructor rules, Kubernetes CEL and live operator
admission checks remain separate. See the
[schema guide](https://github.com/astrivant/polyad/blob/main/docs/apis/json-schemas.md).

## Sources and releases

The [central source catalog](https://github.com/astrivant/polyad/blob/main/schemas/README.md)
and shared models generate both this package and the chart validation schemas.
Run `poetry run python scripts/schemas/generate-all.py` from the repository root;
`--check` detects drift offline. Do not edit generated artifacts.

Release CI builds and tests the standalone wheel on each supported Python
version, ships schemas and license notices in wheels and source distributions,
and publishes this package before the operator's matching `schemas` extra.
See [publishing commands](https://github.com/astrivant/polyad/blob/main/docs/development/toolchain.md#manual-pypi-publishing).

Select `chart="polyad-crds"` to load the named-resource chart contract, including
resource maps and TPL expressions. `partial=True` selects its overlay schema.
