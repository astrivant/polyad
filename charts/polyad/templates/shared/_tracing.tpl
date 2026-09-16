{{- define "polyad.tracingEnv" -}}
- name: POLYAD_TRACING_ENABLED
  value: {{ .Values.tracing.enabled | quote }}
{{- if .Values.tracing.enabled }}
- name: OTEL_EXPORTER_OTLP_TRACES_PROTOCOL
  value: http/protobuf
- name: OTEL_EXPORTER_OTLP_TRACES_ENDPOINT
  value: {{ .Values.tracing.endpoint | quote }}
- name: OTEL_SERVICE_NAME
  value: {{ .Values.tracing.serviceName | quote }}
- name: OTEL_TRACES_SAMPLER
  value: parentbased_traceidratio
- name: OTEL_TRACES_SAMPLER_ARG
  value: {{ .Values.tracing.samplingRatio | quote }}
- name: OTEL_EXPORTER_OTLP_TRACES_TIMEOUT
  value: {{ .Values.tracing.timeoutSeconds | quote }}
- name: OTEL_RESOURCE_ATTRIBUTES
  value: {{ .Values.tracing.resourceAttributes | quote }}
{{- if .Values.tracing.headersSecret }}
- name: OTEL_EXPORTER_OTLP_TRACES_HEADERS
  valueFrom:
    secretKeyRef:
      name: {{ .Values.tracing.headersSecret | quote }}
      key: headers
{{- end }}
{{- end }}
{{- end }}
