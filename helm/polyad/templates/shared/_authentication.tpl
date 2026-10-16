{{- define "polyad.authenticationVolume" -}}
- name: authentication
  projected:
    defaultMode: 0440
    sources:
      - configMap:
          name: {{ .Release.Name }}-polyad-authentication
          items:
            - key: config.json
              path: config.json
      {{- if eq .Values.authentication.mode "Required" }}
      {{- range $group, $keys := pick .Values.authentication "services" "operators" }}
      {{- range $keys }}
      - secret:
          name: {{ .existingSecret | quote }}
          items:
            - key: {{ default "token" .secretKey | quote }}
              path: {{ printf "%s/%s" $group .name | quote }}
      {{- end }}
      {{- end }}
      {{- end }}
{{- end -}}

{{- define "polyad.authenticationDatabaseVolume" -}}
- name: authentication-database
  secret:
    defaultMode: 0440
    {{- if .Values.authentication.storage.separateDatabase }}
    {{- if .Values.authentication.storage.managed }}
    secretName: {{ printf "%s-authentication-app" .Release.Name | quote }}
    {{- else }}
    secretName: {{ required "authentication.storage.existingSecret is required for external authentication storage" .Values.authentication.storage.existingSecret | quote }}
    {{- end }}
    {{- else }}
    secretName: {{ ternary (printf "%s-state-app" .Release.Name) .Values.postgresql.existingSecret .Values.postgresql.managed | quote }}
    {{- end }}
    items:
      - key: {{ if .Values.authentication.storage.separateDatabase }}{{ ternary "uri" .Values.authentication.storage.secretKey .Values.authentication.storage.managed | quote }}{{ else }}{{ ternary "uri" .Values.postgresql.secretKey .Values.postgresql.managed | quote }}{{ end }}
        path: uri
{{- end -}}
