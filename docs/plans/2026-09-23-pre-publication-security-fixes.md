# Pre-publication security fixes

Target: `release/0.8.1`; isolated worktree baseline `ee04378f8`.

The original brief needed three material corrections: deleting workflow references
cannot revoke a GitHub secret grant; shape-only checking cannot replace HMAC
verification; active custody rejection differs from terminal/history degradation.

## 1. Remove judge-key injection without weakening verification

The three key-bearing CI steps verify trust-tier metadata, trust-boundary metadata,
and SARIF findings; they do not sign. Remove their HMAC environment injection.
Preserve PR shape-only mode and required HMAC verification on pushes. A keyless
push must fail closed. Do not clear the standing trust-tier gate to obtain green CI.

Change the regression that currently requires unsafe injection; prove the new
regression fails on baseline and passes on the patch. Preserve missing-key and
CI-never-signs tests. Update `docs/judge-signature-handoff.md`, the judge workflow
comment, and `CONTRIBUTING.md`. Authoritative merge still requires an operator to
verify the exact reviewed candidate using trusted verifier code. Agents never
obtain the key, sign, rekey, or repair the global signature corpus.

### External action required for F-01 closure

Initial read-only GitHub metadata on 2026-09-23 confirmed the repository HMAC secret existed.
Ruleset `12348893` applies only to `~DEFAULT_BRANCH`. Existing `copilot` and
`github-pages` environments provide no judge-key approval boundary. Organisation
secret enumeration returned HTTP 403 requiring `admin:org`. A subsequent
repository-scoped `repos/dta-au/elspeth/actions/organization-secrets` query
succeeded with `{"total_count":0,"secrets":[]}`, resolving the relevant grant
question without broader permissions. Both environment secret lists were empty.

Deleting YAML injection is only a local mitigation: another pushed workflow can
reference a repository or accessible organisation secret. Restricting `release/**`
alone cannot prevent a writer creating a workflow on another writable ref.

The operator explicitly approved the following deletion during execution. It
completed successfully, and the secret-list recheck returned only
`AWS_DEPLOY_ROLE_ARN`, `AWS_REGION`, and `OPENROUTER_API_KEY`:

```bash
GH_TOKEN="$(gh auth token -u johnm-dta)" \
  gh secret delete ELSPETH_JUDGE_METADATA_HMAC_KEY --repo dta-au/elspeth
GH_TOKEN="$(gh auth token -u johnm-dta)" \
  gh api repos/dta-au/elspeth/actions/secrets
```

Measured before/after: the repository secret name disappeared; no local operator
key material or signatures were changed. The effective organisation and environment
inventories establish no inherited copy. The identified Actions secret-grant
exposure is closed; runner-local key custody was not audited. Further settings,
runner, key rotation, or public issue mutations remain separate actions.

Options and costs:

- Restrict creation/update with an operator bypass: preserves that operator's
  direct pushes, excludes other writers. Required PRs add review overhead.
  Neither protects secrets/hardware from workflows on other writable refs.
- Move the key exclusively to an environment with required reviewers and narrow
  deployment refs: approval per key-bearing run, plus exact workflow/dependency/
  code review before release. It does not isolate a self-hosted runner or revoke
  duplicate grants. A verifier independent of candidate-controlled code is needed.
- Remove the Actions key grant: smallest custody fix; CI remains fail-closed until
  a separately approved authoritative verification design exists. Self-hosted
  runner access and the separate OpenRouter credential remain independent risks.

References: [environment protection](https://docs.github.com/en/actions/reference/workflows-and-actions/deployments-and-environments),
[self-hosted runner risks](https://docs.github.com/en/actions/how-tos/manage-runners/self-hosted-runners/add-runners).

## 2. Reject unverified sentinel custody

Specification: [held custody finding](../github-issues/held/sentinel-custody-projection-fails-open-when-live-source-op.md).

Root cause: `validate_guided_reviewed_sentinel_source_mapping` compares identities
only when `blob_ref` exists. Otherwise carrier shape suffices to substitute the
reviewed sentinel over unrelated live bytes.

Chosen behavior: require a canonical matching live `blob_ref`; do not infer identity
from path syntax. This pure validator has no trusted blob storage lookup. Repair
the verified reviewed-source materializer in `tools/sessions.py` to retain the
server-authored `blob_ref` when resolving sentinel-only reviewed options. Prove
that real candidate-building path remains persistable before requiring identity
in the shared validator. Surface this behavior choice before implementation.

Preserve consumer directions:

- Active projection, export and persistence admission reject inconsistent custody;
  preserve `GuidedCustodyIntegrityError` and existing export error translation.
- Terminal and explicitly tolerant history projections return `custody_unavailable`,
  mask paths and remove authoritative review proof. Never emit an unverified sentinel.
- Matching references still work; conflicting references still fail. Cover both path
  carriers (`path`, `file`) and multi-carrier sources.

Replace the existing terminal case-C test that blesses the bug; add missing-ref
regressions beside the conflicting-ref test. Cover YAML export and persistence
admission. Prove regressions fail on unchanged production code before implementing.
Only repair positive fixtures after validating their real producer. Remove sentinel
reattachment code made unreachable by requiring live identity.

Historical byte-stability expectations for the three missing-ref sentinel shapes
must change to active refusal / terminal degradation. Preserve their historical
JSON and corpus inputs. Affected pre-fix settled operations may fail replay because
their old projection hash represented a false custody claim; start a fresh session
or rebind the source. Do not rewrite stored hashes or relax replay verification.

## Validation and landing

Use the primary interpreter explicitly with both worktree source roots in PYTHONPATH,
or a real local environment. Verify imports. Check judge environment variable names
or presence only: the original `env | grep ELSPETH_JUDGE` could print the key.

Run focused CI, custody/redaction, export and session tests and relevant whole-tree
gates. Compare baseline/candidate lint finding sets; do not hand-edit signatures.
The shared validator reaches persistence admission, so run the full default suite
and serial PostgreSQL testcontainer suite before merge. Coordinate host capacity;
record completed exit codes and frozen source state.

Obtain independent review, resolve findings, run branch safety before commit/merge,
and preserve unrelated primary edits. Merge locally into `release/0.8.1` when gates
permit. Remote push, authoritative verification, external settings and live acceptance
are separate states; report a blocking gate instead of claiming partial work complete.

Keep the held finding until closure, then update wording and run
`python3 docs/github-issues/check_issues.py`. The original bulk importer publishes
all pending issues and is not scoped to this task: any public import must select
only the reviewed issue and requires approval. Held files are already public in Git;
holding limits amplification, not disclosure.
