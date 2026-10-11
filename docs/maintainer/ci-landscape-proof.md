# Nyx CI and Landscape proofs

The six Nyx sources, including [CI](../../.github/workflows/ci.yaml) and its
[proof callee](../../.github/workflows/landscape-long-run.yml), are in GitHub's
workflow discovery directory. Activation and publication on the release branch
were authorized on 2026-10-11 UTC. Source activation alone does not establish
runner admission or runtime qualification. Source contracts require the complete
executable inventory, runner routing and supply-chain closure; missing or
duplicate identities fail the contract.

The existing main ruleset requires `CI Success`, `CodeQL`,
`Check cohort-attribution trailers on PR commits`, and `redaction-gate`. This
package must supply genuine successful native checks. Missing required checks
block a new commit under that rule; conditional job skips can report success,
so they are unsuitable as qualification evidence. Existing same-commit results,
producer spoofing and bypass permissions remain external policy concerns.
[GitHub documents the distinction](https://docs.github.com/en/pull-requests/how-tos/merge-and-close-pull-requests/troubleshooting-required-status-checks).
Automatic CI-triggered image publication remains disabled, and release evidence
continues to require literal successful results. No required-check rule is relaxed.

## Admission and native qualification

The [CI admission policy](nyx-ci-admission-proposal.md) records the maintainer's
trusted-workgroup decision, required GitHub setting and remaining qualification.
The current policy trusts the existing organization members and repository
writer, requires `all_external_contributors` fork-run approval, and leaves
outsider runs unapproved. GitHub's organization-member and collaborator approval
exceptions are inside that explicitly accepted trust boundary. Contributor-edited
YAML and runner labels do not provide it.

Read back the contributor policy before publication. During native qualification,
verify that an
outsider-edited workflow remains awaiting approval without acquiring a Nyx runner,
and that trusted same-repository PRs produce the genuine required checks. Do not
approve the outsider run as part of that refusal check. Preserve native PR merge
SHA and run/attempt evidence; do not substitute a manually dispatched success.
These source files neither change GitHub settings nor qualify the runner host.

## Selection and required evidence

The ordinary matrix has eight shards per Python interpreter, with two
pytest workers per job and at most two concurrent ordinary matrix jobs. The
three exact `landscape_long_run` cases are:

| Case | Existing test in `test_web_landscape_mutation_fencing.py` |
| --- | --- |
| `no-escape` | `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` |
| `reserve-actual-sources` | `test_required_work_reserve_admission_on_actual_sources` |
| `protected-formal-recovery` | `test_required_work_reserve_rejects_protected_instance_formal_recovery` |

Each producer first collects `tests/` with the normal marker selection. The
[partition helper](../../scripts/ci_test_shards.py) requires all three catalog
cases and exactly their markers. Ordinary shards exclude those cases; each
proof producer runs one unchanged test serially. All protected formal variants
remain inside their original test. Python 3.12 is uncovered and 3.13 is covered.
Local unsharded pytest selection remains unchanged.

Ordinary assignment uses deterministic longest-estimated-first scheduling from
the [checked-in duration profile](../../config/cicd/pytest-duration-profile.json).
Ties use node ID and then shard index. Producers preserve native collection order
inside each bucket. Only current collected IDs are assigned; new or renamed IDs
receive the positive default estimate, and removed IDs in the profile add no tests.
The profile records the earlier uncovered, six-worker Python 3.13.15 JUnit source
and its digest. These are scheduling estimates, not native 3.12/covered 3.13
qualification or measured shard wall times. Refresh exact ID mappings when moving
test classes or packages.

Coverage requires native success from both ordinary and proof jobs before
validating the complete disjoint union for **each interpreter**. Only the 3.13
union is combined. `CI Success` also requires the proof and coverage results.
Coverage floors remain overall 85%, Landscape 92%, canonical 99%, orchestrator
90% and contracts 62%; they apply to the combined database, never a shard.

Partitioned schema-4 manifests bind checked-out HEAD, expected `GITHUB_SHA`, native
run ID/attempt, interpreter, producer role, partition and shard. Proof evidence
requires an actual successful singleton call, not merely pytest exit zero.
Covered databases must match their finalized hashes and contain executed source
lines. All producers must agree on the complete marker-selected collection.
Every producer also binds the assignment algorithm and exact duration-profile
digest. The consumer re-derives the weighted assignment from the validated union;
old schema-3 evidence or swapped buckets cannot satisfy it. Eight ordinary and
three proof producers are required per interpreter.

Artifacts have seven-day retention and names
`ordinary-shard-{version}-{index}-attempt-{attempt}` and
`landscape-proof-{case}-{version}-attempt-{attempt}`. The consumer selects the
highest observed attempt for each producer, validates only that selected content,
and refuses missing, corrupt, failing or unfinalized selected evidence. It never
falls back to an older success. Native job results remain required because an
artifact-only consumer cannot discover a failed rerun that uploaded nothing.

## Execution limits and qualification

The [Linux supervisor](../../scripts/ci_proof_progress.py) reports child lifetime
including collection every 30 seconds. A heartbeat is not forward progress or an
ETA. Terminal reports contain wall time, reaped-process CPU accounting, child
status, cancellation and cleanup outcome. Only successful completion with the
process group gone succeeds. Timeout returns 124; incomplete cleanup or abandoned
descendants returns 125; cancellation returns `128 + signal`. Cancellation also
invalidates terminal evidence when it arrives during publication.

Proof concurrency is capped at two within its matrix; that is not a global host
budget. Provisional execution caps are 7200 seconds for 3.12 and 14400 seconds for
covered 3.13, with job caps of 150 and 270 minutes. They are not measured optimized
proof timings. Actual native collection parity, all six case/interpreter proofs,
the full 3.13 coverage union, timing budgets and native artifact/rerun/cancellation
behavior still require qualification. Source review and tiny fixtures do not
establish those results.
