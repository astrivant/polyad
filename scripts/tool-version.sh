#!/usr/bin/env bash
# Read one exact tool pin without requiring asdf, Python, or yq to be installed.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
if [[ $# != 1 ]]; then
    echo 'Usage: bash scripts/tool-version.sh TOOL' >&2
    exit 2
fi
awk -v tool="$1" '
  $1 == tool { count++; version=$2; if (NF != 2) invalid=1 }
  END {
    if (count != 1 || invalid || version !~ /^[0-9]+\.[0-9]+\.[0-9]+$/) {
      print "Expected one exact version for " tool > "/dev/stderr"
      exit 2
    }
    print version
  }
' "$root/.tool-versions"
