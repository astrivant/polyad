#!/usr/bin/env bash
# Install selected Helm validation CLIs in Linux CI using the shared asdf pins.
set -euo pipefail
root=$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)
case "$(uname -s)/$(uname -m)" in
    Linux/x86_64)
        arch=amd64
        shellcheck_arch=x86_64
        ;;
    Linux/aarch64 | Linux/arm64)
        arch=arm64
        shellcheck_arch=aarch64
        ;;
    *)
        echo 'This installer supports Linux CI; use scripts/tooling/install-asdf-tools.sh locally.' >&2
        exit 2
        ;;
esac
scratch=$(mktemp -d)
trap 'rm -rf "$scratch"' EXIT
cd "$scratch"
if [[ $# == 0 ]]; then set -- helm kubeconform shellcheck shfmt; fi
elevate=()
if [[ ! -w /usr/local/bin ]]; then elevate=(sudo); fi
for tool in "$@"; do
    version=$(bash "$root/scripts/tooling/tool-version.sh" "$tool")
    case "$tool" in
        helm)
            archive="helm-v${version}-linux-${arch}.tar.gz"
            curl -fsSL "https://get.helm.sh/$archive" -o "$archive"
            curl -fsSL "https://get.helm.sh/$archive.sha256sum" -o "$archive.sha256sum"
            sha256sum --check "$archive.sha256sum"
            tar -xzf "$archive" "linux-$arch/helm"
            binary="linux-$arch/helm"
            ;;
        kubeconform)
            curl -fsSL "https://github.com/yannh/kubeconform/releases/download/v${version}/kubeconform-linux-${arch}.tar.gz" -o kubeconform.tar.gz
            tar -xzf kubeconform.tar.gz kubeconform
            binary=kubeconform
            ;;
        argocd)
            binary="argocd-linux-${arch}"
            curl -fsSL "https://github.com/argoproj/argo-cd/releases/download/v${version}/$binary" -o "$binary"
            curl -fsSL "https://github.com/argoproj/argo-cd/releases/download/v${version}/cli_checksums.txt" -o cli_checksums.txt
            awk -v binary="$binary" '$2 == binary {print}' cli_checksums.txt >argocd.sha256
            test -s argocd.sha256
            sha256sum --check argocd.sha256
            ;;
        shellcheck)
            curl -fsSL "https://github.com/koalaman/shellcheck/releases/download/v${version}/shellcheck-v${version}.linux.${shellcheck_arch}.tar.xz" -o shellcheck.tar.xz
            binary="shellcheck-v${version}/shellcheck"
            tar -xJf shellcheck.tar.xz "$binary"
            ;;
        shfmt)
            curl -fsSL "https://github.com/mvdan/sh/releases/download/v${version}/shfmt_v${version}_linux_${arch}" -o shfmt
            binary=shfmt
            ;;
        *)
            echo "Unsupported Helm validation tool: $tool" >&2
            exit 2
            ;;
    esac
    "${elevate[@]}" install "$binary" "/usr/local/bin/$tool"
done
