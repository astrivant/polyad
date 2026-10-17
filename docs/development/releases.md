# Signed releases

<!-- toc:start -->
**Table of contents**

- [Maintainer setup](#maintainer-setup)
- [Publication and verification](#publication-and-verification)
- [Recovery and contributor feedback](#recovery-and-contributor-feedback)
<!-- toc:end -->

A version tag selects one immutable source commit. The ordinary test and build
stages validate that source before the release stage receives its artifacts.
PRs and branch pushes validate changes without publishing a release.

## Maintainer setup

Install the tools pinned in `.tool-versions`, authenticate `gh`, and explicitly
select the repository:

```bash
bash scripts/release/setup-signing.sh --repo OWNER/REPOSITORY
```

The macOS/Linux helper creates or reuses a dedicated Cosign key pair, stores a
private recovery copy under your user data directory, and configures these inputs:

| Input | Storage | Purpose |
| --- | --- | --- |
| `PYPI_API_TOKEN` | Actions organization, repository or `pypi` environment secret | Existing credential authorized for all five Python projects |
| `COSIGN_PRIVATE_KEY` | Repository Actions secret | Sign finalized release files |
| `COSIGN_PASSWORD` | Repository Actions secret | Unlock the signing key |
| `COSIGN_PUBLIC_KEY_SHA256` | Repository Actions variable | Independently configured SHA-256 fingerprint of the DER public key |
| `--key-dir` | Optional local helper argument | Reuse a reviewed private recovery directory |

The helper refuses partial key sets. Recover the original keys before retrying;
setup does not rotate keys. Keep the private directory out of Git and backups
intended for publication. Share the public fingerprint through an independently
trusted channel. Review workflow changes before allowing them access to secrets.

## Publication and verification

Create a signed annotated version tag on the reviewed commit and push that tag.
The GitHub API must report its signature as verified. Every producer and consumer
uses the resolved commit; release metadata is derived from the tag.

The pipeline downloads the verified five wheel/source pairs and three Helm
packages, signs their final bytes, and assembles a draft GitHub release. It
publishes the Python packages before making the GitHub release public. Downloads
are then checked against the configured key fingerprint, complete checksum
manifest, tag and source commit. A terminal check rejects skipped publication.

Attachments include every package, a detached Cosign bundle for each artifact,
`cosign.pub`, `public-key.sha256`, `SOURCE.json`, and signed `SHA256SUMS`.
The signatures use a repository key without a transparency service. Offline
verification establishes the supplied key's signature, not transparency inclusion.

```bash
# Use an empty directory and independently obtain TAG, SOURCE_SHA and TRUSTED_FINGERPRINT.
gh release download "$TAG" --repo OWNER/REPOSITORY --dir downloaded-release
python scripts/release/attest.py verify --directory downloaded-release \
  --tag "$TAG" --source "$SOURCE_SHA" --fingerprint "$TRUSTED_FINGERPRINT"
```

Obtain the verification script from the reviewed source revision. The public key
bundled with an artifact is not independently trusted by itself. A successful
local test does not establish that a hosted release or registry upload succeeded.

## Recovery and contributor feedback

Retries compare existing signed attachments with the current build. Different
bytes, a different key or an incomplete draft fail visibly instead of overwriting
published files. Preserve the draft and inspect the failed step. Partial PyPI
uploads require operator reconciliation before retrying; immutable registry files
must never be replaced with different bytes.

After downloaded attachments verify, the pipeline comments once on each included
merged PR, mentioning its opener and linking the release. Notes group changes by
labels and retain GitHub's generated summary in a separate section. Failed or
draft releases do not notify contributors. Concurrent runs of the same tag are
serialized; a version-specific marker prevents duplicate comments on retries.
