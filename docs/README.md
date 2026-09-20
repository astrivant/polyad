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
| [Comparing Cheeger bounds and throughput targets](graphs/cheeger-orchestration.md) | Hard bounds versus throughput targets, nested parent/child measurements, subgraph replication and Observe/Adapt diagrams |
| [Replication and KEDA](graphs/replication.md) | Scalable workloads, nested replica groups and instance/definition metrics |
| [Soul searching: application throughput and graph structure](graphs/soul-searching.md) | Separate Cheeger targets, approved connection layouts and bounded traffic balancing in Observe/Adapt modes |
| [Soul searching: approved load profiles and preparation](graphs/load-profiles.md) | Demand-triggered profiles, fixed ceilings, capacity lookahead and a sequence diagram separating replica and node scaling |
| [Balance traffic between workload and graph replicas](graphs/traffic-balancing.md) | Optional Istio percentage routing to workload and graph replicas, calibrated splits and bounded adjustment from measured headroom |
| [Advance capacity planning](graphs/capacity.md) | Forecast upcoming demand, request node capacity and observe handoff |

## Workloads

| Guide | Contents |
| --- | --- |
| [Workload activation and pulses](workloads/activation.md) | Workload pulses, parallel daemons, frequency bounds and the standalone Python SDK |
| [Workload environment](workloads/workload-environment.md) | Automatic graph identity, ancestry, activation receipts and operator endpoint discovery |
| [Workload topology events](workloads/workload-events.md) | Neighbor discovery, structural notifications, scaling membership and replay recovery |
| [Optional workload protocols](workloads/workload-protocols.md) | Opt-in gRPC, AMQP and Redis installs; WebSocket/TCP clients, consent checks and Istio Service ports |
| [Service Symbiosis: writing adaptive microservices](workloads/adaptive-microservices.md) | Cooperative producers and consumers across Graphs and PolyGraphs, backpressure, useful throughput and delta-driven Python SDK hooks |
| [Adaptation strategies for application constraints](workloads/adaptation-strategies.md) | Modular SDK policies for mutation difficulties, independent constraints, pressure-driven profiles and admission |
| [Service-level objectives for adaptive Daemons](workloads/service-level-objectives.md) | Separate adaptation progress from availability, quality, capability and error-budget accounting |
| [Service-level computation reference](workloads/service-level-computations.md) | Exact fixed-window formulas, state precedence, adaptation budgets, instance aggregation and worked examples |
| [SDK telemetry and subprocess plans](workloads/sdk-runtime.md) | OpenTelemetry instrumentation, approved local worker profiles, readiness, replacement, draining and recovery |
| [Reachability and symbiosis models](workloads/reachability.md) | Service interaction effects, finite queue envelopes, runtime guards, optional HJ analysis and state-variable studies |
| [Adapting to Kubernetes conditions](workloads/kubernetes-adaptation.md) | Scheduling delays, flaky connectivity, rollouts, memory pressure and recovery mapped to SDK strategies and application actions |
| [Local Soul searching: processes and network topology](workloads/local-soul-searching.md) | SDK neighbor-routing strategy, real peer work sharing, adaptive workers and measured chain-versus-shortcut comparisons under equal limits |
| [Local Natural Selection: mutation, survival and retirement](workloads/local-natural-selection.md) | Parent planner over Soul searching, typed capability composition, changing outcomes, surviving service PIDs and graceful retirement |
| [Workload controllers and storage](workloads/workload-storage.md) | Deployment or StatefulSet execution, native volumes, PVC templates and retention |

## APIs

| Guide | Contents |
| --- | --- |
| [Composition API service](apis/composition-api.md) | Service setup, authentication, gateway routing, shared shard rate limits and OpenAPI |
| [Composition requests](apis/composition-requests.md) | Request format, ID references, admission, retries and resource audit |
| [Atlas discovery and service connections](apis/discovery.md) | Inherited operator access modes, live service discovery, client filters and cross-cluster consent |
| [Event syntax trees, schemas and limits](apis/event-contract.md) | Importable event ASTs, schema validation, byte budgets, replay tuning and client receive limits |
| [Importable JSON Schemas](apis/json-schemas.md) | Packaged shared models, CRD manifests, events and Helm contracts for offline validation and editors |
| [Temporary connections](apis/temporary-connections.md) | Optional TTL-bound edges, caller and target scope, service-account authentication and cleanup |

