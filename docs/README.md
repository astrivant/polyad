# Polyad documentation

[Polyad overview and diagrams](../README.md)

Start with [graph concepts](concepts.md), then [run your first workload](getting-started.md).

| Guide | Contents |
| --- | --- |
| [Graph concepts](concepts.md) | Nodes, dependencies, subgraphs, placement and recurrence |
| [Container profiles](containers.md) | Development and production builds, runtime permissions and image checks |
| [Getting started](getting-started.md) | Installation, execution models and runnable examples |
| [Networking and events](networking.md) | Scoped rules, Istio authorization, subscriptions and credential health |
| [Operator model](operator.md) | Abstractions, admission, lifecycle, coordination and deployment |
| [Flux health](fluxcd.md) | CEL checks for graph readiness and descendant failures |
| [Argo CD health](argocd.md) | Graph and leaf health, descendant failures and GitOps configuration |
| [Graph status](operator.md#graph-instance-status) | Breadth, depth, lifecycle counters and descendant summaries |
| [Resource registry](resource-registry.md) | Supported kinds, AST models, API identities and scheduling capabilities |
| [Compiler objects](operator.md#resource-compiler-objects) | Attrs resource and status trees, Kubernetes serialization and generated metrics schemas |
| [Mutation plans](mutations.md) | Explicit effects, independence evidence, shared bounds and ordered execution |
| [Mutation diagrams](mutation-diagrams.md) | Commuting squares, triangles, diamonds and cubes, with implementation boundaries |
| [Advance capacity](capacity.md) | Forecast upcoming demand, request node capacity and observe handoff |
| [Replication and KEDA](replication.md) | Scalable workloads, nested replica groups and instance/definition metrics |
| [Metrics API](metrics.md) | Prometheus and JSON queues, object inventories, hierarchies and KEDA query guidance |
| [Performance tuning](performance.md) | Autoscaling stabilization, rate policies, queue polling and observation intervals |
| [Authentication and ESO](authentication.md) | KEDA bearer credentials, ExternalSecrets, namespace boundaries and rotation |
| [Health and backlog](operator.md#health) | Pod probes, inbound updates and API write pressure |
| [Activation and client](activation.md) | Workload pulses, parallel daemons, frequency bounds and the standalone Python client |
| [Workload environment](workload-environment.md) | Automatic graph identity, ancestry, activation receipts and operator endpoint discovery |
| [Composition API](composition-api.md#optional-gateway-api-routing) | Gateway routing, shared shard rate limits, request IDs and audit lookup |
| [Local scheduling](../pkg/polyad/balance/README.md) | Cooperative work, checkpoints, policies, rewrites and graph images |
| [Python types and serialization](toolchain.md#python-types-and-serialization) | Custom graph references, Mypy checks and cattrs round trips |
| [Helm parameters](../charts/polyad/README.md) | Operator, autoscaling and shared queue settings |
| [Development toolchain](toolchain.md) | Pinned tools, editor settings, formatting and generated documentation |
