{{- define "polyad.telemetry.config" -}}
{{- $targets := include "polyad.telemetry.targets" . | fromJsonArray -}}
{{- $namespaces := list .Release.Namespace -}}
{{- range $targets }}{{ $namespaces = append $namespaces .namespace }}{{ end -}}
{{- $namespaces = uniq $namespaces -}}
{{- $t := .Values.telemetry -}}
{{- $cluster := default (default .Release.Namespace .Values.global.multiCluster.clusterName) $t.clusterName -}}
{{- $metricSecret := include "polyad.telemetry.metricSecret" . | fromJson -}}
{{- if eq $t.mode "PrometheusAgent" -}}
global:
  scrape_interval: {{ $t.scrapeIntervalSeconds }}s
  scrape_timeout: {{ $t.scrapeTimeoutSeconds }}s
  external_labels:
    cluster: {{ $cluster | quote }}
    release: {{ .Release.Name | quote }}
remote_write:
  - url: {{ $t.remoteWrite.url | quote }}
    {{- with $t.remoteWrite.credentials }}
    {{- if .secretName }}
    {{- if .username }}
    basic_auth:
      username: {{ .username | quote }}
      password_file: /etc/credentials/metrics/token
    {{- else }}
    authorization:
      credentials_file: /etc/credentials/metrics/token
    {{- end }}
    {{- end }}
    {{- end }}
