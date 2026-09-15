#!/usr/bin/env bash
# Add the pinned Dragonfly API schema to hypothesis-helm's Kubernetes schemas.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec kubeconform "$@" -schema-location "$root/charts/polyad/schemas/{{ .ResourceKind }}{{ .KindSuffix }}.json"
