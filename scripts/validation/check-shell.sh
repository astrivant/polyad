#!/usr/bin/env bash
# Check shell lint and four-space formatting with the pinned tools.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
cd "$root"
# A Python virtual environment may shadow a correctly installed native formatter.
# Search PATH for the exact pin, then retain that executable through the check.
resolve_tool() {
    local tool="$1" expected="$2" candidate actual found=""
    while IFS= read -r candidate; do
        if ! actual=$("$candidate" --version 2>/dev/null); then
            continue
        fi
        case "$tool" in
            shellcheck) actual=$(printf '%s\n' "$actual" | awk '/^version:/ {print $2}') ;;
            shfmt) actual=${actual#v} ;;
        esac
        if [[ "$actual" == "$expected" ]]; then
            printf '%s\n' "$candidate"
            return 0
        fi
        found+="${found:+, }$candidate ($actual)"
    done < <(type -a -p "$tool" || true)
    echo "$tool $expected required; no matching executable on PATH. Found: ${found:-none}. Run scripts/tooling/install-asdf-tools.sh $tool." >&2
    return 2
}
shellcheck_bin=$(resolve_tool shellcheck "$(bash scripts/tooling/tool-version.sh shellcheck)")
shfmt_bin=$(resolve_tool shfmt "$(bash scripts/tooling/tool-version.sh shfmt)")
files=("$@")
if [[ ${#files[@]} == 0 ]]; then
    manifest=$(mktemp)
    trap 'rm -f "$manifest"' EXIT
    git ls-files -z --cached --others --exclude-standard -- '*.sh' '*.bash' >"$manifest"
    while IFS= read -r -d '' file; do
        # Unstaged moves leave deleted paths in the index until the rename is staged.
        if [[ -f "$file" ]]; then files+=("$file"); fi
    done <"$manifest"
fi
if [[ ${#files[@]} != 0 ]]; then
    "$shellcheck_bin" "${files[@]}"
    "$shfmt_bin" -d "${files[@]}"
fi
