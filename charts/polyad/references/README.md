# Polyad values references

These files are optional, composable examples. Helm does not load them
automatically. Pass selected files with `--values`, followed by your own
environment-specific overlay.

[`../values.yaml`](../values.yaml) is the canonical configuration surface and
contains every supported field and default. The files in this directory only
highlight deployment profiles and optional features; they do not introduce
additional settings.

Each reference uses [`../values.reference.schema.json`](../values.reference.schema.json)
for partial-overlay editor validation. Helm validates the fully merged result
against [`../values.schema.json`](../values.schema.json).

The categorized reference index and combination guidance are in the
[chart README](../README.md#reference-values).
