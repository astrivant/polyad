# Polyad types

Shared Python 3.11–3.14 resource, configuration and API models for Polyad. This
package provides the same definitions used by the operator and Python client,
including constructor validation, serialization and a `py.typed` marker.
Its only dependencies are attrs, cattrs and typing-extensions.

## Table of contents

- [Installation](#installation)
- [Example](#example)
- [Public models](#public-models)
- [Serialization](#serialization)
- [Client integration](#client-integration)
- [Publishing](#publishing)

## Installation

Install from a repository checkout:

```sh
pip install ./pkg/polyad-types
```

Release CI builds and publishes this distribution separately. Once that release
is available, install it with `pip install polyad-types` and import `polyad_types`.
Installing it does not install `polyad` or `polyad-client`.

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
| `polyad_types.events` | Event stream observations |

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
