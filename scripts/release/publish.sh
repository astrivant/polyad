#!/usr/bin/env bash
# Publish only verified build bytes, preserving a complete draft for interrupted retries.
set -euo pipefail
set +x
umask 077
: "${GITHUB_REPOSITORY:?Select the release repository}"
: "${RELEASE_TAG:?Select an existing version tag}"
: "${SOURCE_SHA:?Select the tested full source revision}"
: "${PACKAGE_VERSION:?Select the normalized package version}"
: "${COSIGN_PRIVATE_KEY:?Run the documented signing setup before tagging}"
: "${COSIGN_PUBLIC_KEY_SHA256:?Configure the independent signing fingerprint}"
: "${POETRY_PYPI_TOKEN_PYPI:?Grant access to the organization PyPI credential}"
export COSIGN_PASSWORD="${COSIGN_PASSWORD:-}"

# Keep build files separate from the upload set; retries compare payloads before reuse.
release_directory="$(mktemp -d "${RUNNER_TEMP:-${TMPDIR:-/tmp}}/polyad-release.XXXXXX")"

##
# Remove only the owned temporary workspace, preserving the original failure status.
# -> ret::exit_code
cleanup_release() {
    local status="$?"
    trap - EXIT
    if ! rm -rf -- "$release_directory"; then
        if [ "$status" -eq 0 ]; then status=1; fi
    fi
    exit "$status"
}
trap cleanup_release EXIT

for package in polyad_types polyad_schemas polyad_sdk polyad_benchmarks polyad; do
    test -s "dist/${package}-${PACKAGE_VERSION}-py3-none-any.whl"
    test -s "dist/${package}-${PACKAGE_VERSION}.tar.gz"
done
cp dist/*.whl dist/*.tar.gz dist/chart/*.tgz "$release_directory/"

# A partial existing draft is left intact for explicit recovery, never overwritten.
if gh release view "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --json isDraft >"$release_directory/state.json" 2>/dev/null; then
    rm "$release_directory/state.json"
    mkdir "$release_directory/existing"
    gh release download "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --dir "$release_directory/existing"
    python scripts/release/attest.py verify --directory "$release_directory/existing" \
        --tag "$RELEASE_TAG" --source "$SOURCE_SHA" --fingerprint "$COSIGN_PUBLIC_KEY_SHA256"
    for artifact in "$release_directory"/*.whl "$release_directory"/*.tar.gz "$release_directory"/*.tgz; do
        cmp "$artifact" "$release_directory/existing/$(basename "$artifact")"
    done
else
    rm -f "$release_directory/state.json"
    python scripts/release/attest.py prepare --directory "$release_directory" \
        --tag "$RELEASE_TAG" --source "$SOURCE_SHA" --fingerprint "$COSIGN_PUBLIC_KEY_SHA256"
    gh release create "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --verify-tag --draft \
        --title "Polyad $RELEASE_TAG" --notes-file "$RUNNER_TEMP/release-notes.md"
    gh release upload "$RELEASE_TAG" "$release_directory"/* --repo "$GITHUB_REPOSITORY"
fi

# Registry uploads use only downloaded build artifacts. Failure leaves the GitHub release a draft.
poetry -C pkg/polyad-types publish --dist-dir ../../dist --no-interaction
poetry -C pkg/polyad-schemas publish --dist-dir ../../dist --no-interaction
poetry -C pkg/polyad-sdk publish --dist-dir ../../dist --no-interaction
poetry -C pkg/polyad-benchmarks publish --dist-dir ../../dist --no-interaction
poetry publish --no-interaction

gh release edit "$RELEASE_TAG" --repo "$GITHUB_REPOSITORY" --draft=false --notes-file "$RUNNER_TEMP/release-notes.md"
