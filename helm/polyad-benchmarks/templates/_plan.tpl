{{/* One resolved plan feeds the fixture manifests and release notes. */}}
{{- define "polyad.benchmarks.values" -}}
{{- if .Values.polyadResources.enabled }}{{ fail "polyadResources.enabled must remain false: benchmark fixtures use the dependency renderer after loading their JSON plan" }}{{ end -}}
{{- $raw := required "benchmark.planFile must name a JSON plan packaged in this chart" (.Files.Get .Values.benchmark.planFile) -}}
{{- $plan := mustFromJson $raw -}}
{{- if not (kindIs "map" $plan.variables) }}{{ fail "benchmark plan requires a variables mapping" }}{{ end -}}
{{- $fixture := .Files.Get "files/fixture.yaml" | fromYaml -}}
{{- $values := mergeOverwrite (deepCopy .Subcharts.polyadResources.Values) $fixture (deepCopy .Values.polyadResources) -}}
{{- $_ := set $values "variables" (mergeOverwrite (deepCopy $plan.variables) (deepCopy .Values.polyadResources.variables)) -}}
{{- $_ := set $values "enabled" true -}}
{{- $values | toJson -}}
{{- end -}}
