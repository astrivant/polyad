{{/*
Render one opt-in workload Service port. This helper creates no resources by itself.
Application protocols are distinct from Kubernetes' TCP transport and mesh mTLS.
Use the same numeric targetPort in Graph edges / ConnectionRequest ports.

Usage:
  {{ include "polyad.workloadServicePort" (dict "protocol" "grpc" "name" "worker" "port" 50051 "targetPort" 50052) }}
*/}}
{{- define "polyad.workloadServicePort" -}}
{{- $protocols := dict "http" "http" "https" "https" "ws" "http" "wss" "https" "grpc" "grpc" "grpcs" "tls" "amqp" "tcp" "amqps" "tls" "redis" "tcp" "rediss" "tls" "tcp" "tcp" "tls" "tls" -}}
{{- $scheme := required "workload Service port requires protocol" .protocol -}}
{{- if not (hasKey $protocols $scheme) -}}
  {{- fail "unsupported workload Service protocol" -}}
{{- end -}}
{{- $port := required "workload Service port requires port" .port -}}
{{- $target := $port -}}
{{- if hasKey . "targetPort" -}}
  {{- $target = .targetPort -}}
{{- end -}}
{{- range $value := list $port $target -}}
  {{- if or (not (regexMatch "^[0-9]+$" (toString $value))) (lt (int $value) 1) (gt (int $value) 65535) -}}
    {{- fail "workload Service port and targetPort must be integers from 1 through 65535" -}}
  {{- end -}}
{{- end -}}
{{- $application := get $protocols $scheme -}}
{{- $name := printf "%s-%s" $application (default "workload" .name) -}}
{{- if or (gt (len $name) 63) (not (regexMatch "^[a-z]([-a-z0-9]*[a-z0-9])?$" $name)) -}}
  {{- fail "workload Service port name must be a DNS label of at most 63 characters" -}}
{{- end -}}
{{- dict "name" $name "port" (int $port) "targetPort" (int $target) "protocol" "TCP" "appProtocol" $application | toYaml -}}
{{- end -}}
