{{/* Shared by the Dense operator, Distributed bootstrap and component Pod templates. */}}
{{- define "polyad.operatorDeployment" -}}
{{- if .Values.events.rebalance.enabled -}}
{{- if not .Values.events.enabled }}{{ fail "event rebalancing requires events.enabled" }}{{ end -}}
{{- if lt (float64 .Values.operator.terminationGracePeriodSeconds) (addf 35 (mulf .Values.events.rebalance.drainSeconds (ternary 2 1 .Values.mesh.operator.enabled))) }}{{ fail "operator.terminationGracePeriodSeconds must cover preStop drain, optional sidecar drain and 35 seconds of shutdown" }}{{ end -}}
{{- if and (gt (float64 .Values.events.rebalance.maxConnectionSeconds) 0.0) (lt (float64 .Values.events.rebalance.maxConnectionSeconds) (float64 .Values.events.rebalance.cooldownSeconds)) }}{{ fail "events.rebalance.maxConnectionSeconds must be zero or at least cooldownSeconds" }}{{ end -}}
{{- end -}}
{{- if gt (float64 .Values.operator.writeQueue.validationIntervalSeconds) (float64 .Values.operator.writeQueue.validationWindowSeconds) -}}
{{- fail "operator.writeQueue.validationIntervalSeconds must not exceed validationWindowSeconds" -}}
{{- end -}}
{{- $auth := or .Values.authentication.services .Values.authentication.operators -}}
{{- $required := eq .Values.authentication.mode "Required" -}}
{{- $apiToken := and $required .Values.api.enabled (not (hasKey .Values._authEndpoints "composition")) -}}
{{- $eventToken := and $required .Values.events.enabled (not (hasKey .Values._authEndpoints "events")) -}}
{{- $metricsToken := and .Values.metrics.enabled .Values.metrics.authentication.enabled (not (hasKey .Values._authEndpoints "metrics")) -}}
apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ .Release.Name }}-polyad
  labels:
    polyad.astrivant.com/deployment-profile: {{ .Values._profile }}
    {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled (eq .Values.architecture.mode "Distributed") }}
    polyad.astrivant.com/internal: "true"
    {{- end }}
  {{- if or .Values.worker.enabled (and .Values.externalSecrets.enabled .Values.externalSecrets.reloadOnChange) }}
  annotations:
    {{- if .Values.worker.enabled }}
    polyad.astrivant.com/worker-attachment: {{ list .Values.worker.rootClusterName .Values.worker.rootNamespace .Values.worker.rootDeployment .Values.worker.rootGraph .Values.worker.poolName | toJson | quote }}
    polyad.astrivant.com/worker-scaling: {{ .Values.worker.scalingAuthority | quote }}
    {{- end }}
    {{- if and .Values.externalSecrets.enabled .Values.externalSecrets.reloadOnChange }}
    reloader.stakater.com/search: "true"
    {{- end }}
  {{- end }}
