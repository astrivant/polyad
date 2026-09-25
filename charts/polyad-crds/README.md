# Polyad CRDs and named resources

<!-- toc:start -->
**Table of contents**

- [Installation and upgrades](#installation-and-upgrades)
- [Named instances and defaults](#named-instances-and-defaults)
- [References between resources](#references-between-resources)
- [Reference values by kind](#reference-values-by-kind)
- [Schemas and validation](#schemas-and-validation)
- [Parameters](#parameters)
  - [Resource generation](#resource-generation)
  - [Named resources](#named-resources)
<!-- toc:end -->

Install Polyad's versioned CRDs and generate named resource instances from Helm
values, with defaults, schema validation and cross-resource `tpl` references.
This chart can run independently or as the operator and benchmark charts'
`polyadResources` dependency. It does not install an operator.

## Installation and upgrades

From this repository, install the definitions without creating any instances:

```sh
helm upgrade --install polyad-apis charts/polyad-crds --namespace polyad --create-namespace
```

To create instances, supply your values with `-f application-values.yaml`. The
operator chart already includes this dependency; you need not install it twice:

```sh
helm dependency build charts/polyad
helm upgrade --install polyad charts/polyad --namespace polyad --create-namespace \
  -f operator-values.yaml
```

With the operator chart, place instance maps under `polyadResources`. Set
`polyadResources.enabled=false` only when you install the definitions and manage
instances separately. Standalone `enabled=false` disables instance rendering;
use Helm's `--skip-crds` flag to skip definition installation.

The chart's `version` is independent of the operator release. The operator pins
it in `Chart.yaml` and `Chart.lock`. When changing CRDs, bump this chart's version,
update the parent pin, regenerate schemas and rebuild dependencies. Release CI
packages both charts; it does not stamp this chart with the operator version.

Definitions are under `crds/`, so Helm installs them before submitting instances.
Existing CRDs are skipped. Helm does **not** upgrade or remove these definitions;
review and apply a new version's definitions before upgrading resource instances.
See [Helm's CRD lifecycle](https://helm.sh/docs/chart_best_practices/custom_resource_definitions/).
For a reviewed chart archive:

```sh
helm show crds ./polyad-crds-VERSION.tgz > /tmp/polyad-crds.yaml
kubectl diff --server-side -f /tmp/polyad-crds.yaml
kubectl apply --server-side -f /tmp/polyad-crds.yaml
```

The vendored Dragonfly API accompanies Polyad's APIs, retaining the existing
fresh-install behavior. Its controller remains an operator-chart dependency;
[provenance and refresh instructions](../polyad/UPSTREAM.md) describe its source.

## Named instances and defaults

Each top-level kind is a map keyed by Kubernetes resource name. Entries contain
`spec` and optional `metadata` with labels, annotations or namespace. The map key
sets `metadata.name`; an explicit name must agree. Namespace defaults to the Helm
release namespace. `apiVersion` and `kind` come from the CRD catalog, and `status`
is reserved for controllers.

Defaults come directly from the CRD schema. For example, an omitted
`Daemon.spec.replicas` becomes `1`. Explicit `0` and `false` survive defaulting.
Optional objects are not created unless supplied or given a CRD default: omitting
`statefulSet` or `throughput` does not enable them. Within a supplied object, its
missing defaulted fields are filled recursively.

Status-bearing Polyad CRDs default `status` to `{}` so Flux can check optional
observation fields before the operator's first report. This does not manufacture
an observed generation or claim readiness. Apply upgraded CRDs to enable this
default on existing clusters; see [Flux health semantics](../../docs/operations/fluxcd.md#semantics).

All kind maps are empty in [values.yaml](values.yaml). Installing defaults creates
API definitions, with no example workloads or graphs. These defaults remain the
baseline for chart tests.

## References between resources

Every resource field accepts Helm `tpl`, including fields inside lists and native
workload templates. This standalone example creates a Daemon definition, a group
of copies and a Graph that contains that group:

```yaml
variables:
  image: busybox:1.37.0
  copies: 3
daemons:
  worker:
    spec:
      template:
        spec:
          containers:
            - name: worker
              image: '{{ .Values.variables.image }}'
              command: [sh, -c, 'while true; do sleep 3600; done']
              startupProbe:
                exec: {command: [sh, -c, 'kill -0 1']}
              readinessProbe:
                exec: {command: [sh, -c, 'kill -0 1']}
              livenessProbe:
                exec: {command: [sh, -c, 'kill -0 1']}
replicaGroups:
  workers:
    spec:
      replicas: '{{ .Values.variables.copies }}'
      template:
        kind: Daemon
        ref: '{{ .Values.daemons.worker.metadata.name }}'
graphs:
  pipeline:
    spec:
      mode: persistent
      nodes:
        - name: workers
          kind: ReplicaGroup
          ref: '{{ .Values.replicaGroups.workers.metadata.name }}'
```

When using the operator chart, nest that entire values block under
`polyadResources`. References still use the dependency's local scope, such as
`.Values.daemons.worker.metadata.name`; do not add `polyadResources` inside the
expressions. `.Release`, `.Chart` and `.Values.global` are also available.
Names, namespaces and schema defaults are populated before references resolve.

Typed fields retain their types after templating: replicas become integers,
Boolean fields become Booleans. Use `toJson` or `toYaml` to return an entire object
or list. Known string fields remain strings. In native or application-defined
fields without a declared type, quote numeric-looking strings in the expression,
for example `value: '{{ .Values.variables.copies | quote }}'` for an environment
variable. Use Helm's `required` function for mandatory shared inputs.

References may chain across instances. `maxTplPasses` bounds resolution;
remaining expressions, including cycles, fail rendering. TPL operates on desired
values at Helm render time, not live operator observations or resource status.

## Reference values by kind

Each file below has an empty active map and a fully commented field catalog.
Fields identify their required/optional status, exact CRD defaults, allowed
choices and README-generator types. Required fields apply when their containing
object is present. Values without defaults are illustrative placeholders.
Optional alternatives can be mutually exclusive: copy the fields you need into
your values. Open-ended Kubernetes and application payloads are explicitly
marked so administrators can supply the fields their workloads require.

| Resource kind | Named map and full reference |
| --- | --- |
| Activation | [activations](values-activations.reference.yaml) |
| Composition | [compositions](values-compositions.reference.yaml) |
| Daemon | [daemons](values-daemons.reference.yaml) |
| Dragonfly | [dragonflies](values-dragonflies.reference.yaml) |
| DragonflyPool | [dragonflyPools](values-dragonflyPools.reference.yaml) |
| Gate | [gates](values-gates.reference.yaml) |
| GraphPolicy | [graphPolicies](values-graphPolicies.reference.yaml) |
| Graph | [graphs](values-graphs.reference.yaml) |
| OperatorPool | [operatorPools](values-operatorPools.reference.yaml) |
| PolyGraph | [polygraphs](values-polygraphs.reference.yaml) |
| RemoteScale | [remoteScales](values-remoteScales.reference.yaml) |
| ReplicaGroup | [replicaGroups](values-replicaGroups.reference.yaml) |
| Resource | [resources](values-resources.reference.yaml) |
| Rewrite | [rewrites](values-rewrites.reference.yaml) |
| ShutdownPolicy | [shutdownPolicies](values-shutdownPolicies.reference.yaml) |
| TemporaryConnection | [temporaryConnections](values-temporaryConnections.reference.yaml) |
| Workload | [workloads](values-workloads.reference.yaml) |

These files and their annotations are generated with
`poetry run python scripts/schemas/generate-all.py`. The README generator wrapper
also accepts these reference files: it decodes the commented example for its
parameter table. The normal values checker verifies both active values and
commented type annotations without activating the examples.

## Schemas and validation

The CRDs are the source for values schemas, defaults, the rendering catalog and
reference examples. The [central generation pipeline](../../schemas/README.md)
also publishes the contracts through `polyad_schemas.helm.values_schema(chart="polyad-crds")`.
Use `partial=True` for editor validation of overlays.

Helm validates literal inputs against `values.schema.json`; the renderer checks
resolved types, required fields, choices and scalar bounds after TPL evaluation.
Kubernetes admission enforces CEL rules and API semantics; operator admission
still controls graph policies, permissions and supported requests. A Helm template
does not grant permission to bypass connection negotiation or remote-scale
ownership.

CI runs hypothesis-helm over both charts with three shards each. This chart
explicitly permits an empty default instance bundle through rule `HH1107` (formerly
`HH1009`). The action is pinned to a reviewed commit supporting that rule and the
existing kubeconform wrapper, which supplies Polyad and dependency resource schemas.
Update the action pin, rule exceptions and action inputs together; all other checks
remain enabled. Python tests independently
verify all 17 installed definitions, instance rendering, defaults, references,
invalid results and use through the operator dependency.

## Parameters

### Resource generation

| Name           | Description                                                                                                 | Value  |
| -------------- | ----------------------------------------------------------------------------------------------------------- | ------ |
| `enabled`      | **Type: boolean.** Enable instance rendering; the parent uses this to include the dependency and its CRDs.  | `true` |
| `maxTplPasses` | **Type: integer.** Maximum passes for field references; cycles fail instead of rendering unresolved values. | `16` |
| `global`       | **Type: object.** Shared values inherited from the parent chart; reference them with .Values.global.        | `{}` |
| `variables`    | **Type: object.** Shared inputs for tpl expressions; reference them with .Values.variables.                 | `{}` |

### Named resources

| Name                   | Description                                                                                                            | Value |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------------- | ----- |
| `activations`          | **Type: object.** Activation instances keyed by name; spec and metadata support tpl references to other maps.          | `{}` |
| `compositions`         | **Type: object.** Composition instances keyed by name; spec and metadata support tpl references to other maps.         | `{}` |
| `daemons`              | **Type: object.** Daemon instances keyed by name; spec and metadata support tpl references to other maps.              | `{}` |
| `dragonflies`          | **Type: object.** Dragonfly instances keyed by name; spec and metadata support tpl references to other maps.           | `{}` |
| `dragonflyPools`       | **Type: object.** DragonflyPool instances keyed by name; spec and metadata support tpl references to other maps.       | `{}` |
| `gates`                | **Type: object.** Gate instances keyed by name; spec and metadata support tpl references to other maps.                | `{}` |
| `graphPolicies`        | **Type: object.** GraphPolicy instances keyed by name; spec and metadata support tpl references to other maps.         | `{}` |
| `graphs`               | **Type: object.** Graph instances keyed by name; spec and metadata support tpl references to other maps.               | `{}` |
| `operatorPools`        | **Type: object.** OperatorPool instances keyed by name; spec and metadata support tpl references to other maps.        | `{}` |
| `polygraphs`           | **Type: object.** PolyGraph instances keyed by name; spec and metadata support tpl references to other maps.           | `{}` |
| `remoteScales`         | **Type: object.** RemoteScale instances keyed by name; spec and metadata support tpl references to other maps.         | `{}` |
| `replicaGroups`        | **Type: object.** ReplicaGroup instances keyed by name; spec and metadata support tpl references to other maps.        | `{}` |
| `resources`            | **Type: object.** Resource instances keyed by name; spec and metadata support tpl references to other maps.            | `{}` |
| `rewrites`             | **Type: object.** Rewrite instances keyed by name; spec and metadata support tpl references to other maps.             | `{}` |
| `shutdownPolicies`     | **Type: object.** ShutdownPolicy instances keyed by name; spec and metadata support tpl references to other maps.      | `{}` |
| `temporaryConnections` | **Type: object.** TemporaryConnection instances keyed by name; spec and metadata support tpl references to other maps. | `{}` |
| `workloads`            | **Type: object.** Workload instances keyed by name; spec and metadata support tpl references to other maps.            | `{}` |

<!-- The table below is generated from values.yaml. -->
