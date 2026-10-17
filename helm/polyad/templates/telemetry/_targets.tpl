{{- define "polyad.telemetry.serviceName" -}}
{{- $name := printf "%s-telemetry-%s" .root.Release.Name .target -}}
{{- if gt (len $name) 63 -}}
{{- printf "%s-%s" (trunc 54 $name | trimSuffix "-") (sha256sum $name | trunc 8) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- end -}}

{{/* Render dependency selectors from their pinned charts, preserving name/label overrides. */}}
{{- define "polyad.telemetry.targets" -}}
{{- $targets := list -}}
{{- $operator := dict "app.kubernetes.io/instance" .Release.Name "app.kubernetes.io/name" "polyad" -}}
{{- $metricSelector := deepCopy $operator -}}
{{- if eq .Values.architecture.mode "Distributed" }}{{ $_ := set $metricSelector "polyad.astrivant.com/component" "telemetry" }}{{ end -}}
{{- $targets = append $targets (dict "name" "operator" "namespace" .Release.Namespace "podSelector" $operator "metricSelector" $metricSelector "metrics" .Values.metrics.enabled "port" 8092 "path" "/metrics" "logs" true) -}}
{{- if .Values.observer.enabled -}}
{{- $targets = append $targets (dict "name" "observer" "namespace" .Release.Namespace "podSelector" (dict "app.kubernetes.io/instance" .Release.Name "app.kubernetes.io/name" "polyad-observer") "metrics" false "logs" true) -}}
{{- end -}}
{{- if .Values.dragonfly.enabled -}}
{{- $targets = append $targets (dict "name" "cache" "namespace" .Release.Namespace "podSelector" (dict "app" (printf "%s-queue" .Release.Name)) "metrics" true "port" 9999 "path" "/metrics" "logs" true) -}}
{{- end -}}
{{- range $suffix, $enabled := dict "state" (and .Values.postgresql.enabled .Values.postgresql.managed) "authentication" (and .Values.authentication.storage.enabled .Values.authentication.storage.separateDatabase .Values.authentication.storage.managed) -}}
{{- if $enabled -}}
{{- $targets = append $targets (dict "name" $suffix "namespace" $.Release.Namespace "podSelector" (dict "cnpg.io/cluster" (printf "%s-%s" $.Release.Name $suffix)) "metrics" true "port" 9187 "path" "/metrics" "logs" true) -}}
{{- end -}}
{{- end -}}
{{- $dependencies := dict "dragonflyOperator" (list "deployment.yaml") "istiod" (list "deployment.yaml") "istioIngress" (list "deployment.yaml") "istioEastWestGateway" (list "deployment.yaml") "kedaOperator" (list "manager/deployment.yaml" "metrics-server/deployment.yaml" "webhooks/deployment.yaml") -}}
{{- range $alias, $files := $dependencies -}}
{{- with index $.Subcharts $alias -}}
{{- $base := printf "%s/charts/%s/templates" (trimSuffix "/templates" $.Template.BasePath) $alias -}}
{{- $context := merge (dict "Template" (dict "BasePath" $base)) (deepCopy .) -}}
{{- if hasKey $context.Values "_internal_defaults_do_not_set" }}{{ $_ := include (printf "%s/zzz_profile.yaml" $base) $context }}{{ end -}}
{{- range $index, $file := $files -}}
{{- $object := include (printf "%s/%s" $base $file) $context | fromYaml -}}
{{- if eq (get $object "kind") "Deployment" -}}
{{- $target := dict "name" (printf "%s-%d" (lower $alias) $index) "namespace" (default $.Release.Namespace $object.metadata.namespace) "podSelector" $object.spec.selector.matchLabels "metrics" true "port" 8080 "path" "/metrics" "logs" true -}}
{{- if eq $alias "dragonflyOperator" -}}
{{- if $context.Values.rbacProxy.enabled -}}
{{- $_ := set $target "port" 8443 -}}
{{- $_ := set $target "scheme" "https" -}}
{{- $_ := set $target "proxy" true -}}
{{- end -}}
{{- else if eq $alias "istiod" -}}
{{- $_ := set $target "port" 15014 -}}
{{- else if has $alias (list "istioIngress" "istioEastWestGateway") -}}
{{- $_ := set $target "port" 15090 -}}
{{- $_ := set $target "path" "/stats/prometheus" -}}
{{- else if eq $alias "kedaOperator" -}}
{{- $component := index (list "operator" "metricServer" "webhooks") $index -}}
{{- $settings := get $context.Values.prometheus $component -}}
{{- $_ := set $target "metrics" $settings.enabled -}}
{{- $_ := set $target "port" $settings.port -}}
{{- end -}}
{{- $targets = append $targets $target -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if .Values.mesh.operator.enabled -}}
{{- $targets = append $targets (dict "name" "operator-proxy" "namespace" .Release.Namespace "podSelector" $operator "metrics" true "port" 15090 "path" "/stats/prometheus" "logs" false) -}}
{{- end -}}
{{- $names := dict -}}
{{- range .Values.telemetry.extraTargets -}}
{{- if hasKey $names .name }}{{ fail "telemetry.extraTargets names must be unique" }}{{ end -}}
{{- $_ := set $names .name true -}}
{{- $targets = append $targets (merge (dict "name" (printf "extra-%s" .name) "metrics" true) (deepCopy .)) -}}
{{- end -}}
{{- $targets = append $targets (dict "name" "agent" "namespace" .Release.Namespace "podSelector" (dict "app.kubernetes.io/instance" .Release.Name "app.kubernetes.io/name" "polyad-telemetry") "metrics" true "port" (ternary 12345 9090 (eq .Values.telemetry.mode "Alloy")) "path" "/metrics" "logs" true) -}}
{{- toJson $targets -}}
{{- end -}}

{{- define "polyad.telemetry.metricSecret" -}}
{{- $secret := deepCopy .Values.telemetry.operatorMetricsSecret -}}
{{- if and (not $secret.name) .Values.metrics.authentication.enabled -}}
{{- if hasKey .Values._authEndpoints "metrics" }}{{ fail "named metrics API keys require telemetry.operatorMetricsSecret.name" }}{{ end -}}
{{- $_ := set $secret "name" (default (printf "%s-polyad-metrics" .Release.Name) .Values.metrics.authentication.existingSecret) -}}
{{- $_ := set $secret "key" .Values.metrics.authentication.secretKey -}}
{{- end -}}
{{- toJson $secret -}}
{{- end -}}

{{- define "polyad.telemetry.rules" -}}
{{- $rules := list
  (dict "source_labels" (list "__meta_kubernetes_namespace" "__meta_kubernetes_service_name" "__meta_kubernetes_endpointslice_port_name") "regex" (printf "%s;%s;metrics" (regexQuoteMeta .target.namespace) (include "polyad.telemetry.serviceName" (dict "root" .root "target" .target.name) | regexQuoteMeta)) "action" "keep")
  (dict "source_labels" (list "__meta_kubernetes_endpointslice_endpoint_conditions_ready") "regex" "false" "action" "drop")
  (dict "source_labels" (list "__meta_kubernetes_namespace") "target_label" "namespace")
  (dict "source_labels" (list "__meta_kubernetes_pod_name") "target_label" "pod")
  (dict "source_labels" (list "__meta_kubernetes_service_name") "target_label" "service")
-}}
{{- toJson $rules -}}
{{- end -}}
