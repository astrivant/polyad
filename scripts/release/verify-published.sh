#!/usr/bin/env bash
# Re-download the published release and verify its independently configured trust anchor.
set -euo pipefail
: "${PUBLISH_RESULT:?}"
: "${RELEASE_TAG:?}"
: "${SOURCE_SHA:?}"
: "${COSIGN_PUBLIC_KEY_SHA256:?}"
test "$PUBLISH_RESULT" = success
test "$(gh release view "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --json isDraft --jq .isDraft)" = false
release_directory="$(mktemp -d "$RUNNER_TEMP/polyad-verify.XXXXXX")"

##
# Remove only owned downloads and preserve verification failure status.
# -> ret::exit_code
cleanup_verification() {
    local status="$?"
    trap - EXIT
    if ! rm -rf -- "$release_directory"; then
        if [ "$status" -eq 0 ]; then status=1; fi
    fi
    exit "$status"
}
trap cleanup_verification EXIT
gh release download "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --dir "$release_directory"
python scripts/release/attest.py verify --directory "$release_directory" \
    --tag "$RELEASE_TAG" --source "$SOURCE_SHA" --fingerprint "$COSIGN_PUBLIC_KEY_SHA256"
