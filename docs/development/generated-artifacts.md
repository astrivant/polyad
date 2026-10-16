# Generated artifacts and conflict repair

<!-- toc:start -->
**Table of contents**

- [Trusted repair stage](#trusted-repair-stage)
- [Local inspection](#local-inspection)
<!-- toc:end -->

The [artifact inventory](../../.github/generated-artifacts.json) identifies canonical
inputs and the repair contract for each output. Local hooks and CI validate it.
A declared output is not automatically safe to overwrite.

## Trusted repair stage

The existing pipeline scans PRs after default-branch pushes, on PR target events,
and every six hours. The schedule retries GitHub's asynchronous mergeability
calculation. The stage runs only trusted default-branch code; PR objects remain
data in an isolated bare Git database. It never checks out PR code, runs candidate
generators, installs candidate dependencies or loads candidate local actions.

| Renderer | Automatic boundary |
| --- | --- |
| `codeowners` | Merge independent literal additions/removals with one unchanged owner group and unchanged commentary. Changed owners, wildcards and ambiguous policy need review. |
| `copy` | Recreate an exact output from its cleanly merged canonical source. |
| `manual` | Retain the generator and inputs in the inventory; source-aware human review is required. |

All conflict paths must qualify. A source conflict, changed inventory, missing file,
mode change, symlink, ambiguous owner or undeclared output prevents publication.
Schema generators import application models and therefore do not run in the
credential-bearing repair stage. Mixed authored/generated README files and measured
figures also remain review-owned.

The job can update only open, non-draft PR branches in this repository targeting
the default branch. It rechecks head/base bindings, constructs a two-parent merge,
requires GitHub to verify the bot signature, then updates the PR ref without force.
It never writes to the default branch, repairs forks or merges a PR. If the provider
cannot verify the commit or refuses the ref update, the repair fails visibly.

The repaired head still requires ordinary CI and review. GitHub may require approval
for runs triggered by its own token; the repair does not bypass that approval.
Unknown mergeability is reported and retried on the next event or scheduled scan.
The workflow becomes active only after this stage is reviewed into the default branch.

## Local inspection

```bash
python -m scripts.repair.plan --check
python -m scripts.repair.plan --head HEAD_SHA --base BASE_SHA --output /tmp/repair.json
```

Use full commit identifiers already fetched into this checkout. The second command
only writes a proposal; it does not change the worktree, index, branch or remote.
Review refusal diagnostics rather than substituting one side of a conflict.

The planner bounds input bytes, conflict count and publication size. It excludes
Git hooks, external merge drivers and candidate execution. Tests use real temporary
Git repositories for merge behavior and synthetic Octokit calls for publication
guards; those API tests do not establish live bot signing or hosted execution.

See GitHub's [event behavior](https://docs.github.com/en/actions/reference/workflows-and-actions/events-that-trigger-workflows)
and [pull_request_target security contract](https://docs.github.com/en/actions/reference/security/securely-using-pull_request_target).
