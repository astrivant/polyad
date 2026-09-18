{{/* Recursively apply CRD defaults, resolve field templates, and check resolved scalar types. */}}
{{- define "polyad.resources.value" -}}
{{- $value := .value -}}
{{- $schema := default dict .schema -}}
{{- $nullable := false -}}
{{/* Nullable CRD fields keep their ordinary contract when a value is supplied. */}}
{{- if $schema.anyOf -}}
  {{- $types := list -}}
  {{- range $branch := $schema.anyOf -}}
    {{- $types = append $types $branch.type -}}
  {{- end -}}
  {{- $nullable = has "null" $types -}}
  {{- if $nullable -}}
    {{- range $branch := $schema.anyOf -}}
      {{- if ne $branch.type "null" -}}
        {{- $schema = merge (deepCopy $branch) (omit $schema "anyOf") -}}
      {{- end -}}
    {{- end -}}
  {{- else if and (has "integer" $types) (has "string" $types) -}}
    {{- $schema = set (omit $schema "anyOf") "type" "int-or-string" -}}
  {{- end -}}
{{- end -}}
{{- $type := default "" $schema.type -}}
{{- $path := .path -}}
{{- if and .resolve (kindIs "string" $value) (contains "{{" $value) -}}
  {{- $value = tpl $value .context -}}
  {{- if and (not (contains "{{" $value)) (ne $type "string") -}}
    {{- $parsed := fromYaml (printf "value:\n%s" (indent 2 $value)) -}}
    {{- if hasKey $parsed "Error" }}{{ fail (printf "%s: tpl must return valid YAML or JSON" $path) }}{{ end -}}
    {{- $value = $parsed.value -}}
  {{- end -}}
{{- end -}}
{{- if and .validate (kindIs "string" $value) (contains "{{" $value) }}{{ fail (printf "%s: unresolved tpl expression or cyclic reference" $path) }}{{ end -}}
{{- if kindIs "map" $value -}}
  {{- $value = deepCopy $value -}}
  {{- $properties := default dict $schema.properties -}}
  {{- range $name, $field := $properties -}}
    {{- if and (not (hasKey $value $name)) (hasKey $field "default") -}}
      {{- $_ := set $value $name (deepCopy $field.default) -}}
    {{- end -}}
  {{- end -}}
  {{- if $.validate -}}
    {{- range default list $schema.required -}}
      {{- if not (hasKey $value .) }}{{ fail (printf "%s.%s is required" $path .) }}{{ end -}}
    {{- end -}}
  {{- end -}}
  {{- range $name, $child := $value -}}
    {{- $field := default dict (get $properties $name) -}}
    {{- if not (hasKey $properties $name) -}}
      {{- if kindIs "map" $schema.additionalProperties -}}
        {{- $field = $schema.additionalProperties -}}
      {{- else if and $.validate (or (and (hasKey $schema "additionalProperties") (eq (toJson $schema.additionalProperties) "false")) (and (hasKey $schema "properties") (not (hasKey $schema "additionalProperties")) (not (get $schema "x-kubernetes-preserve-unknown-fields")))) -}}
        {{- fail (printf "%s.%s is not supported by the resource schema" $path $name) -}}
      {{- end -}}
    {{- end -}}
    {{- $result := include "polyad.resources.value" (dict "value" $child "schema" $field "context" $.context "resolve" $.resolve "validate" $.validate "path" (printf "%s.%s" $path $name)) | fromJson -}}
    {{- $_ := set $value $name $result.value -}}
  {{- end -}}
{{- else if kindIs "slice" $value -}}
  {{- $items := list -}}
  {{- range $index, $child := $value -}}
    {{- $result := include "polyad.resources.value" (dict "value" $child "schema" (default dict $schema.items) "context" $.context "resolve" $.resolve "validate" $.validate "path" (printf "%s[%d]" $path $index)) | fromJson -}}
    {{- $items = append $items $result.value -}}
  {{- end -}}
  {{- $value = $items -}}
{{- end -}}
{{- if and .validate (not (and $nullable (eq (toJson $value) "null"))) -}}
  {{- $numeric := or (kindIs "float64" $value) (kindIs "int" $value) (kindIs "int64" $value) -}}
  {{- $integer := and $numeric (eq (floor (float64 $value)) (float64 $value)) -}}
  {{- if and (ne $type "") (not (or (and (eq $type "object") (kindIs "map" $value)) (and (eq $type "array") (kindIs "slice" $value)) (and (eq $type "string") (kindIs "string" $value)) (and (eq $type "boolean") (kindIs "bool" $value)) (and (eq $type "number") $numeric) (and (eq $type "integer") $integer) (and (eq $type "int-or-string") (or $integer (kindIs "string" $value))))) -}}
    {{- fail (printf "%s: resolved value must have type %s" $path $type) -}}
  {{- end -}}
  {{- if and $schema.enum (not (has $value $schema.enum)) }}{{ fail (printf "%s: resolved value is not an allowed choice" $path) }}{{ end -}}
  {{- if $numeric -}}
    {{- if and (hasKey $schema "minimum") (lt (float64 $value) (float64 $schema.minimum)) }}{{ fail (printf "%s: resolved value is below minimum" $path) }}{{ end -}}
    {{- if and (hasKey $schema "maximum") (gt (float64 $value) (float64 $schema.maximum)) }}{{ fail (printf "%s: resolved value exceeds maximum" $path) }}{{ end -}}
  {{- end -}}
  {{- if kindIs "string" $value -}}
    {{- if and $schema.pattern (not (regexMatch $schema.pattern $value)) }}{{ fail (printf "%s: resolved value does not match its pattern" $path) }}{{ end -}}
    {{- if and (hasKey $schema "minLength") (lt (len $value) (int $schema.minLength)) }}{{ fail (printf "%s: resolved string is too short" $path) }}{{ end -}}
    {{- if and (hasKey $schema "maxLength") (gt (len $value) (int $schema.maxLength)) }}{{ fail (printf "%s: resolved string is too long" $path) }}{{ end -}}
  {{- end -}}
  {{- if kindIs "slice" $value -}}
    {{- if and (hasKey $schema "minItems") (lt (len $value) (int $schema.minItems)) }}{{ fail (printf "%s: resolved list has too few entries" $path) }}{{ end -}}
    {{- if and (hasKey $schema "maxItems") (gt (len $value) (int $schema.maxItems)) }}{{ fail (printf "%s: resolved list has too many entries" $path) }}{{ end -}}
  {{- end -}}
{{- end -}}
{{- dict "value" $value | toJson -}}
{{- end -}}
