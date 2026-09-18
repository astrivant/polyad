{{/* Local Pod context is available independently of optional telemetry. */}}
{{- define "polyad.podContextEnv" -}}
{{- $fields := dict
  "POLYAD_POD_NAME" "metadata.name"
  "POLYAD_POD_UID" "metadata.uid"
  "POLYAD_POD_NAMESPACE" "metadata.namespace"
  "POLYAD_POD_IP" "status.podIP"
  "POLYAD_POD_IPS" "status.podIPs"
  "POLYAD_HOST_IP" "status.hostIP"
  "POLYAD_HOST_IPS" "status.hostIPs"
  "POLYAD_KUBERNETES_NODE_NAME" "spec.nodeName"
  "POLYAD_SERVICE_ACCOUNT_NAME" "spec.serviceAccountName"
-}}
{{- range $name, $path := $fields }}
- name: {{ $name }}
  valueFrom:
    fieldRef:
      apiVersion: v1
      fieldPath: {{ $path }}
{{- end }}
{{- $resources := dict
  "POLYAD_CPU_REQUEST_MILLICORES" (dict "resource" "requests.cpu" "divisor" "1m")
  "POLYAD_CPU_LIMIT_MILLICORES" (dict "resource" "limits.cpu" "divisor" "1m")
  "POLYAD_MEMORY_REQUEST_BYTES" (dict "resource" "requests.memory" "divisor" "1")
  "POLYAD_MEMORY_LIMIT_BYTES" (dict "resource" "limits.memory" "divisor" "1")
-}}
{{- range $name, $selector := $resources }}
- name: {{ $name }}
  valueFrom:
    resourceFieldRef:
      resource: {{ $selector.resource }}
      divisor: {{ $selector.divisor | quote }}
{{- end }}
- name: POLYAD_POD_CLUSTER
  value: {{ .Values.global.multiCluster.clusterName | quote }}
{{- end -}}
