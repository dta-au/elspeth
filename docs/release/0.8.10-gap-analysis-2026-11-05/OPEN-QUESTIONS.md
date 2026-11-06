# Scope reconciliation and acceptance work

Refreshed 2026-11-06 at `ae88147c6130051b1e3d497a1341a69bd56924e5`.
These items include requirement reconciliation and acceptance work. **O03 now
also has a verified existing failed run on matching production/test/configuration
bytes.** Other unlocated evidence is not thereby absent. No new tests ran during
this refresh. Previously completed implementation units were not reopened.

## O01 — attachment limits, scan reuse and Advisor evidence representation

**Units:** U31/U32. **Kind:** historical/current specification discrepancy.

**Current status: unchanged/open.** Config at 380–389 still permits only 1–262144
bytes, and `test_continuation_attachment_read.py:163` explicitly expects two
scans on repeated reads. Current upgrade notes at 574–594 document that behavior.
The 6 November no-compatibility ruling does not itself decide this resource-policy
tradeoff. Complete Advisor evidence remains credited; its different type name
was not reopened as a missing feature.

The original H3c requirement in [the detailed plan](../../design/composer-continuation-plan.md),
lines 768–802, calls for a default derived from `max_upload_bytes`, support above
256 KiB and reuse of successful scans. Current configuration deliberately caps
complete originals at 262,144 bytes, and current-read tests expect fresh scans.
Complete-original read, hash verification, full UTF-8/JSON admission, credential
scanning and current SQL/actor/selection rechecks are connected. This is not a
demonstrated safety failure.

The originally named `AdvisorEvidenceCoverage` aggregate is implemented through
distributed summary/manifest/review ownership. Complete manifest coverage and
mandatory evidence reaching prepared SDK inputs are present.

Start with [attachment admission](../../../src/elspeth/web/composer/attachment_admission.py),
[current attachment reads](../../../src/elspeth/web/composer/continuation_attachment_read.py),
[Advisor context](../../../src/elspeth/web/composer/advisor_context.py),
[configuration](../../../src/elspeth/web/config.py), and
[mandatory evidence tests](../../../tests/unit/web/composer/test_advisor_mandatory_evidence_budget.py).

**Close by:** locating a superseding decision and aligning current requirements,
or presenting the concrete limit/cache tradeoff for a decision. Do not silently
increase admission bounds or remove safety rescans. A different representation
alone is not a defect if it preserves the required semantics.

## O02 — prescribed combined cold-restoration scenario

**Unit:** U36. **Kind:** combined acceptance coverage not fully located.

**Current status: partial, with additional joined coverage received.**
`test_continuation_semantic_handover.py:69–209,476–621,667–819` now exercises
completed Review, Read/iteration and paid-Stop restoration through actual
handover, fresh drivers and, in relevant cases, fresh service/repository owners.
It checks original effects, cursor consumption and historical corruption. The
PostgreSQL handover wrapper at 129–136 imports completed Review and paid Read cases.
ae881 additionally strengthens cold batch/captured-evidence/provider-history
ownership tests. Those are meaningful constituents, predominantly same-sequence
recovery, and do not close the prescribed combined cross-sequence composition.

[The plan](../../design/composer-continuation-plan.md), lines 925–931, asks for
one scenario retaining inspection, attachment admission, channel cursors and
bound review; discarding service/in-memory owners; restoring under a fresh fence;
committing in sequence 2; and checking cumulative revisions, semantic frontier,
no double drain, review reuse/invalidation and stale/malicious refusal.

The following constituent pointers are retained from the **a6990ffb original
audit**; their line numbers refer to that snapshot. The refreshed joined
handover evidence is described above:

- [Fresh-owner runtime restoration](../../../tests/unit/web/sessions/test_continuation_runtime.py), lines 443–590.
- [Deleted original attachment restoration](../../../tests/unit/web/sessions/test_continuation_attachment_application.py), lines 515–621.
- [Actual model-control successor](../../../tests/unit/web/sessions/test_continuation_control_successor.py), lines 94–191.
- [Actual action-fit successors](../../../tests/unit/web/sessions/test_continuation_action_fit_successor.py), lines 232–311.
- [Revisions across successor](../../../tests/unit/web/sessions/test_composer_continuation_yield.py), lines 416–455.
- [Historical P4 channel drains](../../../tests/unit/web/sessions/test_continuation_p4_historical_application.py), lines 476–535.
- [Review-proof reuse and corruption](../../../tests/unit/web/sessions/test_continuation_review_proof_reuse.py), lines 28–108.

**Close by:** locating equivalent joined coverage or implementing the exact
bounded scenario. The requirement explicitly permits actual session/authority
APIs; a final live worker/provider is not an additional acceptance requirement.
Do not replace required real store/authority ownership with codecs or permissive
mocks. Existing separate and joined recovery tests are evidence of implementation;
do not describe restoration or successors as missing.

## O03 — preparation failure and outstanding production performance evidence

**Unit:** U45. **Kind:** measured runtime failure on matching current code/tests,
plus original production performance acceptance still outstanding.

