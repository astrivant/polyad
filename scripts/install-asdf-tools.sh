#!/usr/bin/env bash
# Register explicit plugin repositories, then install the project's selected tool versions.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
cd "$root"
command -v asdf >/dev/null || {
    echo 'Install asdf >= 0.16 first; see docs/toolchain.md.' >&2
    exit 2
}
export PATH="${ASDF_DATA_DIR:-$HOME/.asdf}/shims:$PATH"
plugins_only=false
if [[ ${1:-} == --plugins-only ]]; then
    plugins_only=true
    shift
fi
tools=("$@")
if [[ ${#tools[@]} == 0 ]]; then
    declared_tools=$(awk 'NF && $1 !~ /^#/ {print $1}' .tool-versions)
    [[ -n "$declared_tools" ]] || {
        echo 'No tools declared in .tool-versions.' >&2
        exit 2
    }
    while IFS= read -r tool; do
        tools+=("$tool")
    done <<<"$declared_tools"
fi
installed=$(asdf plugin list)
for tool in "${tools[@]}"; do
    version=$(bash scripts/tool-version.sh "$tool")
    case "$tool" in
        python) repository=https://github.com/danhper/asdf-python.git ;;
        nodejs) repository=https://github.com/asdf-vm/asdf-nodejs.git ;;
        poetry) repository=https://github.com/asdf-community/asdf-poetry.git ;;
        helm) repository=https://github.com/Antiarchitect/asdf-helm.git ;;
        kubectl) repository=https://github.com/asdf-community/asdf-kubectl.git ;;
        kubeconform) repository=https://github.com/lirlia/asdf-kubeconform.git ;;
        argocd) repository=https://github.com/beardix/asdf-argocd.git ;;
        golang) repository=https://github.com/asdf-community/asdf-golang.git ;;
        shellcheck) repository=https://github.com/luizm/asdf-shellcheck.git ;;
        shfmt) repository=https://github.com/luizm/asdf-shfmt.git ;;
        *)
            echo "No plugin repository configured for $tool" >&2
            exit 2
            ;;
    esac
    if ! printf '%s\n' "$installed" | grep -Fxq "$tool"; then
        asdf plugin add "$tool" "$repository"
        installed+=$'\n'"$tool"
    fi
    if [[ "$plugins_only" == false ]]; then
        asdf install "$tool" "$version"
    fi
done
