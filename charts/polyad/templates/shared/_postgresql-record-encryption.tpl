{{/* Keys are projected only into database-writing operator containers, never the database. */}}
{{- define "polyad.postgresql.recordEncryptionEnv" -}}
{{- if .Values.postgresql.recordEncryption.enabled -}}
- name: POLYAD_POSTGRES_RECORD_ENCRYPTION_ENABLED
  value: "true"
- name: POLYAD_POSTGRES_RECORD_PUBLIC_KEY_FILE
  value: /var/run/polyad/record-encryption/public.pem
{{- if .Values.postgresql.recordEncryption.privateKeyKey }}
- name: POLYAD_POSTGRES_RECORD_PRIVATE_KEY_FILE
  value: /var/run/polyad/record-encryption/private.pem
{{- end }}
{{- if .Values.postgresql.recordEncryption.privateKeyPasswordKey }}
- name: POLYAD_POSTGRES_RECORD_PRIVATE_KEY_PASSWORD_FILE
  value: /var/run/polyad/record-encryption/password
{{- end }}
{{- end -}}
{{- end -}}

{{- define "polyad.postgresql.recordEncryptionMount" -}}
{{- if .Values.postgresql.recordEncryption.enabled -}}
- name: record-encryption
  mountPath: /var/run/polyad/record-encryption
  readOnly: true
{{- end -}}
{{- end -}}

{{- define "polyad.postgresql.recordEncryptionVolume" -}}
{{- with .Values.postgresql.recordEncryption -}}
{{- if .enabled -}}
- name: record-encryption
  secret:
    secretName: {{ required "record encryption requires postgresql.recordEncryption.existingSecret" .existingSecret | quote }}
    defaultMode: 0440
    items:
      - key: {{ .publicKeyKey | quote }}
        path: public.pem
      {{- if .privateKeyKey }}
      - key: {{ .privateKeyKey | quote }}
        path: private.pem
      {{- end }}
      {{- if .privateKeyPasswordKey }}
      - key: {{ .privateKeyPasswordKey | quote }}
        path: password
      {{- end }}
{{- end -}}
{{- end -}}
{{- end -}}
