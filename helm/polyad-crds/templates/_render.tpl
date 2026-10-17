{{- define "polyad.resources.render" -}}
{{- if .Values.enabled -}}
{{- $root := . -}}
{{- $catalog := .Files.Get "files/resource-catalog.json" | fromJson -}}
{{- $values := deepCopy .Values -}}
{{/* Populate names, namespaces and CRD defaults before evaluating references. */}}
{{- range $group, $definition := $catalog -}}
  {{- range $name, $body := (get $values $group) -}}
    {{- $meta := default dict $body.metadata -}}
    {{- if kindIs "map" $meta -}}
    {{- if not (hasKey $meta "name") }}{{ $_ := set $meta "name" $name }}{{ end -}}
    {{- if not (hasKey $meta "namespace") }}{{ $_ := set $meta "namespace" $root.Release.Namespace }}{{ end -}}
    {{- $_ := set $body "metadata" $meta -}}
    {{- end -}}
    {{- $prepared := include "polyad.resources.value" (dict "value" $body "schema" $definition.schema "context" $root "resolve" false "validate" false "path" (printf "%s.%s" $group $name)) | fromJson -}}
    {{- $_ := set (get $values $group) $name $prepared.value -}}
  {{- end -}}
{{- end -}}
{{- $done := false -}}
{{- range until (int .Values.maxTplPasses) -}}
  {{- if not $done -}}
    {{- $context := merge (dict "Values" $values) (omit $root "Values") -}}
    {{- $next := deepCopy $values -}}
    {{- range $field := list "variables" "global" -}}
      {{- $resolved := include "polyad.resources.value" (dict "value" (get $values $field) "schema" dict "context" $context "resolve" true "validate" false "path" $field) | fromJson -}}
      {{- $_ := set $next $field $resolved.value -}}
    {{- end -}}
    {{- range $group, $definition := $catalog -}}
      {{- range $name, $body := (get $values $group) -}}
        {{- $resolved := include "polyad.resources.value" (dict "value" $body "schema" $definition.schema "context" $context "resolve" true "validate" false "path" (printf "%s.%s" $group $name)) | fromJson -}}
        {{- $_ := set (get $next $group) $name $resolved.value -}}
      {{- end -}}
    {{- end -}}
    {{- $values = $next -}}
    {{- $done = not (contains "{{" (toJson $values)) -}}
  {{- end -}}
{{- end -}}
{{- if not $done }}{{ fail "resource tpl references remain unresolved after maxTplPasses; check for cycles or increase the pass limit" }}{{ end -}}
{{- $context := merge (dict "Values" $values) (omit $root "Values") -}}
{{- range $group, $definition := $catalog -}}
  {{- range $name, $body := (get $values $group) -}}
    {{- $meta := default dict $body.metadata -}}
    {{- if kindIs "map" $meta -}}
    {{- if not (hasKey $meta "name") }}{{ $_ := set $meta "name" $name }}{{ end -}}
    {{- if not (hasKey $meta "namespace") }}{{ $_ := set $meta "namespace" $root.Release.Namespace }}{{ end -}}
    {{- $_ := set $body "metadata" $meta -}}
    {{- end -}}
    {{- $checked := include "polyad.resources.value" (dict "value" $body "schema" $definition.schema "context" $context "resolve" false "validate" true "path" (printf "%s.%s" $group $name)) | fromJson -}}
    {{- $body = $checked.value -}}
    {{- if ne $name $body.metadata.name }}{{ fail (printf "%s.%s: metadata.name must equal the map key" $group $name) }}{{ end -}}
    {{- $_ := set $body "apiVersion" $definition.apiVersion -}}
    {{- $_ := set $body "kind" $definition.kind -}}
---
{{ printf "%s\n" (toYaml $body) }}
  {{- end -}}
{{- end -}}
{{- end -}}

{{- end -}}
