# Repository standards review

<!-- toc:start -->
**Table of contents**

- [Applied changes](#applied-changes)
- [Existing behavior retained](#existing-behavior-retained)
- [Verification boundaries](#verification-boundaries)
- [Diff review follow-up](#diff-review-follow-up)
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
| Dependency security | Documentation parsers and the Flux test module use patched dependency locks; validate both JavaScript graphs and the Go contracts when refreshing them. |
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

## Diff review follow-up

The follow-up reviewed the PR comparison from
[2666f45](https://github.com/astrivant/polyad/commit/2666f45dc1640d1986204be227b8c388b4f4c3c9)
through the current head, concentrating on changed delivery, documentation,
ownership, generated output and direct path consumers.

- **Host enforcement gap:** the live `main` protection API on 2026-10-16 returned
  an empty required-check context list. Repository merge settings also still allow
  merge commits and rebases. Apply and verify the declaration in
  [repository settings](../../.github/settings.yml) before relying on the intended
  `CI verification`, independent review and squash-only merge baseline.
- **Generated conflicts:** the inventory and trusted repair stage now make safe
  conflict handling explicit. Only conservative literal CODEOWNERS merges and
  exact generated copies are automatic. See [the repair contract](generated-artifacts.md).
- **Verification remains revision-specific:** hosted chart property tests were
  still running when inspected. A passing source subset does not establish a
  successful full pipeline or a verified live release.
- **Automated audit coverage:** the retained 61.8 MB study-results file exceeds
  the current audit tool's 32 MiB per-file limit. The measurements are preserved;
  no full-workspace automated compliance claim is made.

Other applicable checks retain the existing release-signature, package, coverage,
layout, source-generation and ownership contracts. Runtime APIs, storage and
saved measurement semantics are outside this follow-up's changed surface.
