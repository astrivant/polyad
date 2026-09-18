{{/* Keep injected operator/observer proxy and init containers in the same QoS class. */}}
{{- define "polyad.proxyResources" -}}
sidecar.istio.io/proxyCPU: {{ .Values.mesh.proxyResources.cpu | quote }}
sidecar.istio.io/proxyCPULimit: {{ .Values.mesh.proxyResources.cpu | quote }}
sidecar.istio.io/proxyMemory: {{ .Values.mesh.proxyResources.memory | quote }}
sidecar.istio.io/proxyMemoryLimit: {{ .Values.mesh.proxyResources.memory | quote }}
{{- end -}}