spec:
  {{- if and (not .Values.operator.autoscaling.enabled) (not (and .Values.worker.enabled (eq .Values.worker.scalingAuthority "Root"))) }}
  replicas: {{ .Values.operator.replicaCount }}
  {{- end }}
  strategy:
    type: RollingUpdate
    rollingUpdate:
      maxUnavailable: 0
      maxSurge: 1
  selector:
    matchLabels:
      app.kubernetes.io/instance: {{ .Release.Name }}
      app.kubernetes.io/name: polyad
      polyad.astrivant.com/bootstrap: {{ not .Values.worker.enabled | quote }}
  template:
    metadata:
      annotations:
        checksum/credentials: {{ include (print $.Template.BasePath "/shared/credentials.yaml") . | sha256sum }}
        {{- if $auth }}
        checksum/authentication: {{ .Values.authentication | toJson | sha256sum }}
        {{- end }}
        {{- if .Values.mesh.operator.enabled }}
        sidecar.istio.io/inject: "true"
        sidecar.istio.io/nativeSidecar: "true"
        {{- include "polyad.proxyResources" . | nindent 8 }}
        {{- $proxy := dict "holdApplicationUntilProxyStarts" true -}}
        {{- if .Values.events.rebalance.enabled }}{{ $_ := set $proxy "terminationDrainDuration" (printf "%vs" .Values.events.rebalance.drainSeconds) }}{{ end }}
        proxy.istio.io/config: {{ $proxy | toJson | quote }}
        {{- end }}
      labels:
        app.kubernetes.io/instance: {{ .Release.Name }}
        app.kubernetes.io/name: polyad
        polyad.astrivant.com/deployment-profile: {{ .Values._profile }}
        polyad.astrivant.com/bootstrap: {{ not .Values.worker.enabled | quote }}
        polyad.astrivant.com/component: {{ ternary "executor" (ternary "bootstrap" "dense" (eq .Values.architecture.mode "Distributed")) .Values.worker.enabled }}
        {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled (eq .Values.architecture.mode "Distributed") }}
        polyad.astrivant.com/internal: "true"
        {{- end }}
        {{- if .Values.mesh.operator.enabled }}
        sidecar.istio.io/inject: "true"
        {{- end }}
    spec:
      {{- with .Values.operator.nodeSelector }}
      nodeSelector:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      {{- with .Values.operator.tolerations }}
      tolerations:
        {{- toYaml . | nindent 8 }}
      {{- end }}
      serviceAccountName: {{ .Release.Name }}-polyad
      {{- if .Values.worker.enabled }}
      automountServiceAccountToken: false
      {{- end }}
      terminationGracePeriodSeconds: {{ .Values.operator.terminationGracePeriodSeconds }}
      securityContext:
        runAsNonRoot: true
        runAsUser: 65532
        runAsGroup: 65532
        fsGroup: 65532
        seccompProfile:
          type: RuntimeDefault
      containers:
        - name: operator
          image: {{ printf "%s:%s" .Values.operator.image.repository .Values.operator.image.tag | quote }}
          imagePullPolicy: {{ .Values.operator.image.pullPolicy }}
          command: [/usr/bin/tini, --, python, -m, polyad.operator.runtime]
          args:
            - --namespace={{ ternary .Values.worker.rootNamespace .Release.Namespace .Values.worker.enabled }}
          env:
            {{- include "polyad.connectionEnvironment" . | nindent 12 }}
            {{- include "polyad.podContextEnv" . | nindent 12 }}
            {{- include "polyad.tracingEnv" . | nindent 12 }}
            {{- include "polyad.postgresql.recordEncryptionEnv" . | nindent 12 }}
            - name: POLYAD_SERVICE_ACCESS
              value: {{ .Values.operator.serviceAccess | toJson | quote }}
            - name: POLYAD_AUTH_MODE
              value: {{ .Values.authentication.mode | quote }}
            - name: POLYAD_AUTH_BACKEND
              value: {{ .Values.authentication.backend | quote }}
            {{- if $auth }}
            - name: POLYAD_AUTH_CONFIG_FILE
              value: /var/run/polyad/authentication/config.json
            - name: POLYAD_WORKLOAD_CREDENTIALS_FILE
              value: /var/run/polyad/workload-credentials/config.json
            {{- end }}
            {{- if .Values.authentication.storage.enabled }}
            - name: POLYAD_AUTH_DATABASE_DSN_FILE
              value: /var/run/polyad/auth-database/uri
            {{- end }}
            - name: POLYAD_COMPONENT
              value: {{ ternary "executor" (ternary "bootstrap" "dense" (eq .Values.architecture.mode "Distributed")) .Values.worker.enabled | quote }}
            {{- if .Values.worker.enabled }}
            - name: POLYAD_ROOT_WORKER
              value: "true"
            - name: POLYAD_WORKER_POOL
              value: {{ .Values.worker.poolName | quote }}
            - name: POLYAD_WORKER_DEPLOYMENT
              value: {{ printf "%s-polyad" .Release.Name | quote }}
            - name: POLYAD_WORKER_CLUSTER
              value: {{ .Values.global.multiCluster.clusterName | quote }}
            - name: KUBECONFIG
              value: /var/run/polyad/root/config
            {{- end }}
            {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled }}
            - name: POLYAD_SELF_GRAPH
              value: {{ ternary .Values.worker.rootGraph (printf "%s-atlas" .Release.Name) .Values.worker.enabled | quote }}
            - name: POLYAD_SELF_GRAPH_KIND
              value: PolyGraph
            {{- if eq .Values.architecture.mode "Distributed" }}
            - name: POLYAD_COMPONENT_GRAPH
              value: {{ printf "%s-control-plane" .Release.Name | quote }}
            {{- end }}
            {{- else if eq .Values.architecture.mode "Distributed" }}
            - name: POLYAD_SELF_GRAPH
              value: {{ printf "%s-control-plane" .Release.Name | quote }}
            {{- end }}
            - name: POLYAD_POSTGRES_ENABLED
              value: {{ .Values.postgresql.enabled | quote }}
            - name: POLYAD_POSTGRES_EVENTS_ENABLED
              value: {{ and .Values.postgresql.enabled .Values.postgresql.events.enabled | quote }}
            - name: POLYAD_POSTGRES_EVENTS_RETENTION_DAYS
              value: {{ .Values.postgresql.events.retentionDays | quote }}
            {{- if and .Values.dragonfly.enabled .Values.dragonfly.ha.enabled .Values.dragonfly.autoscaling.enabled }}
            - name: POLYAD_DRAGONFLY_POOL
              value: {{ printf "%s-queue" .Release.Name | quote }}
            {{- end }}
            {{- if .Values.postgresql.enabled }}
            - name: POLYAD_POSTGRES_DSN_FILE
              value: /var/run/polyad/postgresql/uri
            {{- end }}
            - name: POLYAD_STATE_SCOPE
              value: {{ default (printf "%s/%s" .Release.Namespace .Release.Name) .Values.postgresql.scope | quote }}
            - name: POLYAD_OPERATOR_IMAGE
              value: {{ printf "%s:%s" .Values.operator.image.repository .Values.operator.image.tag | quote }}
            - name: POLYAD_ROOT_ENABLED
              value: {{ or .Values.worker.enabled .Values.rootControlPlane.enabled | quote }}
            {{- if .Values.rootControlPlane.enabled }}
            - name: POLYAD_LOCAL_SERVICES
              value: {{ include "polyad.localServices" . | quote }}
            {{- end }}
            {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled }}
            - name: POLYAD_ROOT_MESH_PEERS
              value: {{ .Values.rootControlPlane.meshPeers | toJson | quote }}
            - name: POLYAD_ROOT_DEPLOYMENT
              value: {{ ternary .Values.worker.rootDeployment (printf "%s-polyad" .Release.Name) .Values.worker.enabled | quote }}
            {{- end }}
            - name: POLYAD_ESO_RELOAD_ENABLED
              value: {{ and .Values.externalSecrets.enabled .Values.externalSecrets.reloadOnChange | quote }}
            - name: POLYAD_LOG_LEVEL
              value: {{ .Values.operator.logLevel | quote }}
            - name: POLYAD_WRITE_QUEUE_MAX_PENDING
              value: {{ .Values.operator.writeQueue.maxPending | int | quote }}
            - name: POLYAD_MUTATION_PLANNER_PARALLELISM
              value: {{ .Values.operator.writeQueue.plannerParallelism | int | quote }}
            - name: POLYAD_RECONCILIATION_COOLDOWN_SECONDS
              value: {{ .Values.operator.writeQueue.reconciliationCooldownSeconds | quote }}
            - name: POLYAD_RECONCILIATION_BURST
              value: {{ .Values.operator.writeQueue.reconciliationBurst | int | quote }}
            - name: POLYAD_CONNECTION_PULSE_COOLDOWN_SECONDS
              value: {{ .Values.connections.pulses.cooldownSeconds | quote }}
            - name: POLYAD_CONNECTION_PULSE_BURST
              value: {{ .Values.connections.pulses.burst | int | quote }}
            - name: POLYAD_WRITE_MAX_IN_FLIGHT
              value: {{ .Values.operator.writeQueue.maxInFlight | int | quote }}
            - name: POLYAD_WRITE_VALIDATION_WORKERS
              value: {{ .Values.operator.writeQueue.validationWorkers | int | quote }}
            - name: POLYAD_RECONCILIATION_WORKERS
              value: {{ .Values.operator.writeQueue.reconciliationWorkers | int | quote }}
            - name: POLYAD_WRITE_VALIDATION_INTERVAL_SECONDS
              value: {{ .Values.operator.writeQueue.validationIntervalSeconds | quote }}
            - name: POLYAD_WRITE_VALIDATION_WINDOW_SECONDS
              value: {{ .Values.operator.writeQueue.validationWindowSeconds | quote }}
            - name: POLYAD_WRITE_VALIDATION_BURST
              value: {{ .Values.operator.writeQueue.validationBurst | int | quote }}
            - name: POLYAD_CHEEGER_MAX_VERTICES
              value: {{ .Values.operator.cheeger.maxVertices | int | quote }}
            - name: POLYAD_CHEEGER_MAX_CUTS
              value: {{ .Values.operator.cheeger.maxCuts | int | quote }}
            - name: POLYAD_CHEEGER_TIMEOUT_SECONDS
              value: {{ .Values.operator.cheeger.timeoutSeconds | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_ENABLED
              value: {{ .Values.operator.cheeger.reduction.enabled | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_MAX_VERTICES
              value: {{ .Values.operator.cheeger.reduction.maxVertices | int | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_COMPONENTS
              value: {{ .Values.operator.cheeger.reduction.components | int | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_SUPERNODES
              value: {{ .Values.operator.cheeger.reduction.supernodes | int | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_CACHE
              value: {{ .Values.operator.cheeger.reduction.cache | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_CACHE_ENTRIES
              value: {{ .Values.operator.cheeger.reduction.cacheEntries | int | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_MAX_EDGE_CHURN
              value: {{ .Values.operator.cheeger.reduction.maxEdgeChurn | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_STRATEGY
              value: {{ .Values.operator.cheeger.reduction.strategy | quote }}
            - name: POLYAD_CHEEGER_REDUCTION_TARGET_SECONDS
              value: {{ .Values.operator.cheeger.reduction.targetSeconds | quote }}
            - name: POLYAD_RESCAN_INTERVAL_SECONDS
              value: {{ .Values.operator.tuning.rescanIntervalSeconds | quote }}
            - name: POLYAD_CONSUME_INTERVAL_SECONDS
              value: {{ .Values.operator.tuning.consumeIntervalSeconds | quote }}
            - name: POLYAD_METRICS_INTERVAL_SECONDS
              value: {{ .Values.operator.tuning.metricsIntervalSeconds | quote }}
            - name: POLYAD_BACKLOG_INTERVAL_SECONDS
              value: {{ .Values.operator.tuning.backlogIntervalSeconds | quote }}
            - name: POLYAD_METRICS_ENABLED
              value: {{ .Values.metrics.enabled | quote }}
            - name: POLYAD_API_ENABLED
              value: {{ .Values.api.enabled | quote }}
            - name: POLYAD_EVENTS_ENABLED
              value: {{ .Values.events.enabled | quote }}
            - name: POLYAD_EVENTS_WEBSOCKETS_ENABLED
              value: {{ and .Values.events.enabled .Values.events.websockets.enabled | quote }}
            {{- if .Values.events.rebalance.enabled }}
            - name: POLYAD_EVENTS_REBALANCE
              value: {{ .Values.events.rebalance | toJson | quote }}
            - name: POLYAD_EVENTS_SERVICE
              value: {{ printf "%s-polyad-events" .Release.Name | quote }}
            {{- end }}
            - name: POLYAD_EVENT_PUBLICATION_ENABLED
              value: {{ or .Values.events.enabled (and .Values.postgresql.enabled .Values.postgresql.events.enabled) | quote }}
            - name: POLYAD_EVENTS_MAX_EVENT_BYTES
              value: {{ .Values.events.maxEventBytes | int | quote }}
            - name: POLYAD_EVENTS_READ_BATCH_SIZE
              value: {{ .Values.events.readBatchSize | int | quote }}
            - name: POLYAD_EVENTS_POLL_INTERVAL_SECONDS
              value: {{ .Values.events.pollIntervalSeconds | quote }}
            - name: POLYAD_EVENTS_RETENTION
              value: {{ .Values.events.retention | int | quote }}
            - name: POLYAD_WORKLOAD_API_URL
              value: {{ default (ternary (printf "http://%s-polyad-api.%s.svc:8090" .Release.Name .Release.Namespace) "" .Values.api.enabled) .Values.rootControlPlane.endpoints.api | quote }}
            - name: POLYAD_WORKLOAD_EVENTS_URL
              value: {{ default (ternary (printf "http://%s-polyad-events.%s.svc:8091" .Release.Name .Release.Namespace) "" .Values.events.enabled) .Values.rootControlPlane.endpoints.events | quote }}
            - name: POLYAD_WORKLOAD_METRICS_URL
              value: {{ default (ternary (printf "http://%s-polyad-metrics.%s.svc:8092" .Release.Name .Release.Namespace) "" .Values.metrics.enabled) .Values.rootControlPlane.endpoints.metrics | quote }}
            - name: POLYAD_WORKLOAD_CONNECTIONS_URL
              value: {{ ternary (printf "http://%s-polyad-connections.%s.svc:8093" .Release.Name .Release.Namespace) "" .Values.connections.enabled | quote }}
            - name: POLYAD_CONNECTIONS_ENABLED
              value: {{ .Values.connections.enabled | quote }}
            - name: POLYAD_CONNECTIONS_SCOPE
              value: {{ .Values.connections.scope | quote }}
            - name: POLYAD_CONNECTIONS_NAMESPACE
              value: {{ .Values.connections.namespace | quote }}
            - name: POLYAD_CONNECTIONS_MAX_TTL
              value: {{ .Values.connections.maxTtlSeconds | quote }}
            - name: POLYAD_CONNECTIONS_RETENTION
              value: {{ .Values.connections.retentionSeconds | quote }}
            {{- if and .Values.metrics.enabled .Values.metrics.authentication.enabled }}
            - name: POLYAD_METRICS_AUTH_ENABLED
              value: "true"
            - name: POLYAD_METRICS_TOKEN_FILE
              value: /var/run/polyad/metrics/token
            {{- end }}
            - name: POLYAD_METRICS_GRAPH_SPECTRA
              value: {{ .Values.metrics.graphSpectra | quote }}
            - name: POLYAD_METRICS_GRAPH_LABELS
              value: {{ .Values.metrics.graphLabels | quote }}
            - name: POLYAD_CAPACITY_ENABLED
              value: {{ .Values.capacity.enabled | quote }}
            - name: POLYAD_VPA_ENABLED
              value: {{ .Values.verticalPodAutoscaling.enabled | quote }}
            {{- if .Values.capacity.enabled }}
            - name: POLYAD_CAPACITY_CLASS
              value: {{ .Values.capacity.provisioningClassName | quote }}
            - name: POLYAD_CAPACITY_MAX_PODS
              value: {{ .Values.capacity.maxPods | quote }}
            - name: POLYAD_CAPACITY_IMAGE
              value: {{ .Values.capacity.placeholderImage | quote }}
            - name: POLYAD_CAPACITY_PRIORITY
              value: {{ .Values.capacity.priorityClass.value | quote }}
            - name: POLYAD_CAPACITY_PRIORITY_CLASS
              value: {{ default (printf "%s-%s-polyad-capacity" .Release.Namespace .Release.Name) .Values.capacity.priorityClass.name | quote }}
            {{- end }}
            {{- if .Values.mesh.operator.enabled }}
            - name: POLYAD_OPERATOR_MESH_ENABLED
              value: "true"
            - name: POLYAD_MESH_INJECTION_STATUS
              valueFrom:
                fieldRef:
                  fieldPath: metadata.annotations['sidecar.istio.io/status']
            {{- end }}
            - name: POLYAD_MESH_ENABLED
              value: {{ .Values.mesh.enabled | quote }}
            - name: POLYAD_ISTIO_NAMESPACE
              value: {{ .Values.global.istioNamespace | quote }}
            - name: POLYAD_CLUSTER_NAME
              value: {{ ternary .Values.worker.rootClusterName .Values.global.multiCluster.clusterName .Values.worker.enabled | quote }}
            {{- if .Values.mesh.multicluster.enabled }}
            - name: POLYAD_MESH_PEERS
              value: {{ .Values.mesh.multicluster.peers | toJson | quote }}
            {{- end }}
            {{- if .Values.federation.enabled }}
            - name: POLYAD_FEDERATION_CLUSTERS
              value: {{ .Values.federation.clusters | toJson | quote }}
            {{- end }}
            {{- if .Values.dragonfly.existingSecret }}
            - name: POLYAD_CACHE_URL_FILE
              value: /var/run/polyad/cache/url
            {{- end }}
            - name: POLYAD_CACHE_URL
              {{- if .Values.dragonfly.existingSecret }}
              valueFrom:
                secretKeyRef:
                  name: {{ .Values.dragonfly.existingSecret | quote }}
                  key: url
              {{- else if .Values.dragonfly.enabled }}
              value: {{ printf "redis://%s-queue:6379/0" .Release.Name | quote }}
              {{- else }}
              value: {{ required "dragonfly.externalUrl or dragonfly.existingSecret is required when the bundled cache is disabled" .Values.dragonfly.externalUrl | quote }}
              {{- end }}
            {{- if or .Values.api.enabled .Values.events.enabled .Values.connections.enabled }}
            - name: POLYAD_API_RATE_LIMIT_ENABLED
              value: {{ .Values.api.rateLimit.enabled | quote }}
            - name: POLYAD_API_REQUESTS_PER_MINUTE
              value: {{ .Values.api.rateLimit.requestsPerMinute | quote }}
            {{- end }}
            {{- if .Values.api.enabled }}
            {{- if $apiToken }}
            - name: POLYAD_API_TOKEN_FILE
              value: /var/run/polyad/api/token
            {{- end }}
            {{- end }}
            {{- if .Values.events.enabled }}
            - name: POLYAD_EVENTS_CONNECTIONS
              value: {{ .Values.events.maxConnections | quote }}
            {{- if $eventToken }}
            - name: POLYAD_EVENTS_TOKEN_FILE
              value: /var/run/polyad/events/token
            {{- end }}
            {{- end }}
          volumeMounts:{{ if not (or $auth .Values.postgresql.enabled .Values.federation.enabled $apiToken $eventToken .Values.dragonfly.existingSecret $metricsToken) }} []{{ end }}
            {{- include "polyad.postgresql.recordEncryptionMount" . | nindent 12 }}
            {{- if $auth }}
            - name: authentication
              mountPath: /var/run/polyad/authentication
              readOnly: true
            - name: workload-credentials
              mountPath: /var/run/polyad/workload-credentials
              readOnly: true
            {{- end }}
            {{- if .Values.authentication.storage.enabled }}
            - name: authentication-database
              mountPath: /var/run/polyad/auth-database
              readOnly: true
            {{- end }}
            {{- if .Values.postgresql.enabled }}
            - name: postgres-credentials
              mountPath: /var/run/polyad/postgresql
              readOnly: true
            {{- end }}
            {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled }}
            - name: root-credentials
              mountPath: /var/run/polyad/root
              readOnly: true
            {{- end }}
            {{- if .Values.federation.enabled }}
            - name: remote-transport
              mountPath: /var/run/polyad/transport
            {{- range .Values.federation.clusters }}
            - name: cluster-{{ .name }}
              mountPath: /var/run/polyad/clusters/{{ .name }}
              readOnly: true
            {{- end }}
            {{- end }}
            {{- if $apiToken }}
            - name: api-credentials
              mountPath: /var/run/polyad/api
              readOnly: true
            {{- end }}
            {{- if $eventToken }}
            - name: events-credentials
              mountPath: /var/run/polyad/events
              readOnly: true
            {{- end }}
            {{- if $metricsToken }}
            - name: metrics-credentials
              mountPath: /var/run/polyad/metrics
              readOnly: true
            {{- end }}
            {{- if .Values.dragonfly.existingSecret }}
            - name: cache-credentials
              mountPath: /var/run/polyad/cache
              readOnly: true
            {{- end }}
          ports:
            {{- if .Values.connections.enabled }}
            - name: connections
              containerPort: 8093
            {{- end }}
            {{- if .Values.metrics.enabled }}
            - name: metrics
              containerPort: 8092
            {{- end }}
            {{- if .Values.events.enabled }}
            - name: events
              containerPort: 8091
            {{- end }}
            {{- if .Values.api.enabled }}
            - name: composition
              containerPort: 8090
            {{- end }}
            - name: health
              containerPort: 8080
          securityContext:
            allowPrivilegeEscalation: false
            readOnlyRootFilesystem: true
            capabilities:
              drop: [ALL]
          resources:
            {{- toYaml .Values.operator.resources | nindent 12 }}
          startupProbe:
            httpGet:
              path: /healthz
              port: health
            periodSeconds: 2
            failureThreshold: 60
          livenessProbe:
            httpGet:
              path: /healthz
              port: health
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          readinessProbe:
            exec:
              command: [python, -m, polyad.operator.lifecycle.probes, --ready]
            periodSeconds: 10
            timeoutSeconds: 5
            failureThreshold: 3
          {{- if .Values.events.rebalance.enabled }}
          lifecycle:
            preStop:
              exec:
                command: [python, -m, polyad.events.rebalance]
          {{- end }}
      volumes:{{ if not (or $auth .Values.postgresql.enabled .Values.federation.enabled $apiToken $eventToken .Values.dragonfly.existingSecret $metricsToken) }} []{{ end }}
        {{- include "polyad.postgresql.recordEncryptionVolume" . | nindent 8 }}
        {{- if $auth }}
        {{- include "polyad.authenticationVolume" . | nindent 8 }}
        - name: workload-credentials
          configMap:
            name: {{ .Release.Name }}-polyad-authentication
        {{- end }}
        {{- if .Values.authentication.storage.enabled }}
        {{- include "polyad.authenticationDatabaseVolume" . | nindent 8 }}
        {{- end }}
        {{- if .Values.postgresql.enabled }}
        - name: postgres-credentials
          secret:
            secretName: {{ ternary (printf "%s-state-app" .Release.Name) .Values.postgresql.existingSecret .Values.postgresql.managed | quote }}
            items:
              - key: {{ ternary "uri" .Values.postgresql.secretKey .Values.postgresql.managed | quote }}
                path: uri
        {{- end }}
        {{- if or .Values.worker.enabled .Values.rootControlPlane.enabled }}
        - name: root-credentials
          secret:
            secretName: {{ .Values.rootControlPlane.kubeconfigSecret | quote }}
            defaultMode: 0440
            items:
              - key: config
                path: config
        {{- end }}
        {{- if .Values.federation.enabled }}
        - name: remote-transport
          emptyDir:
            medium: Memory
            sizeLimit: 16Mi
        {{- range .Values.federation.clusters }}
        - name: cluster-{{ .name }}
          secret:
            secretName: {{ .kubeconfigSecret | quote }}
            defaultMode: 0440
            items:
              - key: config
                path: config
        {{- end }}
        {{- end }}
        {{- if $apiToken }}
        - name: api-credentials
          secret:
            secretName: {{ default (printf "%s-polyad-api" .Release.Name) .Values.api.existingSecret | quote }}
            items:
              - key: token
                path: token
        {{- end }}
        {{- if $eventToken }}
        - name: events-credentials
          secret:
            secretName: {{ default (printf "%s-polyad-events" .Release.Name) .Values.events.existingSecret | quote }}
            items:
              - key: token
                path: token
        {{- end }}
        {{- if $metricsToken }}
        - name: metrics-credentials
          secret:
            secretName: {{ default (printf "%s-polyad-metrics" .Release.Name) .Values.metrics.authentication.existingSecret | quote }}
            items:
              - key: {{ .Values.metrics.authentication.secretKey | quote }}
                path: token
        {{- end }}
        {{- if .Values.dragonfly.existingSecret }}
        - name: cache-credentials
          secret:
            secretName: {{ .Values.dragonfly.existingSecret | quote }}
            items:
              - key: url
                path: url
        {{- end }}
{{- end -}}
