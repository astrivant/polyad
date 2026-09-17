# Generated validation schemas

These Draft 7 schemas support chart checks through
`scripts/validation/kubeconform.sh`; they do not install APIs into a cluster.
Every JSON file and upstream license notice here is generated.

## Table of contents

- [Source of truth](#source-of-truth)
- [Regenerate and validate](#regenerate-and-validate)

## Source of truth

The [central catalog](../../../schemas/README.md) documents source ownership,
pinned releases, snapshot digests and licenses. Polyad and Dragonfly schemas come
from the chart CRDs. Gateway API, Istio, KEDA and External Secrets schemas come
from the catalog's pinned snapshots.

The same conversion generates `polyad_schemas.resources`. Constraints and
resource identities match; the Python artifacts use Draft 2020-12 while
Kubeconform uses Draft 7. Upstream Apache-2.0 notices accompany both distributions.

## Regenerate and validate

Run from the repository root:

```sh
poetry run python scripts/schemas/generate-all.py
poetry run python scripts/schemas/generate-all.py --check
```

Both commands work offline. The `schema-artifacts` pre-commit hook and CI reject
changed or missing outputs, source digest mismatches and dependency pin drift.
Follow the [upstream refresh procedure](../../../schemas/README.md#refresh-upstream-contracts)
instead of editing these files.
