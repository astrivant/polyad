# Polyad documentation

[Polyad overview and diagrams](../README.md)

Start with [graph concepts](concepts.md), then [run your first workload](getting-started.md).

| Guide | Contents |
| --- | --- |
| [Graph concepts](concepts.md) | Nodes, dependencies, subgraphs, placement and recurrence |
| [Getting started](getting-started.md) | Installation, execution models and runnable examples |
| [Networking and events](networking.md) | Scoped rules, Istio authorization, subscriptions and credential health |
| [Operator model](operator.md) | Abstractions, admission, lifecycle, coordination and deployment |
| [Graph status](operator.md#graph-instance-status) | Breadth, depth, lifecycle counters and descendant summaries |
| [Compiler objects](operator.md#resource-compiler-objects) | Attrs resource and status trees, Kubernetes serialization and generated metrics schemas |
| [Advance capacity](capacity.md) | Forecast upcoming demand, request node capacity and observe handoff |
| [Metrics API](metrics.md) | Prometheus and JSON queues, object inventories, hierarchies and KEDA query guidance |
| [Health and backlog](operator.md#health) | Pod probes, inbound updates and API write pressure |
| [Composition API](composition-api.md#optional-gateway-api-routing) | Gateway routing, shared shard rate limits, request IDs and audit lookup |
| [Local scheduling](../pkg/polyad/balance/README.md) | Cooperative work, checkpoints, policies, rewrites and graph images |
| [Python types and serialization](toolchain.md#python-types-and-serialization) | Custom graph references, Mypy checks and cattrs round trips |
| [Helm parameters](../charts/polyad/README.md) | Operator, autoscaling and shared queue settings |
| [Development toolchain](toolchain.md) | Pinned tools, editor settings, formatting and generated documentation |
