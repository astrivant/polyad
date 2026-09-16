# Workload environment

Every Job, Deployment and StatefulSet compiled from a Workload, Ephemeral or Daemon definition
receives graph context automatically. It is available in all declared application
containers, init containers and native sidecars. This includes workloads inside
nested graphs, ReplicaGroups and activation runs.

## Activate a downstream workload

A service can address another vertex in its containing graph without looking up
the generated graph name or UID:

```python
import os
from polyad_client import Client

client = Client(os.environ["POLYAD_API_URL"], os.environ["POLYAD_API_TOKEN"])
receipt = client.activate(
    request_id="batch-42",
    graph=os.environ["POLYAD_GRAPH_NAME"],
    graph_uid=os.environ["POLYAD_GRAPH_UID"],
    kind=os.environ["POLYAD_GRAPH_KIND"],
    node="process-batch",
)
```

`process-batch` is the desired target; `POLYAD_NODE_NAME` identifies the caller.
The containing graph can differ from the root, and its generated name can differ
from the reusable definition. Use the graph kind and UID together with the name.
Use a stable request ID per business event and reuse it when retrying that event;
using `batch-42` repeatedly intentionally refers to the same activation receipt.

Enable `api.enabled` and provide an authorized `POLYAD_API_TOKEN` through the
workload's own Secret reference. Identity variables describe the workload; they
do not grant API access. NetworkPolicy and Istio rules must also permit traffic.
See [workload API access](networking.md#workload-access-to-operator-apis) and
[activation semantics](activation.md).

## Variables

All values are strings. Optional context is the empty string when absent.

| Variables | Meaning |
| --- | --- |
| `POLYAD_GRAPH_NAME`, `POLYAD_GRAPH_KIND`, `POLYAD_GRAPH_UID`, `POLYAD_GRAPH_NAMESPACE` | Persisted containing graph instance, including a ReplicaGroup where applicable |
| `POLYAD_ROOT_GRAPH_NAME`, `POLYAD_ROOT_GRAPH_KIND`, `POLYAD_ROOT_GRAPH_UID` | Outermost controlling graph boundary in the same namespace |
| `POLYAD_GRAPH_ANCESTRY` | JSON list from root to containing graph; each entry has `kind`, `namespace`, `name` and `uid` |
| `POLYAD_NODE_NAME` | Logical node key in the containing graph, including replica ordinal keys |
| `POLYAD_NODE_ID` | Composition node ID when supplied, otherwise the node name |
| `POLYAD_NODE_PATH` | Hierarchical audit path through the containing graphs to this node |
| `POLYAD_RUNTIME_NODE_NAME` | Execution node key; distinct from the logical name for individual activation runs |
| `POLYAD_DEFINITION_NAME`, `POLYAD_DEFINITION_KIND`, `POLYAD_DEFINITION_UID`, `POLYAD_DEFINITION_GENERATION` | Reusable Workload, Ephemeral or Daemon definition used to compile this execution |
| `POLYAD_RESOURCE_NAME`, `POLYAD_RESOURCE_KIND` | Native Job, Deployment or StatefulSet containing this Pod |
| `POLYAD_REQUEST_ID`, `POLYAD_COMPOSITION_UID` | Original composition request and persisted receipt identity, when present |
| `POLYAD_ACTIVATION_ID`, `POLYAD_ACTIVATION_UID` | Current activation receipt, or nearest enclosing graph activation |
| `POLYAD_POD_NAME`, `POLYAD_POD_UID`, `POLYAD_POD_NAMESPACE` | This concrete Pod's identity |
| `POLYAD_KUBERNETES_NODE_NAME`, `POLYAD_SERVICE_ACCOUNT_NAME` | Assigned Kubernetes node and Pod service account |
| `POLYAD_API_URL`, `POLYAD_EVENTS_URL`, `POLYAD_METRICS_URL` | Namespace-qualified internal Service URL for each enabled operator listener |

Kubernetes supplies Pod identity, node and service account fields through the
[Downward API](https://kubernetes.io/docs/concepts/workloads/pods/downward-api/).
Each Deployment or StatefulSet replica therefore gets its own Pod UID while sharing its logical
graph node. The compiler obtains graph ancestry from current owner references,
verifies parent UIDs and stops admission if an owner is missing, replaced or
terminating. Cycles and ancestry beyond 32 boundaries are rejected.

## Configuration and lifecycle

The chart configures endpoint discovery from its release name and namespace.
An endpoint URL is empty when that listener is disabled. Outside Helm, configure
`POLYAD_WORKLOAD_API_URL`, `POLYAD_WORKLOAD_EVENTS_URL` and
`POLYAD_WORKLOAD_METRICS_URL` on the operator. Credentials are supplied separately
by workloads; the operator does not copy its own Secrets into them.

The variable names above are reserved. The compiler replaces conflicting explicit
entries and puts context before application environment entries, allowing values
such as `LOG_PREFIX: $(POLYAD_GRAPH_NAME)/$(POLYAD_NODE_NAME)`. Unrelated variables,
`envFrom` entries and Secret references are preserved. Explicit compiler entries
take precedence over values imported through `envFrom`.

Context is a snapshot for that execution. It does not update inside a running
container. Use [workload topology events](workload-events.md) with these identity
variables to discover neighbors and follow graph edits, scaling and execution
membership changes while running. Identity variables participate in workload revision hashes, so adding
this feature or changing endpoint URLs follows the usual workload replacement
and drain rules. Activation runs retain the logical node and receive their own
runtime resource name and receipt IDs. Containers added later by another admission
webhook or through the debug-container API are outside this compilation pass.

This describes application execution templates. Capacity reservation Pods run
inert images and are not application callers of the operator API.
