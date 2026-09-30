# Miscellaneous component integration

Date: 2026-09-30. Target: local `release/0.8.1`.

Local code integration completed: `release/0.8.1` fast-forwarded from
`17a1b83dba3406d0e980f448c83e03095ac7558a` to the accepted code checkpoint
`18aa57b3be9ac1a00868d83d81beaa77f04e7fa4`. The release HEAD and branch were
re-verified after the merge and preservation checks.

## Recovered components

The [starting inventory](2026-09-30-worktree-release-coverage.md) and its
companion JSON record the donor comparisons. Valuable changes were reconciled
with current successors rather than restoring entire stale files.

- Finite CSV, JSON and JSONL source continuation: strict opt-in, sealed
  emissions before downstream effects, retained quarantine identities,
  changed/deleted input resume, bounded capture and safe payload retention.
- Web response admission, bounded GET pagination and JSON record extraction:
  per-page origin/SSRF validation and audit evidence, response limits and
  aligned provenance.
- Auth configuration and confidentiality admission, plus the inert browser
  boundary/archive/replay foundation and its negative controls.
- Composer source approval reconciliation, current planner guidance, output
  declaration/schema precedence and persistence regression coverage.
- Detached interpretation-validation facts adapted to current lowering and
  authority contracts, without retaining process-local services.
- Embedded IPv4 SSRF denial before allowlist admission; dependency security
  fixes for OAuthlib, PyJWT and the incorporated Moment lock entry.
- Useful current plans and a source-resume guide. Superseded Guided/session,
  diagnostics and older contract implementations retain their current
  successors; the inventory explains their dispositions.

## Similar bugs and integration repairs

The requested systems-thinking search examined shared mechanisms and their
other callers. The [similar-bug report](../audit/2026-09-30-misc-components-similar-bugs.md)
records nine confirmed repairs: hidden ordinary-row quarantine metadata,
purge retry accounting/grade recovery, shared encoding-limit handling,
Unicode DNS/HTTP identity, normalized Azure control credit, Composer approval
authority, coalesce result/payload witnesses, sink diversion ordinals and
journal integer admission. Their independent review and focused controls
completed before incorporation. Malformed URL refusal was then made explicit;
344 focused security/browser/network tests completed with exit 0.

The first accumulated default run exposed twelve integration failures. Their
fixture, inventory, schema and planner-guidance repairs preserve the actual
contracts and limits. The old run remains failed evidence, not acceptance.

Current release CI repairs were incorporated before final acceptance:
deterministic pytest shards with complete coverage aggregation at unchanged
85/92/99/90/62 floors, meaningful Landscape/orchestration regression tests,
idempotent export reservations, mandatory HMAC for forced headers in every
mode, accurate browser fixtures and deterministic signal/steering tests.
Their prior frozen full/serial-PostgreSQL gate passed; the composite's own
accumulated checkpoint and subsequent repair evidence are recorded below. The bounded merge review
is GO: both parents' overlapping fixes survive, and the live mutation scanner
matches the release inventory. The final repairs were made directly in the
existing composite worktree. Later release changes through `17a1b83db` were
also retained: CI runner routing/isolation, recorded-dispatch test determinism,
the advisory-service timeout, security-assurance updates and the Power Automate
HTTP source/sink plan.

## Acceptance

Code checkpoint: `18aa57b3be9ac1a00868d83d81beaa77f04e7fa4`.

