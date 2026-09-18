# Polyad benchmark fixtures

An ordinary application Graph containing a private fixture Daemon, discovery
Service, pulsed batch Workload and pulsed runner Workload. All Polyad resources
are rendered through the versioned `polyad-crds` dependency, including its defaults
and cross-field templates. The operator's reserved Graph remains separate.

## Table of contents

- [Install and run](#install-and-run)
- [Ownership and credentials](#ownership-and-credentials)
- [Test profiles](#test-profiles)
- [Plans and observability](#plans-and-observability)
- [Parameters](#parameters)
  - [Benchmark selection](#benchmark-selection)
  - [Shared fixture settings](#shared-fixture-settings)
  - [Plan reloads](#plan-reloads)
  - [Operator observations](#operator-observations)
  - [Prometheus and Grafana](#prometheus-and-grafana)
  - [Trace storage](#trace-storage)
  - [Trace collection](#trace-collection)

## Install and run

Follow [studies/load](../../studies/load/README.md). Build both images first; this
chart does not assume benchmark images have already been published. Fixture
installation is idle until an explicit activation starts the runner.

Only one default fixture bundle can exist per namespace because resource maps
are keyed by their Kubernetes names (`load-study`, `load-fixture`, etc.). Use a
separate namespace/operator endpoint or override the named maps for isolation.
The study defaults expect the operator and graph in namespace `polyad`.

## Ownership and credentials

Helm owns the reusable definitions and Graph; Polyad owns the generated Service,
Deployment, Jobs and activation receipts. Uninstalling the chart deletes the
Graph and starts normal graph cleanup. Export results first. Helm skips already
installed CRDs; their lifecycle remains with the operator's CRD installation.

Use `--skip-crds` only when both Polyad and any enabled monitoring CRDs already exist. For Argo CD,
set `helm.skipCrds: true`, so it does not claim shared CRDs for the fixture app.

An existing Secret may provide two keys: one operator API token authorized for
activation in this graph tree, and a separate fixture token. An empty Secret
name selects unauthenticated fixture calls and is suitable only for the private
GKE demo with operator authentication disabled. NetworkPolicy and Istio must
permit graph workloads to call the operator API. No Kubernetes API RBAC or service
account token is granted to the fixture or runner.

CPU/memory settings apply to all three execution roles. Consumer `placement`
applies to fixture and batch Pods; `runnerPlacement` independently places load
generators. Both default to normal scheduling. The GKE study uses `fixtures` for
consumers/support and `copolyad` for producers, with Polyad on `polyad`. Keep the
runner workload fixed when measuring operator autoscaling.

## Test profiles

Select one test overlay. Each points to a JSON plan shipped inside this chart:

| Values | Plan | Offered rate | Duration | Arrival cap | Fixture Pods / concurrent batch Jobs |
| --- | --- | --- | --- | --- | --- |
| [values-smoke.yaml](values-smoke.yaml) | [plans/smoke.json](plans/smoke.json) | 0.2/s | 60s | 12 | 1 / 2 |
| [values-steady.yaml](values-steady.yaml) | [plans/steady.json](plans/steady.json) | 2/s | 180s | 360 | 2 / 4 |
| [values-burst.yaml](values-burst.yaml) | [plans/burst.json](plans/burst.json) | 10/s | 30s | 300 | 4 / 8 |

The burst is one short, constant-rate window; it does not include an automatic
ramp or warm-up. These are starting experiments, not capacity claims.

```sh
helm upgrade --install benchmarks charts/polyad-benchmarks -n polyad \
  -f charts/polyad-benchmarks/values-steady.yaml \
  -f studies/load/fixtures/gke-values.yaml
```

`values.yaml` holds common settings and selects smoke by default. The reusable
Graph definitions live in [files/fixture.yaml](files/fixture.yaml), rendered by
the CRD chart's shared renderer. Keep `polyadResources.enabled: false`: it prevents
the dependency from rendering the same definitions separately. The parent loads
the selected JSON plan, merges explicit `polyadResources.variables` overrides,
then resolves and validates the fixture through that renderer.

Plan JSON contains the same `requestId` and `variables` accepted by
`polyad-benchmarks-plan --plan`. Copy a plan and change its request ID for each new
client-composed run. The packaged request ID is not used to activate a Helm
installation; installing or switching profiles never starts traffic.

## Plans and observability

The selected JSON plan supplies typed run settings, fixture replicas and batch
concurrency; explicit `polyadResources.variables` fields override those defaults.
The chart mounts the resolved run and replica settings in a Graph-owned ConfigMap. The runner
reads it once; optional Reloader support restarts the fixture on changes while
leaving finite Jobs alone. [Client plans](../../studies/load/README.md#plans-and-replica-counts)
use these same templates to create isolated compositions with immutable plans.

Optional dependencies provide Prometheus/Grafana, Tempo, and an OpenTelemetry
Collector, with a ServiceMonitor and a starter dashboard. All are disabled by
default; [the GKE overlay](../../studies/load/fixtures/gke-values.yaml) enables them and
separates producers, consumers and the operator across dedicated pools. The
[standalone Argo CD UI](../../terraform/README.md#inspect-the-benchmark-application)
can inspect the fixture Graph and its generated resources. Read the
[monitoring instructions](../../studies/load/README.md#monitoring-and-traces) for
operator trace export, private Grafana access, retained data and existing stacks.
The full dependency values remain available under their chart aliases; upstream
charts also validate their own schemas.

If an operator [telemetry agent](../../docs/operations/telemetry-agents.md) supplies
metrics, add [values-agent-metrics.yaml](values-agent-metrics.yaml). This deployment
overlay enables Prometheus remote-write ingestion and disables duplicate direct
operator scrapes; it can be paired with any of the test profiles above.

## Parameters

### Benchmark selection

| Name                 | Description                                                                                                                           | Value              |
| -------------------- | ------------------------------------------------------------------------------------------------------------------------------------- | ------------------ |
| `benchmark.planFile` | **Type: string.** Chart-relative JSON client plan; its variables supply defaults before explicit polyadResources.variables overrides. | `plans/smoke.json` |

### Shared fixture settings

| Name                                                     | Description                                                                                                                                                            | Value                                         |
| -------------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------- | --------------------------------------------- |
| `polyadResources.enabled`                                | **Type: boolean.** Keep false; the benchmark parent renders the shared CRD templates after merging the selected JSON plan.                                             | `false` |
| `polyadResources.variables.images.runner`                | **Type: string.** Repository of the runner image built from services/runner/Dockerfile.                                                                                | `ghcr.io/astrivant/polyad-benchmarks-runner` |
| `polyadResources.variables.images.fixture`               | **Type: string.** Repository of the fixture image built from services/fixture/Dockerfile; also runs finite batch Jobs.                                                 | `ghcr.io/astrivant/polyad-benchmarks-fixture` |
| `polyadResources.variables.images.tag`                   | **Type: string.** Published immutable image tag shared by the runner and fixture images; build and push both before running.                                           | `0.0.1-alpha3` |
| `polyadResources.variables.images.pullPolicy`            | **Type: string.** Settings for polyadResources.variables.images.pullPolicy.                                                                                            | `IfNotPresent` |
| `polyadResources.variables.run`                          | **Type: object.** Settings for polyadResources.variables.run.                                                                                                          | `{}` |
| `polyadResources.variables.placement.nodeSelector`       | **Type: object.** Required node labels for fixture and batch Pods. Empty uses normal scheduling.                                                                       | `{}` |
| `polyadResources.variables.placement.tolerations`        | **Type: array.** Taints these Pods may tolerate; combine with nodeSelector to choose the experiment pool.                                                              | `[]` |
| `polyadResources.variables.runnerPlacement.nodeSelector` | **Type: object.** Required node labels for the runner Pod. Empty uses normal scheduling independently of consumer placement.                                           | `{}` |
| `polyadResources.variables.runnerPlacement.tolerations`  | **Type: array.** Taints these Pods may tolerate; combine with nodeSelector to choose the experiment pool.                                                              | `[]` |
| `polyadResources.variables.resources.requests.cpu`       | **Type: string.** Settings for polyadResources.variables.resources.requests.cpu.                                                                                       | `100m` |
| `polyadResources.variables.resources.requests.memory`    | **Type: string.** Settings for polyadResources.variables.resources.requests.memory.                                                                                    | `64Mi` |
| `polyadResources.variables.resources.limits.cpu`         | **Type: string.** Settings for polyadResources.variables.resources.limits.cpu.                                                                                         | `500m` |
| `polyadResources.variables.resources.limits.memory`      | **Type: string.** Settings for polyadResources.variables.resources.limits.memory.                                                                                      | `128Mi` |
| `polyadResources.variables.secretName`                   | **Type: string.** Existing Secret in the graph namespace with separate operator and fixture tokens. Empty is only for a private operator with authentication disabled. | `""` |
| `polyadResources.variables.operatorTokenKey`             | **Type: string.** Secret key holding a Polyad credential permitted to submit and read activations in this graph tree.                                                  | `operator-token` |
| `polyadResources.variables.fixtureTokenKey`              | **Type: string.** Secret key holding a separate token authenticating the runner to the fixture; never reuse the operator credential.                                   | `fixture-token` |
| `polyadResources.variables.replicas`                     | **Type: object.** Desired fixture Pods and concurrent batch Jobs, independently of operator replica settings.                                                          | `{}` |
| `polyadResources.variables.reloadOnPlanChange`           | **Type: boolean.** Opt the fixture Deployment into ConfigMap-triggered Reloader restarts. Requires bundled or existing Reloader; apply plan changes between runs.      | `false` |

### Plan reloads

| Name                                        | Description                                                                                                      | Value         |
| ------------------------------------------- | ---------------------------------------------------------------------------------------------------------------- | ------------- |
| `reloader.enabled`                          | **Type: boolean.** Install the pinned reloader dependency; disable to use an administrator-managed installation. | `false` |
| `reloader.reloader.watchGlobally`           | **Type: boolean.** Settings for reloader.reloader.watchGlobally.                                                 | `false` |
| `reloader.reloader.ignoreJobs`              | **Type: boolean.** Settings for reloader.reloader.ignoreJobs.                                                    | `true` |
| `reloader.reloader.ignoreCronJobs`          | **Type: boolean.** Settings for reloader.reloader.ignoreCronJobs.                                                | `true` |
| `reloader.reloader.reloadStrategy`          | **Type: string.** Settings for reloader.reloader.reloadStrategy.                                                 | `annotations` |
| `reloader.reloader.deployment.nodeSelector` | **Type: object.** Settings for reloader.reloader.deployment.nodeSelector.                                        | `{}` |
| `reloader.reloader.deployment.tolerations`  | **Type: array.** Settings for reloader.reloader.deployment.tolerations.                                          | `[]` |

### Operator observations

| Name                              | Description                                                                                                       | Value    |
| --------------------------------- | ----------------------------------------------------------------------------------------------------------------- | -------- |
| `observability.scrapeOperator`    | **Type: boolean.** Scrape directly with a ServiceMonitor; disable when an agent already writes operator series.   | `true` |
| `observability.enabled`           | **Type: boolean.** Settings for observability.enabled.                                                            | `false` |
| `observability.operatorNamespace` | **Type: string.** Namespace containing the operator metrics Service.                                              | `polyad` |
| `observability.operatorRelease`   | **Type: string.** Helm release owning the operator metrics Service.                                               | `polyad` |
| `observability.scrapeInterval`    | **Type: string.** Prometheus scrape interval; 5s resolves short benchmark bursts.                                 | `5s` |
| `observability.metricsSecret`     | **Type: string.** Optional existing bearer credential Secret in this chart namespace; empty for the private demo. | `""` |
| `observability.metricsSecretKey`  | **Type: string.** Key containing the bearer token when metricsSecret is set.                                      | `token` |

### Prometheus and Grafana

| Name                                                                                                   | Description                                                                                                                          | Value                   |
| ------------------------------------------------------------------------------------------------------ | ------------------------------------------------------------------------------------------------------------------------------------ | ----------------------- |
| `monitoring.enabled`                                                                                   | **Type: boolean.** Install the pinned monitoring dependency; disable to use an administrator-managed installation.                   | `false` |
| `monitoring.fullnameOverride`                                                                          | **Type: string.** Settings for monitoring.fullnameOverride.                                                                          | `benchmarks-monitoring` |
| `monitoring.alertmanager.enabled`                                                                      | **Type: boolean.** Settings for monitoring.alertmanager.enabled.                                                                     | `false` |
| `monitoring.grafana.fullnameOverride`                                                                  | **Type: string.** Settings for monitoring.grafana.fullnameOverride.                                                                  | `benchmarks-grafana` |
| `monitoring.grafana.service.type`                                                                      | **Type: string.** Settings for monitoring.grafana.service.type.                                                                      | `ClusterIP` |
| `monitoring.grafana.persistence.enabled`                                                               | **Type: boolean.** Settings for monitoring.grafana.persistence.enabled.                                                              | `true` |
| `monitoring.grafana.persistence.size`                                                                  | **Type: string.** Settings for monitoring.grafana.persistence.size.                                                                  | `5Gi` |
| `monitoring.grafana.additionalDataSources`                                                             | **Type: array.** Settings for monitoring.grafana.additionalDataSources.                                                              | `[{"name": "Tempo", "type": "tempo", "uid": "tempo", "url": "http://benchmarks-tempo:3200", "access": "proxy", "editable": false}]` |
| `monitoring.prometheus.prometheusSpec.retention`                                                       | **Type: string.** Settings for monitoring.prometheus.prometheusSpec.retention.                                                       | `7d` |
| `monitoring.prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes`                | **Type: array.** Settings for monitoring.prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.accessModes.                 | `["ReadWriteOnce"]` |
| `monitoring.prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage` | **Type: string.** Settings for monitoring.prometheus.prometheusSpec.storageSpec.volumeClaimTemplate.spec.resources.requests.storage. | `10Gi` |
| `monitoring.kubeControllerManager.enabled`                                                             | **Type: boolean.** Settings for monitoring.kubeControllerManager.enabled.                                                            | `false` |
| `monitoring.kubeScheduler.enabled`                                                                     | **Type: boolean.** Settings for monitoring.kubeScheduler.enabled.                                                                    | `false` |
| `monitoring.kubeEtcd.enabled`                                                                          | **Type: boolean.** Settings for monitoring.kubeEtcd.enabled.                                                                         | `false` |
| `monitoring.kubeProxy.enabled`                                                                         | **Type: boolean.** Settings for monitoring.kubeProxy.enabled.                                                                        | `false` |

### Trace storage

| Name                        | Description                                                                                                   | Value              |
| --------------------------- | ------------------------------------------------------------------------------------------------------------- | ------------------ |
| `tempo.enabled`             | **Type: boolean.** Install the pinned tempo dependency; disable to use an administrator-managed installation. | `false` |
| `tempo.fullnameOverride`    | **Type: string.** Settings for tempo.fullnameOverride.                                                        | `benchmarks-tempo` |
| `tempo.persistence.enabled` | **Type: boolean.** Settings for tempo.persistence.enabled.                                                    | `true` |
| `tempo.persistence.size`    | **Type: string.** Settings for tempo.persistence.size.                                                        | `10Gi` |
| `tempo.tempo.retention`     | **Type: string.** Settings for tempo.tempo.retention.                                                         | `72h` |

### Trace collection

| Name                                                   | Description                                                                                                       | Value                                  |
| ------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------- | -------------------------------------- |
| `collector.enabled`                                    | **Type: boolean.** Install the pinned collector dependency; disable to use an administrator-managed installation. | `false` |
| `collector.fullnameOverride`                           | **Type: string.** Settings for collector.fullnameOverride.                                                        | `benchmarks-otel` |
| `collector.mode`                                       | **Type: string.** Settings for collector.mode.                                                                    | `deployment` |
| `collector.replicaCount`                               | **Type: integer.** Settings for collector.replicaCount.                                                           | `1` |
| `collector.image.repository`                           | **Type: string.** Settings for collector.image.repository.                                                        | `otel/opentelemetry-collector-contrib` |
| `collector.image.tag`                                  | **Type: string.** Settings for collector.image.tag.                                                               | `0.160.0` |
| `collector.command.name`                               | **Type: string.** Settings for collector.command.name.                                                            | `otelcol-contrib` |
| `collector.config.exporters.otlp/tempo.endpoint`       | **Type: string.** Settings for collector.config.exporters.otlp/tempo.endpoint.                                    | `benchmarks-tempo:4317` |
| `collector.config.exporters.otlp/tempo.tls.insecure`   | **Type: boolean.** Settings for collector.config.exporters.otlp/tempo.tls.insecure.                               | `true` |
| `collector.config.service.pipelines.logs`              | **Type:  or null.** Settings for collector.config.service.pipelines.logs.                                         | `null` |
| `collector.config.service.pipelines.metrics`           | **Type:  or null.** Settings for collector.config.service.pipelines.metrics.                                      | `null` |
| `collector.config.service.pipelines.traces.receivers`  | **Type: array.** Settings for collector.config.service.pipelines.traces.receivers.                                | `["otlp"]` |
| `collector.config.service.pipelines.traces.processors` | **Type: array.** Settings for collector.config.service.pipelines.traces.processors.                               | `["memory_limiter", "batch"]` |
| `collector.config.service.pipelines.traces.exporters`  | **Type: array.** Settings for collector.config.service.pipelines.traces.exporters.                                | `["otlp/tempo"]` |
| `collector.ports.jaeger-compact.enabled`               | **Type: boolean.** Settings for collector.ports.jaeger-compact.enabled.                                           | `false` |
| `collector.ports.jaeger-thrift.enabled`                | **Type: boolean.** Settings for collector.ports.jaeger-thrift.enabled.                                            | `false` |
| `collector.ports.jaeger-grpc.enabled`                  | **Type: boolean.** Settings for collector.ports.jaeger-grpc.enabled.                                              | `false` |
| `collector.ports.zipkin.enabled`                       | **Type: boolean.** Settings for collector.ports.zipkin.enabled.                                                   | `false` |

<!-- Parameters are generated from typed values.yaml annotations. -->
