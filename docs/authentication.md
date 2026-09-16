# Authentication and external credentials

KEDA reads scheduler demand through Polyad's dedicated metrics API. An optional
bearer token protects every route on that listener, including workload metrics
and OpenAPI. This credential grants read-only access to the operator namespace's
metrics; it does not authorize composition submissions or event subscriptions.
Use separate credentials for those APIs.

## Connect KEDA with a Secret

Create a Secret named `polyad-metrics` in the operator namespace with a `token`
key, or let ESO populate it as shown below. Enable these chart values:

```yaml
metrics:
  enabled: true
  authentication:
    enabled: true
    existingSecret: polyad-metrics
    secretKey: token
keda:
  authentication:
    enabled: true
    name: polyad-metrics
```

The chart mounts the Secret in each operator Pod and creates a namespaced
`TriggerAuthentication` referencing the same Secret key as parameter `token`.
Add these fields to a KEDA `metrics-api` trigger:

```yaml
metadata:
  authMode: bearer
authenticationRef:
  name: polyad-metrics
```

See the [complete ReplicaGroup ScaledObject example](replication.md#connect-keda).
KEDA's [Metrics API scaler](https://keda.sh/docs/2.20/scalers/metrics-api/)
sends the token as a bearer credential. The
[TriggerAuthentication](https://keda.sh/docs/2.20/concepts/authentication/#re-use-credentials-and-delegate-auth-with-triggerauthentication)
must be in the ScaledObject's namespace, alongside the referenced Secret.
Several ScaledObjects can share it. Install KEDA and its CRDs separately.

Tokens must be nonempty printable ASCII without whitespace or a trailing
newline. Missing or invalid credentials return HTTP 401. Enabling authentication
without a valid token fails startup. Authentication is opt-in; disabling it keeps
the existing unauthenticated listener. For local runs, use
`POLYAD_METRICS_AUTH_ENABLED=true` with `POLYAD_METRICS_TOKEN_FILE` or
`POLYAD_METRICS_TOKEN`.

## Populate Secrets with ESO

Install External Secrets Operator with the `external-secrets.io/v1` API and
configure a `SecretStore` or `ClusterSecretStore`. Then add the following chart
values to the authenticated setup above:

```yaml
externalSecrets:
  enabled: true
  refreshInterval: 1h
  secretStoreRef:
    name: platform-vault
    kind: ClusterSecretStore
  secrets:
    - name: polyad-metrics
      data:
        - secretKey: token
          remoteRef:
            key: production/polyad/metrics
            property: token
```

Each entry generates an
[ExternalSecret](https://external-secrets.io/latest/api/externalsecret/) with
periodic refresh and an owned target Secret of the same name. Omit `property`
when the provider entry is itself the token; `remoteRef.version` can select a
provider version. Provider entries must already exist. The chart declares
references; it does not upload inline values into your secret manager or create
the SecretStore and its provider credentials.

The same mappings support all operator credentials:

| Consumer | Chart Secret reference | Required Secret key |
| --- | --- | --- |
| Metrics and KEDA | `metrics.authentication.existingSecret` | `metrics.authentication.secretKey`, default `token` |
| Composition API | `api.existingSecret` | `token` |
| Event subscriptions | `events.existingSecret` | `token` |
| Shared Redis/Dragonfly connection | `dragonfly.existingSecret` | `url` containing the complete Redis connection URL |
| Read-only observers | `observer.existingSecret` | `token` |
| Remote Graph management | `federation.clusters[].kubeconfigSecret` | `config` containing the destination kubeconfig |

Add one `externalSecrets.secrets` entry per target and point the corresponding
`existingSecret` value at its name. For example, a cache Secret can map the
provider's `url` property to `secretKey: url`; use `rediss://` when your cache
requires TLS. This configures the operator's connection credentials, not the
cache server's users or passwords. ESO mappings can also populate other required
Secrets, but those consumers must reference them explicitly.

Wait for ESO's target Secrets to exist before expecting operator Pods to start.
Duplicate targets, duplicate data keys and collisions with inline managed
credentials are rejected at Helm render time. Authentication and ESO resources
are disabled by default. No KEDA or ESO controllers are installed by this chart.

Inline `metrics.authentication.key` is also supported when `existingSecret` is
empty. Inline credentials become part of Helm release data; prefer an existing
Secret or ESO for production. Do not configure both owners for the same Secret.

## Restart consumers after rotation

ESO synchronizes Secret data. To request workload restarts, install
[Stakater Reloader](https://github.com/stakater/Reloader#3--targeted-reload-match--search-annotations)
in the cluster and enable both chart settings:

```yaml
externalSecrets:
  enabled: true
  reloadOnChange: true
```

Keep the SecretStore and Secret mappings from the example above. Reloading is
disabled by default. The chart adds `reloader.stakater.com/match: "true"` under
each ExternalSecret's `spec.target.template.metadata.annotations`, so the
generated **Secret** carries the marker. It adds
`reloader.stakater.com/search: "true"` to the operator and enabled observer
Deployments' **top-level metadata**. Pod template annotations are a different
location and do not opt a controller into Reloader.

Reloader's search mode restarts a consumer only when it references a marked
Secret that changes. Operator API, metrics, event, cache and federation credentials
are covered when their references point to these ESO targets. Observer tokens
are covered in the same way. No Secret data is embedded in reload annotations,
and the chart does not install Reloader. Use its opt-in configuration rather
than `--auto-reload-all` if these per-workload choices should control restarts.

Graph services opt in individually through their Daemon definition:

```yaml
apiVersion: polyad.astrivant.com/v1alpha1
kind: Daemon
metadata:
  name: secret-aware-service
spec:
  reloadOnSecretChange: true
  controller: Deployment
  template:
    spec:
      containers:
        - name: service
          image: your-service-image
          envFrom:
            - secretRef:
                name: service-credentials
          startupProbe:
            tcpSocket: {port: 8080}
          readinessProbe:
            tcpSocket: {port: 8080}
          livenessProbe:
            tcpSocket: {port: 8080}
```

Declare `service-credentials` as an ESO target in the workload's namespace and
reference this Daemon from a Graph or ReplicaGroup. The operator emits the
search annotation on the resulting Deployment, or StatefulSet when selected.
`spec.reloadOnSecretChange` defaults to `false` and takes effect only when both
chart settings are enabled on the **executing cluster's operator**. Replicas and
activations of the Daemon retain the setting. For an ESO target managed outside
this chart, add the same match annotation to that ExternalSecret's target
template. Secret mounts and Secret environment references can trigger reloads.

Finite Workloads remain Jobs; this option does not rerun completed work.
Reloader follows the native controller's update behavior: Daemon Deployments
use `Recreate`, while StatefulSets use their configured update strategy.
`OnDelete` still requires Pod deletion, and a rolling partition can retain older
Pods. Choose a rollout strategy consistent with the service's availability needs.
Changing the Daemon opt-in or the operator's reload setting changes the desired
controller revision and uses Polyad's normal replacement admission. Subsequent
Secret rotations let Reloader update that controller in place.

These restarts are independent and do not enforce ordering between graph nodes,
replicas or clusters. The [graph rollout and rotation proposal](rotations.md)
describes separate policies for ordered adoption, readiness barriers and root
coordination; that proposed API is not implemented yet.

## Namespace, transport and rotation

The chart creates ExternalSecrets and TriggerAuthentication in its release
namespace. For a ScaledObject or Prometheus monitor in another namespace,
provision a local credential Secret from the same provider entry and configure
that consumer's authentication reference there. Credentials are not copied
automatically across namespaces. Polyad does not generate
ClusterTriggerAuthentication resources.

Bearer authentication works alongside `networkPolicy.metricsPeers` and
`mesh.operator.metricsPrincipals`. Allow KEDA's traffic through any enabled
policy. Protect credentials in transit with Istio mTLS or an HTTPS proxy; the
operator's metrics listener itself serves HTTP. KEDA's Kubernetes permissions
to update a workload's scale subresource remain a separate requirement.

Projected API, events, metrics and cache Secret files are fingerprinted at
startup and checked every five seconds. After Kubernetes projects a changed or
removed credential, the replica reports that replacement is required through
its existing health probes; Kubernetes restarts it to load the new credential.
Credentials are not refreshed in the running process. Inline Secret edits also
change the Pod template checksum during a Helm upgrade.

KEDA's credential reads, Secret projection and Pod replacement are asynchronous.
Rotation can temporarily produce 401 or 503 responses; it is not an atomic
credential switch. Health probes retain their separate endpoint and do not
require the metrics token. Missing or stale metrics remain unavailable rather
than being reported as zero demand.
