{{/* One storage selection for every managed database instance, including new HA replicas. */}}
{{- define "polyad.postgresql.encryptedStorageClass" -}}
{{- $config := .Values.postgresql.encryptionAtRest -}}
{{- if $config.storageClass -}}
{{- $config.storageClass -}}
{{- else if eq $config.provider "GKE" -}}
{{- $name := printf "%s-%s-pg-encrypted" .Release.Namespace .Release.Name -}}
{{- if gt (len $name) 63 -}}
{{- printf "%s-%s" ($name | trunc 54 | trimSuffix "-") ($name | sha256sum | trunc 8) -}}
{{- else -}}
{{- $name -}}
{{- end -}}
{{- else -}}
{{- fail "postgresql.encryptionAtRest requires an explicit encrypted storageClass for ExistingStorageClass" -}}
{{- end -}}
{{- end -}}

{{- define "polyad.postgresql.storageClass" -}}
{{- if .root.Values.postgresql.encryptionAtRest.enabled -}}
{{- $selected := include "polyad.postgresql.encryptedStorageClass" .root -}}
{{- if and .configured (ne .configured $selected) -}}
{{- fail (printf "%s conflicts with postgresql.encryptionAtRest.storageClass; clear it or use the selected encrypted class" .path) -}}
{{- end -}}
{{- $selected -}}
{{- else -}}
{{- .configured -}}
{{- end -}}
{{- end -}}
