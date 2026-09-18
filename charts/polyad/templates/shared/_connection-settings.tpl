{{/* No secrets in this projection. Every process receives identical typed limits. */}}
{{- define "polyad.connectionEnvironment" -}}
{{- range $name := list "state" "authentication" -}}
{{- $pool := index $.Values.operator.connections $name -}}
{{- if gt (int $pool.minConnections) (int $pool.maxConnections) -}}
{{- fail (printf "operator.connections.%s.minConnections must not exceed maxConnections" $name) -}}
{{- end -}}
{{- end -}}
- name: POLYAD_CONNECTION_SETTINGS
  value: {{ .Values.operator.connections | toJson | quote }}
{{- end -}}

{{- define "polyad.connectionTrigger" -}}
- type: metrics-api
  metricType: AverageValue
  metadata:
    url: {{ printf "http://%s-polyad-metrics.%s.svc:8092/v1/components/%s/connectionPressure" .root.Release.Name .root.Release.Namespace .component | quote }}
    valueLocation: value
    targetValue: {{ divf .root.Values.operator.autoscaling.connections.targetUtilizationPercentage 100 | quote }}
    {{- if .root.Values.metrics.authentication.enabled }}
    authMode: bearer
  authenticationRef:
    name: {{ default (printf "%s-polyad-metrics" .root.Release.Name) .root.Values.keda.authentication.name | quote }}
    {{- end }}
{{- end -}}
