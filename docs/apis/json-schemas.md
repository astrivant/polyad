# Importable JSON Schemas

`polyad-types` ships JSON Schemas alongside the models shared by the operator and
client. Applications, editors and build tools can load the same versioned
contracts without installing the operator or contacting a cluster. The JSON files
are included in both wheels and source distributions.

## Table of contents

- [Install and import](#install-and-import)
- [Choose a contract](#choose-a-contract)
- [Validate documents](#validate-documents)
- [Use the files directly](#use-the-files-directly)
- [Validation boundaries](#validation-boundaries)
- [Regenerate artifacts](#regenerate-artifacts)

## Install and import

```sh
pip install polyad-types
```

```python
from polyad_types import ConnectionRequest
from polyad_types.schemas import (
    available_schemas, load_schema, resource_schema, schema_for,
)

request_schema = schema_for(ConnectionRequest)
graph_schema = resource_schema("Graph")
events_schema = load_schema("events")
overlay_schema = load_schema("helm-reference")
print(available_schemas())
```

The four helpers are also exported from `polyad_types`. Each load returns an
independent dictionary. All `$ref` references resolve within that returned
document, including those in the Helm overlay schema. An `$id` identifies a
schema; loading it does not fetch that URL. See the JSON Schema guide to
[identifiers and local references](https://json-schema.org/understanding-json-schema/structuring).

## Choose a contract

| API | Contract | Dialect |
| --- | --- | --- |
| `schema_for(Model)` | Serialized shape of a shared attrs model: requests, configuration, topology, traffic, authentication policy, observations, resource envelopes or mutation plans | Draft 2020-12 |
| `resource_schema("Graph")` | Complete Graph manifest fields from the chart's CRD; the same API supports all Polyad-owned kinds | Draft 2020-12 |
| `load_schema("events")` | Discriminated event envelopes and their payload trees; equivalent to `event_schema()` | Draft 2020-12 |
| `load_schema("helm-values")` | Complete values after merging chart defaults and administrator overrides | Draft 7 |
| `load_schema("helm-reference")` | Partial administrator overrides; supplied replacement array entries still require their complete fields | Draft 7 |
| `load_schema("models")` | Definition library used by `schema_for`; select a `$defs` entry before validating an instance | Draft 2020-12 |

Resource artifacts use lowercase kind and version names, such as
`graph.v1alpha1`, `polygraph.v1alpha1`, `replicagroup.v1alpha1` and
`temporaryconnection.v1alpha1`. `resource_schema(kind, version="v1alpha1")`
selects one. Third-party CRDs such as Dragonfly's upstream CRD are outside this
catalog. `available_schemas()` lists the exact artifacts in the installed release.

Model definitions use fully qualified Python names. This distinguishes the
`polyad_types.resources.Graph` resource envelope from topology configuration,
and the resource `PolyGraph` from `polyad_types.topology.PolyGraph`.
Generic model classes use their declared type-variable bounds; schemas for
application-defined subclasses or generic specializations are not generated.

## Validate documents

Schema loading adds no validator dependency. Applications that want local
validation can install `jsonschema` separately:

```sh
pip install jsonschema
```

```python
from jsonschema import validate
from polyad_types import ConnectionRequest, NetworkPort, to_dict
from polyad_types.schemas import load_schema, resource_schema, schema_for

request = ConnectionRequest(
    requestId="edge-1", namespace="workloads", kind="Graph",
    graph="pipeline", graphUid="graph-uid", source="ingest", target="process",
    ttlSeconds=60, ports=(NetworkPort(8080),),
)
validate(to_dict(request), schema_for(ConnectionRequest))

manifest = {
    "apiVersion": "polyad.astrivant.com/v1alpha1",
    "kind": "Graph",
    "metadata": {"name": "pipeline"},
    "spec": {"nodes": []},
}
validate(manifest, resource_schema("Graph"))
validate({"ha": True, "events": {"maxEventBytes": 2097152}}, load_schema("helm-reference"))
```

For event envelope validation, typed decoding and transport budgets, see
[event syntax trees, schemas and limits](event-contract.md). For configuration
and resource serialization, see the [types package](../../pkg/polyad-types/README.md).

## Use the files directly

The `polyad_types.schemas` package contains ordinary `*.schema.json` resources.
Use `importlib.resources` so code works with both installed directories and
zipped distributions:

```python
from importlib.resources import as_file, files

artifact = files("polyad_types.schemas").joinpath("graph.v1alpha1.schema.json")
with as_file(artifact) as path:
    # Pass this path to an editor or tool while the context remains open.
    print(path.read_text(encoding="utf-8"))
```

To keep an exported schema for a non-Python consumer, write the loaded document:

```python
import json
from pathlib import Path
from polyad_types.schemas import resource_schema

Path("graph.schema.json").write_text(
    json.dumps(resource_schema("Graph"), indent=2) + "\n", encoding="utf-8",
)
```

## Validation boundaries

Model schemas describe field types, required constructor arguments, enums and
explicit `schema` metadata such as byte, port, replica and computation limits.
They describe the serialized object: native AST extension fields are flattened,
and resource envelopes contain `apiVersion` and `kind`. Configuration models
reject extra fields; native resource ASTs preserve unmodeled fields.

Resource envelopes intentionally accept arbitrary `spec` dictionaries. Use
`resource_schema("Graph")` to check the complete manifest contract, rather than
`schema_for(Graph)`. The CRD conversion translates nullable fields and exclusive
bounds to JSON Schema while retaining `x-kubernetes-*` annotations. A standard
JSON Schema validator does not execute Kubernetes CEL rules, defaulting,
pruning, admission webhooks or live graph checks.

Python constructor checks, including relationships between fields, still apply
when decoding with `from_dict`. The operator still checks authorization,
connection consent, graph references, Cheeger bounds and current cluster state.
Helm validates the fully merged values against its canonical chart schema;
validating a partial overlay does not establish that the eventual merged
configuration satisfies every feature dependency.

Use the schema artifacts from the same release as the target operator and chart.
The installed package pins the document content; an identifier containing
`main` is not a request to replace it with the latest repository version.

## Regenerate artifacts

Models and chart contracts remain the sources of truth. From a development
checkout, regenerate their existing derived schemas before packaging:

```sh
poetry run python scripts/schemas/generate-status-schemas.py
poetry run python scripts/schemas/generate-network-schemas.py
poetry run python scripts/schemas/generate-reference-schema.py
poetry run python scripts/schemas/generate-event-schemas.py
poetry run python scripts/schemas/generate-json-schemas.py
```

The last command emits the shared model library, served versions of all
Polyad-owned CRDs and self-contained Helm schemas. `--check` reports drift
without editing files. The `packaged-json-schemas` pre-commit hook and CI keep
those artifacts synchronized; standalone wheel checks verify that consumers can
import them without operator dependencies. See the
[development toolchain](../development/toolchain.md) for release commands.
