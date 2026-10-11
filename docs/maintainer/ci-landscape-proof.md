# Nyx CI and Landscape proofs

[CI](../../.github/workflows/ci.yaml) runs eight ordinary shards and calls the
[Landscape proof workflow](../../.github/workflows/landscape-long-run.yml).
Both use the existing trusted Nyx pool and same-repository PR admission.
Runner labels and contributor-edited YAML are not an external trust boundary;
the repository's contributor permissions and fork-approval policy remain
separate from these source checks. See the
[runner hardening guide](../runbooks/self-hosted-runner-hardening.md).

This CI package is independent of release activation. It does not change the
package version, schemas, production behavior, runner access or required-check
settings. A green result must come from the native checks on the candidate's
actual PR merge commit; evidence from another release head is not qualification.

## Selection and evidence

Each ordinary job uses two pytest workers; at most two ordinary matrix jobs run
concurrently. Each proof job runs one of these existing cases serially:

| Case | Test in `test_web_landscape_mutation_fencing.py` |
| --- | --- |
| `no-escape` | `test_no_mutation_alias_wrapper_dynamic_or_raw_write_escape_exists` |
| `reserve-actual-sources` | `test_required_work_reserve_admission_on_actual_sources` |
| `protected-formal-recovery` | `test_required_work_reserve_rejects_protected_instance_formal_recovery` |

All producers first collect `tests/` using normal marker selection. The
[partition helper](../../scripts/ci_test_shards.py) requires exactly these
three marked cases. Ordinary shards exclude them; each proof producer selects
exactly one. Local unsharded selection remains unchanged. Both Python 3.12
and 3.13 must pass; coverage is collected on 3.13.

Ordinary assignment uses deterministic longest-estimated-first scheduling from
the [duration profile](../../config/cicd/pytest-duration-profile.json). Ties
use node ID and then shard index. Native collection order is preserved inside
each bucket. Only collected IDs are assigned; absent profile entries use a
positive default, and obsolete entries add no tests. The profile records its
earlier uncovered Python 3.13.15 JUnit source and digest. Some source IDs came
from release-side test extractions that are not prerequisites of this CI port.
These are scheduling estimates, not measured balance or wall-clock predictions
for main. Refresh exact ID mappings when moving tests; do not rewrite provenance
to imply a measurement that was not performed.

Schema-4 manifests bind checked-out HEAD, expected `GITHUB_SHA`, native run and
attempt, interpreter, producer role, partition, shard, assignment algorithm and
profile digest. All producers must agree on the complete marker-selected
collection. Each proof must record a successful singleton call; pytest exit
zero alone is insufficient. Coverage databases must match finalized hashes
and contain executed source lines.

The consumer requires eight ordinary plus three proof producers **per
interpreter**, verifies their disjoint complete union, and re-derives the weighted
assignment. Only the 3.13 union is combined. Existing coverage floors remain
85% overall, 92% Landscape, 99% canonical, 90% orchestrator and 62% contracts.
Both the coverage job and `CI Success` require actual successful ordinary and
proof job results; neither missing evidence nor a skipped producer is success.

Artifacts have seven-day retention and attempt-qualified names:
`ordinary-shard-{version}-{index}-attempt-{attempt}` and
`landscape-proof-{case}-{version}-attempt-{attempt}`. The consumer selects the
highest observed attempt before opening its content. A superseded malformed
artifact cannot block a valid retry, but missing, corrupt, failing or unfinalized
selected evidence is rejected. It never falls back to an older success. Native
job results remain necessary because an artifact consumer cannot discover a
failed rerun that uploaded nothing.

## Execution and qualification

The [Linux supervisor](../../scripts/ci_proof_progress.py) reports elapsed child
lifetime, including collection, every 30 seconds. Heartbeats show liveness,
not forward progress or an ETA. Terminal evidence contains wall time,
reaped-process CPU, child status, cancellation and cleanup outcome. Success
requires successful completion with the process group gone. Timeout returns
124; incomplete cleanup or abandoned descendants returns 125; cancellation
returns `128 + signal`. Cancellation during publication invalidates terminal
evidence.

Proof concurrency is capped at two within its matrix, not across the host.
Provisional process caps are 7,200 seconds on 3.12 and 14,400 seconds on covered
3.13; job caps are 150 and 270 minutes. These are limits, not timing claims.
Coordinate broad qualification with other host workloads and avoid launching a
duplicate local suite alongside candidate CI.

Focused tests cover partition identity, retry recovery, coverage binding,
failure propagation and process cleanup, with negative controls. Native
candidate CI must additionally establish collection parity, all six long proofs,
the complete 3.13 coverage union and every protected required context. Neither
source review nor fixture-only tests establish those broader results.
