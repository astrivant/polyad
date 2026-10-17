#!/usr/bin/env bash
# Provision or reuse one repository-scoped Cosign identity without logging secret values.
set -euo pipefail
set +x
umask 077

##
# Describe explicit setup inputs without contacting the host or creating credentials.
# -> ret::exit_code
usage() {
    cat <<'USAGE'
Usage: bash scripts/release/setup-signing.sh --repo OWNER/REPO [--key-dir DIRECTORY]
Requires gh, Cosign and OpenSSL. The default private backup directory is
${XDG_DATA_HOME:-$HOME/.local/share}/polyad/signing/OWNER-REPO.
Reuses a complete existing identity and rejects partial key sets. Creates secrets
COSIGN_PRIVATE_KEY and COSIGN_PASSWORD and variable COSIGN_PUBLIC_KEY_SHA256.
No commit, tag, merge or release is created. Retain the private directory for recovery.
USAGE
}
repository=''
key_directory=''
while [ "$#" -gt 0 ]; do
    case "$1" in
        --repo | --key-dir)
            if [ "$#" -lt 2 ] || [ -z "$2" ]; then
                usage >&2
                exit 2
            fi
            if [ "$1" = --repo ]; then repository="$2"; else key_directory="$2"; fi
            shift 2
            ;;
        -h | --help)
            usage
            exit 0
            ;;
        *)
            usage >&2
            exit 2
            ;;
    esac
done
if [[ ! "$repository" =~ ^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$ ]]; then
    usage >&2
    exit 2
fi
for dependency in gh cosign openssl; do command -v "$dependency" >/dev/null; done
gh repo view "$repository" --json nameWithOwner --jq .nameWithOwner
key_directory="${key_directory:-${XDG_DATA_HOME:-$HOME/.local/share}/polyad/signing/${repository//\//-}}"
if [ -L "$key_directory" ]; then
    echo "Refusing a symlinked key directory" >&2
    exit 1
fi
mkdir -p "$key_directory"
chmod 700 "$key_directory"
if [ ! -e "$key_directory/cosign.key" ] && [ ! -e "$key_directory/cosign.pub" ] && [ ! -e "$key_directory/cosign.password" ]; then
    openssl rand -base64 48 >"$key_directory/cosign.password"
    export COSIGN_PASSWORD
    COSIGN_PASSWORD="$(cat "$key_directory/cosign.password")"
    cosign generate-key-pair --output-key-prefix "$key_directory/cosign"
fi
for filename in cosign.key cosign.pub cosign.password; do
    if [ ! -s "$key_directory/$filename" ] || [ -L "$key_directory/$filename" ]; then
        echo 'Incomplete or unsafe key directory; recover the original files before retrying.' >&2
        exit 1
    fi
done
export COSIGN_PASSWORD
COSIGN_PASSWORD="$(cat "$key_directory/cosign.password")"
cosign public-key --key "$key_directory/cosign.key" | cmp - "$key_directory/cosign.pub"
chmod 600 "$key_directory"/cosign.*
fingerprint="$(openssl pkey -pubin -in "$key_directory/cosign.pub" -outform DER | openssl dgst -sha256 | awk '{print $NF}')"
gh secret set COSIGN_PRIVATE_KEY --repo "$repository" <"$key_directory/cosign.key"
gh secret set COSIGN_PASSWORD --repo "$repository" <"$key_directory/cosign.password"
gh variable set COSIGN_PUBLIC_KEY_SHA256 --repo "$repository" --body "$fingerprint"
printf 'Signing configured. Retain the private backup at %s\nTrusted fingerprint: %s\n' "$key_directory" "$fingerprint"