scrape_configs:
{{- range $targets }}
{{- if .metrics }}
  - job_name: {{ printf "polyad/%s" .name | quote }}
    metrics_path: {{ .path | quote }}
    scheme: {{ default "http" .scheme }}
    sample_limit: {{ $t.sampleLimit }}
    {{- if .proxy }}
    authorization:
      credentials_file: /var/run/secrets/kubernetes.io/serviceaccount/token
    tls_config:
      insecure_skip_verify: true
    {{- else if and (eq .name "operator") $metricSecret.name }}
    authorization:
      credentials_file: /etc/credentials/operator/token
    {{- end }}
    kubernetes_sd_configs:
      - role: endpointslice
        namespaces:
          names: {{ $namespaces | toJson }}
    relabel_configs:
      {{- include "polyad.telemetry.rules" (dict "root" $ "target" .) | fromJsonArray | toYaml | nindent 6 }}
{{- end }}
{{- end }}
{{- else -}}
discovery.kubernetes "endpoints" {
  role = "endpointslice"
  namespaces { names = {{ $namespaces | toJson }} }
}
prometheus.remote_write "backend" {
  external_labels = { "cluster" = {{ $cluster | quote }}, "release" = {{ .Release.Name | quote }} }
  endpoint {
    url = {{ $t.remoteWrite.url | quote }}
    {{- with $t.remoteWrite.credentials }}
    {{- if .secretName }}
    {{- if .username }}
    basic_auth {
      username = {{ .username | quote }}
      password_file = "/etc/credentials/metrics/token"
    }
    {{- else }}
    authorization { credentials_file = "/etc/credentials/metrics/token" }
    {{- end }}
    {{- end }}
    {{- end }}
  }
}
{{ range $index, $target := $targets }}
{{- if .metrics }}
discovery.relabel "metrics_{{ $index }}" {
  targets = discovery.kubernetes.endpoints.targets
  {{- range (include "polyad.telemetry.rules" (dict "root" $ "target" .) | fromJsonArray) }}
  rule {
    {{- range $key, $value := . }}
    {{ $key }} = {{ $value | toJson }}
    {{- end }}
  }
  {{- end }}
  rule {
    target_label = "job"
    replacement = {{ printf "polyad/%s" $target.name | quote }}
  }
}
prometheus.scrape "metrics_{{ $index }}" {
  targets = discovery.relabel.metrics_{{ $index }}.output
  forward_to = [prometheus.remote_write.backend.receiver]
  scrape_interval = "{{ $t.scrapeIntervalSeconds }}s"
  scrape_timeout = "{{ $t.scrapeTimeoutSeconds }}s"
  sample_limit = {{ $t.sampleLimit }}
  metrics_path = {{ .path | quote }}
  scheme = {{ default "http" .scheme | quote }}
  clustering { enabled = true }
  {{- if .proxy }}
  authorization { credentials_file = "/var/run/secrets/kubernetes.io/serviceaccount/token" }
  // This pinned dependency uses kube-rbac-proxy's self-signed serving certificate.
  tls_config { insecure_skip_verify = true }
  {{- else if and (eq .name "operator") $metricSecret.name }}
  authorization { credentials_file = "/etc/credentials/operator/token" }
  {{- end }}
}
{{- end }}
{{- end }}
{{ if $t.logs.enabled }}
discovery.kubernetes "pods" {
  role = "pod"
  namespaces { names = {{ $namespaces | toJson }} }
}
discovery.relabel "logs" {
  targets = discovery.kubernetes.pods.targets
  {{- range $targets }}
  {{- if .logs }}
  {{- $labels := list "__meta_kubernetes_namespace" -}}
  {{- $matches := list (regexQuoteMeta .namespace) -}}
  {{- range $label, $value := .podSelector -}}
  {{- $labels = append $labels (printf "__meta_kubernetes_pod_label_%s" (regexReplaceAll "[^a-zA-Z0-9_]" $label "_")) -}}
  {{- $matches = append $matches (regexQuoteMeta $value) -}}
  {{- end }}
  rule {
    source_labels = {{ $labels | toJson }}
    regex = {{ join ";" $matches | quote }}
    target_label = "__tmp_polyad_logs"
    replacement = "true"
  }
  {{- end }}
  {{- end }}
  rule {
    source_labels = ["__tmp_polyad_logs"]
    regex = "true"
    action = "keep"
  }
  {{- range $label, $source := dict "namespace" "namespace" "pod" "pod_name" "container" "pod_container_name" }}
  rule {
    source_labels = [{{ printf "__meta_kubernetes_%s" $source | quote }}]
    target_label = {{ $label | quote }}
  }
  {{- end }}
  rule {
    target_label = "cluster"
    replacement = {{ $cluster | quote }}
  }
  rule {
    target_label = "release"
    replacement = {{ .Release.Name | quote }}
  }
}
loki.source.kubernetes "pods" {
  targets = discovery.relabel.logs.output
  forward_to = [loki.write.backend.receiver]
  clustering { enabled = true }
}
loki.write "backend" {
  endpoint {
    url = {{ $t.logs.url | quote }}
    {{- with $t.logs.credentials }}
    {{- if .secretName }}
    {{- if .username }}
    basic_auth {
      username = {{ .username | quote }}
      password_file = "/etc/credentials/logs/token"
    }
    {{- else }}
    bearer_token_file = "/etc/credentials/logs/token"
    {{- end }}
    {{- end }}
    {{- end }}
  }
}
{{- end }}
{{ if $t.traces.enabled }}
{{- with $t.traces.credentials }}
{{- if .secretName }}
local.file "trace_token" {
  filename = "/etc/credentials/traces/token"
  is_secret = true
}
{{- if .username }}
otelcol.auth.basic "traces" {
  client_auth {
    username = {{ .username | quote }}
    password = local.file.trace_token.content
  }
}
{{- end }}
{{- end }}
{{- end }}
otelcol.receiver.otlp "traces" {
  grpc { endpoint = "[" + sys.env("POD_IP") + "]:4317" }
  http { endpoint = "[" + sys.env("POD_IP") + "]:4318" }
  output { traces = [otelcol.processor.batch.traces.input] }
}
otelcol.processor.batch "traces" {
  output { traces = [otelcol.exporter.otlphttp.backend.input] }
}
otelcol.exporter.otlphttp "backend" {
  client {
    endpoint = {{ $t.traces.endpoint | quote }}
    {{- with $t.traces.credentials }}
    {{- if .secretName }}
    {{- if .username }}
    auth = otelcol.auth.basic.traces.handler
    {{- else }}
    headers = { "Authorization" = "Bearer " + local.file.trace_token.content }
    {{- end }}
    {{- end }}
    {{- end }}
  }
}
{{- end }}
{{- end -}}
{{- end -}}
