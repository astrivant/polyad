# Polyad types

Shared Python 3.11–3.14 resource, configuration and API models for Polyad. This
package provides the same definitions used by the operator and Python client,
including constructor validation, serialization, importable JSON Schemas and a `py.typed` marker.
Its only dependencies are attrs, cattrs and typing-extensions.

## Table of contents

- [Installation](#installation)
- [Example](#example)
- [Public models](#public-models)
- [Serialization](#serialization)
- [JSON Schemas](#json-schemas)
- [Event syntax trees and schemas](#event-syntax-trees-and-schemas)
- [Client integration](#client-integration)
- [Publishing](#publishing)

## Installation

Install the published package:

```sh
pip install polyad-types
```

Import it as `polyad_types`. Installing it does not install `polyad` or
`polyad-client`. From a repository checkout, use `pip install ./pkg/polyad-types`.

## Example

```python
from polyad_types import (
    Cheeger, Connection, Graph, Node, ObjectMeta, StructuralRule, Topology,
    from_dict, to_dict, to_document,
)

layout = Topology(
    nodes=(
        Node(name="source", kind="Workload", ref="source"),
        Node(name="sink", kind="Workload", ref="sink"),
    ),
    connections=(Connection(source="source", target="sink"),),
)
graph = Graph(metadata=ObjectMeta(name="pipeline"), spec=to_dict(layout))
manifest = to_document(graph)
rule = from_dict(
    {"relation": "connections", "cheeger": {"minimum": 0.5}},
    StructuralRule,
)
assert rule.cheeger == Cheeger(minimum=0.5)
```

## Public models

`ServiceEndpoint`, `ServiceConnectionRequest`, `AtlasAccess`, `ServiceAccess` and
`AccessMode` describe [atlas discovery and service negotiation](https://github.com/astrivant/polyad/blob/main/docs/apis/discovery.md).

| Module | Public models |
| --- | --- |
| `polyad_types.resources` | Kubernetes resource envelopes, metadata, status metrics, capacity status and mutation plans |
| `polyad_types.topology` | Nodes, dependencies, connections, placement and graph specifications |
| `polyad_types.replication` | Replica templates, bounds and connection modes |
| `polyad_types.rules` | Structural, spectral and Cheeger configuration |
| `polyad_types.network` | Network access, peers, ports and traffic rules |
| `polyad_types.traffic` | Istio routes, destination weights and bounds, and approved traffic splits |
| `polyad_types.throughput` | Application throughput and per-destination capacity reports for Soul searching |
| `polyad_types.activation`, `capacity`, `storage` | Activation, advance capacity and persistence configuration |
| `polyad_types.requests` | Composition, activation and temporary connection requests |
| `polyad_types.events`, `event_models` | Event envelopes, typed payload trees, stream settings and the packaged JSON Schema |

Top-level `Graph`, `PolyGraph` and `Daemon` are Kubernetes resource envelopes.
`Topology` describes a Graph's specification; `polyad_types.topology.PolyGraph`
describes a PolyGraph's specification. Resource `spec` dictionaries preserve
native Kubernetes extensions and should be populated from the relevant
configuration model when local validation is needed.

Reference a Daemon definition inside a persistent graph with
`Node(name="server", kind="Daemon", ref="server")`. The `Daemon` resource model
describes the reusable service definition; `Node` describes its place in a graph.

## Serialization

`to_dict(model)` and `from_dict(document, Model)` serialize and validate models.
`to_document(resource)` and `from_document(manifest)` handle Kubernetes envelopes,
preserving unmodeled native fields and checking kind and API version. Configuration
and request decoding rejects unknown fields. These checks validate the supplied
document; live graph admission, Cheeger computation and reconciliation run in the
operator.

## JSON Schemas

The package ships generated schemas for shared models, Polyad CRD manifests,
event envelopes and Helm values. Load them without installing the operator or
a JSON Schema validator:

```python
from polyad_types import ConnectionRequest
from polyad_types.schemas import available_schemas, load_schema, resource_schema, schema_for

request = schema_for(ConnectionRequest)
graph = resource_schema("Graph")
overlay = load_schema("helm-reference")
print(available_schemas())
```

Each result is an independent dictionary with local references; the JSON files
also ship as `polyad_types.schemas` package resources in wheels and source
distributions. `schema_for(Graph)` describes the Python resource envelope;
`resource_schema("Graph")` checks the fuller chart manifest contract.
See [importable JSON Schemas](https://github.com/astrivant/polyad/blob/main/docs/apis/json-schemas.md)
for validation examples, direct file access, dialects and regeneration.

## Event syntax trees and schemas

Import `EventAST`, `GraphEvent`, `TopologyEvent`, `ConnectionEvent`, `ControlEvent`
and `HeartbeatEvent` from `polyad_types`. `decode_event(document)` validates and
constructs the matching AST; `Event.typed()` does the same for client observations.
`to_dict(ast)` serializes it. Nested payload models live in `polyad_types.event_models`.

`event_schema()` reads the shipped Draft 2020-12 JSON Schema without an operator
import or a network request. `EventStreamSettings` defines configurable event byte,
batch and polling budgets, and `EventTooLarge` identifies size rejection. See the
[event contract and Helm tuning guide](https://github.com/astrivant/polyad/blob/main/docs/apis/event-contract.md)
for each tree, validated examples and transport framing.

## Client integration

The [Python client](https://github.com/astrivant/polyad/blob/main/pkg/client/README.md) accepts shared request objects:

```python
from polyad_client import Client
from polyad_types import CompositionItem, CompositionRequest

request = CompositionRequest(
    requestId="pipeline-1",
    rootId="root",
    objects=(CompositionItem(id="root", kind="Graph", spec={"nodes": []}),),
)
client = Client("http://polyad-api:8090", "configured-token")
receipt = client.compose(request)
```

From the repository root, install both local packages with
`pip install ./pkg/polyad-types ./pkg/client`. Release versions of the client
and operator depend on the matching `polyad-types` version.

## Publishing

See [manual PyPI publishing](https://github.com/astrivant/polyad/blob/main/docs/development/toolchain.md#manual-pypi-publishing) for Poetry release commands.
