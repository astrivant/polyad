#!/usr/bin/env bash
# Add pinned Dragonfly and Gateway API schemas to Kubernetes validation.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec kubeconform -schema-location default -schema-location "$root/charts/polyad/schemas/{{ .ResourceKind }}{{ .KindSuffix }}.json" "$@"
