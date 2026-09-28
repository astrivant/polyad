#!/bin/sh
# Keep the compatibility rule local to this opt-in Minikube installation.
set -eu

# Do not leave our table behind on an ordinary Helm uninstall or Pod rollout.
trap 'nft delete table inet polyad_kindnet_compat 2>/dev/null || true; exit 0' TERM INT

while true; do
    if rules="$(nft list chain inet kindnet-network-policies postrouting 2>/dev/null)"; then
        # The accepted label and hook order are an explicit compatibility
        # contract with the pinned kindnet image, not arbitrary firewall rules.
        case "$rules" in
            *'priority srcnat - 5;'*'ct label 28'*) ;;
            *)
                nft delete table inet polyad_kindnet_compat 2>/dev/null || true
                printf '%s\n' 'Unsupported kindnet hook/label; compatibility rule removed' >&2
                exit 1
                ;;
        esac
        nft -f /config/kindnet-compat.nft
    fi

    # Waiting on a child lets the shell promptly process Kubernetes SIGTERM.
    sleep 10 &
    wait "$!"
done
