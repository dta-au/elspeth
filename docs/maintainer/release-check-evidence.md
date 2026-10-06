# Publication check evidence

`scripts/cicd/check_release_required_checks.py` reads the active `main` ruleset
before any image build or registry login. Every required context must succeed;
the gate preserves GitHub App bindings and the explicit CodeQL context alias.
It does not modify branch protection or permit a manual dispatch to bypass CI.

CI and CodeQL run on main pushes. Cohort attribution and composer redaction
run on pull requests, so a new main merge commit normally has neither PR-only
context. Requiring those two contexts to run on that commit prevented image
publication after release promotion PR #269 despite successful reviewed PR
checks and successful main CI.

## Accepted proof

The verifier first evaluates checks and statuses on the immutable image SHA.
Direct success remains sufficient for a version-tag or manual publication.
Direct failure, pending, cancellation or skip remains a refusal. CI, CodeQL and
unknown future required contexts must have successful exact-image evidence.

Only missing cohort-attribution and redaction contexts may use source evidence:

1. GitHub must identify exactly one closed, merged PR producing this exact
   image commit on the configured target branch, with base and source in this
   repository. Association with a commit or a successful ancestor is insufficient.
2. A two-parent merge must have that PR head as its second parent. A one-parent
   squash or final rebase result uses GitHub's exact merged-result relationship.
   Other parent topologies are refused.
3. The immutable PR head and image commit must have identical Git tree IDs.
   An integration that changes the resulting tree needs fresh proof; the gate
   does not guess that a conflict resolution is harmless.
4. Each inherited check must come from GitHub Actions, satisfy any ruleset App
   binding, and belong to the latest matching trusted PR workflow's check suite.
   The workflow must have the expected path, event, head SHA, source branch and
   repository. A newer failed or pending run cannot be replaced by old success.
   Workflow run numbers and rerun attempts determine freshness, rather than
   the completion time of an older long-running job.

The API can return empty PR-association lists for merged workflow/check runs.
This proof therefore uses GitHub's SHA-scoped successful workflow evidence,
anchored by the verified merged PR's exact result/head/tree relationship. It
does not claim unique per-PR execution attestation when the API omits that
association. If a nonempty workflow PR list is supplied, it must include the
selected PR. No legacy status is used to invent inherited workflow provenance.

Malformed, unreadable, ambiguous or absent lineage/provenance refuses
publication. The log names any inherited source SHA and merged PR. The
publisher has read-only PR access for these API reads; existing registry/OIDC
write permissions and repository settings remain unchanged.

## Event and commit binding

For `workflow_run`, `IMAGE_SHA` comes from the completed CI run's `head_sha`,
because the event's `github.sha` can be a later default-branch tip. Tag pushes
and manual dispatches use their event SHA. All publisher checkouts, immutable
image tags and OCI revision labels use `IMAGE_SHA`; smoke and version promotion
consume the verified image digests. No workflow dispatch or tag is created by
the gate.

Gate code is checked out from the image commit. Rerunning a historical failed
publication therefore uses its historical checker. A later main commit that
contains this fix can use the new proof; this change does not promise to repair
an old run merely by retrying it.

## Verification

Regression tests exercise the production CLI and API parsers with offline
GitHub-shaped fixtures. Controls cover source-identical merge/squash/rebase
results, direct tag/manual evidence, pagination, wrong PR/result/repository/tree,
wrong workflow/App/suite/head, app-bound contexts, unknown contexts, newer
refusal states and malformed data. Workflow tests pin event-selected SHA,
trusted-push admission, checkout/metadata identity and minimal read permissions.
These tests make no provider calls and do not publish images.

GitHub documents [merged PR result SHA semantics](https://docs.github.com/en/rest/pulls/pulls#get-a-pull-request),
[workflow-run metadata](https://docs.github.com/en/rest/actions/workflow-runs#get-a-workflow-run),
and [run-number/attempt semantics](https://docs.github.com/en/actions/reference/workflows-and-actions/variables).
