REVOKE ALL ON DATABASE "{{ .database | replace "\"" "\"\"" }}" FROM PUBLIC;
