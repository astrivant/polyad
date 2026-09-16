{{/* Normalize a fresh values copy for each template; never mutate Helm's shared context. */}}
{{- define "polyad.profileValues" -}}
{{- $values := deepCopy .Values -}}
{{- $selected := list -}}
{{- range $name, $enabled := $values.tags -}}
{{- if $enabled }}{{ $selected = append $selected $name }}{{ end -}}
{{- end -}}
{{- if gt (len $selected) 1 }}{{ fail "select at most one deployment tag: singular or ha" }}{{ end -}}
{{- $profile := "ha" -}}
{{- if $selected }}{{ $profile = first $selected }}{{ end -}}
{{- $_ := set $values "_profile" $profile -}}
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