**Current execution evidence.** The new committed handover points directly to
`nov06-semantic-handoff-cold-primary-original-workflow-r220` under
`.claude/lanes/codex-takeover-20261101/`. The reviewer read its actual exit,
supervisor, JUnit, before/after manifests and safe driver trace; this is not a
result inferred from a status report. Raw result:

```text
adapter exit=1
supervisor exit_status=1 child_returncode=1 cleanup_complete=True forced_kill=False
before manifest == after manifest: True
JUnit cases=1 failures=1 errors=0 skips=0
elapsed_seconds=143.50928358803503
```

`test_supported_driver_reaches_original_paid_transitions[read_end_retry]`
settles `failed` instead of `completed`. Its safe driver trace records cursor 29,
original Advisor preparation allowance of 5 seconds and `TimeoutError` through
`bind_checkpoint_end_physical_request:365` and the action clock's preparation
guard. The whole advance takes 10.80121856695041 seconds; this is not preparation-only
CPU attribution or a proven hotspot. Do not describe it as failing after a
scripted malformed physical END response without establishing that dispatch.

R220 records c9f HEAD plus held edits. To avoid treating that SHA as proof of
current source, the reviewer compared every ae881 tracked entry by path/mode/
content against the held manifest:

```text
current tracked entries=7254
different/new entries=9  (.gitignore and eight Markdown documents)
receipt-only entries=14 (all Markdown documents)
current production/test/helper/runtime-configuration differences=0
```

This is concrete existing failure evidence on matching ae881 code and tests,
not a new audit execution or a whole-tree qualification run. The safe trace is
at `.scratch/pytest-nov06-semantic-handoff-cold-primary-original-workflow-r220/test_supported_driver_reaches_0/supported-read_end_retry-driver.json`;
the receipt directory and trace are existing local artifacts, not portable
contents of this handoff. If unavailable in the receiving checkout, reproduce
the named scenario rather than assume its result.

Earlier R176 had five passes, and R179/R180 failed the larger-history workflow;
those are historical after ae881. R179/R180 differ from ae881 in 16 production
files and the supported-path test and must not be described as current-source
receipts.

Clock, memo, worker and first-Compose accounting controls are implemented.
[First-Compose tests](../../../tests/unit/web/sessions/test_continuation_composition_initialization.py),
lines 221–323, check real prompt/table/replay/provider-count behavior, but their
observer policy allows retention of 120 seconds and cannot certify the production
five-second bound.
[Clock tests](../../../tests/unit/web/composer/test_continuation_action_admission.py),
lines 305–315, deliberately overrun a synchronous stage and require timeout.
The original at-least-20-run production-bound profile receipt was not established.

**Close by:** diagnose and correct the actual preparation failure, then receive
the scenario on current source and complete the required production-bound
profile with controlled capacity. A green short-flow test does not close the
larger-history failure. Keep per-transition provider-call accounting and tutorial
parity; do not widen production limits or remove provider work to manufacture a
pass. Preserve the distinction between observed guard failure and its not-yet-
established detailed cause.

## O04 — complete current donor residue partition

**Unit:** U49. **Kind:** incomplete semantic reconciliation, not a missing-module finding.

The refresh reconstructed native Git path sets with known-positive/negative,
same-tree and set-partition controls. Original donor union
`28e27543e→255f1cff3` plus `28e27543e→897dabeb8`: 811 paths, 95 absent, unchanged
from the original audit. No completed exclusion was reopened.

```text
64 excluded review documents
19 dropped gateway-worker paths
 2 retired hook scripts
 1 excluded mutation script
 1 explicitly retired production planner module
 8 test/helper paths
```

The only absent production module is `planning_application.py`, explicitly
required absent by [planner retirement tests](../../../tests/unit/web/composer/test_planner_route_retirement.py),
lines 108–109. Related replacement cancellation tests exist; equivalence of
every deleted assertion was not established.

The literal current-target-versus-`897dabeb8` difference is now:

```text
a6990ffb: 1586 = 721 current-only + 121 donor-only + 744 both-present/different
c9f568e6: 1625 = 752 current-only + 121 donor-only + 752 both-present/different
ae88147c: 1629 = 756 current-only + 121 donor-only + 752 both-present/different
newly absent original-donor paths=[]
```

The current complete semantic disposition and seeded omission-control receipt
were not established. Historical carve/landing-queue partitions and code-review
partitions are different snapshots or different sets. These counts are not
defect counts; this refresh did not semantically rereview all 752 common paths.

**Close by:** recomputing the exact current diff, partitioning required carry,
intentional replacement, exclusion and unrelated later changes, and verifying
the partition's sensitivity with the specified seeded control. Explain any
deleted assertion that has no preserved equivalent. Do not restore dropped
gateway architecture, retired planner code or excluded review noise.

## O05 — narrower test-matrix questions

**Units:** U39/U41/U44a. **Kind:** review boundaries, no demonstrated product defect.

- U39: real process recovery and retained/uncertain paid-result tests exist, but
  one complete accounting matrix over ordinal reservation, quota admission,
  may-dispatch, retention, settlement and projection was not located.
