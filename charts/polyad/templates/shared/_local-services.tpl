{{/* Observe rendered identities without copying Pod or Secret contents into the inventory. */}}
{{- define "polyad.localServices" -}}
{{- $sources := dict "endpoints" (list) "observer" (list) "dragonfly" (list) "postgresql" (list) "mesh" (list) "keda" (list) -}}
{{- range $file := list "service.yaml" "events-service.yaml" "metrics-service.yaml" "connections.yaml" -}}
{{- $_ := set $sources "endpoints" (append (get $sources "endpoints") (dict "path" (printf "%s/shared/%s" $.Template.BasePath $file) "context" $)) -}}
{{- end -}}
{{- range $group, $files := dict "observer" (list "multicluster/observer.yaml") "dragonfly" (list "shared/dragonfly.yaml") "postgresql" (list "shared/postgresql.yaml" "shared/authentication-database.yaml") -}}
{{- range $files -}}
{{- $_ := set $sources $group (append (get $sources $group) (dict "path" (printf "%s/%s" $.Template.BasePath .) "context" $)) -}}
{{- end -}}
{{- end -}}
{{- $dependencies := dict "dragonflyOperator" (dict "group" "dragonfly" "files" (list "deployment.yaml" "service.yaml")) "istiod" (dict "group" "mesh" "files" (list "deployment.yaml" "service.yaml" "remote-istiod-service.yaml")) "istioIngress" (dict "group" "mesh" "files" (list "deployment.yaml" "service.yaml")) "istioEastWest" (dict "group" "mesh" "files" (list "deployment.yaml" "service.yaml")) "kedaOperator" (dict "group" "keda" "files" (list "manager/deployment.yaml" "manager/service.yaml" "metrics-server/deployment.yaml" "metrics-server/service.yaml" "webhooks/deployment.yaml" "webhooks/service.yaml")) -}}
{{- range $alias, $settings := $dependencies -}}
{{- with index $.Subcharts $alias -}}
{{- $base := printf "%s/charts/%s/templates" (trimSuffix "/templates" $.Template.BasePath) $alias -}}
{{- $context := merge (dict "Template" (dict "BasePath" $base)) (deepCopy .) -}}
{{/* Istio applies profiles in an earlier template; repeat that step on this isolated copy. */}}
{{- if hasKey $context.Values "_internal_defaults_do_not_set" -}}
{{- $_ := include (printf "%s/zzz_profile.yaml" $base) $context -}}
{{- end -}}
{{- range $settings.files -}}
{{- $_ := set $sources $settings.group (append (get $sources $settings.group) (dict "path" (printf "%s/%s" $base .) "context" $context)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- $inventory := dict -}}
{{- range $group, $templates := $sources -}}
{{- $targets := list -}}
{{- range $templates -}}
{{- $rendered := include .path .context -}}
{{- range regexSplit "(?m)^---[ \\t]*$" $rendered -1 -}}
{{- $object := fromYaml . -}}
{{- if has (get $object "kind") (list "Deployment" "StatefulSet" "DaemonSet" "Service" "Dragonfly" "Cluster") -}}
{{- $targets = append $targets (dict "kind" $object.kind "namespace" (default $.Release.Namespace $object.metadata.namespace) "name" $object.metadata.name) -}}
{{- if eq $object.kind "Dragonfly" -}}
{{/* The upstream controller creates this cache StatefulSet; observe its rollout too. */}}
{{- $targets = append $targets (dict "kind" "StatefulSet" "namespace" (default $.Release.Namespace $object.metadata.namespace) "name" $object.metadata.name) -}}
{{- $targets = append $targets (dict "kind" "Service" "namespace" (default $.Release.Namespace $object.metadata.namespace) "name" $object.metadata.name) -}}
{{- else if eq $object.kind "Cluster" -}}
{{- range $suffix := list "rw" "r" "ro" -}}
{{- $targets = append $targets (dict "kind" "Service" "namespace" (default $.Release.Namespace $object.metadata.namespace) "name" (printf "%s-%s" $object.metadata.name $suffix)) -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- end -}}
{{- if $targets }}{{ $_ := set $inventory $group $targets }}{{ end -}}
{{- end -}}
{{- if and (not .Values.keda.install) .Values.keda.observation.enabled -}}
{{- $targets := list -}}
{{- range $kind, $names := dict "Deployment" .Values.keda.observation.deployments "Service" .Values.keda.observation.services -}}
{{- range $names -}}
{{- $targets = append $targets (dict "kind" $kind "namespace" $.Values.keda.observation.namespace "name" .) -}}
{{- end -}}
{{- end -}}
{{- if not $targets }}{{ fail "KEDA observation requires at least one Deployment or Service" }}{{ end -}}
{{- $_ := set $inventory "keda" $targets -}}
{{- end -}}
{{- if .Values.telemetry.enabled -}}
{{- $targets := list (dict "kind" "StatefulSet" "namespace" .Release.Namespace "name" (printf "%s-telemetry" .Release.Name)) (dict "kind" "Service" "namespace" .Release.Namespace "name" (printf "%s-telemetry" .Release.Name)) -}}
{{- if .Values.telemetry.traces.enabled -}}
{{- $targets = append $targets (dict "kind" "Service" "namespace" .Release.Namespace "name" (include "polyad.telemetry.serviceName" (dict "root" . "target" "otlp"))) -}}
{{- end -}}
{{- range (include "polyad.telemetry.targets" . | fromJsonArray) -}}
{{- if .metrics -}}
{{- $targets = append $targets (dict "kind" "Service" "namespace" .namespace "name" (include "polyad.telemetry.serviceName" (dict "root" $ "target" .name))) -}}
{{- end -}}
{{- end -}}
{{- $_ := set $inventory "collectors" $targets -}}
{{- end -}}
{{- toJson $inventory -}}
{{- end -}}
