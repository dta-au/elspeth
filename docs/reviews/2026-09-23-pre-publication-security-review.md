# Pre-publication security plan review

Reviewed for `release/0.8.1` against baseline `ee04378f8`. Four independent readers
checked source reality, architecture, test quality and downstream effects. The
original brief required changes; the corrected plan addresses these findings.

| Finding | Resolution |
| --- | --- |
| Workflow removal cannot revoke an Actions secret grant | Operator approved repository-secret deletion; effective repository, organisation and environment inventories checked below |
| Shape-only push checking would lose signature authenticity | Preserve required-mode push verification and its fail-closed result |
| Release rules alone do not constrain workflows on other writable refs | Document the actual secret and runner boundaries and options |
| Plan environment dump could print the signing key | Presence/names only; no key values accessed |
| Missing live identity was treated as valid sentinel custody | Require matching canonical blob identity |
| Existing terminal case-C test blessed the false custody claim | Replace it with unavailable/masked projection regression |
| Active and historical custody consumers have different failure directions | Preserve strict active/export/admission rejection and terminal/history degradation |
| Trusted guided materializer dropped identity while resolving a sentinel | Retain identity from the verified reviewed binding; test real candidate production |
| Shared validator also reaches export and persistence | Exercise those consumers and run default plus PostgreSQL suites |
| Bulk issue importer would publish unrelated pending issues | Keep amplification separate and scoped; do not publish mitigation as full closure |

Controlled baseline sentinel probe, using the real validator and synthetic data:

```text
matching_ref: accepted
conflicting_ref: rejected
missing_ref: accepted
```

The third result confirms the defect while the first two control the instrument.
No production records or real uploaded bytes were modified.

Read-only GitHub metadata commands (maintainer credential selected per command):

```text
gh api repos/dta-au/elspeth/actions/secrets
  ELSPETH_JUDGE_METADATA_HMAC_KEY present
gh api repos/dta-au/elspeth/rulesets/12348893
  enforcement: active; include: [~DEFAULT_BRANCH]
gh api repos/dta-au/elspeth/rules/branches/release%2F0.8.1
  []
gh api repos/dta-au/elspeth/environments/copilot/secrets
  {"total_count":0,"secrets":[]}
gh api repos/dta-au/elspeth/environments/github-pages/secrets
  {"total_count":0,"secrets":[]}
gh api orgs/dta-au/actions/secrets
  HTTP 403: requires admin:org / actions-secrets permission
```

These measurements establish a repository grant and a permission ceiling for
organisation grants. They do not prove organisation grants absent.

After the operator explicitly approved removal, execution completed:

```text
gh secret delete ELSPETH_JUDGE_METADATA_HMAC_KEY --repo dta-au/elspeth
  exit 0
gh api repos/dta-au/elspeth/actions/secrets
  total_count: 3
  names: AWS_DEPLOY_ROLE_ARN, AWS_REGION, OPENROUTER_API_KEY
```

The repository grant is removed. A subsequent repository-scoped query resolved
the inherited-grant uncertainty:

```text
gh api --paginate repos/dta-au/elspeth/actions/organization-secrets?per_page=100
  {"total_count":0,"secrets":[]}
  exit 0
```

Together with the empty environment inventories, this closes the identified
Actions secret-grant exposure. Runner-local custody remains unreviewed; this
does not establish that no other route to operator key material exists.

Authoritative signature verification remains operator-only and was not performed.
See the
[implementation plan](../plans/2026-09-23-pre-publication-security-fixes.md) for
execution order, approval boundary, and verification requirements.

## Implementation and validation

The source patch requires canonical matching live blob identity, preserves the
identity already verified by the guided materializer, and removes unreachable
sentinel-identity inference in export. Independent specification and security
review, including complete reads of the three changed production files, found
no blocking production defect.

Nineteen failing baseline regression arms established the custody defect and
writer behavior before the fix. The first corrected focused suite passed 319
tests. CI policy tests passed 73, and signature-mode tests passed 84.

The frozen gate at base `1e9cafa9d` completed with these raw summary records:

```text
stage=ruff exit=0
stage=mypy exit=0
stage=contracts exit=0
stage=lints exit=1
stage=pytest exit=1: 23 failed, 55734 passed, 100 skipped, 2 xfailed
stage=testcontainer exit=0: 560 passed, 1 skipped
frozen=yes
```

The controlled lint comparison found `before=1078 after=1078 added=0 removed=0`
distinct finding records after normalizing source locations. The expected
signature-stage failure remains; no signature or allowlist was changed.

The default-suite failures comprised nine historical byte expectations for false
sentinel custody, twelve no-ref fixtures, and two planner-error audit-retention
assertions. The latter two failed identically in serial runs on both the exact
baseline and the candidate: rejected planner prose is intentionally retained in
three private audit records. Revised assertions validate their complete envelopes
and hashes while prohibiting leaks through all other fields, public messages,
provider replay, logs and output. An ordinary-message append proves that the leak
check still rejects incorrectly retained content.

The repaired historical suite passed 54 tests; its baseline negative control
failed exactly the nine intended cases and passed the other 45. Historical JSON
and executable corpus data were unchanged. The four fixture repairs passed 69
focused tests, preserving the existing negative custody checks. The planner file
then recorded 337 passes and four failures in the new negative control because
its attempted update correctly tripped the database's append-only trigger. The
control now uses the production message-append API; all five affected cases
passed, including the executed leak controls. Independent post-gate review found
no weakened oracle or blocking defect.

Our repairs after the frozen run are test-only, plus documentation and a stale
pre-commit custody comment; all three patched production files retain their tested
hashes. Three subsequently integrated release commits through `64863ff15` changed
only documentation. Release then advanced to `a6803f2e4`, incorporating a separate
advisor metadata fingerprint fix in `completion_gates.py`. That change was reviewed
and integrated before combined execution/session validation, which passed all
660 tests (exit 0). Final Ruff checks passed; the final controlled lint comparison
again reported `before=1078 after=1078 added=0 removed=0`. The broad run remains
a failed run reconciled by bounded repairs, not a newly green full-suite result.

Historical missing-ref sentinel snapshots now refuse active projection or degrade
terminal projection. Their old settled-operation replay hashes may no longer match.
Affected pre-fix operations require a fresh session or source rebinding; the repair
does not rewrite their hashes or reproduce the false custody claim for compatibility.
