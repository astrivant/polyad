# Polyad documentation

[Polyad overview and diagrams](../README.md)

Start with [graph concepts](concepts.md), then [run your first workload](getting-started.md).

| Guide | Contents |
| --- | --- |
| [Graph boundaries](graph-boundaries.md) | Three boundary types, repeated execution, placement and storage |
| [Graph concepts](concepts.md) | Nodes, dependencies, subgraphs, placement and recurrence |
| [Container profiles](containers.md) | Development and production builds, runtime permissions and image checks |
| [Getting started](getting-started.md) | Installation, execution models and runnable examples |
| [Networking and events](networking.md) | Scoped rules, Istio authorization, subscriptions and credential health |
| [Root control plane](root-control-plane.md) | Dedicated management clusters, root-managed execution replicas, centralized reports and KEDA targets |
| [Multicluster and observers](multicluster.md) | Architecture diagrams for remote ownership, local execution, KEDA scaling, Istio traffic and optional shared readers |
| [Temporary connections](temporary-connections.md) | Optional TTL-bound edges, caller and target scope, service-account authentication and cleanup |
| [Operator model](operator.md) | Abstractions, admission, lifecycle, coordination and deployment |
| [Flux health](fluxcd.md) | CEL checks for graph readiness and descendant failures |
| [Argo CD health](argocd.md) | Graph and leaf health, descendant failures and GitOps configuration |
| [Graph status](operator.md#graph-instance-status) | Breadth, depth, lifecycle counters and descendant summaries |
| [Resource registry](resource-registry.md) | Supported kinds, AST models, API identities and scheduling capabilities |
| [Compiler objects](operator.md#resource-compiler-objects) | Attrs resource and status trees, Kubernetes serialization and generated metrics schemas |
| [Mutation plans](mutations.md) | Explicit effects, independence evidence, shared bounds and ordered execution |
| [Mutation diagrams](mutation-diagrams.md) | Commuting squares, triangles, diamonds and cubes, with implementation boundaries |
| [Workload controllers and storage](workload-storage.md) | Deployment or StatefulSet execution, native volumes, PVC templates and retention |
| [Advance capacity](capacity.md) | Forecast upcoming demand, request node capacity and observe handoff |
| [Replication and KEDA](replication.md) | Scalable workloads, nested replica groups and instance/definition metrics |
| [Metrics API](metrics.md) | Prometheus and JSON queues, object inventories, hierarchies and KEDA query guidance |
| [Performance tuning](performance.md) | Autoscaling stabilization, rate policies, queue polling and observation intervals |
| [Authentication and ESO](authentication.md) | KEDA bearer credentials, ExternalSecrets, namespace boundaries and rotation |
| [Health and backlog](operator.md#health) | Pod probes, inbound updates and API write pressure |
| [Activation and client](activation.md) | Workload pulses, parallel daemons, frequency bounds and the standalone Python client |
| [Workload environment](workload-environment.md) | Automatic graph identity, ancestry, activation receipts and operator endpoint discovery |
| [Workload topology events](workload-events.md) | Neighbor discovery, structural notifications, scaling membership and replay recovery |
| [Graph rules](graph-rules.md) | Every structural, spectral, Cheeger and network constraint, with Mermaid examples |
| [Composition requests](composition-requests.md) | Request format, ID references, admission, retries and resource audit |
| [Composition API service](composition-api.md) | Service setup, authentication, gateway routing, shared shard rate limits and OpenAPI |
| [Local scheduling](../pkg/polyad/balance/README.md) | Cooperative work, checkpoints, policies, rewrites and graph images |
| [Python types and serialization](toolchain.md#python-types-and-serialization) | Custom graph references, Mypy checks and cattrs round trips |
| [Standalone types package](../pkg/polyad-types/README.md) | Resource, configuration and request models without operator dependencies |
| [Helm parameters](../charts/polyad/README.md) | Operator, autoscaling and shared queue settings |
| [Development toolchain](toolchain.md) | Pinned tools, editor settings, formatting and generated documentation |
