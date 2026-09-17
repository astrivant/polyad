# Polyad documentation

[Polyad overview and diagrams](../README.md)

Browse guides by category. Start with [graph concepts](introduction/concepts.md),
then [run your first workload](introduction/getting-started.md).

## Table of contents

- [Introduction](#introduction)
- [Graphs and scaling](#graphs-and-scaling)
- [Workloads](#workloads)
- [APIs](#apis)
- [Deployment](#deployment)
- [Operations](#operations)
- [Development](#development)
- [Proposals](#proposals)
- [Package and chart references](#package-and-chart-references)

## Introduction

| Guide | Contents |
| --- | --- |
| [Graph concepts](introduction/concepts.md) | Nodes, dependencies, subgraphs, placement and recurrence |
| [Getting started](introduction/getting-started.md) | Installation, execution models and runnable examples |

## Graphs and scaling

| Guide | Contents |
| --- | --- |
| [Graph boundaries](graphs/graph-boundaries.md) | Three boundary types, repeated execution, placement and storage |
| [Graph rules](graphs/graph-rules.md) | Every structural, spectral, Cheeger and network constraint, with Mermaid examples |
| [Practical Cheeger tuning](graphs/cheeger-tuning.md) | Bounds, throughput response settings, computation budgets, important cuts and a runnable reference |
| [Comparing Cheeger policies](graphs/cheeger-orchestration.md) | Hard bounds versus throughput targets, nested parent/child measurements, subgraph replication and Observe/Adapt diagrams |
| [Replication and KEDA](graphs/replication.md) | Scalable workloads, nested replica groups and instance/definition metrics |
| [Soul searching](graphs/throughput-feedback.md) | Topology optimization through application throughput feedback, separate Cheeger targets and bounded Observe/Adapt modes |
| [Advance capacity](graphs/capacity.md) | Forecast upcoming demand, request node capacity and observe handoff |

## Workloads

| Guide | Contents |
| --- | --- |
| [Activation and client](workloads/activation.md) | Workload pulses, parallel daemons, frequency bounds and the standalone Python client |
| [Workload environment](workloads/workload-environment.md) | Automatic graph identity, ancestry, activation receipts and operator endpoint discovery |
| [Workload topology events](workloads/workload-events.md) | Neighbor discovery, structural notifications, scaling membership and replay recovery |
| [Workload controllers and storage](workloads/workload-storage.md) | Deployment or StatefulSet execution, native volumes, PVC templates and retention |

## APIs

| Guide | Contents |
| --- | --- |
| [Composition API service](apis/composition-api.md) | Service setup, authentication, gateway routing, shared shard rate limits and OpenAPI |
| [Composition requests](apis/composition-requests.md) | Request format, ID references, admission, retries and resource audit |
| [Temporary connections](apis/temporary-connections.md) | Optional TTL-bound edges, caller and target scope, service-account authentication and cleanup |

## Deployment

| Guide | Contents |
| --- | --- |
| [Helm deployment profiles](deployment/deployment-profiles.md) | Singular and HA tags, optional split components, replica floors and cluster placement |
| [Container profiles](deployment/containers.md) | Development and production builds, runtime permissions and image checks |
| [Process and thread hierarchy](deployment/process-hierarchy.md) | Tini, Python threads, async tasks, shared HTTP workers, observers and graceful shutdown |
| [Operator model](deployment/operator.md) | Abstractions, admission, lifecycle, coordination and deployment |
| [Graph status](deployment/operator.md#graph-instance-status) | Breadth, depth, lifecycle counters and descendant summaries |
| [Compiler objects](deployment/operator.md#resource-compiler-objects) | Attrs resource and status trees, Kubernetes serialization and generated metrics schemas |
| [Health and backlog](deployment/operator.md#health) | Pod probes, inbound updates and API write pressure |
| [Component deployments](deployment/components.md) | Dense or split services, the operator's own Graph, bootstrap recovery and KEDA demand |
| [Root control plane](deployment/root-control-plane.md) | One reserved PolyGraph containing the root and remote operator group Graphs, live membership, centralized reports and KEDA targets |
| [Helm-installed downstream operators](deployment/helm-workers.md) | Administrator-owned installation, explicit root attachment, and a choice of root or local replica scaling |
| [Multicluster and observers](deployment/multicluster.md) | Architecture diagrams for remote ownership, local execution, KEDA scaling, Istio traffic and optional shared readers |
| [Networking and events](deployment/networking.md) | Scoped rules, Istio authorization, subscriptions and credential health |
| [Optional PostgreSQL](deployment/postgresql.md) | Durable graph state, HA database setup and connection-based KEDA scaling |
| [Dragonfly HA and KEDA](deployment/dragonfly.md) | Bounded cache replica scaling, primary connection metrics and replication readiness |

## Operations

| Guide | Contents |
| --- | --- |
| [Metrics API](operations/metrics.md) | Prometheus and JSON queues, object inventories, hierarchies and KEDA query guidance |
| [OpenTelemetry traces and decision logs](operations/tracing.md) | Readable decisions and conflicts, trace correlation, independent OTLP log export, sampling and collector configuration |
| [Performance tuning](operations/performance.md) | Autoscaling stabilization, rate policies, queue polling and observation intervals |
| [Authentication and ESO](operations/authentication.md) | KEDA bearer credentials, ExternalSecrets, namespace boundaries and rotation |
| [API keys and request lanes](operations/api-keys.md) | Service/operator groups, credential directions and HA-wide per-key rate/concurrency limits |
| [Argo CD health](operations/argocd.md) | Graph and leaf health, descendant failures and GitOps configuration |
| [Flux health](operations/fluxcd.md) | CEL checks for graph readiness and descendant failures |

## Development

| Guide | Contents |
| --- | --- |
| [Resource registry](development/resource-registry.md) | Supported kinds, AST models, API identities and scheduling capabilities |
| [Operator package layout](development/operator-layout.md) | Lifecycle, reconciliation, policies, coordination, clusters, observability and adapters |
| [Mutation plans](development/mutations.md) | Explicit effects, independence evidence, shared bounds and ordered execution |
| [Mutation diagrams](development/mutation-diagrams.md) | Commuting squares, triangles, diamonds and cubes, with implementation boundaries |
| [Python types and serialization](development/toolchain.md#python-types-and-serialization) | Custom graph references, Mypy checks and cattrs round trips |
| [Development toolchain](development/toolchain.md) | Pinned tools, editor settings, formatting and generated documentation |

## Proposals

| Guide | Contents |
| --- | --- |
| [Graph rollouts and rotations (proposal)](proposals/rotations.md) | Root-coordinated waves, graph policy bindings, Secret revisions, traversal and KEDA coordination |
| [Rollout sparsity and events (proposal)](proposals/rollout-sparsity.md) | Inherited frequency limits, bounded debounce, queued triggers and rollout lifecycle notifications |
| [Transistor gates and decision programs (proposal)](proposals/decision-gates.md) | Conditional activation, durable choices, typed facts and a Python/CEL authoring direction |

## Package and chart references

| Reference | Contents |
| --- | --- |
| [Local scheduling](../pkg/polyad/balance/README.md) | Cooperative work, checkpoints, policies, rewrites and graph images |
| [Standalone types package](../pkg/polyad-types/README.md) | Resource, configuration and request models without operator dependencies |
| [Helm parameters](../charts/polyad/README.md) | Operator, autoscaling and shared queue settings |
