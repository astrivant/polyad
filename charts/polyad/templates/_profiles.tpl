{{/* Normalize a fresh values copy for each template; never mutate Helm's shared context. */}}
{{- define "polyad.profileValues" -}}
{{- $values := deepCopy .Values -}}
{{- $profile := ternary "ha" "singular" $values.ha -}}
{{- $_ := set $values "_profile" $profile -}}
{{- if $values.worker.enabled -}}
{{- if $values.keda.install }}{{ fail "install KEDA at the root; worker releases must set keda.install=false" }}{{ end -}}
{{- range $field := list "rootNamespace" "rootClusterName" "rootDeployment" "rootGraph" "poolName" -}}
{{- if not (index $values.worker $field) }}{{ fail (printf "worker.enabled requires worker.%s" $field) }}{{ end -}}
{{- end -}}
{{- if or (not $values.global.multiCluster.clusterName) (eq $values.global.multiCluster.clusterName $values.worker.rootClusterName) }}{{ fail "worker hosting cluster must be set and distinct from worker.rootClusterName" }}{{ end -}}
{{- if or $values.rootControlPlane.enabled (ne $values.architecture.mode "Dense") }}{{ fail "worker mode requires rootControlPlane.enabled=false and architecture.mode=Dense" }}{{ end -}}
{{- if or $values.api.enabled $values.events.enabled $values.metrics.enabled $values.connections.enabled $values.observer.enabled }}{{ fail "worker mode exposes only health; disable api, events, metrics, connections and observer listeners" }}{{ end -}}
{{- if or $values.dragonfly.enabled (not $values.dragonfly.existingSecret) (not $values.rootControlPlane.kubeconfigSecret) (not $values.federation.enabled) }}{{ fail "worker mode requires external root/cache Secrets, federation.enabled=true and dragonfly.enabled=false" }}{{ end -}}
{{- if or (and $values.postgresql.enabled $values.postgresql.managed) (and $values.authentication.storage.enabled $values.authentication.storage.managed) }}{{ fail "workers may connect to existing root databases but must not provision their own databases" }}{{ end -}}
{{- if or $values.mesh.install $values.mesh.operator.enabled $values.mesh.ingress.enabled $values.mesh.ingress.gatewayAPI.enabled $values.mesh.multicluster.eastWest.enabled $values.mesh.egress.gateway.enabled }}{{ fail "install mesh infrastructure separately; worker Pods use root HTTPS and cache connections" }}{{ end -}}
{{- $host := dict -}}
{{- range $values.federation.clusters -}}
{{- if eq .name $values.global.multiCluster.clusterName }}{{ $_ := set $host "namespace" .namespace }}{{ end -}}
{{- end -}}
{{- if ne (get $host "namespace") .Release.Namespace }}{{ fail "worker federation registry must include its hosting cluster with this release namespace" }}{{ end -}}
{{- if and (eq $values.worker.scalingAuthority "Root") (or $values.operator.autoscaling.enabled (ne $values.operator.replicaCount nil)) }}{{ fail "Root scaling omits Helm replicas and HPA; set counts on the root OperatorPool" }}{{ end -}}
{{- if and $values.postgresql.enabled (not $values.postgresql.scope) }}{{ fail "workers using PostgreSQL must set the same postgresql.scope as the root" }}{{ end -}}
{{- end -}}
{{- $authEndpoints := dict -}}
{{- if and $values.telemetry.enabled (not $values.worker.enabled) }}{{ $_ := set $values.metrics "enabled" true }}{{ end -}}
{{- range $group, $keys := pick $values.authentication "services" "operators" -}}
{{- $names := dict -}}
{{- range $keys -}}
{{- if hasKey $names .name }}{{ fail "authentication key names must be unique within each group" }}{{ end -}}
{{- $_ := set $names .name true -}}
{{- range .endpoints -}}
{{- $_ := set $authEndpoints . true -}}
{{- if has . (list "activations" "throughput") }}{{ $_ := set $authEndpoints "composition" true }}{{ end -}}
{{- if has . (list "topology" "discovery") }}{{ $_ := set $authEndpoints "events" true }}{{ end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $_ := set $values "_authEndpoints" $authEndpoints -}}
{{- if eq $values.authentication.mode "Disabled" -}}
{{- $_ := set $values.api.rateLimit "enabled" false -}}
{{- $_ := set $values.metrics.authentication "enabled" false -}}
{{- $_ := set $values.keda.authentication "enabled" false -}}
{{- else if hasKey $authEndpoints "metrics" -}}
{{- $_ := set $values.metrics.authentication "enabled" true -}}
{{- end -}}
{{- if eq $values.authentication.mode "Required" -}}
{{- if and $values.api.enabled (not (hasKey $authEndpoints "composition")) (not (or $values.api.key $values.api.existingSecret)) }}{{ fail "API requires api.key, api.existingSecret, or a named inbound key" }}{{ end -}}
{{- if and $values.events.enabled (not (hasKey $authEndpoints "events")) (not (or $values.events.key $values.events.existingSecret)) }}{{ fail "events require events.key, events.existingSecret, or a named inbound key" }}{{ end -}}
{{- end -}}
{{- if and $values.dragonfly.enabled $values.dragonfly.ha.enabled $values.dragonfly.autoscaling.enabled -}}
{{- $_ := set $values.metrics "enabled" true -}}
{{- end -}}
{{- if and (eq $values.operator.replicaCount nil) (not (and $values.worker.enabled (eq $values.worker.scalingAuthority "Root"))) -}}
{{- $_ := set $values.operator "replicaCount" (ternary 1 2 (eq $profile "singular")) -}}
{{- end -}}
{{- if and $values.worker.enabled (eq $values.worker.scalingAuthority "Root") -}}
{{/* Replica intent belongs to the root; ha selects no local replica floor here. */}}
{{- else if eq $profile "singular" -}}
{{- if or (ne (int $values.operator.replicaCount) 1) $values.operator.autoscaling.enabled }}{{ fail "singular requires one operator replica and operator.autoscaling.enabled=false" }}{{ end -}}
{{- if or (eq $values.architecture.mode "Distributed") $values.rootControlPlane.enabled }}{{ fail "split components and remote execution management require the ha profile" }}{{ end -}}
{{- else -}}
{{- if or (lt (int $values.operator.replicaCount) 2) (and $values.operator.autoscaling.enabled (lt (int $values.operator.autoscaling.minReplicas) 2)) }}{{ fail "ha requires at least two operator replicas and an autoscaling minimum of two" }}{{ end -}}
{{- end -}}
{{- if and $values.architecture.autoscaling (ne $values.architecture.mode "Distributed") }}{{ fail "component autoscaling requires architecture.mode=Distributed" }}{{ end -}}
{{- if and $values.rootControlPlane.pools (not $values.rootControlPlane.enabled) }}{{ fail "rootControlPlane.pools requires rootControlPlane.enabled=true" }}{{ end -}}
{{- if $values.operator.autoscaling.connections.enabled -}}
{{- if or (not $values.ha) $values.worker.enabled }}{{ fail "connection-pressure autoscaling requires ha and a local operator/component target; remote workers retain their pool scaling authority" }}{{ end -}}
{{- if not (or $values.operator.autoscaling.enabled $values.architecture.autoscaling) }}{{ fail "connection-pressure autoscaling requires operator.autoscaling.enabled or architecture.autoscaling" }}{{ end -}}
{{- $_ := set $values.metrics "enabled" true -}}
{{- if and $values.metrics.authentication.enabled (not $values.keda.authentication.enabled) }}{{ fail "authenticated connection-pressure autoscaling requires keda.authentication.enabled" }}{{ end -}}
{{- end -}}
{{- toJson $values -}}
{{- end -}}