- U41: strict HTTP/SSE admission/replay/Stop and real PostgreSQL two-application
  streams exist; the complete Stop/yield/claim/Revert race matrix was not mapped.
- U44a: **the broad dialect question is narrowed.** Migrated PostgreSQL wrappers
  import current TITLE tests that call `complete_v4_title_result_sync` and cover
  actual response/ledger settlement, rollback, duplicate writers, cold reclaim
  and postcommit handoff. See `test_continuation_title_provider.py:155–259,
  303–359,628–667,734–807` and its PostgreSQL wrapper at 6–49. Additional live-adapter
  deadline/cancellation/read-fault/overflow cases still need mapping to required
  dialect assertions; equivalent coverage was not ruled out. Do not implement
  generic TITLE PostgreSQL coverage as though it were absent.

New rolled-back-start/expired-claim opposition and Read/Review PG handover cases
also strengthen U39/U41 constituents. The complete accounting/race matrices are
still not mapped; no missing live recovery, claim/start or Stop path is asserted.

**Close by:** map the specified scenarios to actual test bodies, identify which
require PostgreSQL/process execution, and fill only demonstrated missing
assertions. Do not use filenames/import wrappers alone as proof of behavior.

## O06 — historical documentation and dossier binding

**Units:** U01b/U01c/U17. **Kind:** bounded documentation/reconciliation work.

- Mark the historical seven-seam note's supersession clearly and repair or
  explain any historical handover reference still broken. The old missing-link
  note was not precisely rediscovered in this refresh; it is not a newly verified
  broken link. The actual 19-line seams note remains unchanged and unlabelled.
- Reconcile the specified HY1 named-tip inventory with its designated record;
  the cleanup procedure itself exists. This is no authority to clean branches.
- All eight U17 dossier test carries are present. The binding JSON still names
  historical `9c22be98` and declares an unadopted mechanism. Reconcile that text
  with the versioned reference's final-binding instruction; do not install a new
  enforcement process merely to close a documentation item.

**Close by:** reading original unit requirements and the current documents,
updating only retained useful instructions, and preserving historical facts as
historical. No signature package, seal or approval chain is required.

## O07 — exact final-source release qualification

**Unit:** Q; A1–A10. **Kind:** execution/landing evidence not established by source audit.

Required gate machinery exists in [CI](../../../.github/workflows/ci.yaml),
[shard verification](../../../scripts/ci_test_shards.py) and
[the full-suite driver](../../../scripts/full-suite-gate.sh). It binds ordinary
and proof collections to exact commit/run/interpreter, requires successful
disjoint union, includes serial PostgreSQL and frontend/browser/gateway jobs,
and refuses changed or unproved source in the local gate.

Local `main` and cached `origin/main` remain `3b28d537b`. The pinned release
source is not landed there; no branch fetch or merge was performed.

The parent refreshed GitHub read-only on 6 November. At c9f, the actual
[CI run 42193077999](https://github.com/dta-au/elspeth/actions/runs/42193077999)
was completed/cancelled; its CI Success and Combined coverage jobs were failure,
while PostgreSQL, Playwright and long proofs were cancelled. Other CodeQL,
redaction and judge workflows succeeded. This is not an all-required-checks pass,
nor is cancellation itself a diagnosed product defect.

The first ae881 lookup preceded its publication and returned no runs / HTTP422.
A final read after publication found
[CI run42217173692](https://github.com/dta-au/elspeth/actions/runs/42217173692)
**queued**, with no conclusion. CodeQL, telemetry and some redaction/judge runs
had succeeded; other runs remained queued. These are the final dated observations
for this refresh, not live status. A complete final-source frozen gate,
merged-SHA CI and reviewed keyless set-diff
were not established. G02's two stale guards and O03's actual matching-code
failed workflow are independently concrete. The raw R220 receipt is bounded
workflow evidence, not full release qualification.

**Close by:** after implementation, obtain actual receipts required by current
release rules on the exact final source. Verify landing through current refs
when authorized; source presence and passing focused tests are distinct from
integration, qualification and deployment. See [VALIDATION](VALIDATION.md).

## O08 — issue-to-implementation acceptance mapping

**Acceptance:** master-plan A6. **Kind:** external record mapping not refreshed.

**Current status: issue read refreshed; acceptance mapping remains open.** The
parent read [dta-au/elspeth#327](https://github.com/dta-au/elspeth/issues/327) on
6 November after confirming the repository remote. Its live response was
`state=OPEN`, all 11 checklist entries unchecked, `comments=[]`. Its own body
says the queue is not a landing claim. Thus unchecked boxes do not reopen the
completed code findings; a per-item implementation/versioned-contract/regression/
integration mapping is still required before checking them off. No record was
modified. Historical internal IDs are not GitHub issue numbers.

**Close by:** confirm the live repository/issue, read the current requirements,
and map them to actual commits/tests. Do not infer an original red run from test
presence or publish/close records without the applicable authority. This is
explicitly a follow-up record check, not an eighth confirmed implementation gap.
