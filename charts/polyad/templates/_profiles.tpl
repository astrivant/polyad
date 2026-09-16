{{/* Normalize a fresh values copy for each template; never mutate Helm's shared context. */}}
{{- define "polyad.profileValues" -}}
{{- $values := deepCopy .Values -}}
{{- $profile := ternary "ha" "singular" $values.ha -}}
{{- $_ := set $values "_profile" $profile -}}
{{- $authEndpoints := dict -}}
{{- range $group, $keys := pick $values.authentication "services" "operators" -}}
{{- $names := dict -}}
{{- range $keys -}}
{{- if hasKey $names .name }}{{ fail "authentication key names must be unique within each group" }}{{ end -}}
{{- $_ := set $names .name true -}}
{{- range .endpoints -}}
{{- $_ := set $authEndpoints . true -}}
{{- if has . (list "activations" "throughput") }}{{ $_ := set $authEndpoints "composition" true }}{{ end -}}
{{- if eq . "topology" }}{{ $_ := set $authEndpoints "events" true }}{{ end -}}
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
{{- if eq $values.operator.replicaCount nil -}}
{{- $_ := set $values.operator "replicaCount" (ternary 1 2 (eq $profile "singular")) -}}
{{- end -}}
{{- if eq $profile "singular" -}}
{{- if or (ne (int $values.operator.replicaCount) 1) $values.operator.autoscaling.enabled }}{{ fail "singular requires one operator replica and operator.autoscaling.enabled=false" }}{{ end -}}
{{- if or (eq $values.architecture.mode "Distributed") $values.rootControlPlane.enabled }}{{ fail "split components and remote execution management require the ha profile" }}{{ end -}}
{{- else -}}
{{- if or (lt (int $values.operator.replicaCount) 2) (and $values.operator.autoscaling.enabled (lt (int $values.operator.autoscaling.minReplicas) 2)) }}{{ fail "ha requires at least two operator replicas and an autoscaling minimum of two" }}{{ end -}}
{{- end -}}
{{- if and $values.architecture.autoscaling (ne $values.architecture.mode "Distributed") }}{{ fail "component autoscaling requires architecture.mode=Distributed" }}{{ end -}}
{{- if and $values.rootControlPlane.pools (not $values.rootControlPlane.enabled) }}{{ fail "rootControlPlane.pools requires rootControlPlane.enabled=true" }}{{ end -}}
{{- toJson $values -}}
{{- end -}}
