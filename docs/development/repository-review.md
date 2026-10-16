# Repository standards review

<!-- toc:start -->
**Table of contents**

- [Applied changes](#applied-changes)
- [Existing behavior retained](#existing-behavior-retained)
- [Verification boundaries](#verification-boundaries)
<!-- toc:end -->

This review strengthens Polyad's contributor and delivery contracts. It does not
certify a deployment or substitute for verification of the eventual tagged release.

## Applied changes

| Surface | Contract and evidence |
| --- | --- |
| Repository layout | Charts live under `helm/`; Python tests live under `tests/contracts/` and `tests/features/`. Build, schema, container, Terraform and documentation consumers use those paths. |
| Ownership | A semantics-preserving CODEOWNERS compiler groups tracked paths; commit and push hooks complement the existing syntax/path validator. |
| Documentation | TOCs preserve the introduction; Markdown table, math and Mermaid checks run with JavaScript lint and regressions. Each chart reference has its own values file and generated README. |
| CI | Maintained actions use reviewed immutable revisions. Independent build and test stages converge at `CI verification`. Source settings require that check and an independent approving review. |
| Packaging | Every Python distribution retains a PyPI publication path. Signed release verification binds all Python and Helm artifacts to the tag, commit and independently configured key. |
| Coverage | Main publishes measured coverage and its exact source receipt through a verified atomic API commit on the artifact branch. Invalid or superseded inputs cannot publish. |
| Contributor feedback | Verified releases produce categorized notes and idempotent version notifications on included merged PRs. |
| Onboarding | Root `SKILL.md` describes supported local workflows; README inputs link to the complete generated chart parameters. |

## Existing behavior retained

The operator already separates application services, metrics, admission and
lifecycle ownership. Runtime, coordination, authorization, cancellation and chart
behavior remain covered by the feature suite. Container production and development
paths keep Tini, non-root execution and locked dependencies. Saved study
measurements are historical evidence and are not rewritten by layout migration.

## Verification boundaries

Hosted CI checks the supported Python matrix, database integrations, chart property
tests, container builds and cluster-oriented tests. Local results and hosted
results must be reported separately for the actual PR revision.

Repository settings are declarative until applied by the host integration. Confirm
that `CI verification`, one independent approval and squash-only merging are
actually enforced before merging. Administrator bypass remains a maintainer
policy, not automatic authorization for an agent.

No release, credential setup or live deployment is performed by this review.
Before tagging, configure and verify the release inputs in [release operations](releases.md).
Legacy typing scope and operational limits remain governed by their existing
contracts; this review does not claim that every historical test is strictly typed
or that synthetic checks establish production capacity.
