# Supported resource registry

`polyad.compiler.registry` is the public catalog of Kubernetes resource types
understood by Polyad. It links each kind to its immutable metadata and attrs AST
class. API routing, graph inventory, resource counts, composition eligibility,
Kopf reconciliation duties and Argo CD customization generation use this catalog.

```python
from polyad.compiler.registry import GRAPH_OWNED_KINDS, RESOURCE_MODELS, RESOURCE_TYPES

job = RESOURCE_TYPES["Job"]
print(job.api_version, job.plural, job.description)
# batch/v1 jobs Finite workload execution.

job_ast = RESOURCE_MODELS["Job"]
for kind in sorted(GRAPH_OWNED_KINDS):
    descriptor = RESOURCE_TYPES[kind]
    print(kind, descriptor.boundary, descriptor.auxiliary)
```

## Table of contents

- [Metadata](#metadata)
- [Extending the catalog](#extending-the-catalog)

## Metadata

| Field | Meaning |
| --- | --- |
| `kind` | Kubernetes kind used for dispatch |
| `api_version`, `api_group`, `plural`, `prefix` | Versioned API identity and routing |
| `description` | Purpose of the supported type |
| `namespaced` | Whether Kubernetes scopes instances to a namespace |
| `polyad` | Whether its CRD belongs to Polyad's API group |
| `boundary` | Owns graph execution and descendant summaries |
| `graph_owned` | Included in graph resource inventory and counts |
| `definition` | Reusable definition without independent execution |
| `reconciled` | Receives operator reconciliation duties |
| `composable` | May be declared in a composition request |
| `required_feature` | Operator feature (`mesh` or `capacity`) required before graph inventory reads |
| `auxiliary` | `network`, `capacity`, `activation`, `connection`, or `None`; auxiliary resources do not count as graph vertices |

The module also exports immutable sets named `BOUNDARY_KINDS`,
`GRAPH_OWNED_KINDS`, `DEFINITION_KINDS`, `RECONCILED_KINDS`, `COMPOSABLE_KINDS`,
`POLYAD_KINDS`, `NETWORK_POLICY_KINDS`, `CAPACITY_KINDS` and `AUXILIARY_KINDS`.
These are derived from the shared resource metadata.

All currently supported resource endpoints are namespaced. Kind names are unique
within this catalog. Use Kubernetes API discovery for resources outside it.
Optional Istio and autoscaler types remain listed even when those APIs are absent
from a particular cluster. Their existing feature and discovery checks still
control their use.

The reserved root Graph also counts explicitly observed `Dragonfly` and
CloudNativePG `Cluster` objects. They are registered native kinds but are not
graph-owned resources. `PostgreSQLCluster` is the shared Python model for `Cluster`;
the operator's adapter permits only reads through that route. Observation does
not install those APIs or take lifecycle ownership from their controllers.

## Extending the catalog

Declare metadata on the AST class's `resource_type` in the appropriate
[resource module](../../pkg/polyad-types/polyad_types/resources/): `kubernetes`,
`polyad`, `istio` or `infrastructure`. Include that class in `RESOURCE_CLASSES` in
[`registry.py`](../../pkg/polyad-types/polyad_types/resources/registry.py).
The public catalog and its capability sets derive
from those declarations. Both mappings and descriptors are immutable at runtime.

Supporting a new kind also requires its compiler or reconciliation behavior and
any required RBAC or CRD. For graph-owned kinds, add the corresponding typed field
to `ResourceCounts` and regenerate status schemas. Registry tests catch drift
between owned kinds and typed counts, and between Python API identities and the
shipped Polyad CRDs. Existing `polyad_types.resources.RESOURCE_TYPES` imports remain
available.

`ReplicaGroup` is a composable, reconciled graph boundary with a Kubernetes scale
subresource. It accepts any executable node definition, including nested groups.
See [replication and KEDA](../graphs/replication.md).

`StatefulSet` is a graph-owned native execution kind selected through
`Daemon.spec.controller`. It participates in ownership inventory, cleanup,
readiness, audit output and typed resource counts alongside `Deployment`.
See [workload controllers and storage](../workloads/workload-storage.md).

`TemporaryConnection` is a reconciled, graph-owned receipt for the optional
[temporary connections API](../apis/temporary-connections.md). It is auxiliary and cannot
be composed as a graph vertex or submitted as a reusable definition.