| Check | Measured result |
| --- | --- |
| Frozen final Ruff, mypy, contracts | Each exit 0 on the code checkpoint. |
| Keyless lint diagnostic | Earlier frozen `18a631f01` checkpoint: exit 1, 2456 findings; nonfatal diagnostic stage. Controlled comparison adds no semantic findings and removes the URL swallowed-error R6. The subsequent closed error-code registry addition was verified by focused redaction tests and normal commit hooks. This diagnostic does not grant operator signature clearance. |
| Default Python suite with coverage | Frozen `18a631f01` checkpoint: 60617 passed, 36 failed, 90 skipped, 2 xfailed; exit 1. All 36 exact failed identities subsequently have explicit PASS outcomes in the bounded checks below. The original run remains failed evidence. |
| Complete serial PostgreSQL selection | Frozen checkpoint: 651 passed, one readiness probe timeout, one skipped; exit 1. The failing node passed serially; all four readiness tests also passed with production timeouts unchanged. |
| Architecture repair | Complete file: 726 passed, exit 0. All 34 previously failing architecture identities pass. Production, architecture file, seven helper/config inputs and import provenance remained identical during the run. |
| Final focused CI and web checks | 175 passed, exit 0 on the frozen code checkpoint; covers both web failures, closed-code redaction controls, and all five incoming CI/ACA test files. |
| Final PostgreSQL readiness proofs | All four tests passed, exit 0 on the frozen code checkpoint, including connection refusal and bounded unfinished-probe controls. |
| All five unchanged CI coverage floors | Each actual CI command exit 0: overall 88%, Landscape 93%, canonical 99%, orchestrator 93%, contracts 66%. |
| Fresh root and frontend npm audits | Both exit 0; each reports zero vulnerabilities. |
| Authorized Python dependency audit | Exit 0, 210 records, zero unignored vulnerabilities, five existing exceptions. Current dependency manifests/lock exactly match the audited tree; lock SHA256 `a19a3aadd50ffd9048a4dd024941e13288704337a895a042477992cdf126f625`. |

The accumulated runtime checkpoint ran with two workers alongside hosted CI,
explicitly authorized by the user after the shared-host capacity check.

The 34 architecture failures shared one stale executable-AST journal binding.
Review established that the strict journal ordinal/size repair retained deadline
registration, event ordering and rollback behavior. The pin was refreshed after
that review; no scanner or mutation control was weakened. The other two failures
were a missing closed error-code registration and an ineffective content-safety
fixture. The fixture now supplies explicit blocking thresholds and proves its
success-path control before isolating the unprotected quarantine route.

The PostgreSQL failure logged two-second database probe timeouts. Its readiness
production and test files are byte-identical to the previously passing release
checkpoint. Serial checks passed without changing production readiness behavior
or relaxing deadlines. This is bounded acceptance of the diagnosed failure, not
a claim that the original complete PostgreSQL run exited successfully.

After the accumulated run, only the three measured repair paths and 17 current
release workflow/document/test paths changed. The sole Python production change
adds `web_scrape_auth_unavailable` to the closed error-code registry. Current
release Python production remains intact. Exact tree comparisons and original
failure-to-PASS identity checks bound this acceptance; the broad suite was not
repeated for the isolated repairs or history integration.

## Preservation and limits

Fresh controlled donor comparison: all 40 original nonrelease refs and 37
donor worktrees retain their original tips and registrations; all 382
recorded dirty/untracked/deleted donor paths retain their recorded bytes,
modes and index entries. Zero donor differences. This excludes primary and
ignored content, and does not claim historical whole-index identity.

Primary custody completed: a stable capture measured 63 original dirty paths,
six exact untracked-document collisions and no staged changes. The six originals
were held in a private ignored archive while the local release fast-forwarded,
then restored. Verification reports `checked_original_paths=63`,
`preserved=true`, `problems=[]`, `successful_ff=true`, and all six originals
restored. Their bytes, modes and inode identities were preserved; the six paths
now show the expected tracked-file modifications against the integrated versions.

The transfer helper was controlled in disposable repositories against successful
and failed fast-forwards, concurrent edits, mode/inode preservation and invalid
gate evidence. Bounded admission retains the original failed runtime records,
proves the exact permitted code delta and native architecture inputs, and matches
each original failed identity to a passing repair result. Controls refused every
individually omitted failing node, stale/dirty captures, malformed exits and extra
production changes. The documentation report is a subsequent isolated addition;
all other accepted tracked blobs and modes are retained.

Authenticated web execution remains disabled before DNS; browser code is an
unregistered foundation; S2 retains its hold. Composer authoring retains
provider calls and the ordinary shared backend. Local integration does not
claim hosted CI, deployment, push or operator signature clearance.
