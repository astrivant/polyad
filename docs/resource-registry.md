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
| `auxiliary` | `network`, `capacity`, `activation`, or `None`; auxiliary resources do not count as graph vertices |

The module also exports immutable sets named `BOUNDARY_KINDS`,
`GRAPH_OWNED_KINDS`, `DEFINITION_KINDS`, `RECONCILED_KINDS`, `COMPOSABLE_KINDS`,
`POLYAD_KINDS`, `NETWORK_POLICY_KINDS`, `CAPACITY_KINDS` and `AUXILIARY_KINDS`.
These are derived from metadata, rather than maintained as separate lists.

All currently supported resource endpoints are namespaced. Kind names are unique
within this catalog; it is not a generic Kubernetes API discovery service.
Optional Istio and autoscaler types remain listed even when those APIs are absent
from a particular cluster. Their existing feature and discovery checks still
control their use.

## Extending the catalog

Declare metadata on the AST class's `resource_type` in
[`asts/resources.py`](../pkg/polyad/compiler/asts/resources.py), then include the
class in `RESOURCE_CLASSES`. The public catalog and its capability sets derive
from those declarations. Both mappings and descriptors are immutable at runtime.

Supporting a new kind also requires its compiler or reconciliation behavior and
any required RBAC or CRD. For graph-owned kinds, add the corresponding typed field
to `ResourceCounts` and regenerate status schemas. Registry tests catch drift
between owned kinds and typed counts, and between Python API identities and the
shipped Polyad CRDs. Existing `polyad.compiler.asts.RESOURCE_TYPES` imports remain
available.