## Deployment

| Guide | Contents |
| --- | --- |
| [Helm deployment profiles](deployment/deployment-profiles.md) | One `ha` Boolean, optional split components, replica floors and cluster placement |
| [CRDs and named resource templates](../charts/polyad-crds/README.md) | Independently versioned APIs, name-keyed instance maps, cross-resource TPL references and complete commented reference values |
| [Development and production containers](deployment/containers.md) | Development and production builds, runtime permissions and image checks |
| [Operator processes, threads and async tasks](deployment/process-hierarchy.md) | Tini, Python threads, async tasks, shared HTTP workers, observers and graceful shutdown |
| [Pod context and health binding](deployment/pod-context.md) | Pod-only health listeners, Downward API identity and addresses, node placement and container resource variables |
| [Graph orchestration and adaptation on Kubernetes](deployment/operator.md) | Workload deployment, graph constraints, demand-driven adaptation, APIs and replica coordination |
| [Graph status](deployment/operator.md#graph-instance-status) | Breadth, depth, lifecycle counters and descendant summaries |
| [Compiler objects](deployment/operator.md#resource-compiler-objects) | Attrs resource and status trees, Kubernetes serialization and generated metrics schemas |
| [Health and backlog](deployment/operator.md#health) | Pod probes, inbound updates and API write pressure |
| [Dense and distributed operator deployments](deployment/components.md) | Dense or split services, the operator's own Graph, bootstrap recovery and KEDA demand |
| [GKE scaling test environment](../terraform/README.md) | Terraform, an isolated 2–10-node Ubuntu pool, Argo CD Git sync and the operator's self-managed component Graph |
| [Local services in the root Graph](deployment/local-services.md) | Complete local chart inventory, bundled or existing KEDA, observation permissions and lifecycle ownership |
| [Root control plane](deployment/root-control-plane.md) | One reserved PolyGraph containing the root and remote operator group Graphs, live membership, centralized reports and KEDA targets |
| [Helm-installed downstream operators](deployment/helm-workers.md) | Administrator-owned installation, explicit root attachment, and a choice of root or local replica scaling |
| [Cross-cluster composition and optional observers](deployment/multicluster.md) | Architecture diagrams for remote ownership, local execution, KEDA scaling, Istio traffic and optional shared readers |
| [Graph networking and event subscriptions](deployment/networking.md) | Scoped rules, Istio authorization, subscriptions and credential health |
| [Advanced Istio integration](deployment/istio-features.md) | Route resilience, proxy telemetry, locality, JWT ingress, scoped configuration, egress and policy observation |
| [Vertical Pod Autoscaler compatibility](deployment/vpa.md) | Native VPA bounds, in-place resize ownership and SDK-visible container resources |
| [Optional PostgreSQL state storage](deployment/postgresql.md) | Durable graph state, encrypted storage and GKE KMS configuration, database HA and connection-based KEDA scaling |
| [PostgreSQL record encryption](deployment/record-encryption.md) | Optional encryption before SQL writes, administrator key Secrets, decryption and key rotation |
| [Dragonfly HA and KEDA](deployment/dragonfly.md) | Bounded cache replica scaling, primary connection metrics and replication readiness |

## Operations

| Guide | Contents |
| --- | --- |
| [Scheduler metrics API](operations/metrics.md) | Prometheus and JSON queues, object inventories, hierarchies and KEDA query guidance |
| [OpenTelemetry traces and decision logs](operations/tracing.md) | Readable decisions and conflicts, trace correlation, independent OTLP log export, sampling and collector configuration |
| [Telemetry collectors](operations/telemetry-agents.md) | Alloy metrics, logs and traces; Prometheus Agent alternative; chart-wide discovery, backend credentials and benchmark integration |
| [Operator performance and autoscaling](operations/performance.md) | Autoscaling stabilization, rate policies, queue polling and observation intervals |
| [Event connection rebalancing and copulses](operations/event-rebalancing.md) | Istio or client routing, rolling subscription resets, ready endpoint discovery and scale-down draining |
| [Authentication and external credentials](operations/authentication.md) | KEDA bearer credentials, ExternalSecrets, namespace boundaries and rotation |
| [API keys and request lanes](operations/api-keys.md) | Service/operator groups, credential directions and HA-wide per-key rate/concurrency limits |
| [Argo CD graph health](operations/argocd.md) | Graph and leaf health, descendant failures and GitOps configuration |
| [Flux graph health](operations/fluxcd.md) | CEL checks for graph readiness and descendant failures |
| [Resilience control loops](operations/resilience-control-loops.md) | How SLA evidence, Cheeger bounds, Soul searching, Natural Selection, SDK strategies and autoscalers interact |

## Development

| Guide | Contents |
| --- | --- |
| [Python extension interfaces](development/python-interfaces.md) | Public ABCs for workloads, process ownership, scheduling, SDK transports and operator adapters |
| [Supported resource registry](development/resource-registry.md) | Supported kinds, AST models, API identities and scheduling capabilities |
| [Operator package layout](development/operator-layout.md) | Lifecycle, reconciliation, the central Soul searching decision pipeline, policies, coordination, clusters, observability and adapters |
| [Mutation plans](development/mutations.md) | Explicit effects, independence evidence, shared bounds and ordered execution |
| [Kubernetes write pipeline](development/write-pipeline.md) | Bounded admission, dependency contracts, validation windows, watch invalidation, duplicate coalescing and targeted recovery |
| [Mutation diagram patterns](development/mutation-diagrams.md) | Commuting squares, triangles, diamonds and cubes, with implementation boundaries |
| [Python types and serialization](development/toolchain.md#python-types-and-serialization) | Custom graph references, Mypy checks and cattrs round trips |
| [Development toolchain](development/toolchain.md) | Pinned tools, editor settings, formatting and generated documentation |
| [Benchmark studies](../studies/README.md) | Repeatable load experiments, fixture images, pytest smoke checks and CI refresh phases |
| [Soul process study](../studies/soul/README.md) | Six-service SDK strategy experiments, worker replacement, bounded admission and measured process plots |
| [Nature process study](../studies/nature/README.md) | Composition changes above adaptive services, capability survival and retirement, and measured outcomes |

## Proposals

| Guide | Contents |
| --- | --- |
| [Copolyad: deriving graphs from desired outcomes (proposal)](proposals/copolyad.md) | Natural Selection composition algorithm, capability contracts, outcome feedback and Polyad's admission boundary |
| [Copolyad contracts and the Natural Selection IR (proposal)](proposals/copolyad-language.md) | Typed outcome and capability language, semantic matching, CEL conditions, Contract IR and Plan IR examples |
| [Graph-scoped rollouts and rotations (proposal)](proposals/rotations.md) | Root-coordinated waves, graph policy bindings, Secret revisions, traversal and KEDA coordination |
| [Rollout sparsity and events (proposal)](proposals/rollout-sparsity.md) | Inherited frequency limits, bounded debounce, queued triggers and rollout lifecycle notifications |
| [Transistor gates and decision programs (proposal)](proposals/decision-gates.md) | Conditional activation, durable choices, typed facts and a Python/CEL authoring direction |

## Package and chart references

| Reference | Contents |
| --- | --- |
| [Polyad scheduling guide](../pkg/polyad/scheduling/README.md) | Cooperative work, checkpoints, policies, rewrites and graph images |
| [Polyad SDK](../pkg/polyad-sdk/README.md) | Adaptive service deltas and strategies, operator APIs, managed subprocesses and OpenTelemetry |
| [Polyad types](../pkg/polyad-types/README.md) | Validated resource, graph, networking, event and API models with shared serialization |
| [Polyad schemas](../pkg/polyad-schemas/README.md) | Versioned model, resource, event and Helm JSON Schemas with offline Python loaders |
| [Polyad benchmarks](../pkg/polyad-benchmarks/README.md) | API-driven plans, mock fixtures, bounded load generation, activation measurements and study refreshes |
| [Polyad Helm chart](../charts/polyad/README.md) | Single-replica and HA deployments, multicluster coordination, autoscaling, storage and telemetry |
| [Polyad CRD chart](../charts/polyad-crds/README.md) | Versioned CRDs and named resource instances with defaults, schemas and cross-resource templates |
| [Polyad benchmark chart](../charts/polyad-benchmarks/README.md) | Graph-managed fixtures and runners, JSON test plans, metrics, dashboards and tracing |
