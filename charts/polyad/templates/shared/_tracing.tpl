{{- define "polyad.tracingEnv" -}}
- name: POLYAD_TRACING_ENABLED
  value: {{ .Values.tracing.enabled | quote }}
- name: POLYAD_LOGS_ENABLED
  value: {{ .Values.tracing.logs.enabled | quote }}
{{- if or .Values.tracing.enabled .Values.tracing.logs.enabled }}
- name: OTEL_SERVICE_NAME
  value: {{ .Values.tracing.serviceName | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ .Values.tracing.resourceAttributes | quote }}
{{- end }}
{{- if .Values.tracing.enabled }}
- name: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
  value: http/protobuf
- name: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
  {{- if and .Values.telemetry.enabled .Values.telemetry.traces.enabled .Values.telemetry.traces.routeOperator }}
  value: {{ printf "http://%s.%s.svc:4318/v1/traces" (include "polyad.telemetry.serviceName" (dict "root" . "target" "otlp")) .Release.Namespace | quote }}
  {{- else }}
  value: {{ .Values.tracing.endpoint | quote }}
  {{- end }}
- name: OTEL_TRACES_SAMPLER
  value: parentbased_traceidratio
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ .Values.tracing.samplingRatio | quote }}
- name: OTEL_EXPORTER_OTLP_TRACES_TIMEOUT
  value: {{ .Values.tracing.timeoutSeconds | quote }}
{{- if .Values.tracing.headersSecret }}
- name: OTEL_EXPORTER_OTLP_TRACES_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.tracing.headersSecret | quote }}
      key: headers
{{- end }}
{{- end }}
{{- if .Values.tracing.logs.enabled }}
- name: OTEL_EXPORTER_OTLP_LOGS_PROTOCOL
  value: http/protobuf
- name: OTEL_EXPORTER_OTLP_LOGS_ENDPOINT
  value: {{ .Values.tracing.logs.endpoint | quote }}
- name: OTEL_EXPORTER_OTLP_LOGS_TIMEOUT
  value: {{ .Values.tracing.timeoutSeconds | quote }}
{{- if .Values.tracing.headersSecret }}
- name: OTEL_EXPORTER_OTLP_LOGS_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.tracing.headersSecret | quote }}
      key: headers
{{- end }}
{{- end }}
{{- end }}
