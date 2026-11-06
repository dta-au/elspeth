# Composer Continuation Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep one authorized Composer request working across execution segments and worker loss without losing exact context, repeating committed effects, resetting accounting, or asking the user to continue.

**Architecture:** Retain the existing public operation as the logical request and schedule durable, fenced segments through the existing SQL worker. Atomically retain a complete typed snapshot at every durable transition; replay retained results and reconcile uncertain effects before admitting more work. The provider continues to author graphs and the Advisor continues to judge semantic progress; host code owns structural admission, custody, persistence and observation.

**Tech Stack:** Python 3.12+, frozen dataclasses/Pydantic 2, SQLAlchemy Core, SQLite/PostgreSQL, asyncio required-work owners, LiteLLM provider gateway, FastAPI HTTP/SSE, React/TypeScript/Vitest, pytest.

---

**Executable plan complete, 21 October.** The same Astra reviewer has given the
assembled task decomposition **GO** in the
[executable-plan review](../../design/composer-continuation-executable-review.md#final-assembled-executable-plan-verdict--2026-10-21).
The user has authorized delivery of the specified work and routine coordination
without further approval prompts. Implementation proceeds through ordinary
focused checks and integration gates. The earlier design GO is separate from
this completed decomposition review. The bespoke test controller and its checkpoint envelopes are
shelved. Implementation uses the repository's ordinary pytest, frontend,
branch-safety, hooks and full-suite commands; no replacement controller,
admission marker or persistent execution guard is part of this plan.

Implementation update, 22 October: work continues on branch
`codex/composer-continuation-integration-20261021`, committed HEAD
`28e27543e8a0805d1c9e9a225a8fe93e48f7ef23`, with reviewed working changes staged
by the integration owner. The pure frame/frontier and C2 storage prerequisites,
atomic content publication, and common provider-cost caller changes have
independent specification and Astra source GO. The C3.1 record slice has also
passed renewed specification and Astra review after repairing both projection
findings. The C3.2 attempt-history guard has current specification and same-Astra
GO after repairing the private owed-P4 scope's transaction binding, and is
staged. The optional A2 mutation backend and finite eight-file provider-cost
scanner update have specification and independent Astra GO and are staged;
automatic callers and P4 wiring remain separate work. The subsequent five-file
residual scanner reconciliation changes 15 values; its 219-case focused run
exited 0, but independent specification and Astra review remain pending. It
does not establish the complete scanner pair or package qualification.
Admission/segment recovery, worker activation, frame execution, client
negotiation and combined release qualification are still incomplete. No source
GO establishes CI, deployment, operator signature clearance or live-model
acceptance.

The following is the earlier combined qualification snapshot, retained to make
its failed gate and reuse limits explicit. That source was on branch
`codex/composer-continuation-integration-20261021`, committed HEAD
`51bb8131723baa822c1ce65ce1a981722d6c90f6` and staged source tree
`f0844ebe32b061a1fcbea250be6cfe66261bc1a0`. The committed tree includes
recovery/secrets guidance, the neutral waiting UI, pure retained timing policy,
bounded content, dependency repair, continuation-clock preparation and selected
Advisor NET7 repair. Engine carry, graph checksum, protected-runtime security
source, current-session secret-name/caller repairs and later `.9` test/docs
repairs are staged. The bounded caller repair and the assembled executable
Continuation plan received their separate Astra GO verdicts. Combined Ruff, mypy and
contracts completed with native exit 0 at these source bytes. Keyless lints
completed with findings. The frozen default Python stage exited 1 with
71,847 passed, 482 failed, 117 skipped, 2 xfailed and 30 errors. The serial
PostgreSQL stage completed with native exit 0: 1,062 passed and 1 skipped.
The run's final receipt records `frozen=yes` and `RESULT=FAIL`, driver exit 1.
The actual JUnit inventory is retained in the integration lane and default-stage
failures require repair before combined qualification.
Neither an implementation nor a passing test is inferred from code in a plan.

The separate local guidance checkpoint is now
`c299f2e7d852f897d0e34a1178aed4aa732cbd41`, single parent `67ad14c2`, tree
`afd97b96a87710722430f5f85b509b8823b603ff`. Its six phases and ordinary hooks
exited 0; independent closure and explicit capacity release are complete.
It changes only `recovery_controls.py` and `tools/secrets.py` guidance. It does
not implement Continuation or durable configured
PromptShield inspection. Canonical TC-003/TC-007 remain open for integration
and acceptance. This checkpoint does not make the older source assumptions
below current; P0 must reconcile them before implementation.

**Compatibility target (John, 23 October; clarified 19:58:55 UTC):** Preserve
the intended tools, capabilities and contracts from the existing Astra-reviewed
nominal tool audit. Unnecessary `.9` backward compatibility is waived for `.10`.
Legacy entry points and accidental behavior are not compatibility requirements.
There is no `.9` binary/client interoperability or predecessor-store upgrade
acceptance requirement. C7's earlier prior-release-client Stop/v1-ACK bridge is
not a `.9` support deliverable; qualify authenticated Stop and cancellation for
the client and lifecycle contracts supported by `.10`. This removes planned
prior-release interoperability work, not an installed runtime adapter.

The risk of integrating landed `.9` fixes, dependency floors and the existing
provider/audit accounting ABI is `.10` correctness and integration risk, not a
promise of `.9` interoperation. Preserve data bytes and lineage, current restart,
authorization, security and accounting, intended audited tool contracts, and
every existing release, review and test gate.

The current private `.10` source remains under qualification. Its Sessions epoch
is **79**: only an empty unstamped target is initialized, and an incompatible
existing store is preserved and refused. No migration, reset, downgrade or
deletion is supplied or authorized. The strict root tag is
`composer.continuation.snapshot.v1`, SQL `frame_stack_version` is `1`, and chunk
codecs are `snapshot.v2` and `event.v2`; these are separate format layers, not
release interoperability claims. Current P4 recovery requires
`composer.persisted-turn.v2` with complete original prepared input and audit
joins. Passive metadata with no version does not grant old-format recovery.
Internal admission represents lifecycle `1` and `2`, with defaults `1` and
continuation disabled; the actual HTTP admission and worker still select `1`.
C7 version negotiation and the default-false deployment flag below remain
implementation targets, not public V2 activation. This clarification changes
no runtime, dependency lock, settings, deployment or data.

The audit artifact is docs/reference/composer-advisor-tools/register.json in
the existing tool-contract-audit-20261020 checkout, SHA-256
4fe9bb89fdc54e30ecad181dca23167ccc75c67dd4e2881d1d8b79790064ede2,
source commit 67ad14c22b3987a16f0e9a71155102ee451da342. Its LLM-FIT-01
surface review identifies Astra/high; the reviewed per-tool references provide
source-scoped contracts. The register explicitly remains partially reconciled:
its 52 historical identities are not an approved replacement palette, and the
first-principles design dispositions remain provisional. Reconcile each touched
capability with its reviewed contract, actual role/route and approved .10
changes. A legacy planner-only emit_pipeline_proposal fixture must not restore
that tool to the common web loop. Preserve meaningful custody, validation,
publication, accounting and audit assertions against the relevant contract.
Where these sources do not uniquely determine a target role or capability,
record the precise ambiguity before changing that surface. This instruction
neither starts another audit nor expands .11 scope.

**Implementation quality within touched .10 scope:** Replace obviously
inefficient or brittle code when the intended contract is clear. Preserve
meaningful capability, custody, validation, publication, accounting and audit
contracts; record any material behavior change against that contract. Do not
copy accidental behavior solely for equivalence. This permission creates no
new cleanup or audit programme and does not resume .11.

**Test and implementation capacity:** Measure available CPUs, RAM and active
owned/shared runs at the next natural boundary. Overlap independent .10 source
work, reviews and established tests where ownership, shared state and frozen
source windows permit. Use available pytest workers; no cap or wait may be
justified solely by conserving idle CPU. Keep limits required by RAM, shared
fixtures/databases or a frozen source/control window, and record the measured
reason. Do not duplicate runs, interrupt unrelated processes, or change a
healthy active run solely to tune it. Non-.10 work remains halted, including
held .11 review. Normal correctness, security, review and merge gates remain.

## Execution boundary and source

This plan supplies the remaining Continuation implementation. **The existing .9 owner has reported its
completed local GREEN at `2e869240a388d1ede0454f1fbb9b019d22a9a9da`.** P0
retains the actual native logs, tested-input reuse limits and resulting source.
This is local Python 3.13 without coverage; it is not full CI-matrix, operator
signing, provider, staging or deployment clearance. Ordinary
independent source work and checks may proceed alongside healthy .9 tests;
.9 has priority at real CPU, memory, database or port contention. There is no
blanket serial/four-worker limit or reservation of idle CPU. Do not run the future
commands below while authoring the plan. The .9 prerequisite now permits the
already authorized isolated integration. No new permission chain is required for an already authorized
ordinary step; deployment, paid-provider trials and security activation retain
their separately defined authority boundaries.

The earlier design and acceptance specification is
[`docs/design/composer-continuation-plan.md`](../../design/composer-continuation-plan.md).
Its Astra GO covers design hash
`354918236911912f7655437577b3192e40f86e0c11bf3816e9bbc7f12d19ac77`,
not this new task decomposition or later source changes. Preserve its lifecycle,
effect, evidence, compatibility and acceptance contracts. This document and its
companion task plans supply the implementation sequence and code packets.

The qualified `.9` baseline used to create integration is
`2e869240a388d1ede0454f1fbb9b019d22a9a9da`, tree
`a44b6334aa674c5868ef79b4c2035b5563e47efe`, single parent b003 below.
Its actual commit changes only
`tests/unit/web/sessions/test_prompt_guard_capacity_errors.py`: configured and
required modes retain refusal, while explicit unconfigured debug mode proves
complete provider dispatch, truthful bypass audit, accounting and exact replay.
The complete diff was read. Production source is unchanged from b003. The
owner reported completed ordinary/static/PostgreSQL/long-proof qualification
after runner/test corrections. PostgreSQL and proof runs used b003 with exact
unchanged affected inputs; the separate collection manifests remain separate.
The original failed qualification remains recorded. No tests ran in this plan
checkout and no prior failed exit is relabeled GREEN.

The combined integrator has created the actual isolated checkout
`.claude/worktrees/composer-continuation-integration-20261021` under the primary
project, branch `codex/composer-continuation-integration-20261021`. At creation, Git readback
returned HEAD `2e869240a388d1ede0454f1fbb9b019d22a9a9da`, tree
`a44b6334aa674c5868ef79b4c2035b5563e47efe`, empty status and an empty index.
The current source above supersedes that startup receipt. The PromptShield core writer used its own separate
`promptshield-dot10-20261021` checkout/index. Committed batch/history/persistence
handoffs enter the combined target before Continuation reconciles those shared
seams; neither writer edits the other's index.

The production source reference is the .9 repair checkpoint
`b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree
`6a34e912debc469e9ce7e2048d433c08d5e606a9`, single parent ce410 below.
The qualified `2e869240` checkpoint retains this production code. Its immutable
Git diff from ce410 changes three production files:
`advisor_checkpoint.py` retains `type(exc).__name__` for request failures;
`prompt_guard.py` applies physical Azure character limits except in explicit
unconfigured debug mode and binds that decision into its fingerprint;
`tool_batch.py` preserves the separate safe request-rejection payload. Keep
those changes exactly. The associated changed tests and source-guard declaration
are part of that checkpoint, not an invitation to import its excluded donors.

The previous immutable source reference was
`ce4103539fe97f07fc60fb0cd096256c207c8bc5`, tree
`deead153bbabbe1754d3c313235ed349c18274b1`. The separate guidance checkpoint is
`c299f2e7d852f897d0e34a1178aed4aa732cbd41`, tree
`afd97b96a87710722430f5f85b509b8823b603ff`; both have the single parent
`67ad14c22b3987a16f0e9a71155102ee451da342`. Read-only Git measurement returned
`rev-list --left-right --count c299f2e7...ce410353 = 1 1` and that same merge-base.
These older extraction anchors precede the current integration. The isolated
planning checkout remains at `d647dd929d2b05be4918f175bc5ed96b18fdd198`;
no product files were imported. P0 selects the actual tested .9 checkpoint and
reads the changed symbols before applying these packets.

At the immutable source, H1's hidden retry and English intent classifiers are already
removed. Advisor required graph values, full current request, authenticated
chronological transcript, effective tool arguments and actual read delivery are
also already represented. Preserve them. The remaining H2 change is the
empty-graph selector/common entry and retirement of its private facade. H3a
adds durable explicit coverage and recovery of the current transcript owner;
it must not restore prefix limits or the earlier request projection. Preserve
ce410's `AdvisorCheckpointPassRecord.from_dict` admission and safe request-
rejection evidence. The two-file neutral waiting change has a 131-test and
two-file ESLint scoped pass and ordinary checkpoint
`52543bcc195419561b32412c79f4004e3fa8af62`, parent `c299f2e7`, tree
`e6cb88e8d1c11de4ea2ae653b5971201f6472c33`. Normal branch-safety and hooks exited
0; actual changed paths are exactly `ComposingIndicator.tsx` and its test.
That exact checkpoint is already carried;
do not reimplement it or call its isolated pass combined .10 qualification.

The separately owned .10 PromptShield route in
[`composer-plus-dot10-carryforward.md`](../../design/composer-plus-dot10-carryforward.md)
is an integration prerequisite for the evidence/guard owner seams. Its existing
writer supplies the typed actual-use/context/coverage contracts, both-role
projections, complete-value materialisation, fenced publication and provider-
independent recovery. The current target includes its bounded-content and
caller work. The producer companion's T6 completes the current-use cold bridge
through those real builders and SQL owners. Every content use, retry and resume requires
fresh validation of its actual composition, even with identical IDs and context.
Retained receipts and hashes provide identity/history, never reusable clearance.

Existing owners retain their dirty source, primary checkout, service, release
PR/CI and main custody. P0 reads the then-current handoff; historical owner IDs
and old capacity measurements do not transfer that custody.

## Read first

- `AGENTS.md` and `CONTRIBUTING.md`, especially whole-tree gates.
- `docs/architecture/adr/031-tutorial-is-a-fixed-script-canary.md` and
  `docs/architecture/adr/032-validate-by-trust-domain.md`.
- `docs/design/composer-continuation-plan.md`, lifecycle/evidence/action/HTTP
  tables and final acceptance table.
- `src/elspeth/web/composer/advisor_transcript.py`: current observed-message
  identity, pending adoption, received/effective arguments and read-delivery
  facts are state, not replaceable presentation.

## File boundaries

| Files | Responsibility |
| --- | --- |
| `composer/continuation.py`, `continuation_codec.py` | Closed private frame/chunk contracts and exact codec. No SQL, SDK calls or Tasks. |
| `composer/continuation_policy.py` | Complete immutable admitted action/segment policy and strict retained-policy decoder; created before the frame codec. |
| `composer/continuation_local_clock.py` | Process-local cumulative batch P/H/V timing, including nested finish-once SQL projection; never retained as authority or a monotonic anchor. |
| `composer/continuation_progress.py` | Structural obligation/action state and receipt-based recurrence. No host interpretation of user prose. |
| `composer/continuation_evidence.py` | Complete private snapshots of owned evidence, transcript and buffer state; stable logical binding. |
| `composer/continuation_message_shelf.py`, `continuation_prompt_producers.py`, `continuation_caches.py` | Preserve exact message aliases, reconstruct current owned producer metadata, and retain cache history without reusing clearance. |
| `composer/continuation_tool_batch.py` | Closed batch/call state, retained internal results, once-only entry, actual-owner result application and P4 publication. |
| `composer/continuation_actions.py`, `continuation_provider_input.py` | Exact model-prescribed action input, complete Advisor context and prepared physical input/configuration binding. |
| `composer/advisor_coverage.py` | Required/included/excluded evidence manifest shared by prepared input and verdict admission. |
| `composer/attachment_admission.py` | Complete-original credential inspection receipt and policy-bound request cache. |
| `sessions/continuation.py`, `sessions/models.py` | Private immutable chunks/events/snapshots, segments, attempt history, effects and ordinals. |
| `coordination/composer_operation_authority.py` | Session transaction, current-generation CAS, historical fence lookup, claim/yield/recovery/Stop. |
| `sessions/composer_turn.py`, `composer_async_worker.py` | Reserved ingress, cold resume below ingress, required-work outcome verification and worker scheduling. |
| `sessions/composer_continuation_stop.py` | Closed locator for a prepared Stop; the terminal SQL owner independently checks its current claim, selected head, durable marker and absence of owed work. |
| `composer/provider_quota.py`, `provider_gateway.py` | Actual physical attempt authority, pre-dispatch intent, retained response, clocks and settlement. |
| `composer/service.py`, `tool_batch.py`, `composition_completion.py` | Explicit compose/tool/advisor/END transitions using existing production owners. |
| `sessions/composer_continuation_dtos.py`, routes | Closed public v2 snapshot/version negotiation; private frames never enter DTOs. |
| Frontend API/decoder/observer/custody/store | Version-bound observations, monotone revisions, Stop and retained pending request. |

All `composer/` paths above are under `src/elspeth/web/composer/`; all
`sessions/` and `coordination/` paths are under `src/elspeth/web/`. Each task
below and in companion plans names its complete paths. This map grants no
permission to restructure unrelated large modules.

## Read and execute the packets in this order

1. P0 below: source, custody, environment and capacity admission.
2. [`2026-10-20-composer-heuristic-cleanup.md`](2026-10-20-composer-heuristic-cleanup.md): H1, H2.
3. H3a below, then H3b/H3c in the heuristic-cleanup plan.
4. C1a.0 in the [storage companion](2026-10-20-composer-continuation-storage.md), then C6.1/C6.2's pure decision/frontier contracts below and C1a.1/C1a.2. Create C6.4's pure `continuation_actions.py` module before its producer consumers; defer actual C6 behavior to step 9. Execute F1–F6 in the [frames companion](2026-10-21-composer-continuation-frames.md) for pure response/transcript/buffer prerequisites; defer F7's actual-authority cold restore until step 8. This does not enable continuation behavior.
5. Storage C5.1's pure clock arithmetic, including `size_segment`, precedes its C3 segment-start consumer. The client C7.0 disabled-by-default flag field precedes storage C5.2's settings/authority/precondition seed wiring. C1a.5 supplies the ingress snapshot builder. These are prerequisites, not activation.
6. Storage C2 schema/chunk repository, then C3.0's strict owned-state decoder and C3 records, immutable history, admission and initial segment packets. Install C3.8b/d/e's pure P4/proposal/interpretation/blob values and codecs, plus the five-field owner-commit verifier with strict current SQL claim binding and refusal of every different creator. Keep its future selected-read helper deferred. After the actual B1 nominal codec exists, install C3.8c plus all four complete C3.8e passive value-admission/current-row functions under their explicit prerequisite boundary; this does not activate a reader or writer. Its default empty credential-reference inventory conservatively refuses states containing credential references until the actual metadata owner is integrated; keep the existing credential guard. Do not install the result-retention/pending-reader or service/protocol writer hooks yet: their original unconditional read imports require actual B1/C3.8c owners in step 9. V2 validates a non-null bound base before quota/parent/user insertion and preserves legacy recovery. Runtime imports and whole-tree static checks must pass at each checkpoint; lazy imports do not excuse unavailable body dependencies. Run pure checks now and defer the remaining behavioral DB tests to their actual effect/action prerequisites below.
7. Start with storage C5.2a's complete prepared-only first-fence storage variant: exact nominal Running/claim/context, strict True and unconditional actual SQL claim/history six-tuple, fresh-work owed-P4 refusal, selected complete-head/input-root CAS, reserved source-free frame and pre-write family/digest checks, provider-only generic preparation, and same-creator prepared/no-result/no-physical observation. Use its actual module-owned fixture binding and complete SQLite/PostgreSQL controls; qualify native outcomes separately from import/static success. The reserved initializer/family0/arbitrary-digest positive proves storage association only. Keep the complete later generic SQL/read and response/attempt/ledger variants in C5.2/C5.3 explicitly deferred, with no active fallthrough. The later raw mint's nominality/context check is insufficient physical eligibility: HOLD custody/mint/SDK consumers until actual selected frame/source/action/input/clock owners and fresh admission are identified and qualified. Then continue C5.3–C5.9's actual provider/effect SQL, custody, stage clocks, audit settlement, undispatched cancellation, physical recovery and exact prepared-input comparison, together with [physical outcomes](2026-10-21-composer-continuation-physical-outcomes.md) O1–O4's complete actual audit codec. These later owners close the physical effect/action-receipt dependencies of C3's owner hooks and T7's shared source validator; storage preparation alone does not. Defer C5.7's `test_physical_recovery_preserves_actual_attempt_and_known_or_unknown_usage` and its PostgreSQL alias/omission controls until step 9's C3.4b; recovery production alone does not qualify a resumed start. Likewise C3.5/C3.6 implementation and their rollover GREEN occur with C3.4b in step 9, while step 6 supplies initial C3.4, C3.9, pure owner-result values/verifier and projection production.
8. T1–T3 in the [trusted-producer/cache companion](2026-10-21-composer-continuation-producers.md), A1–A3's [attachment history](2026-10-21-composer-continuation-attachment-history.md) contracts, then producer T6–T8's actual native/attachment source and per-use owners. Complete F7 production, reusable evidence fixtures and its pure/single-fence tests before T4/T5 tests; F8's pure codec precedes those SQL fixtures and its current-authority facet consumes that early F7 work. The C1b module prefix (`admit_continuation` and retained-policy `start_continuation`) is available after initial C3.4; it imports no later F28 runtime. Defer F7.13's named two-fence test, C1b's cold test body, C3.10's two-creator projection test, C3.11's historical-proposal rollover test, C4's retry/recovery tests, and C5.7's physical-recovery test until step 9. Run the standalone initial claim/start SQLite/PostgreSQL modules and other actual first-fence C3 tests now; do not claim full F7/C1b/C3/C4b module qualification at this checkpoint.
9. Complete frames cold bootstrap/phase producers through existing F28.3, the [batch packet](2026-10-21-composer-continuation-batches.md) including B8 clocks before B7 tests, B9 Stop reconciliation, [attachment history](2026-10-21-composer-continuation-attachment-history.md), [physical outcomes](2026-10-21-composer-continuation-physical-outcomes.md), [END](2026-10-21-composer-continuation-end.md), and C6.3–C6.5 scheduling, actual-owner evidence and semantic execution integration. Keep the entire actual read/writer/service/protocol cluster deferred together through this boundary; C5.2a's storage-only binding and observation supply no alternate read admission, physical result or writer. Reintroduce the complete later C5 generic SQL/read and physical observation variants only with their actual owners, retaining the initial strict current-claim/fresh-work checks and resolving the raw wrong-codec-to-None defect as an explicit owned-codec refusal. The complete passive C3.8c/C3.8e prerequisite may already exist after actual B1's nominal ToolResult codec; its tests prove value admission/current-row equality, never authorization or cold activation. Require B4's complete owner binding before integrating these functions into any actual retained-read or writer path. Add the full selected-read helper only with its real selected-state/private-chunk/frontier/tool-completion/action-source owners; then restore its narrowly authorized foreign-creator read branch. Preserve refusal of every foreign-creator `session_sql` effect, including still-prepared proposal, interpretation and P4 effects; B4's preparing/committer provenance must not widen that policy. The selected immutable read exception requires fresh current authorization and version/response admission. Already committed results use their pending-result application path without reexecuting a writer. Cross-creator execution of uncommitted business SQL remains HOLD and must be resolved explicitly before claiming that recovery cut supported. Install the complete result-retention/pending-reader and service/protocol writer hooks only after those imports and static checks pass. Qualify their real writer/read atomicity and current-version controls before use. With actual selected-state/private-chunk and frame/end/tool-batch owners available, implement C3.4b together with corrected C3.5/C3.6 producers, reusing F28.3's exact selector. Run its actual SQLite/PostgreSQL producer/start module, then the deferred F7.13, C1b, C3.10/C3.11, C4 retry/recovery and C5.7 physical-recovery tests and their existing controls; only then require the corresponding complete module GREEN. F7's `ComposeFrame(action_ready)` rollover passes the selected `primary` allowance, not admission SQL. Only after these storage/resume checks wire C4a's worker/runner, W4's cooperative Stop watch and W8's persisted retry backoff; run C4b physical/process-cut tests and the [real-driver equivalence packet](2026-10-21-composer-continuation-driver-tests.md), including D9 and F34/F35. B5 consumes F28's actual current boundary reader before business authority; O8/O9 complete failed Advisor application and proved owed P4 after Stop. No worker/settings/flag admission is enabled by the earlier first-start increment.
10. Remaining C7 routes, C8 client and S7 preservation in the [client companion](2026-10-20-composer-continuation-client.md). H0 already applied S7.2's selected net repair once; S7 now checks it survived.
11. Q below: combined qualification, deployment acceptance and protected delivery.

The packets are sequential integration checkpoints, not parallel writers of
shared files. V2 admission stays disabled through C8/S7 and the required gates.
Code steps are small changes to one owner; test commands can take longer than
the two-to-five-minute editing steps. Do not replace an honest long test with
a shorter test simply to meet a step estimate.

## Requirement coverage

The design's acceptance requirements map to these concrete implementation and
test packets. Their proposed tests remain execution steps, not reported passes.

| Required behavior | Implementation and behavioral checks |
| --- | --- |
| One logical request, original ingress, exact cold state and fenced restart | Storage C1a/C2/C3/C4, frames F1–F39 and driver D1–D6; immutable history and current CAS are checked on SQLite and PostgreSQL. |
| Exact values, native messages, attachment history and fresh current-use inspection | H3a/H3b/H3c, producer T1–T10, attachment A1–A6 and frames F7/F12–F21/F31; T9 and D preserve complete wire and inspect actual retained source substitutions. |
| Once-only business effects, complete failed/successful physical outcomes and truthful accounting | Storage C5, batches B1–B8, physical O1–O12 and driver D6/D9; actual result/ledger receipts and held SQL futures control application and cancellation. |
| Stop/Revert ordering without dropping an owed prefix or result | Storage C3/W4, batches B9, END E3/E8 and physical O9; actual worker tests require terminal cancellation and refuse a premature Stop locator. Client C7 preserves authenticated Stop/Revert and exact replay. |
| Independent time, fairness, durable retry eligibility and model-owned progress | Storage C5/W8, batch B8, END E7, main C6 and END's bound semantic decisions; real owner timing and controlled SQL backoff tests accompany structural action-recurrence controls. H1/H2 preserve provider authorship and the common tutorial path. |
| Version negotiation, reconnect, private-data boundaries and quiet continuous-thinking UI | Client C7/C8/S7 and storage DTO/schema-refusal tests; strict v1/v2 transport, stale revisions/timers and existing-request draining remain separately tested. |
| Reviewable delivery and bounded compatibility/activation | P0 and Q1–Q4: affected checks, frozen default/serial PostgreSQL gates, frontend/CI, schema preserve/refuse, disabled admission and separately selected live acceptance. |

## Common command setup — run only after P0 admission

Start from the agreed isolated checkout. Use its already provisioned interpreter
and both source roots; never let `uv run` rebuild the environment or change the
shared venv. The ordinary supported existing venv link may be retained. Missing
prerequisites stop the affected check. The following are shell variables for
literal repository commands, not a new runner or installed helper.

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
export COMPOSER_WORKTREE COMPOSER_PYTHON
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"
mkdir -p "$COMPOSER_WORKTREE/.claude/lanes"
COMPOSER_LOG_DIR="$(mktemp -d "$COMPOSER_WORKTREE/.claude/lanes/continuation-execution.XXXXXX")"
export COMPOSER_LOG_DIR
test -x "$COMPOSER_PYTHON"
COMPOSER_COMMON_GIT_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"
COMPOSER_TEMP_PARENT="$(dirname "$COMPOSER_COMMON_GIT_DIR")/.claude/lanes"
mkdir -p "$COMPOSER_TEMP_PARENT"
COMPOSER_TMP_DIR="$(mktemp -d "$COMPOSER_TEMP_PARENT/t.XXXXXX")"
export TMPDIR="$COMPOSER_TMP_DIR"
```

The short task-private temporary directory remains inside the project root.
The .9 owner's ordinary Terraform checks exposed Unix-socket failure under a
long pytest temporary path; the same provider/configuration worked with a
short project-local path. Keep logs under the worktree lane and use this short
directory for native temporary files. The combined run showed that adding
`lanes/ct-dot10.` still produces a 111-byte Unix socket path; the same fixture
under `lanes/t.` produces 104 bytes. Keep lane logs separate from
this native temporary directory. Do not export timeout options through
`PYTEST_ADDOPTS`: nested pytest must not inherit the outer runner's timeout.

Every RED command must exit 1 with the named assertion/owned refusal and valid
collection. Exit 2, missing modules/fixtures and unavailable Docker do not prove
the behavior is absent. On a new module, first add its complete importable
types/codec packet, then use the stated mutation/counterexample to establish
RED; do not count an import error as the required negative control.

Before each code commit, after reading its GREEN native exit, stage only that
task's explicit named paths and inspect the complete staged diff. Then run this
sequence against that staged set:

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git diff --check
cd "$COMPOSER_WORKTREE" && git diff --cached --check
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m ruff check src/elspeth/web tests/unit/web
cd "$COMPOSER_WORKTREE" && git diff --cached --name-only
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
```

Use the task's commit message. Include generated contract-census changes only when the actual census
producer changed them for owned edits; never hand-edit counts/signatures.
Normal hooks remain enabled. No lane logs, private inputs or plan hash sidecars
are staged. A code or test fix belongs before the commit, not in a later ticket.

### Task P0.1: Bind the actual input without starting implementation

**Files:** Read `AGENTS.md`, `CONTRIBUTING.md`; update this plan's source paragraph
only if the source changes. Coordination notes stay under `.claude/lanes/`.

- [ ] Read the actual execution instruction and current owner handoff. Require
  exact target repository/branch/version, immutable candidate SHA and owned
  paths; retain the existing release/primary/service owner.
- [ ] Record the source commands and their raw outputs:

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
cd "$COMPOSER_WORKTREE" && hostname
cd "$COMPOSER_WORKTREE" && pwd -P
cd "$COMPOSER_WORKTREE" && git symbolic-ref --short HEAD
cd "$COMPOSER_WORKTREE" && git rev-parse HEAD 'HEAD^{tree}'
cd "$COMPOSER_WORKTREE" && git --no-optional-locks status --short
cd "$COMPOSER_WORKTREE" && git worktree list --porcelain
cd "$COMPOSER_WORKTREE" && git log -1 --format='%H %T %s' codex/composer-fixes
```

- [ ] Read the .9 owner's actual normal pytest/product-test logs and native exits,
  its immutable resulting commit, changed paths and remaining dirty ownership.
  Require actual GREEN before any .10 integration/rebase. Compare that commit
  with `2e869240a388d1ede0454f1fbb9b019d22a9a9da`; read every changed seam in the
  file map. Preserve safe request-rejection evidence and the prepared-input
  correction rather than replaying older files. A source GO or stale test exit
  does not satisfy this prerequisite.
- [ ] In one isolated integration branch based on that tested checkpoint, carry
  the exact `c299f2e7` guidance commit if ancestry shows it absent, followed by
  `52543bcc195419561b32412c79f4004e3fa8af62`. Verify changed paths and
  compare semantics on conflicts; do not copy dirty whole files, rewrite the
  shared checkout or disturb an owner's index. Record the resulting HEAD/tree.
  Read the actual release PR identity/head through the supported GitHub tool if
  one exists; a PR is not a substitute for the .9 GREEN prerequisite.
- [ ] Bind the existing .10 PromptShield writer's selected typed-contract
  checkpoint before H3a/C1 frame wiring. Use its actual public private-code
  interfaces and preserve fresh per-use validation. If a changed signature
  invalidates a packet below, update that packet before applying it; do not
  introduce an adapter that treats a historical receipt as current clearance.
- [ ] Preserve the selected .7 shared-auth/source no-op changes already
  represented at the immutable source. Use S7's exact path list and tests;
  historical staged SMK10 is not an outstanding input or authority to copy
  another owner's index. Integrate S7.2's exact selected net Advisor repair
  from donor parent `2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31` to
  `e6b247943f911637f144fd11ba1c8130397fa2a9` on the actual .9 GREEN base.
  Preserve the excluded URL retention; neither the final tip alone nor the
  entire b09 parent branch represents the selected seven-path repair.
- [ ] Preserve the actual .9 provider/audit ABI when reconciling independently
  owned engine work: `call_id=attempt_id`, governance outcome timestamps,
  `LLMAuditParent.call_id`/`created_at`, returned-model persistence and their
  existing propagation paths. The older engine tree lacks those contracts.
  Its compatibility reference patch is untested input for that owner, not an
  instruction to transplant its old stack/schema into this branch or add a
  dynamic-attribute fallback. Keep that owner's index and working paths intact;
  any selected engine integration occurs on the actual qualified .9 base.
- [ ] Read the checksum and security owners' actual committed handoffs before
  the combined integration checkpoint. Preserve their selected unique changes
  and partial-checkpoint limits without replaying whole donor branches or
  touching their indices. A source checksum or security checkpoint does not
  authorize signing, key access, configuration activation or deployment. These
  independently owned changes are integration inputs, not new Continuation
  implementation work or an authority transfer.
- [ ] Confirm no duplicate writer and actual resource contention. Use normal
  supported worker configuration and available capacity; .9 has priority when
  CPU, memory, database or ports actually conflict. No
  controller profile, numeric grant, marker or new scheduling wrapper is used.
  A later primary/service handoff remains necessary for the final deployment
  checkpoint.

**Exit:** current source/dependencies/ownership are recorded and materially
changed design contracts have been reconciled. No runtime or code action is
authorized merely because this planning task is complete.

### Task P0.2: Prove the admitted environment points into this checkout

**Files:** Read `scripts/branch-safety-check.sh` and the existing venv only.

- [ ] After execution admission, run the common setup and this provenance
  assertion. Expected: two paths under this checkout and native exit 0.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -c 'from pathlib import Path; import os, elspeth, elspeth_lints; root=Path(os.environ["COMPOSER_WORKTREE"]).resolve(); paths=(Path(elspeth.__file__).resolve(), Path(elspeth_lints.__file__).resolve()); print(*paths, sep="\n"); assert all(p.is_relative_to(root) for p in paths)'
```

- [ ] Run the unchanged affected test packet before its first edit. Record any
  inherited failure's exact node ID/exit and compare its named control on the
  agreed base. Do not accept collection failure as a baseline test result.

**Exit:** the interpreter/imports are correct and the next task has bounded
baseline evidence. No environment rebuilding is part of this plan.

## H3a — Preserve complete review input and make its coverage durable

### Task H3a.1: Add an owned required-evidence manifest

**Files:** Create `src/elspeth/web/composer/advisor_coverage.py`; create
`tests/unit/web/composer/test_advisor_coverage.py`.

- [ ] Write these concrete model tests. Run after adding the importable model
  below; for RED remove `included` from `complete_required` in a local mutation,
  observe the missing-tail assertion fail, and restore the correct code.

```python
from dataclasses import replace
from elspeth.contracts.hashing import stable_hash
from elspeth.web.composer.advisor_coverage import (
    AdvisorEvidenceCoverage, AdvisorEvidenceUnit,
)


def test_missing_required_tail_is_not_complete():
    request = AdvisorEvidenceUnit("request", stable_hash("full request"))
    tail = AdvisorEvidenceUnit("graph.nodes[0].options.prompt", stable_hash("late tail"))
    coverage = AdvisorEvidenceCoverage("binding", "graph", (request, tail), (request,), ())
    assert not coverage.complete_required
    assert replace(coverage, included=(request, tail)).complete_required


def test_same_unit_id_with_different_bytes_is_not_coverage():
    required = AdvisorEvidenceUnit("request", stable_hash("middle amendment"))
    supplied = AdvisorEvidenceUnit("request", stable_hash("earlier request"))
    coverage = AdvisorEvidenceCoverage("binding", "graph", (required,), (supplied,), ())
    assert not coverage.complete_required
```

- [ ] Add this complete module. Exclusions are explicit redacted/name-only
  units, never missing required units relabeled optional.

```python
from __future__ import annotations

from dataclasses import dataclass

from elspeth.contracts.hashing import stable_hash


@dataclass(frozen=True, slots=True)
class AdvisorEvidenceUnit:
    path: str
    content_hash: str


@dataclass(frozen=True, slots=True)
class AdvisorEvidenceExclusion:
    path: str
    reason: str


@dataclass(frozen=True, slots=True)
class AdvisorEvidenceCoverage:
    request_binding: str
    graph_hash: str
    required: tuple[AdvisorEvidenceUnit, ...]
    included: tuple[AdvisorEvidenceUnit, ...]
    excluded: tuple[AdvisorEvidenceExclusion, ...]
    optional_omissions: tuple[str, ...] = ()
    schema_version: str = "composer.advisor-evidence.v1"

    @property
    def complete_required(self) -> bool:
        required = {(unit.path, unit.content_hash) for unit in self.required}
        included = {(unit.path, unit.content_hash) for unit in self.included}
        return bool(required) and len(required) == len(self.required) and required <= included

    @property
    def binding(self) -> str:
        return stable_hash({
            "schema_version": self.schema_version,
            "request": self.request_binding,
            "graph": self.graph_hash,
            "required": [(u.path, u.content_hash) for u in self.required],
            "included": [(u.path, u.content_hash) for u in self.included],
            "excluded": [(u.path, u.reason) for u in self.excluded],
            "optional_omissions": self.optional_omissions,
        })
```

- [ ] Run the ordinary test command and read its native exit/full log.
  Expected: exit 0; the mutated subset check must have exited 1.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_advisor_coverage.py >"$COMPOSER_LOG_DIR/h3a-coverage.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/h3a-coverage.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```
- [ ] Stage only these two files; commit
  `feat(composer): bind complete Advisor evidence coverage` through the common
  commit checks.

### Task H3a.2: Bind coverage to the producer's exact required units

**Files:** Modify `src/elspeth/web/composer/advisor_context.py` at
`AdvisorSummaryParts` and `fit_advisor_checkpoint_summary`; modify
`src/elspeth/web/composer/advisor_checkpoint.py` at the prepared-call owner;
test `tests/unit/web/composer/test_advisor_mandatory_evidence_budget.py` and
`tests/unit/web/composer/test_advisor_terminal_publication.py`.

- [ ] Preserve the candidate's full-value producer and its existing prepared-
  input late-tail, middle-history, request-once and configured-source tests.
  Extend `AdvisorSummaryParts` with
  `required_units: tuple[AdvisorEvidenceUnit, ...] = ()` and
  `included_required_units: tuple[AdvisorEvidenceUnit, ...] = ()`.
- [ ] At `fit_advisor_checkpoint_summary`, immediately after producing
  `required_units`, derive the manifest from the owned rendered-unit paths:

```python
required_manifest = tuple(
    AdvisorEvidenceUnit(
        f"graph.required_rendered_units[{index}]",
        stable_hash("".join(part.text for part in unit)),
    )
    for index, unit in enumerate(required_units)
)
```

- [ ] Use `dataclasses.replace` on every returned `AdvisorSummaryParts` value
  from this function, assigning both manifest fields to `required_manifest`.
  Keep `_join_advisor_summary_units`' configured-source spans unchanged. It is
  the selection owner that knows required units survived; do not infer that
  from a substring search over the rendered message.

```python
def covered(parts: AdvisorSummaryParts) -> AdvisorSummaryParts:
    return replace(
        parts,
        required_units=required_manifest,
        included_required_units=required_manifest,
    )
```

- [ ] Wrap its existing four return expressions with `covered(...)`. Keep all
  existing required units before optional selection; retain the candidate's
  actual provider/service-bound refusal. The configured `char_cap` continues
  to bound optional prose only.
- [ ] At the Advisor prepared-input owner, construct the full coverage from
  the existing exact request/transcript values plus `summary.required_units`.
  The producer paths are `request`, `transcript.events` and the rendered graph
  paths above. Use `AdvisorEvidenceUnit(path, stable_hash(exact_text))` for
  request/transcript, not hashes of an excerpt or model claims. Store its
  binding on the same private prepared-call receipt and on the parsed verdict.
- [ ] Before accepting whole-scope CLEAN/STOP/publication, compare the current
  request/graph/policy and the prepared-call coverage binding, then require
  `complete_required`. Reuse the existing
  `AdvisorGraphCoverageUnavailable`/`graph_coverage_withheld()` path for missing
  coverage. Add this exact check in the owned admission branch:

```python
if not coverage.complete_required:
    raise AdvisorGraphCoverageUnavailable("required Advisor evidence is incomplete")
```

- [ ] Extend the existing real prepared-input test by replacing one
  `included_required_units` item with an empty tuple before the call; assert
  the provider is not called and no whole-scope verdict is accepted. Retain the
  positive full-input late-tail control. Run:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_advisor_coverage.py tests/unit/web/composer/test_advisor_mandatory_evidence_budget.py tests/unit/web/composer/test_advisor_graph_visibility.py tests/unit/web/composer/test_advisor_request.py tests/unit/web/composer/test_advisor_terminal_publication.py >"$COMPOSER_LOG_DIR/h3a-input.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/h3a-input.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Commit the four owned production/test changes and actual affected
  contract declarations as `feat(composer): retain required review coverage`.

**Exit:** complete input already supplied by the candidate remains complete;
the private snapshot can prove what the actual Advisor call covered. Storage
name/secret exclusions remain explicit. The manifest never asserts raw secret
inspection or provider acceptance merely from a hash.

## C1a — Make resumable state explicit before adding a worker

### Task C1a.1: Define the logical request, frame and immutable chunk contracts

**Files:** Create `src/elspeth/web/composer/continuation.py`; test
`tests/unit/web/composer/test_continuation_frames.py`.

- [ ] Add the following importable contracts as one code packet. These are
  private storage types. Their existence does not enable v2 admission. The
  packet adopts the already implemented C1 module and requires the existing
  pure A1a `src/elspeth/contracts/composer_mutation.py` contract first; retain
  that qualified dependency when assembling C1 rather than recreating it.

```python
from __future__ import annotations

from dataclasses import dataclass
from typing import Annotated, Literal
from uuid import UUID, uuid5

from pydantic import BaseModel, ConfigDict, Field, model_validator

from elspeth.contracts.composer_mutation import ComposerMutationCeiling, ComposerMutationRevocation
from elspeth.contracts.errors import AuditIntegrityError


class ContinuationValue(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)


class LogicalRequestBinding(ContinuationValue):
    session_id: str
    operation_id: str
    original_user_message_id: str
    request_hash: str
    kind: Literal["compose_message", "compose_recompose"]
    base_state_id: str | None


class MutationCeilingSnapshot(ContinuationValue):
    session_id: str
    request_id: str
    initial_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationCeiling:
        return ComposerMutationCeiling(self.session_id, self.request_id, self.initial_trust_mode)

    @classmethod
    def capture(cls, value: ComposerMutationCeiling) -> MutationCeilingSnapshot:
        if type(value) is not ComposerMutationCeiling:
            raise TypeError("Continuation capture requires an owned mutation ceiling")
        return cls(session_id=value.session_id, request_id=value.request_id, initial_trust_mode=value.initial_trust_mode)

    def require_logical(self, logical: LogicalRequestBinding) -> None:
        self.materialize()
        if self.session_id != logical.session_id or self.request_id != logical.original_user_message_id:
            raise AuditIntegrityError("Continuation mutation ceiling changed its original request")


class MutationRevocationSnapshot(ContinuationValue):
    ceiling: MutationCeilingSnapshot
    current_trust_mode: Literal["auto_commit", "explicit_approve"]

    def materialize(self) -> ComposerMutationRevocation:
        return ComposerMutationRevocation(self.ceiling.materialize(), self.current_trust_mode)

    @classmethod
    def capture(cls, value: ComposerMutationRevocation) -> MutationRevocationSnapshot:
        if type(value) is not ComposerMutationRevocation:
            raise TypeError("Continuation refusal capture requires its actual nominal value")
        return cls(ceiling=MutationCeilingSnapshot.capture(value.ceiling), current_trust_mode=value.current_trust_mode)


class ContinuationProviderCostPolicy(ContinuationValue):
    applicability: Literal["not_applicable"]
    lifecycle_version: Literal[2]
    accounting_owner: Literal["physical_audit_and_token_ledger"]


class PrivateChunkRef(ContinuationValue):
    codec: Literal[
        "composer.messages.v1",
        "composer.tools.v1",
        "composer.state.v1",
        "composer.evidence.v1",
        "composer.audit-buffer.v1",
        "composer.transcript.v1",
        "composer.tool-batch.v1",
        "composer.advisor.v1",
        "composer.end.v1",
        "composer.caches.v1",
        "composer.attachment-admissions.v1",
        "composer.response.v1",
        "composer.semantic-frontier.v1",
        "composer.owner-result.v1",
        "composer.bootstrap.v1",
        "composer.iteration.v1",
    ]
    digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    byte_length: int = Field(ge=0)


class EffectFrontierEntry(ContinuationValue):
    effect_id: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    receipt_id: str | None


class RetainedOwnerResultRef(ContinuationValue):
    effect_key: str
    kind: Literal["proposal", "persisted_turn", "read", "interpretation"]
    result: PrivateChunkRef

    @model_validator(mode="after")
    def require_owner_result_codec(self) -> RetainedOwnerResultRef:
        if self.result.codec != "composer.owner-result.v1":
            raise ValueError("owner result requires its closed receipt codec")
        return self


class ContinuationProviderFrame(ContinuationValue):
    provider_effect_key: str | None = None
    provider_input_sha256: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def require_paired_provider_locator(self) -> ContinuationProviderFrame:
        if (self.provider_effect_key is None) != (self.provider_input_sha256 is None):
            raise ValueError("provider frame lost its selected prepared provider locator")
        return self


class InitializeFrame(ContinuationProviderFrame):
    frame_kind: Literal["initialize"] = "initialize"
    request_json: str
    ingress_phase: Literal["reserved", "inserted", "title_pending", "ready"]
    title_attempt_id: str | None
    ingress_receipt_json: str | None = None
    bootstrap: PrivateChunkRef | None = None
    title_response: PrivateChunkRef | None = None

    @model_validator(mode="after")
    def require_prepared_original_bootstrap(self) -> InitializeFrame:
        if self.ingress_phase in ("title_pending", "ready") and self.bootstrap is None:
            raise ValueError("prepared initialization lost its original pre-title bootstrap")
        return self


class ComposeFrame(ContinuationProviderFrame):
    frame_kind: Literal["compose"] = "compose"
    phase: Literal["action_ready", "awaiting_response", "response_retained", "classify"]
    bootstrap: PrivateChunkRef
    iteration: PrivateChunkRef
    messages: PrivateChunkRef
    tools: PrivateChunkRef | None
    initial_state: PrivateChunkRef
    state: PrivateChunkRef
    evidence: PrivateChunkRef
    audit_buffer: PrivateChunkRef
    transcript: PrivateChunkRef
    caches: PrivateChunkRef
    attachment_admissions: PrivateChunkRef
    initial_version: int = Field(ge=1)
    current_state_id: str | None
    slice_sequence: int = Field(ge=1)
    composition_slice: int = Field(ge=0)
    discovery_slice: int = Field(ge=0)
    composition_total: int = Field(ge=0)
    discovery_total: int = Field(ge=0)
    advisor_calls: int = Field(ge=0)
    repair_turns: int = Field(ge=0)
    advisor_checkpoint_passes: int = Field(ge=0)
    mutation_success_seen: bool
    persisted_assistant_message_id: str | None
    persisted_assistant_content: str | None
    persisted_tool_call_turn: bool
    advisor_repair_context_introduced: bool
    pending_advisor_elision_indices: tuple[int, ...]
    pending_response: PrivateChunkRef | None


class ToolBatchFrame(ContinuationValue):
    frame_kind: Literal["tool_batch"] = "tool_batch"
    phase: Literal["tool_ready", "effect_reconciling", "result_retained"]
    response: PrivateChunkRef
    accumulator: PrivateChunkRef
    next_tool_index: int = Field(ge=0)
    settled_effect_ids: tuple[str, ...]


class AdvisorFrame(ContinuationProviderFrame):
    frame_kind: Literal["advisor"] = "advisor"
    phase: Literal["input_ready", "awaiting_response", "response_retained", "decision_ready"]
    review: PrivateChunkRef
    response: PrivateChunkRef | None
    coverage_binding: str


class EndFrame(ContinuationValue):
    frame_kind: Literal["end"] = "end"
    phase: Literal["review_needed", "repair_needed", "publication_ready"]
    state: PrivateChunkRef
    candidate: PrivateChunkRef
    publication_binding: str | None


Frame = Annotated[
    InitializeFrame | ComposeFrame | ToolBatchFrame | AdvisorFrame | EndFrame,
    Field(discriminator="frame_kind"),
]


class ContinuationActionSource(ContinuationValue):
    effect_key: str = Field(pattern=r"^provider:1:(0|[1-9][0-9]*)$")
    input_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    response: PrivateChunkRef

    @model_validator(mode="after")
    def require_actual_advisor_response(self) -> ContinuationActionSource:
        if self.response.codec != "composer.response.v1":
            raise ValueError("prescribed action source requires its actual admitted Advisor response")
        return self


class ContinuationFrameStack(ContinuationValue):
    schema_version: Literal["composer.continuation.snapshot.v1"] = "composer.continuation.snapshot.v1"
    logical: LogicalRequestBinding
    mutation_ceiling: MutationCeilingSnapshot
    provider_cost_policy: ContinuationProviderCostPolicy
    policy_hash: str
    admission_policy_json: str
    cursor: int = Field(ge=0)
    frames: tuple[Frame, ...] = Field(min_length=1)
    effect_frontier: tuple[EffectFrontierEntry, ...]
    pending_settlement_ids: tuple[str, ...]
    semantic_frontier: PrivateChunkRef | None
    active_action_json: str | None = None
    active_action_source: ContinuationActionSource | None = None
    pending_owner_results: tuple[RetainedOwnerResultRef, ...] = ()

    @property
    def is_handoff_safe(self) -> bool:
        if self.pending_settlement_ids or self.pending_owner_results:
            return False
        if any(e.disposition in ("dispatched", "unknown") for e in self.effect_frontier):
            return False
        top = self.frames[-1]
        return (
            (isinstance(top, InitializeFrame) and top.ingress_phase == "ready")
            or (isinstance(top, ComposeFrame) and top.phase == "action_ready")
            or (isinstance(top, ToolBatchFrame) and top.phase == "tool_ready")
            or (isinstance(top, AdvisorFrame) and top.phase == "input_ready")
            or (isinstance(top, EndFrame) and top.phase in ("review_needed", "repair_needed", "publication_ready"))
        )

    @model_validator(mode="after")
    def require_unique_effects(self) -> ContinuationFrameStack:
        self.mutation_ceiling.require_logical(self.logical)
        if (self.active_action_json is None) != (self.active_action_source is None):
            raise ValueError("prescribed action lost its exact actual Advisor source")
        keys = tuple(effect.effect_id for effect in self.effect_frontier)
        if len(set(keys)) != len(keys):
            raise ValueError("duplicate continuation effect identity")
        pending_keys = tuple(result.effect_key for result in self.pending_owner_results)
        if len(set(pending_keys)) != len(pending_keys):
            raise ValueError("duplicate pending owner result identity")
        effects = {effect.effect_id: effect for effect in self.effect_frontier}
        for result in self.pending_owner_results:
            effect = effects.get(result.effect_key)
            if effect is None or effect.disposition != "committed" or effect.receipt_id is None:
                raise ValueError("pending owner result lacks its committed effect receipt")
        return self


USER_MESSAGE_NAMESPACE = UUID("7ba7381f-68a6-5ae5-a7ec-c9a52b05a3e2")


def reserved_user_message_id(session_id: UUID, operation_id: str) -> str:
    UUID(operation_id)
    return str(uuid5(USER_MESSAGE_NAMESPACE, f"{session_id}:{operation_id}:composer.v2.user-message"))


@dataclass(frozen=True, slots=True)
class ContinuationAdmissionSeed:
    snapshot_json: str
    reserved_user_message_id: str
    action_json: str
    policy_hash: str

    def validate_binding(
        self,
        *,
        session_id: UUID,
        operation_id: str,
        kind: str,
        request_json: str,
        request_hash: str,
        base_state_id: UUID | None,
        reserved_user_message_id: str,
    ) -> None:
        from elspeth.web.composer.continuation_codec import decode_continuation_snapshot

        stack = decode_continuation_snapshot(self.snapshot_json)
        expected = (
            str(session_id),
            operation_id,
            reserved_user_message_id,
            request_hash,
            kind,
            str(base_state_id) if base_state_id is not None else None,
        )
        logical = stack.logical
        actual = (
            logical.session_id,
            logical.operation_id,
            logical.original_user_message_id,
            logical.request_hash,
            logical.kind,
            logical.base_state_id,
        )
        first = stack.frames[0]
        if (
            actual != expected
            or self.reserved_user_message_id != reserved_user_message_id
            or stack.policy_hash != self.policy_hash
            or stack.cursor != 0
            or len(stack.frames) != 1
            or not isinstance(first, InitializeFrame)
            or first.request_json != request_json
            or first.ingress_phase != "reserved"
        ):
            raise AuditIntegrityError("continuation initializer differs from admission")


def prepare_composer_continuation_admission(
    *,
    session_id: UUID,
    operation_id: str,
    kind: Literal["compose_message", "compose_recompose"],
    request_json: str,
    request_hash: str,
    base_state_id: UUID | None,
    policy_hash: str,
    admission_policy_json: str,
    initial_trust_mode: Literal["auto_commit", "explicit_approve"],
    initial_action_json: str,
    original_user_message_id: str | None = None,
) -> ContinuationAdmissionSeed:
    from elspeth.web.composer.continuation_codec import encode_continuation_snapshot

    if kind == "compose_recompose":
        if original_user_message_id is None:
            raise AuditIntegrityError("recompose requires verified original user ingress")
        message_id = original_user_message_id
    else:
        if original_user_message_id is not None:
            raise AuditIntegrityError("compose cannot choose another original message")
        message_id = reserved_user_message_id(session_id, operation_id)
    stack = ContinuationFrameStack(
        logical=LogicalRequestBinding(
            session_id=str(session_id),
            operation_id=operation_id,
            original_user_message_id=message_id,
            request_hash=request_hash,
            kind=kind,
            base_state_id=str(base_state_id) if base_state_id is not None else None,
        ),
        mutation_ceiling=MutationCeilingSnapshot(
            session_id=str(session_id),
            request_id=message_id,
            initial_trust_mode=initial_trust_mode,
        ),
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
        policy_hash=policy_hash,
        admission_policy_json=admission_policy_json,
        cursor=0,
        frames=(InitializeFrame(request_json=request_json, ingress_phase="reserved", title_attempt_id=None),),
        effect_frontier=(),
        pending_settlement_ids=(),
        semantic_frontier=None,
    )
    return ContinuationAdmissionSeed(encode_continuation_snapshot(stack), message_id, initial_action_json, policy_hash)
```

- [ ] Add complete codec functions in
  `src/elspeth/web/composer/continuation_codec.py`:

```python
from pydantic import ValidationError
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation import ContinuationFrameStack


def encode_continuation_snapshot(frame_stack: ContinuationFrameStack) -> str:
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    if type(frame_stack) is not ContinuationFrameStack:
        raise TypeError("continuation codec requires its owned frame stack")
    decode_continuation_policy(frame_stack.admission_policy_json, frame_stack.policy_hash)
    return canonical_json(frame_stack.model_dump(mode="json"))


def decode_continuation_snapshot(encoded: str) -> ContinuationFrameStack:
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation snapshot is not strict JSON")
    try:
        # JSON mode admits JSON arrays as tuples while retaining scalar strictness.
        stack = ContinuationFrameStack.model_validate_json(canonical_json(value))
    except ValidationError as exc:
        raise AuditIntegrityError("continuation snapshot violates its owned codec") from exc
    decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if stack.active_action_json is not None:
        from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
        decode_prescribed_continuation_action(stack.active_action_json)
    return stack
```

- [ ] Add these exact behavioral tests, then run their RED mutations and GREEN:

```python
import json
from uuid import UUID
import pytest
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import (
    ContinuationFrameStack, ContinuationProviderCostPolicy, EffectFrontierEntry,
    InitializeFrame, LogicalRequestBinding, MutationCeilingSnapshot,
    PrivateChunkRef, prepare_composer_continuation_admission,
)
from elspeth.web.composer.continuation_codec import (
    decode_continuation_snapshot, encode_continuation_snapshot,
)
from tests.helpers.continuation_policy import minimal_continuation_policy


def initial_stack():
    policy = minimal_continuation_policy()
    return ContinuationFrameStack(
        logical=LogicalRequestBinding(
            session_id="00000000-0000-4000-8000-000000000001",
            operation_id="00000000-0000-4000-8000-000000000002",
            original_user_message_id="original",
            request_hash="a" * 64,
            kind="compose_message",
            base_state_id=None,
        ),
        mutation_ceiling=MutationCeilingSnapshot(
            session_id="00000000-0000-4000-8000-000000000001",
            request_id="original",
            initial_trust_mode="auto_commit",
        ),
        provider_cost_policy=ContinuationProviderCostPolicy(
            applicability="not_applicable",
            lifecycle_version=2,
            accounting_owner="physical_audit_and_token_ledger",
        ),
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        cursor=0,
        frames=(
            InitializeFrame(
                request_json='{"content":"café"}',
                ingress_phase="ready",
                title_attempt_id=None,
                bootstrap=PrivateChunkRef(codec="composer.bootstrap.v1", digest="b" * 64, byte_length=2),
            ),
        ),
        effect_frontier=(),
        pending_settlement_ids=(),
        semantic_frontier=None,
    )


def test_roundtrip_preserves_bytes_order_and_private_phase():
    original = initial_stack()
    encoded = encode_continuation_snapshot(original)
    assert decode_continuation_snapshot(encoded) == original
    assert encode_continuation_snapshot(decode_continuation_snapshot(encoded)) == encoded


def test_unknown_dispatch_is_not_a_handoff_boundary():
    stack = initial_stack()
    assert stack.is_handoff_safe
    unknown = stack.model_copy(update={"effect_frontier": (
        EffectFrontierEntry(effect_id="provider:1", disposition="unknown", receipt_id=None),
    )})
    assert not unknown.is_handoff_safe
    assert not stack.model_copy(update={"pending_settlement_ids": ("attempt:1",)}).is_handoff_safe


@pytest.mark.parametrize("field,value", [("schema_version", "future"), ("cursor", True), ("extra", "ignored")])
def test_unknown_or_mistyped_storage_is_integrity_failure(field, value):
    data = json.loads(encode_continuation_snapshot(initial_stack()))
    data[field] = value
    with pytest.raises(AuditIntegrityError):
        decode_continuation_snapshot(json.dumps(data))


def test_reserved_id_and_initializer_are_bound_before_ingress():
    sid = UUID("00000000-0000-4000-8000-000000000001")
    operation_id = "00000000-0000-4000-8000-000000000002"
    policy = minimal_continuation_policy()
    kwargs = dict(session_id=sid, operation_id=operation_id, kind="compose_message",
                  request_json='{"content":"Build"}', request_hash="a" * 64,
                  base_state_id=None, policy_hash=policy.policy_hash,
                  admission_policy_json=policy.model_dump_json(), initial_action_json='{"kind":"ingress"}',
                  initial_trust_mode="auto_commit")
    first = prepare_composer_continuation_admission(**kwargs)
    assert first == prepare_composer_continuation_admission(**kwargs)
    first.validate_binding(
        session_id=sid, operation_id=operation_id, kind="compose_message",
        request_json=kwargs["request_json"], request_hash="a" * 64, base_state_id=None,
        reserved_user_message_id=first.reserved_user_message_id,
    )
    with pytest.raises(AuditIntegrityError):
        first.validate_binding(
            session_id=sid, operation_id=operation_id, kind="compose_message",
            request_json='{"content":"changed"}', request_hash="a" * 64, base_state_id=None,
            reserved_user_message_id=first.reserved_user_message_id,
        )
```

- [ ] Run the ordinary frame tests:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_frames.py >"$COMPOSER_LOG_DIR/c1a-model.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c1a-model.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  To establish RED, temporarily make `is_handoff_safe` return true for the
  `unknown` case; require the negative test to fail, then restore it and obtain
  exit 0. Mutate the initializer's original message ID and observe its binding
  test reject before any SQL exists.
- [ ] Commit these three files as
  `feat(composer): define versioned continuation frames and codec`.

The namespace is a new product constant. Keep it fixed after shipping; it is
not a release-specific or plan-specific namespace. No lease/token, callable,
Task, SDK object, decrypted credential or transport kwarg is in these models.

### Task C1a.2: Retain exact immutable values through one chunk owner

**Files:** Modify `src/elspeth/web/composer/continuation_codec.py`; test
`tests/unit/web/composer/test_continuation_codec.py`.

- [ ] Add this exact chunk codec. The caller passes an already owned private
  projection. Complete credential admission is performed before persisting its
  canonical bytes; it must never be replaced by a scan of a prefix.

```python
from dataclasses import dataclass
from hashlib import sha256
from elspeth.web.composer.continuation import PrivateChunkRef


@dataclass(frozen=True, slots=True)
class PrivateChunk:
    reference: PrivateChunkRef
    canonical: str


def encode_private_chunk(
    *, codec: str, canonical: str, env_ref_names: frozenset[str] = frozenset(),
) -> PrivateChunk:
    from elspeth.web.composer.bounded_json import JSON_MAX_TOTAL_UTF8_BYTES
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_in_state
    encoded = canonical.encode("utf-8")
    if len(encoded) > JSON_MAX_TOTAL_UTF8_BYTES:
        raise AuditIntegrityError("private chunk exceeds the complete JSON admission bound")
    value, error = parse_json_strict(canonical)
    if error is not None or canonical_json(value) != canonical:
        raise AuditIntegrityError("private chunk must retain canonical strict JSON")
    if codec == "composer.state.v1":
        if type(value) is not dict:
            raise AuditIntegrityError("state chunk is not an owned state object")
        state = CompositionState.from_dict(value)
        require_no_credential_material_in_state(
            state, surface="composer_continuation_state", env_ref_names=env_ref_names,
        )
    else:
        require_no_credential_material(
            canonical, surface="composer_continuation_private_chunk", env_ref_names=env_ref_names,
            max_string_bytes=JSON_MAX_TOTAL_UTF8_BYTES,
        )
    reference = PrivateChunkRef.model_validate({
        "codec": codec, "digest": sha256(encoded).hexdigest(), "byte_length": len(encoded),
    })
    return PrivateChunk(reference, canonical)


def verify_private_chunk(reference: PrivateChunkRef, canonical: str) -> None:
    value, error = parse_json_strict(canonical)
    if error is not None or canonical_json(value) != canonical:
        raise AuditIntegrityError("retained private chunk is not canonical strict JSON")
    encoded = canonical.encode("utf-8")
    if len(encoded) != reference.byte_length or sha256(encoded).hexdigest() != reference.digest:
        raise AuditIntegrityError("private chunk bytes differ from their binding")
```

  Non-state chunks receive a complete canonical-wire credential scan so a
  typed field label is not reclassified as a credential slot merely because
  it appears inside an owner snapshot. This does not replace the producer's
  field-aware admission: state owners use the actual state projection, tool
  owners use `require_no_credential_material_for_tool` with the actual tool and
  current state/inventory, and provider owners admit the complete native and
  argument fields before encoding. Nested state JSON in evidence must pass
  the state owner before the containing snapshot is retained. Verification of
  stored bytes proves integrity only; every actual content use is admitted
  afresh by its current owner.

- [ ] Add this complete test. Its native order is deliberately significant.

```python
import pytest
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.web.composer.continuation_codec import encode_private_chunk, verify_private_chunk
from elspeth.web.credential_guard import CredentialMaterialRefused
from tests.unit.web.test_credential_guard import _field_mapper_state


def test_exact_native_order_and_omitted_tools_are_distinct():
    one = canonical_json({"messages": [{"role": "assistant", "thinking_blocks": [
        {"type": "thinking", "thinking": "first", "signature": "s1"},
        {"type": "redacted_thinking", "data": "second"},
    ]}], "tools": None})
    two = canonical_json({"messages": [{"role": "assistant", "thinking_blocks": [
        {"type": "redacted_thinking", "data": "second"},
        {"type": "thinking", "thinking": "first", "signature": "s1"},
    ]}], "tools": []})
    chunk = encode_private_chunk(codec="composer.messages.v1", canonical=one)
    verify_private_chunk(chunk.reference, one)
    with pytest.raises(AuditIntegrityError):
        verify_private_chunk(chunk.reference, two)


def test_private_chunk_admission_inspects_the_complete_tail_before_retention():
    safe = canonical_json({"prefix": "x" * 8192, "tail": {"description": "synthetic value"}})
    assert encode_private_chunk(codec="composer.messages.v1", canonical=safe).canonical == safe
    unsafe = canonical_json({"prefix": "x" * 8192, "tail": {"api_key": "synthetic-literal-credential"}})
    with pytest.raises(CredentialMaterialRefused):
        encode_private_chunk(codec="composer.messages.v1", canonical=unsafe)


def test_state_chunks_keep_field_mapper_labels_under_the_real_state_projection():
    encoded = canonical_json(_field_mapper_state().to_dict())
    actual = encode_private_chunk(codec="composer.state.v1", canonical=encoded)
    verify_private_chunk(actual.reference, encoded)
```

- [ ] Run the ordinary chunk-codec tests:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_codec.py >"$COMPOSER_LOG_DIR/c1a-chunks.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c1a-chunks.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  Remove digest comparison in a disposable code mutation: changed bytes must
  make this test fail. Restore and obtain exit 0.
- [ ] Commit as `feat(composer): bind exact immutable continuation chunks`.

### Task C1a.3: Extract the live compose locals into explicit phase state

**Files:** Modify `src/elspeth/web/composer/service.py` at `_compose_loop`,
`src/elspeth/web/composer/_compose_loop_carriers.py`,
`src/elspeth/web/composer/tool_batch.py` at `BatchAccumulator`/`run_tool_batch`;
test `tests/unit/web/composer/test_continuation_frames.py` and existing
`tests/unit/web/composer/test_service.py`.

- [ ] Move each current loop local into its same-named `ComposeFrame` field or
  the typed owned cache/evidence subrecord named below. Preserve existing
  `_call_model_turn`, `_try_terminate_no_tools`, `_dispatch_tool_batch`,
  `_persist_turn_audit` and `_classify_and_budget_turn` bodies and call order.
  They remain the behavior owners. Do not replace the compose loop with a new
  graph generator or re-enter top-level `compose()` on resume.
- [ ] Use these exact phase edges around the existing calls:

```text
action_ready -> persist prepared exact messages/tools + awaiting_response
awaiting_response -> actual provider admission -> pre-SDK dispatch-intent snapshot
provider response -> retain admitted complete native response + response_retained
response_retained with tool calls -> ToolBatchFrame(tool_ready, next_tool_index=0)
response_retained without tools -> EndFrame(review_needed, exact candidate)
entry_pending -> entry_admitted -> call_prepared -> call_reconciling
              -> call_result_retained -> outcome_application_pending -> applied
              -> next call_prepared OR publication_ready -> published -> classify
END repair -> persist exact repair exchange -> action_ready
END admitted -> authority terminal commit
```

- [ ] Apply the frames companion's once-only batch entry, per-call result
  application and P4 packets. A saved tool index alone is insufficient: it
  would repeat abandonment invalidation, duplicate-ID preflight, assistant
  append and transcript observation, then reset the accumulated prefix.
  Actual SQL writers atomically retain their nominal owner receipt in
  `pending_owner_results`. Cold application consumes that receipt and clears
  its queue entry in the same checkpoint CAS as the evidence/frame update;
  it never calls the writer again. Advance the index only after that actual
  result is applied. Preserve handler/decoding/redaction exception primacy.
- [ ] For each exact pure phase, compare uninterrupted execution with
  encode/discard-all-owners/decode/new-owner execution using the same public
  deterministic provider replies already used by `test_service.py`. Assert the
  exact next prepared messages, native blocks, tools, tool result pairing,
  graph, counters, current state ID and final publication. Put the serialized
  checkpoint between the same two calls in each parametrized case; never use
  a mock that returns the saved final result directly.
- [ ] Run the existing compose-loop persistence, tool-batch, pending-review,
  wire-fidelity and service tests, plus the new frame tests. Use the individual
  concrete packets in Q's verification table with ordinary pytest, and
  require every native exit 0. Change one retained tool index, counter, native
  order and review binding separately: each uninterrupted-equivalence assertion
  must fail. This gate remains pure; C1b supplies the later SQL/custody proof.
- [ ] Commit only the extracted carriers, driver/batch changes and their tests
  as `refactor(composer): expose resumable compose phase state`.

### Task C1a.4: Retain every request-owned subrecord, including new transcript state

**Files:** Create `src/elspeth/web/composer/continuation_evidence.py`; modify
`src/elspeth/web/composer/turn_effects.py`, `advisor_transcript.py`, `audit.py`,
`schema_disclosure.py` and `anti_anchor.py`; test
`tests/unit/web/composer/test_continuation_frames.py`.

Snapshot/restore methods belong on the owners, where private fields are typed.
Use closed frozen values, not reflection over `__dict__`/`__slots__`, pickle or
a general arbitrary-object serializer. The complete field transfer list is:

| Owner | Exact state to retain and restoration rule |
| --- | --- |
| `TurnEvidence` | `effects`, `_abandonment_review`, `_abandonment_limitations`, `_abandonment_transcript_binding`, `_pages`, `_quoted_inspections`, `_conflicting_page_ids`, `_registration_revision`, `_delivery_generations`, `_current_pages`, `_current_calls`, `_historical_pages`, `_retained_history`, `transcript`, request/session bindings. Rebind quoted projection `.owner` to the newly constructed owner. Preserve the v1 constructor/hash path. |
| `AdvisorTranscriptOwner` | `request`, authenticated `_history`, every `_excluded`/`_observed` message, its stable event and `pending_adoption`, received/effective argument provenance, admission and read-delivery flags. Store shared message relationships as indexes into one retained message shelf, including messages elided from current provider context; never store Python object IDs. Recreate `_blob_reader` from the current authorized service. |
| `BufferingRecorder` | `_invocations`, `_llm_calls`, `_planner_attempts`, `_withheld_replies`, `_visible_tool_events`, `_private_candidates`, `_private_authorings`, plus per-channel committed cursors. Recreate a new lock. Retained physical custody IDs refer to the real quota owner; they are not live `ProviderCallCustody` objects. |
| `AntiAnchorTracker` | Failure deque in order, retry/drift fire state and any current category state. Preserve its actual existing structural rules; no English classifier is introduced. |
| `SchemaDisclosureTracker` | Successful schema IDs and current catalog/policy binding. Restore the current session's set or explicitly invalidate it with recorded review debt. |
| Discovery/runtime caches | Exact keys, values and policy/state identity; a stale catalog/config binding invalidates the cache rather than presenting stale evidence. |
| `AttachmentReadAdmission` | Retain the successful receipt and its full policy/detector binding as history. Every actual read/use/retry/resume performs fresh current-custody and complete-original inspection; restoring this receipt grants no clearance. |

- [ ] Add `snapshot_for_continuation()` and nominal classmethod
  `for_continuation(snapshot, current_execution_binding)` to `TurnEvidence`.
  The snapshot's logical identity is stable; its current `operation_id` remains
  the newly validated fence operation ID. Historical effects restore through
  the authority's `ValidatedHistoricalEffectReference`, never a reminted live
  `CommittedCheckpointWitness`.
- [ ] Keep v1 `identity` bytes unchanged. Add the v2 branch before the existing
  body, using the same semantic payload plus the stable logical binding and
  retained physical creator facts. Do not include the new current lease/fence
  merely because it changed during recovery.
- [ ] Add transcript snapshot/restore by interning actual owned messages once
  by identity while snapshotting, storing their indexes in the excluded and
  observed records, then allocating a single decoded message shelf on restore.
  Point restored `llm_messages`, exclusions and observations to that same shelf.
  Persist current events even when their messages were elided; preserve
  `pending_adoption` until the normal provider-reply owner admits it.
- [ ] Extend the frame equivalence tests with: a prior amendment in the middle;
  backend-finalized arguments; a withheld assistant reply; a full read result
  delivered to a specific provider call; repeated identical human words as
  separate chronological events; pending adoption; saved revision frontiers;
  channel partially drained; and a stale policy. The exact prepared Advisor
  transcript must match the uninterrupted control and its semantic binding.
- [ ] Run the new frame tests plus `test_turn_effects.py`,
  `test_advisor_transcript.py`, `test_transcript_argument_order.py` and
  `test_audit.py`. Put new disclosure/owner snapshot cases in
  `tests/unit/web/composer/test_continuation_owner_snapshots.py`; no existing
  `test_schema_disclosure.py` is assumed.
- [ ] Commit as `feat(composer): preserve evidence and transcript across cold frames`.

**Exit C1a:** every next-transition input survives a pure round trip with new
owners and no SQL/custody shortcut. A snapshotting field list alone is not this
proof. No v2 provider/worker admission is enabled before C1b/C4b.

### Task C1a.5: Keep initialized ingress recoverable before building the compose frame

**Files:** Modify `src/elspeth/web/composer/continuation_codec.py`; extend
`tests/unit/web/sessions/test_run_composer_turn.py` in C1b after C3 ingress exists.

- [ ] Add this complete builder. It runs inside the existing ingress transaction
  after obtaining the exact same-connection transcript; it does not write a
  second message or construct a graph. Caller performs full private credential
  admission before the snapshot is committed.

```python
from pydantic import TypeAdapter
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.sessions.protocol import MessageIngressFresh


_INGRESS_CODEC = TypeAdapter(MessageIngressFresh)


def snapshot_after_composer_ingress(
    *, prior_snapshot_json: str, receipt: MessageIngressFresh, next_cursor: int,
) -> str:
    stack = decode_continuation_snapshot(prior_snapshot_json)
    top = stack.frames[-1]
    if (
        not isinstance(top, InitializeFrame) or top.ingress_phase != "reserved"
        or str(receipt.operation_id) != stack.logical.operation_id
        or str(receipt.message.id) != stack.logical.original_user_message_id
        or str(receipt.message.session_id) != stack.logical.session_id
        or next_cursor <= stack.cursor
    ):
        raise AuditIntegrityError("continuation ingress differs from its reserved logical request")
    retained = _INGRESS_CODEC.dump_json(receipt, warnings="error").decode("utf-8")
    next_top = top.model_copy(update={"ingress_phase": "inserted", "ingress_receipt_json": retained})
    return encode_continuation_snapshot(stack.model_copy(update={
        "cursor": next_cursor, "frames": (*stack.frames[:-1], next_top),
    }))


def restored_composer_ingress(frame: InitializeFrame) -> MessageIngressFresh:
    if frame.ingress_phase == "reserved" or frame.ingress_receipt_json is None:
        raise AuditIntegrityError("continuation ingress has not committed")
    try:
        return _INGRESS_CODEC.validate_json(frame.ingress_receipt_json, strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("retained ingress receipt is corrupt") from exc
```

- [ ] The SQL authority passes its exact computed next cursor, checks the
  returned root cursor, and commits message/transcript receipt/snapshot/pointer
  together. A fresh process restores `MessageIngressFresh` and enters below the
  user insertion/title phase instead of calling `run_composer_turn()` from the
  beginning. Title attempt/outcome remains separately retained.
- [ ] In the existing ingress test, crash immediately after its transaction
  commits; discard the old service, restore this receipt and compare original
  message ID, operation ID, complete ordered transcript and title attempt with
  the uninterrupted control. Repeat the restore twice and assert one user row
  and one ingress receipt from authoritative SQL. A reserved-ID mismatch must
  fail before message insertion. Run the C1b ingress/cold-restore packet, then
  commit as `feat(composer): retain atomic original ingress for cold resume`.

## C6 — Bound continuation to evidence and actual next actions

### Task C6.1: Add the closed v2 Advisor decision

**Files:** Modify `src/elspeth/web/composer/advisor_output.py`; create
`tests/unit/web/composer/test_continuation_progress.py`.

- [ ] Add these complete new models next to the existing v1 abandonment model.
  Keep v1 parsing/response-format functions unchanged. Use the v2 parser only
  for a persisted lifecycle-v2 request.

```python
from typing import Annotated
from elspeth.contracts.errors import AuditIntegrityError


class ContinuationEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    receipt_id: str = Field(min_length=1)
    source_kind: Literal["graph", "read", "provider", "validation"] = "read"
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    original_attempt_id: str | None
    coverage_binding: str


class ContinuationObligationProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    obligation_id: str = Field(min_length=1, max_length=128)
    parent_id: str | None
    description: str = Field(min_length=1)
    original_message_id: str = Field(min_length=1)
    start: int = Field(ge=0)
    end: int = Field(ge=0)
    status: Literal["open", "blocked", "satisfied"]


class ContinuationActionBase(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    obligation_ids: tuple[str, ...] = Field(min_length=1)
    evidence_ids: tuple[str, ...]
    input_hash: str
    graph_hash: str
    policy_hash: str
    tool_manifest_hash: str

    @model_validator(mode="after")
    def require_unique_obligation_ids(self) -> ContinuationActionBase:
        if len(set(self.obligation_ids)) != len(self.obligation_ids):
            raise ValueError("Advisor repeated an action obligation reference")
        return self


class PrimaryContinuationAction(ContinuationActionBase):
    kind: Literal["primary_turn"]
    instruction: str = Field(min_length=1)


class ReadContinuationAction(ContinuationActionBase):
    kind: Literal["read_tool"]
    tool_name: str = Field(min_length=1)
    arguments_json: str


class ReviewContinuationAction(ContinuationActionBase):
    kind: Literal["review"]
    candidate_ref: str = Field(pattern=r"^candidate:[0-9a-f]{64}$")
    evidence_binding: str


PrescribedContinuationAction = Annotated[
    PrimaryContinuationAction | ReadContinuationAction | ReviewContinuationAction,
    Field(discriminator="kind"),
]


class _ContinuationAdvisorAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    schema_version: Literal["composer.continuation-decision.v1"]
    decision: Literal["CONTINUE", "CONTINUE_WITH_LIMITATION", "STOP_CONFIRMED"]
    scope: Literal["subset", "whole_request"]
    obligation_ids: tuple[str, ...] = Field(min_length=1)
    obligations: tuple[ContinuationObligationProposal, ...]
    evidence: tuple[ContinuationEvidenceRef, ...]
    reason: str = Field(min_length=1)
    remaining_actionable_ids: tuple[str, ...]
    prescribed_action: PrescribedContinuationAction | None

    @model_validator(mode="after")
    def require_owned_decision_shape(self) -> _ContinuationAdvisorAnswer:
        _require_unicode_scalars(self.reason)
        if self.decision == "STOP_CONFIRMED":
            if self.prescribed_action is not None:
                raise ValueError("stop decision cannot prescribe an unattempted action")
            if self.scope == "whole_request" and self.remaining_actionable_ids:
                raise ValueError("whole-request stop cannot leave actionable work")
        elif self.prescribed_action is None:
            raise ValueError("continuation must prescribe a concrete action")
        return self


class ContinuationAdvisorDecision(_ContinuationAdvisorAnswer):
    decision_id: str = Field(min_length=1)


def parse_continuation_advisor_decision(
    text: str, *, actual_advisor_call_id: str,
) -> ContinuationAdvisorDecision | None:
    if not actual_advisor_call_id:
        raise AuditIntegrityError("continuation decision lacks its actual Advisor call")
    parsed, error = parse_json_strict(text)
    if error is not None or type(parsed) is not dict:
        return None
    try:
        answer = _ContinuationAdvisorAnswer.model_validate_json(text)
        return ContinuationAdvisorDecision(
            **answer.model_dump(mode="python"), decision_id=actual_advisor_call_id,
        )
    except ValidationError:
        return None


def continuation_advisor_response_format() -> dict[str, object]:
    return {
        "type": "json_schema",
        "json_schema": {
            "name": "composer_continuation_decision",
            "strict": True,
            "schema": _ContinuationAdvisorAnswer.model_json_schema(),
        },
    }
```

- [ ] Add these parser controls. A structurally valid call remains subject to
  receipt/policy validation in C6.2; it does not authorize a tool by itself.

```python
import json
from elspeth.web.composer.advisor_output import parse_continuation_advisor_decision


def stop_decision():
    return dict(schema_version="composer.continuation-decision.v1",
                decision="STOP_CONFIRMED", scope="whole_request", obligation_ids=["request"],
                obligations=[], evidence=[], reason="A reviewed blocker covers the request.",
                remaining_actionable_ids=[], prescribed_action=None)


def test_continue_without_action_is_not_a_continuation():
    data = stop_decision()
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is not None
    data["decision"] = "CONTINUE"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None


def test_whole_stop_cannot_hide_actionable_remainder_or_user_question():
    data = stop_decision()
    data["remaining_actionable_ids"] = ["request/child"]
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None
    data["remaining_actionable_ids"] = []
    data["question"] = "Continue?"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None


def test_only_the_actual_owner_assigns_the_physical_decision_identity():
    data = stop_decision()
    decision = parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1")
    assert decision is not None and decision.decision_id == "actual-call:1"
    data["decision_id"] = "model-invented-physical-id"
    assert parse_continuation_advisor_decision(json.dumps(data), actual_advisor_call_id="actual-call:1") is None
```

- [ ] Run the ordinary decision tests:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_progress.py tests/unit/web/composer/test_advisor_output.py >"$COMPOSER_LOG_DIR/c6-decision.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c6-decision.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  Temporarily remove the action-required check and require the continuation
  control to fail; restore and require exit 0.
- [ ] Commit as `feat(composer): admit scoped continuation decisions`.

### Task C6.2: Make action recurrence and completion receipt-based

**Files:** Create `src/elspeth/web/composer/continuation_progress.py`; extend
`tests/unit/web/composer/test_continuation_progress.py`.

- [ ] Add this complete product frontier reducer. Only an actual reviewed Advisor
  decision changes semantic obligation status; its model-authored descriptions
  and evidence relevance are never derived from user-text matching.

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal
from typing import TYPE_CHECKING

from pydantic import ConfigDict, TypeAdapter, ValidationError
from pydantic.dataclasses import dataclass

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision, ContinuationEvidenceRef,
    PrescribedContinuationAction, ReadContinuationAction, ReviewContinuationAction,
)

if TYPE_CHECKING:
    from elspeth.web.composer.continuation import ContinuationFrameStack
    from elspeth.web.composer.continuation_codec import PrivateChunk


class ContinuationProtocolError(ValueError):
    pass


_ACTION_CODEC = TypeAdapter(PrescribedContinuationAction)


def decode_prescribed_continuation_action(encoded: str) -> PrescribedContinuationAction:
    from elspeth.contracts.hashing import canonical_json
    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("prescribed continuation action is not strict JSON")
    try:
        return _ACTION_CODEC.validate_json(canonical_json(parsed), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("prescribed continuation action violates its owned codec") from exc


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ContinuationObligation:
    obligation_id: str
    parent_id: str | None
    description: str
    original_message_id: str
    start: int
    end: int
    status: Literal["open", "blocked", "satisfied"] = "open"


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ActionExecutionReceipt:
    action_key: str
    owner_receipt_id: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]


@dataclass(frozen=True, slots=True, config=ConfigDict(extra="forbid", strict=True))
class ContinuationFrontier:
    obligations: tuple[ContinuationObligation, ...]
    evidence: tuple[ContinuationEvidenceRef, ...]
    pending_review_bindings: tuple[str, ...]
    action_receipts: tuple[ActionExecutionReceipt, ...]
    graph_hash: str


_FRONTIER_CODEC = TypeAdapter(ContinuationFrontier)


def encode_continuation_frontier(frontier: ContinuationFrontier) -> str:
    from elspeth.contracts.hashing import canonical_json
    if type(frontier) is not ContinuationFrontier:
        raise TypeError("frontier codec requires its owned product value")
    return canonical_json(_FRONTIER_CODEC.dump_python(frontier, mode="json", warnings="error"))


def decode_continuation_frontier(encoded: str) -> ContinuationFrontier:
    from elspeth.contracts.hashing import canonical_json
    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation frontier is not strict JSON")
    try:
        return _FRONTIER_CODEC.validate_json(canonical_json(parsed), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("continuation frontier violates its owned codec") from exc


def initial_continuation_frontier(
    *, request: str, original_message_id: str, graph_hash: str,
) -> ContinuationFrontier:
    if not request or not original_message_id:
        raise ContinuationProtocolError("continuation needs its complete original request")
    root = ContinuationObligation(
        "request", None, request, original_message_id, 0, len(request),
    )
    return ContinuationFrontier((root,), (), (), (), graph_hash)


def canonicalize_continuation_obligations(
    decision: ContinuationAdvisorDecision, frontier: ContinuationFrontier,
) -> ContinuationAdvisorDecision:
    known = {item.obligation_id: item for item in frontier.obligations}
    proposals = {item.obligation_id: item for item in decision.obligations}
    if len(proposals) != len(decision.obligations):
        raise ContinuationProtocolError("Advisor repeated an obligation reference")
    aliases: dict[str, str] = {}
    visiting: set[str] = set()

    def resolve(reference: str) -> str:
        if reference in known:
            return reference
        if reference in aliases:
            return aliases[reference]
        if reference not in proposals:
            return reference
        if reference in visiting:
            raise ContinuationProtocolError("obligation parentage is cyclic")
        visiting.add(reference)
        proposal = proposals[reference]
        if proposal.parent_id is None:
            raise ContinuationProtocolError("new obligation lacks its existing request parent")
        parent = resolve(proposal.parent_id)
        canonical = "obligation:" + stable_hash({
            "original_message_id": proposal.original_message_id,
            "start": proposal.start, "end": proposal.end, "parent": parent,
        })
        visiting.remove(reference)
        aliases[reference] = canonical
        return canonical

    normalized = tuple(item.model_copy(update={
        "obligation_id": resolve(item.obligation_id),
        "parent_id": None if item.parent_id is None else resolve(item.parent_id),
    }) for item in decision.obligations)
    if len({item.obligation_id for item in normalized}) != len(normalized):
        raise ContinuationProtocolError("multiple obligation labels name the same immutable source span")
    action = decision.prescribed_action
    if action is not None:
        resolved = tuple(resolve(key) for key in action.obligation_ids)
        if len(set(resolved)) != len(resolved):
            raise ContinuationProtocolError("Advisor repeated an action obligation reference")
        action = action.model_copy(update={"obligation_ids": resolved})
    return decision.model_copy(update={
        "obligations": normalized,
        "obligation_ids": tuple(resolve(key) for key in decision.obligation_ids),
        "remaining_actionable_ids": tuple(resolve(key) for key in decision.remaining_actionable_ids),
        "prescribed_action": action,
    })


def proposed_obligations(
    decision: ContinuationAdvisorDecision, frontier: ContinuationFrontier,
) -> tuple[ContinuationObligation, ...]:
    current = {item.obligation_id: item for item in frontier.obligations}
    if len(current) != len(frontier.obligations) or "request" not in current:
        raise ContinuationProtocolError("frontier lacks a unique whole-request obligation")
    root = current["request"]
    updates = {item.obligation_id: item for item in decision.obligations}
    if len(updates) != len(decision.obligations):
        raise ContinuationProtocolError("Advisor repeated an obligation identity")
    for update in decision.obligations:
        if (
            update.original_message_id != root.original_message_id
            or not 0 <= update.start < update.end <= root.end
            or (update.obligation_id == "request" and (
                update.parent_id is not None or update.start != 0 or update.end != root.end
            ))
            or (update.obligation_id != "request" and update.parent_id is None)
        ):
            raise ContinuationProtocolError("obligation lost its original human source")
        prior = current.get(update.obligation_id)
        if prior is not None and (
            prior.parent_id, prior.original_message_id, prior.start, prior.end,
        ) != (update.parent_id, update.original_message_id, update.start, update.end):
            raise ContinuationProtocolError("an existing obligation changed its source binding")
        current[update.obligation_id] = ContinuationObligation(
            update.obligation_id, update.parent_id, update.description,
            update.original_message_id, update.start, update.end, update.status,
        )
    for item in current.values():
        seen = {item.obligation_id}
        parent = item.parent_id
        while parent is not None:
            if parent in seen or parent not in current:
                raise ContinuationProtocolError("obligation parentage is cyclic or missing")
            seen.add(parent)
            ancestor = current[parent]
            if not ancestor.start <= item.start < item.end <= ancestor.end:
                raise ContinuationProtocolError("child obligation exceeds its immutable parent source span")
            parent = current[parent].parent_id
    source_bindings = {(item.original_message_id, item.start, item.end) for item in current.values()}
    if len(source_bindings) != len(current):
        raise ContinuationProtocolError("duplicate immutable source binding")
    return tuple(current.values())


def current_action_receipts(
    history: tuple[ActionExecutionReceipt, ...],
) -> tuple[ActionExecutionReceipt, ...]:
    latest: dict[str, ActionExecutionReceipt] = {}
    transitions = {
        "prepared": {"dispatched", "committed", "refused", "unknown"},
        "dispatched": {"committed", "refused", "unknown"},
        "unknown": {"committed", "refused"},
        "committed": set(),
        "refused": set(),
    }
    for receipt in history:
        prior = latest.get(receipt.action_key)
        if prior is not None and prior != receipt:
            if (
                prior.owner_receipt_id != receipt.owner_receipt_id
                or receipt.disposition not in transitions[prior.disposition]
            ):
                raise AuditIntegrityError("action custody history changed or moved backwards")
        latest[receipt.action_key] = receipt
    return tuple(latest.values())


def continuation_action_key(
    action: PrescribedContinuationAction, *,
    retained_evidence: Mapping[str, ContinuationEvidenceRef] | None = None,
) -> str:
    payload = action.model_dump(mode="json")
    payload["obligation_ids"] = sorted(action.obligation_ids)
    premise_evidence = set()
    for receipt_id in action.evidence_ids:
        if retained_evidence is None or receipt_id not in retained_evidence:
            raise ContinuationProtocolError("action premise lacks an actual retained evidence receipt")
        item = retained_evidence[receipt_id]
        if item.source_kind != "provider":
            premise_evidence.add((item.source_kind, item.content_hash, item.coverage_binding))
    payload["evidence_ids"] = sorted(premise_evidence)
    if isinstance(action, ReadContinuationAction):
        arguments, error = parse_json_strict(action.arguments_json)
        if error is not None or type(arguments) is not dict:
            raise ContinuationProtocolError("read action arguments must be one strict object")
        payload["arguments_json"] = arguments
    elif isinstance(action, ReviewContinuationAction):
        # The exact candidate text identity, graph and normalized actual
        # evidence remain. A newer physical review receipt is not novelty.
        payload.pop("evidence_binding")
    return stable_hash(payload)


def require_bound_continuation_decision(
    decision: ContinuationAdvisorDecision, *, actual_advisor_call_id: str,
    frontier: ContinuationFrontier, retained_evidence: Mapping[str, ContinuationEvidenceRef],
    input_hash: str, policy_hash: str, tool_manifest_hash: str, permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> None:
    decision = canonicalize_continuation_obligations(decision, frontier)
    if not actual_advisor_call_id or decision.decision_id != actual_advisor_call_id:
        raise ContinuationProtocolError("decision lacks its actual Advisor call")
    proposed = proposed_obligations(decision, frontier)
    obligations = {item.obligation_id for item in proposed}
    selected = set(decision.obligation_ids)
    remainder = set(decision.remaining_actionable_ids)
    if not selected <= obligations or not remainder <= obligations:
        raise ContinuationProtocolError("decision names an unknown obligation")
    if len(selected) != len(decision.obligation_ids) or len(remainder) != len(decision.remaining_actionable_ids):
        raise ContinuationProtocolError("decision repeated an obligation reference")
    if any(item.status != "open" and item.obligation_id in remainder for item in proposed):
        raise ContinuationProtocolError("actionable remainder contains a closed obligation")
    open_ids = {item.obligation_id for item in proposed if item.status == "open"}
    if remainder != open_ids:
        raise ContinuationProtocolError("decision omitted an open obligation from its remainder")
    for evidence in decision.evidence:
        if retained_evidence.get(evidence.receipt_id) != evidence:
            raise ContinuationProtocolError("decision names unretained evidence")
    action = decision.prescribed_action
    if action is None:
        if decision.scope == "whole_request":
            if "request" not in selected or not complete_required_coverage or frontier.pending_review_bindings:
                raise ContinuationProtocolError("whole stop lacks complete reviewed scope")
            if any(receipt.disposition in ("prepared", "dispatched", "unknown") for receipt in current_action_receipts(frontier.action_receipts)):
                raise ContinuationProtocolError("whole stop hides unresolved action custody")
            if any(item.status == "open" for item in proposed):
                raise ContinuationProtocolError("whole stop hides an open obligation")
        elif remainder:
            raise ContinuationProtocolError("subset stop leaves actionable work without a continuation action")
        return
    if not set(action.obligation_ids) <= obligations or not set(action.evidence_ids) <= retained_evidence.keys():
        raise ContinuationProtocolError("action lacks actual obligation/evidence binding")
    if (action.input_hash, action.graph_hash, action.policy_hash, action.tool_manifest_hash) != (
        input_hash, frontier.graph_hash, policy_hash, tool_manifest_hash,
    ):
        raise ContinuationProtocolError("action premise changed")
    if isinstance(action, ReadContinuationAction) and action.tool_name not in permitted_read_tools:
        raise ContinuationProtocolError("Advisor cannot prescribe a mutation as a read")
    key = continuation_action_key(action, retained_evidence=retained_evidence)
    if any(receipt.action_key == key for receipt in frontier.action_receipts):
        raise ContinuationProtocolError("unchanged continuation action has already been prepared")


def apply_bound_continuation_decision(
    decision: ContinuationAdvisorDecision, *, actual_advisor_call_id: str,
    frontier: ContinuationFrontier, retained_evidence: Mapping[str, ContinuationEvidenceRef],
    input_hash: str, policy_hash: str, tool_manifest_hash: str,
    permitted_read_tools: frozenset[str], complete_required_coverage: bool,
) -> ContinuationFrontier:
    decision = canonicalize_continuation_obligations(decision, frontier)
    require_bound_continuation_decision(
        decision, actual_advisor_call_id=actual_advisor_call_id, frontier=frontier,
        retained_evidence=retained_evidence, input_hash=input_hash,
        policy_hash=policy_hash, tool_manifest_hash=tool_manifest_hash,
        permitted_read_tools=permitted_read_tools,
        complete_required_coverage=complete_required_coverage,
    )
    from dataclasses import replace
    merged = {item.receipt_id: item for item in frontier.evidence}
    merged.update((item.receipt_id, item) for item in decision.evidence)
    return replace(
        frontier, obligations=proposed_obligations(decision, frontier),
        evidence=tuple(merged.values()),
    )


def record_action_receipt(
    frontier: ContinuationFrontier, receipt: ActionExecutionReceipt,
    *, actual_owner_receipt_id: str,
) -> ContinuationFrontier:
    if not actual_owner_receipt_id or receipt.owner_receipt_id != actual_owner_receipt_id:
        raise AuditIntegrityError("continuation action lacks its owning execution receipt")
    from dataclasses import replace
    previous = tuple(item for item in current_action_receipts(frontier.action_receipts) if item.action_key == receipt.action_key)
    if previous and previous[-1].disposition in ("committed", "refused"):
        if previous[-1] == receipt:
            return frontier
        raise AuditIntegrityError("terminal action receipt changed")
    history = (*frontier.action_receipts, receipt)
    current_action_receipts(history)
    return replace(frontier, action_receipts=history)


def retain_advisor_action(
    stack: ContinuationFrameStack, decision: ContinuationAdvisorDecision, *,
    actual_advisor_call_id: str, frontier: ContinuationFrontier,
    actual_source: ContinuationActionSource,
    retained_evidence: Mapping[str, ContinuationEvidenceRef], input_hash: str,
    policy_hash: str, tool_manifest_hash: str, permitted_read_tools: frozenset[str],
    complete_required_coverage: bool,
) -> tuple[ContinuationFrameStack, PrivateChunk]:
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.continuation_codec import encode_frontier_chunk
    from elspeth.web.composer.continuation import ContinuationActionSource
    if type(actual_source) is not ContinuationActionSource:
        raise TypeError("Advisor action retention requires its exact current SQL-verified source")
    decision = canonicalize_continuation_obligations(decision, frontier)
    if stack.active_action_json is not None:
        raise ContinuationProtocolError("a prior prescribed action still owns execution")
    if stack.semantic_frontier != encode_frontier_chunk(frontier).reference:
        raise AuditIntegrityError("Advisor frontier differs from the selected complete snapshot")
    accepted = apply_bound_continuation_decision(
        decision, actual_advisor_call_id=actual_advisor_call_id,
        frontier=frontier, retained_evidence=retained_evidence,
        input_hash=input_hash, policy_hash=policy_hash,
        tool_manifest_hash=tool_manifest_hash, permitted_read_tools=permitted_read_tools,
        complete_required_coverage=complete_required_coverage,
    )
    if decision.decision == "STOP_CONFIRMED" and decision.scope == "whole_request":
        if stack.pending_settlement_ids or stack.pending_owner_results or any(
            item.disposition in ("prepared", "dispatched", "unknown") for item in stack.effect_frontier
        ):
            raise ContinuationProtocolError("whole stop has unsettled actual execution")
    chunk = encode_frontier_chunk(accepted)
    action = decision.prescribed_action
    action_json = None if action is None else canonical_json(action.model_dump(mode="json"))
    return stack.model_copy(update={
        "semantic_frontier": chunk.reference, "active_action_json": action_json,
        "active_action_source": actual_source if action_json is not None else None,
    }), chunk
```

- [ ] Add a read-action test using real closed models, with no fake tool owner:

```python
from dataclasses import replace
import pytest
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.advisor_output import (
    ContinuationAdvisorDecision, ReadContinuationAction,
)
from elspeth.web.composer.continuation_progress import (
    ActionExecutionReceipt, ContinuationFrontier, ContinuationObligation,
    ContinuationProtocolError, continuation_action_key,
    require_bound_continuation_decision,
)


def test_same_action_cannot_gain_freshness_from_rollover_or_advice_words():
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")
    action = ReadContinuationAction(
        kind="read_tool", obligation_ids=("request",), evidence_ids=(),
        input_hash="i", graph_hash="g", policy_hash="p", tool_manifest_hash="t",
        tool_name="get_plugin_schema", arguments_json='{"name":"csv","type":"source"}',
    )
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1", decision_id="advisor:1",
        decision="CONTINUE", scope="whole_request", obligation_ids=("request",),
        obligations=(), evidence=(), reason="Inspect the missing contract.", remaining_actionable_ids=("request",),
        prescribed_action=action,
    )
    kwargs = dict(actual_advisor_call_id="advisor:1", retained_evidence={}, input_hash="i",
                  policy_hash="p", tool_manifest_hash="t",
                  permitted_read_tools=frozenset({"get_plugin_schema"}),
                  complete_required_coverage=True)
    require_bound_continuation_decision(decision, frontier=frontier, **kwargs)
    seen = replace(frontier, action_receipts=(ActionExecutionReceipt(
        continuation_action_key(action), "tool-receipt:1", "committed",
    ),))
    with pytest.raises(ContinuationProtocolError, match="already been prepared"):
        require_bound_continuation_decision(decision, frontier=seen, **kwargs)
    with pytest.raises(ContinuationProtocolError, match="mutation as a read"):
        require_bound_continuation_decision(decision, frontier=frontier,
            **{**kwargs, "permitted_read_tools": frozenset()})


def test_completed_action_history_does_not_permanently_block_whole_stop():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import record_action_receipt
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")
    for disposition in ("prepared", "dispatched", "committed"):
        frontier = record_action_receipt(
            frontier, ActionExecutionReceipt("action:1", "effect:1", disposition),
            actual_owner_receipt_id="effect:1",
        )
    stop = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1", decision_id="advisor:stop",
        decision="STOP_CONFIRMED", scope="whole_request", obligation_ids=("request",),
        obligations=(ContinuationObligationProposal(
            obligation_id="request", parent_id=None, description="Reviewed whole-request blocker",
            original_message_id="original", start=0, end=13, status="blocked",
        ),), evidence=(), reason="The current reviewed blocker covers the whole request.",
        remaining_actionable_ids=(), prescribed_action=None,
    )
    kwargs = dict(actual_advisor_call_id="advisor:stop", retained_evidence={}, input_hash="i",
                  policy_hash="p", tool_manifest_hash="t", permitted_read_tools=frozenset(),
                  complete_required_coverage=True)
    require_bound_continuation_decision(stop, frontier=frontier, **kwargs)
    with pytest.raises(ContinuationProtocolError, match="unresolved action custody"):
        require_bound_continuation_decision(
            stop, frontier=replace(frontier, action_receipts=frontier.action_receipts[:2]), **kwargs,
        )
    with pytest.raises(AuditIntegrityError):
        record_action_receipt(
            frontier, ActionExecutionReceipt("action:1", "effect:1", "refused"),
            actual_owner_receipt_id="effect:1",
        )
    with pytest.raises(AuditIntegrityError):
        record_action_receipt(
            frontier, ActionExecutionReceipt("action:1", "different-effect", "committed"),
            actual_owner_receipt_id="different-effect",
        )


def test_subset_stop_cannot_drop_still_open_whole_request():
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    decision = ContinuationAdvisorDecision(
        schema_version="composer.continuation-decision.v1", decision_id="advisor:subset",
        decision="STOP_CONFIRMED", scope="subset", obligation_ids=("request",),
        obligations=(), evidence=(), reason="One item is blocked.",
        remaining_actionable_ids=(), prescribed_action=None,
    )
    with pytest.raises(ContinuationProtocolError, match="omitted an open obligation"):
        require_bound_continuation_decision(
            decision, actual_advisor_call_id="advisor:subset",
            frontier=ContinuationFrontier((root,), (), (), (), "g"),
            retained_evidence={}, input_hash="i", policy_hash="p", tool_manifest_hash="t",
            permitted_read_tools=frozenset(), complete_required_coverage=True,
        )


def test_new_obligation_labels_do_not_change_the_host_source_identity():
    from elspeth.web.composer.advisor_output import ContinuationObligationProposal
    from elspeth.web.composer.continuation_progress import canonicalize_continuation_obligations
    root = ContinuationObligation("request", None, "whole request", "original", 0, 13)
    frontier = ContinuationFrontier((root,), (), (), (), "g")
    def proposed(label, description):
        action = ReadContinuationAction(
            kind="read_tool", obligation_ids=(label,), evidence_ids=(),
            input_hash="i", graph_hash="g", policy_hash="p", tool_manifest_hash="t",
            tool_name="get_plugin_schema", arguments_json='{"name":"csv","type":"source"}',
        )
        return ContinuationAdvisorDecision(
            schema_version="composer.continuation-decision.v1", decision_id="actual-call",
            decision="CONTINUE", scope="subset", obligation_ids=(label,),
            obligations=(ContinuationObligationProposal(
                obligation_id=label, parent_id="request", description=description,
                original_message_id="original", start=0, end=5, status="open",
            ),), evidence=(), reason="Inspect this source-bound requirement.",
            remaining_actionable_ids=("request", label), prescribed_action=action,
        )
    first = canonicalize_continuation_obligations(proposed("first-label", "Inspect the input"), frontier)
    renamed = canonicalize_continuation_obligations(proposed("new-label", "Read the input"), frontier)
    assert first.obligations[0].obligation_id == renamed.obligations[0].obligation_id
    assert first.prescribed_action is not None and renamed.prescribed_action is not None
    assert continuation_action_key(first.prescribed_action) == continuation_action_key(renamed.prescribed_action)
    assert canonicalize_continuation_obligations(first, frontier) == first


def test_duplicate_physical_evidence_cannot_create_a_new_action_premise():
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
    first = ContinuationEvidenceRef(
        receipt_id="physical-one", content_hash="f" * 64,
        original_attempt_id="attempt-one", coverage_binding="same-complete-tool-input-and-range",
    )
    duplicate = first.model_copy(update={"receipt_id": "physical-two", "original_attempt_id": "attempt-two"})
    action = ReadContinuationAction(
        kind="read_tool", obligation_ids=("request",), evidence_ids=(first.receipt_id,),
        input_hash="i", graph_hash="g", policy_hash="p", tool_manifest_hash="t",
        tool_name="get_plugin_schema", arguments_json='{"name":"csv","type":"source"}',
    )
    repeated = action.model_copy(update={"evidence_ids": (duplicate.receipt_id,)})
    assert continuation_action_key(action, retained_evidence={first.receipt_id: first}) == continuation_action_key(
        repeated, retained_evidence={duplicate.receipt_id: duplicate},
    )
```

- [ ] Run the ordinary frontier tests:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_progress.py >"$COMPOSER_LOG_DIR/c6-frontier.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c6-frontier.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  Temporarily omit the recurrence check; the repeated-action assertion must
  fail. Restore it, then obtain exit 0.
- [ ] Commit as `feat(composer): retain action frontier and owner receipts`.

### Task C6.3: Add scheduling to the existing control palette

**Files:** Modify `src/elspeth/web/composer/provider_gateway.py` at
`composer_loop_tool_definitions` and its control classification; modify
`src/elspeth/web/composer/recovery_controls.py` and
`src/elspeth/web/composer/tool_batch.py`; create
`tests/unit/web/composer/test_continuation_control.py` and
`tests/unit/web/composer/test_recovery_control_palettes.py`.

- [ ] Add this strict schema only to the eligible lifecycle-v2 primary palette,
  through the existing dialect stamping owner. Do not add it to the synchronous
  registry or the restricted recovery palette.

```python
def continuation_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, object]]:
    function: dict[str, object] = {
        "name": "checkpoint_and_continue",
        "description": "Schedule the same authorized request to continue after a durable safe checkpoint.",
        "parameters": {
            "type": "object",
            "properties": {"notes": {"type": ["string", "null"]}},
            "required": ["notes"],
            "additionalProperties": False,
        },
    }
    if dialect is ToolContractDialect.OPENAI_STRICT:
        function["strict"] = True
    return [{"type": "function", "function": function}]
```

- [ ] In `provider_gateway.py`, replace only `composer_loop_tool_definitions`
  with the following function. V1 retains its identical palette and trailing
  cache anchor; a persisted v2 phase explicitly selects the new control. Keep
  the restricted recovery palette and synchronous registry unchanged.

```python
def composer_loop_tool_definitions(
    dialect: ToolContractDialect, *, include_continuation: bool = False,
) -> list[dict[str, Any]]:
    from elspeth.web.composer.recovery_controls import (
        continuation_tool_definitions, recovery_tool_definitions,
    )
    tools = wire_tool_definitions(dialect)
    controls = recovery_tool_definitions(dialect)
    if include_continuation:
        controls = [*controls, *continuation_tool_definitions(dialect)]
    return [*tools[:-1], *controls, tools[-1]]
```

- [ ] Add this nominal parser to `recovery_controls.py`. Parse the entire batch
  before dispatching any call; notes retain the provider's actual words and
  become neither graph arguments nor a completion claim.

```python
from dataclasses import dataclass
from elspeth.contracts.json_parser import parse_json_strict


@dataclass(frozen=True, slots=True)
class ContinuationControlRequest:
    tool_call_id: str
    notes: str | None


def parse_continuation_tool_batch(batch: _AdmittedToolBatch) -> ContinuationControlRequest | None:
    if not any(call.function.name == "checkpoint_and_continue" for call in batch.calls):
        return None
    if len(batch.calls) != 1:
        raise ValueError("Continuation control must be the sole tool call in a response")
    call = batch.calls[0]
    arguments, error = parse_json_strict(call.function.arguments)
    if error is not None or type(arguments) is not dict or set(arguments) != {"notes"}:
        raise ValueError("Continuation arguments require exactly the notes field")
    notes = arguments["notes"]
    if notes is not None and type(notes) is not str:
        raise ValueError("Continuation notes must be a string or null")
    if notes is not None:
        require_no_credential_material(notes, surface="composer_continuation_notes")
    return ContinuationControlRequest(call.id, notes)
```

- [ ] Create `tests/unit/web/composer/test_continuation_control.py` with these
  parser and palette tests. The phase integration packet additionally asserts
  zero actual effects for a refused mixed batch.

```python
import pytest
from elspeth.contracts.composer_llm_audit import ToolContractDialect
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedToolBatch, _AdmittedToolCall, _AdmittedToolFunction,
)
from elspeth.web.composer.provider_gateway import composer_loop_tool_definitions
from elspeth.web.composer.recovery_controls import parse_continuation_tool_batch


def call(name, arguments, identity):
    return _AdmittedToolCall(identity, _AdmittedToolFunction(name, arguments))


def batch(*calls):
    return _AdmittedToolBatch(tuple(calls), frozenset(item.id for item in calls))


def test_continuation_is_sole_control_and_retains_exact_notes():
    control = call("checkpoint_and_continue", '{"notes":"Retain the saved review."}', "control:1")
    admitted = parse_continuation_tool_batch(batch(control))
    assert admitted.tool_call_id == "control:1"
    assert admitted.notes == "Retain the saved review."
    mutation = call("set_pipeline", "{}", "mutation:1")
    with pytest.raises(ValueError, match="sole tool call"):
        parse_continuation_tool_batch(batch(control, mutation))
    assert parse_continuation_tool_batch(batch(mutation)) is None


@pytest.mark.parametrize("arguments", ['{}', '{"notes":false}', '{"notes":null,"graph":{}}', '{"notes":null,"notes":null}'])
def test_continuation_refuses_malformed_or_extra_arguments(arguments):
    with pytest.raises(ValueError):
        parse_continuation_tool_batch(batch(call("checkpoint_and_continue", arguments, "control:1")))


def test_only_v2_primary_palette_contains_continuation_control():
    dialect = ToolContractDialect.OPENAI_STRICT
    legacy = composer_loop_tool_definitions(dialect)
    modern = composer_loop_tool_definitions(dialect, include_continuation=True)
    assert "checkpoint_and_continue" not in {item["function"]["name"] for item in legacy}
    control = next(item for item in modern if item["function"]["name"] == "checkpoint_and_continue")
    assert control["function"]["strict"] is True
    assert control["function"]["parameters"]["required"] == ["notes"]
    assert modern[-1] == legacy[-1]
```

- [ ] In the existing primary control dispatcher, reject any mixed batch before
  the first effect. A sole scheduling request stores its exact tool-use/result
  pair and notes, then uses the same authority yield path as automatic rollover.
  Its normal result is exactly:

```json
{"status":"continuation_queued","same_request":true,"successor_started":false}
```

- [ ] Treat that result as a scheduling receipt, never semantic progress or
  graph publication. If the same frontier has no pending executable action,
  call the existing Advisor owner directly rather than queue another empty
  segment. A repeated rejected control state cannot mint another segment or
  another identical Advisor call.
- [ ] Add a deterministic provider test whose first reply contains scheduling
  plus a real graph-mutating tool: assert zero dispatched mutations, exact
  mixed-batch refusal and no successor. The positive control sends scheduling
  alone after a retained action; assert one queued successor and the exact
  tool-result pair. Run:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_control.py tests/unit/web/composer/test_recovery_control_palettes.py tests/unit/web/composer/test_wire_fidelity_matrix.py >"$COMPOSER_LOG_DIR/c6-palette.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c6-palette.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Commit as `feat(composer): add provider-visible continuation control`.

### Task C6.4: Route prescribed work and every terminal proposal through its owner

**Files:** Modify `src/elspeth/web/composer/service.py`,
`advisor_abandonment.py`, `advisor_checkpoint.py`, `composition_completion.py`,
`recovery_controls.py`, `provider_quota.py` and `turn_effects.py`; create
`src/elspeth/web/composer/continuation_actions.py`; test
`tests/unit/web/composer/test_continuation_control.py`,
`tests/unit/web/composer/test_continuation_advisor.py`,
`test_advisor_terminal_publication.py`, `test_pending_review_advisor_terminal.py`.

- [ ] Create this complete pure primary-input adapter. Its input is the actual
  closed model-authored action already bound by C6.2, not a host interpretation
  of the original request. The instruction remains untrusted provider content;
  the ordinary actual-use input preparation scans the complete assembled
  conversation. The driver retains its append phase so resume cannot append
  it twice.

```python
from __future__ import annotations

from elspeth.web.composer.advisor_output import PrimaryContinuationAction
from elspeth.web.composer.continuation import AdvisorFrame, ContinuationFrameStack, LogicalRequestBinding
from elspeth.web.composer.continuation_progress import ContinuationFrontier
from elspeth.contracts.hashing import stable_hash
from pydantic import BaseModel, ConfigDict
from elspeth.web.credential_guard import require_no_credential_material


CONTINUATION_ADVISOR_INSTRUCTIONS = (
    "Return one composer.continuation-decision.v1 answer using the exact supplied response schema. "
    "Review the complete original request, current graph, chronological evidence and continuation frontier. "
    "Bind any new obligation to an exact span of the original request and preserve every other open obligation. "
    "Reuse existing obligation IDs for updates; new labels are local references that the host binds to their source span and parent. "
    "CONTINUE and CONTINUE_WITH_LIMITATION must prescribe one concrete primary_turn, read_tool or review action. "
    "A primary instruction is advice for the authoring model; only that model chooses graph mutations. "
    "Use only the supplied exact input, graph, policy and tool-manifest bindings. "
    "STOP_CONFIRMED for a subset preserves every remaining actionable obligation. "
    "Whole-request STOP_CONFIRMED requires complete required coverage and no pending review or action. "
    "Do not ask the human whether to continue. Your decision is separate from final-message publication admission."
)


class ContinuationAdvisorContext(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    question_id: str
    input_hash: str
    policy_hash: str
    tool_manifest_hash: str
    frontier: ContinuationFrontier


def continuation_advisor_question_id(stack: ContinuationFrameStack) -> str:
    top = stack.frames[-1]
    if type(top) is not AdvisorFrame:
        raise TypeError("continuation question requires its retained Advisor frame")
    return "continuation-advisor:" + stable_hash({
        "logical": stack.logical.model_dump(mode="json"),
        "review": top.review.digest, "coverage": top.coverage_binding,
    })


def continuation_premise_input_hash(
    *, logical: LogicalRequestBinding, frontier: ContinuationFrontier,
    policy_hash: str, tool_manifest_hash: str,
) -> str:
    return stable_hash({
        "logical": logical.model_dump(mode="json"),
        "graph_hash": frontier.graph_hash,
        "evidence": sorted({(item.source_kind, item.content_hash, item.coverage_binding)
                            for item in frontier.evidence if item.source_kind != "provider"}),
        "policy_hash": policy_hash, "tool_manifest_hash": tool_manifest_hash,
    })


def continuation_primary_message(action: PrimaryContinuationAction) -> dict[str, str]:
    if type(action) is not PrimaryContinuationAction:
        raise TypeError("primary continuation requires its closed Advisor action")
    require_no_credential_material(
        action.instruction, surface="composer_continuation_primary_instruction",
    )
    return {"role": "user", "content": action.instruction}
```

- [ ] Add these literal tests to `test_continuation_control.py`. No synthetic
  system trust or server-authored graph is introduced by this adapter.

```python
import pytest

from elspeth.web.composer.advisor_output import PrimaryContinuationAction
from elspeth.web.composer.continuation_actions import continuation_primary_message


def test_primary_action_keeps_exact_model_instruction_as_untrusted_content():
    action = PrimaryContinuationAction(
        kind="primary_turn", obligation_ids=("request",), evidence_ids=(),
        input_hash="input", graph_hash="graph", policy_hash="policy",
        tool_manifest_hash="tools", instruction="Inspect the complete schema, then choose the next change.",
    )
    assert continuation_primary_message(action) == {
        "role": "user", "content": action.instruction,
    }


def test_primary_action_does_not_accept_a_dictionary_as_model_authority():
    with pytest.raises(TypeError, match="closed Advisor action"):
        continuation_primary_message({"instruction": "set_pipeline"})
```

- [ ] Preserve C5.3's exact `ProviderCallCustody.continuation_binding` property
  and use it below. It is introduced with its actual field before the C5.8
  gateway consumer; no second property is added here.

- [ ] Apply T1/T1.1 in the
  [trusted-producer companion](2026-10-21-composer-continuation-producers.md)
  exactly once. Its complete `_build_prompt_messages` and
  `_prepare_provider_request` bodies own the fixed current instruction,
  complete untrusted context, closed response format, safe physical-input
  digest and newly minted guard metadata. Extend the existing audit-body
  keyword arguments with the exact two declarations below; T1.1 provides the
  full fresh-preparation/comparison code that consumes them. Import
  `PreparedAdvisorRequest` from `advisor_request.py`. Do not duplicate the
  removed inline prompt/options assembly after this extraction.

```python-signature
continuation: ContinuationAdvisorContext | None = None,
prepared: PreparedAdvisorRequest | None = None,
```

  Replace its existing enclosing `response = await asyncio.wait_for(...)`
  expression with this complete block. C5's actual gateway enforces separate
  preparation/guard/quota/physical/retention stages for durable custody; a v1
  total timeout must not cancel that whole sequence midway through retention.

```python
completion_call = provider_gateway._litellm_acompletion(
    provider_custody=provider_custody,
    on_provider_dispatch=on_provider_dispatch,
    prompt_guard_mount=self._prompt_guard_mount,
    prompt_guard_mode=self._settings.composer_prompt_guard_mode,
    prompt_guard_role="advisor",
    prompt_guard_trusted_message_indexes=trusted_indexes,
    prompt_guard_content_parts=guard_evidence.content_parts,
    prepared_advisor_content=tuple(
        PreparedAdvisorContent(index, message["content"])
        for index, message in enumerate(messages)
        if message["role"] == "user"
    ),
    **kwargs,
)
if provider_custody is not None and provider_custody.continuation_binding is not None:
    response = await completion_call
else:
    response = await asyncio.wait_for(completion_call, timeout=effective_timeout)
```

  After the existing complete
  `_require_no_credential_material_in_completion_fields(...)` call, replace
  its current success-assignment/metadata/return block with this complete
  block. Import `RetainedAdvisorResultMetadata` from `continuation_codec`.
  It retains the exact actual body's result metadata, including measured
  latency, before any cold owner can need to reconstruct `_AdvisorCallSuccess`:

```python
usage = token_usage_from_response(response)
metadata = {
    "model": safe_response_model(response) or advisor_model,
    "prompt_tokens": usage.prompt_tokens,
    "completion_tokens": usage.completion_tokens,
    "cached_prompt_tokens": usage.cached_prompt_tokens,
    "latency_ms": (time.monotonic_ns() - started_ns) // 1_000_000,
}
if provider_custody is not None and provider_custody.continuation_binding is not None:
    await provider_custody.retain_continuation_text_response(
        content=raw_content, provider_metadata=response_metadata,
        advisor_result_metadata=RetainedAdvisorResultMetadata.model_validate(metadata),
    )
guidance = raw_content
status = ComposerLLMCallStatus.SUCCESS
return guidance, metadata
```

  Keep all existing exception classification, full finally audit construction,
  original exception identity and physical settlement. Durable custody selects
  staged clocks and retention for manual, EARLY and deterministic END calls.
  Only deterministic END receives `ContinuationAdvisorContext`; manual/EARLY
  retain their current prompt/schema. Callers without durable custody retain
  their existing enclosing timeout and byte construction.
  Complete response encoding and credential admission run inside this method's
  one V stage; the caller does not pre-encode a chunk outside that allowance.

- [ ] Add this complete fresh-call adapter to `AdvisorCheckpointOwner`.
  Import `AdvisorPublicationReview`, `ContinuationAdvisorDecision`,
  `parse_continuation_advisor_decision` and `ContinuationProtocolError` from
  their existing/above owner modules. The driver supplies a fresh
  `TurnEvidence.review_candidate(...)` result after restoring actual current
  state and complete evidence. The actual physical ID is read from custody and
  assigned to the persisted decision after strict answer parsing. The model
  response schema excludes that identity. The separately retained question ID
  is part of the exact prepared input, known before provider admission.

```python
@dataclass(frozen=True, slots=True)
class ContinuationAdvisorOutcome:
    decision: ContinuationAdvisorDecision
    actual_call_id: str


async def _call_continuation_advisor(
    self, *, context: ContinuationAdvisorContext, state: CompositionState,
    publication_review: AdvisorPublicationReview, recorder: BufferingRecorder,
    custody: ProviderCallCustody, timeout: float, prepared: PreparedAdvisorRequest,
) -> ContinuationAdvisorOutcome:
    if type(context) is not ContinuationAdvisorContext or type(publication_review) is not AdvisorPublicationReview:
        raise TypeError("continuation advice requires actual owned context and publication evidence")
    if publication_review.transcript is None:
        raise AuditIntegrityError("continuation advice lacks the complete chronological transcript")
    if type(custody) is not ProviderCallCustody or custody.continuation_binding is None:
        raise AuditIntegrityError("continuation advice requires prepared durable custody")
    arguments = self._build_checkpoint_arguments(
        phase="end", state=state, publication_review=publication_review,
        advisor_transcript=publication_review.transcript,
    )
    async with provider_call_scope(custody):
        guidance, _metadata = await self._call_advisor_with_audit_body(
            arguments, recorder=recorder, timeout=timeout, structured_output=True,
            provider_custody=custody, continuation=context, prepared=prepared,
        )
        call = custody.call
        if call is None or call.call_id is None or call.status is not ComposerLLMCallStatus.SUCCESS:
            raise AuditIntegrityError("continuation advice lacks its actual successful physical audit")
        decision = parse_continuation_advisor_decision(guidance, actual_advisor_call_id=call.call_id)
        if decision is None:
            raise ContinuationProtocolError("Advisor response violates its closed continuation answer schema")
        outcome = ContinuationAdvisorOutcome(decision, call.call_id)
    return outcome
```

  `ContinuationAdvisorOutcome` is module-level; the async method is indented
  on the owner class. Cold replay does not call this method: it decodes F1's
  actual retained completion and obtains its physical call ID from the SQL
  effect/attempt/ledger join, passes it to the same strict answer parser, and
  applies C6.2 against the current complete evidence bindings. The actual
  response/input join also verifies the retained question; a model string does
  not establish that join.
  `ContinuationAdvisorContext.input_hash` is the semantic premise produced by
  `continuation_premise_input_hash`, not a hash of a request containing that
  very field. The physical `input_sha256` separately hashes the complete exact
  prepared provider wire, including this context. A segment/fence/timer change
  alone changes neither semantic premise nor action freshness.
  The premise also excludes physical receipt IDs, action-history growth,
  obligation labels/descriptions and observation counters. Full history still
  reaches the Advisor and remains retained for audit. The actual normalized
  evidence content/coverage and authored graph hash determine premise identity;
  the graph hash comes from `turn_effects.authored_state_hash(current_state)`.
  Action keys normalize evidence references to those same content/coverage
  values, so a duplicate physical receipt cannot manufacture a fresh action.

- [ ] Create `tests/unit/web/composer/test_continuation_advisor.py` with these
  actual owner-boundary controls. The positive complete physical-call and
  cold-response cases run through the driver/SQL packet after its fixture is
  present; these controls separately prove that nominal context cannot bypass
  durable custody and that the full frontier is retained without clipping.

```python
import json
from dataclasses import replace

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import provider_gateway
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation import LogicalRequestBinding
from elspeth.web.composer.continuation_actions import ContinuationAdvisorContext, continuation_premise_input_hash
from elspeth.web.composer.continuation_progress import ActionExecutionReceipt, initial_continuation_frontier
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
from elspeth.web.composer.turn_effects import TurnEvidence
from tests.unit.web.composer import test_advisor_checkpoint as checkpoint_fixtures

make_service = checkpoint_fixtures.make_service
simple_state = checkpoint_fixtures.simple_state


def advisor_context():
    return ContinuationAdvisorContext(
        question_id="retained-question:1", input_hash="premise", policy_hash="policy",
        tool_manifest_hash="manifest", frontier=initial_continuation_frontier(
            request="Inspect the complete original requirement.",
            original_message_id="original-message", graph_hash="graph",
        ),
    )


def test_advisor_context_keeps_the_complete_frontier_and_question():
    context = advisor_context()
    encoded = context.model_dump_json()
    restored = ContinuationAdvisorContext.model_validate_json(encoded)
    assert restored == context
    payload = json.loads(encoded)
    assert payload["question_id"] == "retained-question:1"
    assert payload["frontier"]["obligations"][0]["description"] == "Inspect the complete original requirement."
    del payload["frontier"]["action_receipts"]
    with pytest.raises(ValueError):
        ContinuationAdvisorContext.model_validate_json(json.dumps(payload))


def test_physical_history_and_advice_labels_do_not_create_a_new_semantic_premise():
    logical = LogicalRequestBinding(
        session_id="session", operation_id="operation", original_user_message_id="original-message",
        request_hash="request-hash", kind="compose_message", base_state_id=None,
    )
    frontier = advisor_context().frontier
    bookkeeping = replace(
        frontier, obligations=(replace(frontier.obligations[0], description="Restated requirement"),),
        action_receipts=(ActionExecutionReceipt("action", "physical-owner", "committed"),),
    )
    def premise(value):
        return continuation_premise_input_hash(
            logical=logical, frontier=value, policy_hash="policy", tool_manifest_hash="manifest",
        )
    assert premise(frontier) == premise(bookkeeping)
    actual_evidence = ContinuationEvidenceRef(
        receipt_id="actual-owner-result", content_hash="f" * 64,
        original_attempt_id="actual-attempt", coverage_binding="complete-registered-input-and-range",
    )
    assert premise(frontier) != premise(replace(frontier, evidence=(actual_evidence,)))


@pytest.mark.asyncio
async def test_continuation_context_without_durable_custody_refuses_before_provider(
    make_service, simple_state, monkeypatch,
):
    service = make_service()
    request = "Inspect the complete original requirement."
    evidence = TurnEvidence(
        session_id=None, session_operation_context=None, request_id="request",
        request=request, initial_state_id=None, initial_state=simple_state,
    )
    review = evidence.review_candidate(candidate="Candidate.", request=request, current_state=simple_state)
    owner = service._advisor_checkpoint
    arguments = owner._build_checkpoint_arguments(
        phase="end", state=simple_state, publication_review=review,
        advisor_transcript=review.transcript,
    )
    calls = []
    async def forbidden(**kwargs):
        calls.append(kwargs)
        raise AssertionError("provider dispatch must not occur")
    monkeypatch.setattr(provider_gateway, "_litellm_acompletion", forbidden)
    with pytest.raises(AuditIntegrityError, match="durable physical custody"):
        await owner._call_advisor_with_audit_body(
            arguments, recorder=BufferingRecorder(), structured_output=True,
            continuation=advisor_context(),
        )
    assert calls == []
```

  Behavioral RED: remove only the durable-custody precondition in the body
  while retaining the matching precondition in the new outer adapter; invoke
  this direct-body negative control and require the forbidden transport
  assertion to fail. Restore the check and run the module with native exit 0.

- [ ] For `primary_turn`, append only the Advisor's exact model-authored
  instruction through the existing role/admission owner and make a real primary
  provider call. For `read_tool`, validate current live registry metadata is
  read-only and dispatch its exact admitted arguments through normal audit and
  result owners. For `review`, call the existing bound review owner with the
  exact candidate/evidence. The host never supplies graph/mutation arguments.
- [ ] Persist `prepared`, `dispatched`, outcome and actual owner receipt in the
  same transition transactions as C3/C4b. Stop/Revert current authority checks
  still run immediately before dispatch and effect commit. Serialize the
  frontier in `semantic_frontier`; restoration never clears action history.
- [ ] Send ordinary no-tools, restricted recovery and attempted early terminal
  proposals through the v2 scoped Advisor decision and separate END/publication
  admission. Keep authenticated human Stop/Revert immediate. A subset blocker
  preserves other actionable obligations; a whole stop needs complete required
  coverage, no pending action and a separately admitted final message.
- [ ] Add controlled cases for pure automatic rollover, graph oscillation,
  useful negative discovery, notes-only continuation, unexecuted prescribed
  action, repeated advice, subset blocker with actionable remainder, whole
  blocker and missing review. Assert actual provider ownership on each graph
  transition, not merely a total call count for the request.
- [ ] Run:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_control.py tests/unit/web/composer/test_continuation_progress.py tests/unit/web/composer/test_continuation_advisor.py tests/unit/web/composer/test_graph_recovery.py tests/unit/web/composer/test_advisor_terminal_publication.py tests/unit/web/composer/test_pending_review_advisor_terminal.py tests/unit/web/composer/test_wire_fidelity_matrix.py >"$COMPOSER_LOG_DIR/c6-integration.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c6-integration.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Commit as `feat(composer): continue reviewed work without terminal bypass`.

**Exit C6:** the provider chooses every authoring action; actual receipts prove
execution. No lease/timer/label/note/graph churn manufactures semantic progress,
and a scheduling request cannot become a user-visible claim of completed work.

### Task C6.5: Derive semantic evidence from actual retained SQL owners

**Files:** Modify `src/elspeth/web/sessions/continuation.py` and
`src/elspeth/web/coordination/composer_operation_authority.py`; extend
`tests/unit/web/composer/test_continuation_progress.py` and the real-driver
test packet. Run this after the actual C3/C5/F7 owners exist and before the
driver constructs its first deterministic END context.

- [ ] Add the complete resolver below to `sessions/continuation.py`. Its
  existing `Connection`, `select`, `AuditIntegrityError`, `canonical_json`,
  `stable_hash`, `require_snapshot_cas`, `require_continuation_mutation`,
  `read_chunk`, `decode_continuation_snapshot` and historical-reference
  validator imports/functions already exist in C3/C5. The explicit aliases
  below avoid confusing segment-attempt history with provider attempts.

```python
from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
from elspeth.web.composer.continuation import PrivateChunkRef
from elspeth.web.composer.continuation_codec import RetainedCompletion, verify_private_chunk
from elspeth.web.composer.turn_effects import authored_state_hash
from elspeth.web.sessions.converters import state_from_record, state_record_from_row
from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt, decode_read_owner_result
from elspeth.web.sessions.models import (
    composer_async_operations_table as fact_jobs,
    composer_continuation_chunks_table as fact_chunks,
    composer_continuation_effects_table as fact_effects,
    composer_segment_attempts_table as fact_creators,
    composition_states_table as fact_states,
    quota_provider_attempts_table as fact_provider_attempts,
    sessions_table as fact_sessions,
    token_usage_ledger_table as fact_ledger,
)
from elspeth.web.sessions.time_normalization import restore_utc


def resolve_frontier_evidence_on_connection(
    conn: Connection, running: ComposerOperationRunning, *,
    expected_checkpoint_id: str, expected_cursor: int,
) -> tuple[ContinuationEvidenceRef, ...]:
    require_continuation_mutation(conn, running.session_operation_context,
                                  allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    stored = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
                                  expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
    stack = decode_continuation_snapshot(stored.payload_json)
    parent = conn.execute(select(fact_jobs).where(
        fact_jobs.c.session_id == sid, fact_jobs.c.operation_id == operation_id,
    )).one()
    principal = conn.execute(select(fact_sessions.c.user_id, fact_sessions.c.archived_at).where(
        fact_sessions.c.id == sid,
    )).one()
    if (parent.lifecycle_version != 2 or principal.archived_at is not None
        or parent.actor_user_id != principal.user_id
        or (stack.logical.session_id, stack.logical.operation_id, stack.logical.request_hash,
            stack.logical.original_user_message_id, stack.logical.base_state_id)
        != (sid, operation_id, parent.request_hash, parent.user_message_id, parent.base_state_id)):
        raise AuditIntegrityError("semantic evidence changed its current principal or immutable logical parent")
    if stack.pending_owner_results or stack.pending_settlement_ids:
        raise AuditIntegrityError("semantic evidence requires applied and settled actual owners")
    values: list[ContinuationEvidenceRef] = []

    def graph_fact(receipt_id: str, graph_hash: str, original_attempt_id: str | None) -> None:
        values.append(ContinuationEvidenceRef(
            receipt_id=receipt_id, source_kind="graph", content_hash=graph_hash,
            original_attempt_id=original_attempt_id,
            coverage_binding=stable_hash({"kind": "complete_authored_graph", "graph_hash": graph_hash}),
        ))

    if stack.logical.base_state_id is not None:
        base = conn.execute(select(fact_states).where(
            fact_states.c.session_id == sid, fact_states.c.id == stack.logical.base_state_id,
        )).one()
        graph_fact("base:" + stack.logical.base_state_id,
                   authored_state_hash(state_from_record(state_record_from_row(base))), None)

    entries = {entry.effect_id: entry for entry in stack.effect_frontier}
    rows = conn.execute(select(fact_effects).where(
        fact_effects.c.session_id == sid, fact_effects.c.operation_id == operation_id,
    ).order_by(fact_effects.c.effect_key)).all()
    if set(entries) != {row.effect_key for row in rows}:
        raise AuditIntegrityError("semantic evidence lost an actual selected-root effect")
    for effect in rows:
        entry = entries[effect.effect_key]
        if (entry.disposition, entry.receipt_id) != (effect.disposition, effect.receipt_key):
            raise AuditIntegrityError("semantic evidence changed its actual effect disposition or receipt")
        if effect.disposition != "committed":
            continue
        creator = conn.execute(select(fact_creators).where(
            fact_creators.c.session_id == sid, fact_creators.c.operation_id == operation_id,
            fact_creators.c.sequence == effect.creator_sequence,
            fact_creators.c.generation == effect.creator_generation,
        )).one()
        if effect.owner in ("session_sql", "read"):
            if effect.committer_sequence is None or effect.committer_generation is None:
                raise AuditIntegrityError("semantic SQL/read evidence lacks its actual committing owner")
            creator = conn.execute(select(fact_creators).where(
                fact_creators.c.session_id == sid, fact_creators.c.operation_id == operation_id,
                fact_creators.c.sequence == effect.committer_sequence,
                fact_creators.c.generation == effect.committer_generation,
            )).one()
        row = conn.execute(select(fact_chunks).where(
            fact_chunks.c.session_id == sid, fact_chunks.c.operation_id == operation_id,
            fact_chunks.c.chunk_key == effect.result_chunk_key,
        )).one()
        if effect.owner == "session_sql" and effect.effect_key.startswith("action:review:"):
            import json
            from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, advisor_checkpoint_pass_audit_envelope
            from elspeth.web.composer.advisor_output import ReviewContinuationAction
            from elspeth.web.composer.continuation_progress import continuation_action_key, decode_prescribed_continuation_action
            from elspeth.web.sessions.models import chat_messages_table
            if row.codec != "composer.advisor-pass.v1" or effect.action_json is None:
                raise AuditIntegrityError("semantic review receipt has an unfamiliar actual result variant")
            canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
                                   key=effect.result_chunk_key).decode("utf-8", errors="strict")
            payload, error = parse_json_strict(canonical)
            if error is not None or type(payload) is not dict or set(payload) != {
                    "pass", "audit_message_id", "physical_call_id", "provider_effect_key"}:
                raise AuditIntegrityError("semantic review lost its closed actual pass/physical envelope")
            record = AdvisorCheckpointPassRecord.from_dict(payload["pass"])
            action = decode_prescribed_continuation_action(effect.action_json)
            if type(action) is not ReviewContinuationAction or stack.semantic_frontier is None:
                raise AuditIntegrityError("semantic review receipt changed its actual prescribed owner")
            frontier_json = read_chunk(conn, session_id=sid, operation_id=operation_id,
                key=f"value:{stack.semantic_frontier.codec}:{stack.semantic_frontier.digest}").decode("utf-8", errors="strict")
            from elspeth.web.composer.continuation_progress import decode_continuation_frontier
            frontier = decode_continuation_frontier(frontier_json)
            expected_key = "action:review:" + continuation_action_key(action,
                retained_evidence={item.receipt_id: item for item in frontier.evidence})
            audit = conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == sid,
                chat_messages_table.c.id == effect.receipt_key)).one()
            physical_effect = conn.execute(select(fact_effects).where(fact_effects.c.session_id == sid,
                fact_effects.c.operation_id == operation_id,
                fact_effects.c.effect_key == payload["provider_effect_key"])).one()
            envelope = advisor_checkpoint_pass_audit_envelope(record)
            if (effect.effect_key != expected_key or payload["audit_message_id"] != effect.receipt_key
                or (audit.role, audit.writer_principal, audit.content, audit.tool_calls) != (
                    "audit", "compose_loop", json.dumps(envelope, sort_keys=True), [envelope])
                or (physical_effect.owner, physical_effect.disposition, physical_effect.provider_attempt_id) != (
                    "provider", "committed", payload["physical_call_id"])
                or not physical_effect.effect_key.startswith("provider:1:") or record.phase != "end"):
                raise AuditIntegrityError("semantic review result contradicts actual pass/physical SQL custody")
            continue
        reference = PrivateChunkRef(codec=row.codec, digest=row.sha256, byte_length=row.byte_length)
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
                               key=effect.result_chunk_key).decode("utf-8", errors="strict")
        verify_private_chunk(reference, canonical)
        payload, error = parse_json_strict(canonical)
        if error is not None or type(payload) is not dict:
            raise AuditIntegrityError("actual semantic evidence result is not a complete owned object")
        if effect.owner == "session_sql":
            if payload.get("kind") != "persisted_turn":
                from elspeth.web.sessions.continuation_owner_results import decode_proposal_owner_result, decode_interpretation_owner_result
                if row.codec != "composer.owner-result.v1":
                    raise AuditIntegrityError("semantic SQL result has an unfamiliar owned codec")
                if "tool_name" in payload and "arguments_json" in payload:
                    proposal = decode_proposal_owner_result(canonical)
                    owned_identity = (str(proposal.session_id), str(proposal.id))
                elif "interpretation_source" in payload and "affected_node_id" in payload:
                    interpretation = decode_interpretation_owner_result(canonical)
                    owned_identity = (str(interpretation.session_id), str(interpretation.id))
                else:
                    raise AuditIntegrityError("semantic SQL result has an unfamiliar actual owner variant")
                if owned_identity != (sid, effect.receipt_key):
                    raise AuditIntegrityError("semantic non-graph SQL result changed its actual session/receipt")
                continue
            receipt = PersistedComposerTurnReceipt.model_validate_json(canonical)
            if receipt.effect_key != effect.effect_key or receipt.input_sha256 != effect.input_sha256:
                raise AuditIntegrityError("semantic graph result changed its actual P4 binding")
            if receipt.checkpoint_disposition != "committed":
                continue
            if receipt.authored_state_hash is None:
                raise AuditIntegrityError("semantic graph result lacks an actual authored hash")
            validate_historical_effect_reference(
                conn, session_id=running.claim.session_id, operation_id=operation_id,
                effect_key=effect.effect_key, expected_input_sha256=effect.input_sha256,
                expected_creator_operation_id=receipt.creator_operation_id,
                expected_receipt_id=str(receipt.assistant_id),
                expected_inserted_state_ids=receipt.inserted_state_ids,
                expected_authored_state_hash=receipt.authored_state_hash,
            )
            graph_fact(effect.effect_key, receipt.authored_state_hash, creator.session_operation_id)
        elif effect.owner == "read":
            read = decode_read_owner_result(canonical)
            envelope, envelope_error = parse_json_strict(read.response_json)
            arguments, argument_error = parse_json_strict(read.arguments_json)
            if envelope_error is not None or type(envelope) is not dict or argument_error is not None:
                raise AuditIntegrityError("semantic read lost its complete selected response or arguments")
            # ToolResult.to_dict's exact root version is execution bookkeeping.
            # Every other response field and every selected byte/range survives.
            content = {key: value for key, value in envelope.items() if key != "version"}
            versions = [version.model_dump(mode="json") for version in read.blob_versions]
            values.append(ContinuationEvidenceRef(
                receipt_id=effect.effect_key, source_kind="read", content_hash=stable_hash(content),
                original_attempt_id=creator.session_operation_id,
                coverage_binding=stable_hash({"kind": "actual_read_delivery", "tool": read.tool_name,
                                              "arguments": arguments, "immutable_sources": versions,
                                              "complete_selected_response": content}),
            ))
        elif effect.owner == "provider":
            parts = effect.effect_key.split(":")
            if (len(parts) != 3 or parts[0] != "provider" or not parts[1].isdigit()
                or not parts[2].isdigit() or effect.effect_key != f"provider:{int(parts[1])}:{int(parts[2])}"):
                raise AuditIntegrityError("semantic provider result has a noncanonical family/ordinal")
            if int(parts[1]) not in (0, 1):
                continue  # Title/planner bookkeeping is not semantic evidence.
            if reference.codec != "composer.response.v1":
                raise AuditIntegrityError("semantic provider result lacks its actual complete response")
            response = RetainedCompletion.model_validate_json(canonical)
            physical = conn.execute(select(fact_creators).where(
                fact_creators.c.session_id == sid, fact_creators.c.operation_id == operation_id,
                fact_creators.c.sequence == effect.physical_sequence,
                fact_creators.c.generation == effect.physical_generation,
            )).one()
            attempt = conn.execute(select(fact_provider_attempts).where(
                fact_provider_attempts.c.attempt_id == effect.provider_attempt_id,
            )).one()
            ledger = conn.execute(select(fact_ledger).where(fact_ledger.c.entry_id == attempt.ledger_entry_id)).one()
            usage = response.metadata.usage
            if (attempt.settled_at is None or attempt.source != "composer" or attempt.run_id is not None
                or (attempt.session_id, attempt.operation_id, attempt.operation_epoch, attempt.lease_token)
                != (sid, physical.session_operation_id, physical.session_operation_epoch,
                    physical.session_operation_lease_token)
                or (ledger.identity_id, ledger.session_id, ledger.source, ledger.run_id)
                != (attempt.identity_id, sid, attempt.source, None)
                or (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens)
                != (usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens)
                or effect.response_finished_at is None
                or restore_utc(ledger.recorded_at) != restore_utc(effect.response_finished_at)
                or ledger.model != (response.metadata.model_returned
                                    if response.metadata.model_returned is not None else effect.requested_model)):
                raise AuditIntegrityError("semantic provider result contradicts its actual physical financial owner")
            content = {"content": response.content,
                       "calls": [{"name": call.name, "arguments": call.arguments} for call in response.calls],
                       "reasoning_content": response.metadata.reasoning_content,
                       "reasoning_details": response.metadata.reasoning_details,
                       "thinking_blocks": response.metadata.thinking_blocks}
            values.append(ContinuationEvidenceRef(
                receipt_id=effect.effect_key, source_kind="provider", content_hash=stable_hash(content),
                original_attempt_id=attempt.attempt_id,
                coverage_binding=stable_hash({"kind": "complete_admitted_provider_response",
                                              "input_sha256": effect.input_sha256, "content": content}),
            ))
        else:
            raise AuditIntegrityError("semantic evidence encountered an unsupported committed owner")
    if len({value.receipt_id for value in values}) != len(values):
        raise AuditIntegrityError("actual semantic evidence receipt is not unique")
    return tuple(sorted(values, key=lambda value: value.receipt_id))
```

- [ ] Add this exact synchronous method to `ComposerAsyncOperationAuthority`;
  import `resolve_frontier_evidence_on_connection` from `sessions.continuation`.
  The driver calls it through a reserved `RETAINED_OPERATION_READ_SQL` /
  `RETAINED_OPERATION_READ_PROJECTION` pair and preserves deferred cancellation,
  using F7.10's actual read/projection pattern. It is the same authority instance
  passed as required `operation_authority` to `advance_continuation` by C4.

```python
def resolve_continuation_frontier_evidence(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> tuple[ContinuationEvidenceRef, ...]:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return resolve_frontier_evidence_on_connection(
            conn, running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
        )
```

The resolver performs no provider call, settlement, new read dispatch, guard
decision or live-witness minting. It proves historical content/receipt identity
under current read authority. Immutable-source history remains history; every
new read/use still passes current custody and full-original inspection.
The driver replaces the frontier evidence tuple only with these actual values
before constructing END input, and persists that updated frontier with its
complete selected-root CAS. Model-supplied evidence is checked against this
exact map. `receipt_id` is the actual effect key, or `base:<state_id>` for a
non-null admitted base; it is not generally the effect's receipt-key column.
Graph hashes are authored-state hashes, reads hash the complete selected content
and actual source coverage, and providers hash actual admitted response fields.
None is inferred to equal a chunk digest.

`source_kind` is part of the closed reference and must equal the current
resolver's value. Provider observations remain in the complete Advisor context
and audit, but do not change the semantic premise/action freshness merely by
producing another piece of advice. Actual graph/read/validation content and
coverage determine novelty; the Advisor still judges their semantic relevance.

- [ ] Add this complete premise control to `test_continuation_advisor.py`.
  `advisor_context()` is the literal C6.4 helper in that same module.

```python
def test_another_provider_observation_does_not_manufacture_a_fresh_semantic_premise():
    from dataclasses import replace
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef
    from elspeth.web.composer.continuation import LogicalRequestBinding
    from elspeth.web.composer.continuation_actions import continuation_premise_input_hash
    logical = LogicalRequestBinding(session_id="s", operation_id="o", original_user_message_id="u",
                                   request_hash="r", kind="compose_message", base_state_id=None)
    frontier = advisor_context().frontier
    observation = ContinuationEvidenceRef(receipt_id="provider:1:2", source_kind="provider",
        content_hash="d" * 64, original_attempt_id="actual-call", coverage_binding="complete-response")
    def premise(value):
        return continuation_premise_input_hash(logical=logical, frontier=value,
                                              policy_hash="policy", tool_manifest_hash="tools")
    assert premise(frontier) == premise(replace(frontier, evidence=(*frontier.evidence, observation)))
    actual_read = observation.model_copy(update={"receipt_id": "actual-read", "source_kind": "read"})
    assert premise(frontier) != premise(replace(frontier, evidence=(*frontier.evidence, actual_read)))


def test_review_action_recurrence_uses_actual_candidate_and_evidence_content():
    from elspeth.web.composer.advisor_output import ContinuationEvidenceRef, ReviewContinuationAction
    from elspeth.web.composer.continuation_progress import continuation_action_key
    actual = ContinuationEvidenceRef(receipt_id="actual-read", source_kind="read", content_hash="a" * 64,
        original_attempt_id="actual-physical-owner", coverage_binding="complete-selected-content")
    action = ReviewContinuationAction(kind="review", obligation_ids=("request",), evidence_ids=(actual.receipt_id,),
        input_hash="input", graph_hash="graph", policy_hash="policy", tool_manifest_hash="manifest",
        candidate_ref="candidate:" + "b" * 64, evidence_binding="physical-review-binding-one")
    evidence = {actual.receipt_id: actual}
    key = continuation_action_key(action, retained_evidence=evidence)
    assert key == continuation_action_key(action.model_copy(update={"evidence_binding": "physical-review-binding-two"}), retained_evidence=evidence)
    assert key != continuation_action_key(action.model_copy(update={"candidate_ref": "candidate:" + "c" * 64}), retained_evidence=evidence)
    changed = actual.model_copy(update={"content_hash": "d" * 64})
    assert key != continuation_action_key(action, retained_evidence={changed.receipt_id: changed})
```

- [ ] In the real-driver packet, observe the actual resolver through a passthrough
  spy, require final frontier references equal its current returned tuple, then
  change an actual P4 graph binding, read chunk byte and provider ledger identity
  separately. Each must raise at this real SQL owner before another SDK request;
  the untouched source is the positive control. A model changing `source_kind`
  or content/coverage for an existing receipt must fail C6's exact-map admission.
  The driver-test companion supplies the complete store/cut/assertion code.
- [ ] Run the C6 progress/Advisor and actual driver packets with native exit 0;
  commit only the named resolver/model/tests as
  `feat(composer): resolve continuation evidence from actual retained owners`.

## Q — Qualify the combined product and hand it back for delivery

### Task Q.1: Check the composed seams with the ordinary repository runners

**Files:** Tests created by the preceding packets; existing
`src/elspeth/web/frontend/package.json`, `scripts/check_contracts.py`,
`tests/unit/web/test_sessions_composer_attribute_contracts.py` and
`scripts/full-suite-gate.sh`. This task adds no runner or controller.

- [ ] Confirm the P0 tested-source prerequisite. Check actual resource use and
  give .9 priority only at real contention; do not reserve idle CPU or introduce
  a blanket four-worker/serial limit. Record integration HEAD/tree and status.
  Pause edits to the
  measured production/test inputs until each check exits; source that changed
  during a check requires that affected check again. Do not recreate the old
  controller's profiles, markers, lifecycle census or proof chains.
- [ ] Execute the complete [real-driver equivalence packet](2026-10-21-composer-continuation-driver-tests.md)
  through its actual store, selected-boundary clone, SDK and process-death
  controls. Its named missing scenario or unreachable cut remains a failure
  of completeness, never a silent skip. Then run this cross-component packet
  after its files exist:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_frames.py tests/unit/web/composer/test_continuation_codec.py tests/unit/web/composer/test_continuation_progress.py tests/unit/web/composer/test_continuation_control.py tests/unit/web/composer/test_continuation_advisor.py tests/unit/web/sessions/test_continuation_cold_restore.py tests/unit/web/sessions/test_composer_continuation_recovery.py tests/unit/web/sessions/test_run_composer_turn.py tests/unit/web/composer/test_provider_quota_end_to_end.py >"$COMPOSER_LOG_DIR/combined-continuation.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/combined-continuation.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  Require actual equivalence of the uninterrupted and cold-restored executions,
  one original ingress row, one committed result per stable effect, cumulative
  physical accounting, current-fence Stop/Revert, both-role fresh composition
  validation and final publication. A transport ACK or mocked final result is
  insufficient. Each packet's negative cases retain their explicit expected
  refusals; missing collection/imports are failures of the check.
- [ ] Run the relevant whole-tree contracts after the composed input changes:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m scripts.check_contracts >"$COMPOSER_LOG_DIR/contracts.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/contracts.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/web/composer/test_wire_fidelity_matrix.py >"$COMPOSER_LOG_DIR/attribute-wire.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/attribute-wire.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

  Update a census only through its canonical producer when the owned code
  legitimately changes its scanned set. Preserve exact wire/template tests,
  nominal trust-domain boundaries and the key-free signing seam. A lint
  suppression, forged signature or broad weakening is not a repair.
- [ ] Run the normal frontend dependency checks and test suite, then typecheck,
  ESLint and build, each serially. `npm test` already runs `test:dependencies`;
  do not install packages or recreate a dependency checker.

```bash
cd "$COMPOSER_WORKTREE" && npm --prefix src/elspeth/web/frontend test >"$COMPOSER_LOG_DIR/frontend-tests.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/frontend-tests.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
cd "$COMPOSER_WORKTREE" && npm --prefix src/elspeth/web/frontend run typecheck >"$COMPOSER_LOG_DIR/frontend-types.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/frontend-types.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
cd "$COMPOSER_WORKTREE" && npm --prefix src/elspeth/web/frontend run lint >"$COMPOSER_LOG_DIR/frontend-lint.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/frontend-lint.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
cd "$COMPOSER_WORKTREE" && npm --prefix src/elspeth/web/frontend run build >"$COMPOSER_LOG_DIR/frontend-build.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/frontend-build.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

**Exit:** the integrated source has actual native results, and the final frontend
assets/types belong to that source. The earlier 131-test scoped pass remains
attributed to its own source and is not relabeled as this combined result.

### Task Q.2: Run the established frozen-tree integration gate

**Files:** Existing `scripts/full-suite-gate.sh`, CI configuration and the final
owned source; results remain private lane logs, not committed plan manifests.

- [ ] Agree the shared host's test capacity with the current owner. Use only one
  broad suite on the host. Read the canonical script's worker cap and existing
  CI commands; no independent parallel suite is launched by a child agent.
- [ ] Run the canonical repository gate from the owned integration checkout:

```bash
cd "$COMPOSER_WORKTREE" && scripts/full-suite-gate.sh --execute --detach --stages ruff,mypy,contracts,lints,pytest,testcontainer --root "$COMPOSER_WORKTREE" --log-dir "$COMPOSER_LOG_DIR/integration"
```

- [ ] Wait for that invocation's printed completion path and read its entire
  `summary.txt` and failed stage logs. Require every required stage's native
  exit 0 and its frozen-tree evidence. The default pytest selection excludes
  PostgreSQL. The `testcontainer` stage must actually run serially with Docker
  and include the new continuation contention, ingress, fencing, accounting
  and preserve/refuse cases. If Docker is unavailable, report PostgreSQL as
  unexecuted and stop integration acceptance; do not substitute SQLite.
- [ ] Compare any keyless lint findings with the same baseline corpus. Retain
  `REVIEW_REQUIRED`/stale-binding findings honestly; a shape-only diagnostic
  does not clear operator signatures. Agents remain key-free. Any actual
  signing requirement follows `judge-signature-workflow` with the operator
  firing the final reviewed code bundle.
- [ ] For a named recovery/lease flake, rerun that exact node with `-n 0` and
  compare the same failure on the agreed base before assigning causation.
  Repair and rerun affected checks. Repeat the broad gate only when a changed
  production tree, unexplained failure or unresolved integration concern
  requires it; never rerun it solely to replace an explained isolated flake.

**Exit:** ordinary full-suite and PostgreSQL qualification are complete for the
actual combined candidate, with limitations separately stated. This does not
constitute signing, deployment or live model acceptance.

### Task Q.3: Document compatibility, activation and the release-owner handoff

**Files:** Create `docs/guides/composer-continuation.md` as the feature's user and
operator guide. Link the actual selected target's existing deployment procedure
when the release owner identifies it; do not duplicate that procedure or change
unselected release notes.

- [ ] Keep `WebSettings.composer_continuation_enabled=False` by default. The
  deployment setting is `ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED`. The client
  negotiates the advertised version; v1 behavior remains strict, existing v2
  reads/Stop/replay/worker drain use their persisted lifecycle after new v2
  admission is disabled. Add this activation boundary to the actual target's
  existing configuration documentation:

```text
ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED=false
```

  Changing that value to `true` is an operator activation step on a supported
  store after Q. It is not performed by writing or approving this plan.
- [ ] State the actual new schema epoch selected in C2, affected child tables,
  supported fresh-store path, and exact refusal for an older store. Preserve
  every existing store's bytes on refusal. This plan does not authorize an
  in-place migration, store reset, downgrade or deletion. An existing
  installation needs the operator's explicit new-store selection or a separately
  implemented and approved upgrader before enabling this feature there.
- [ ] Document rollback as disabling **new** v2 admissions while compatible
  readers/workers finish, reconcile or cancel already admitted requests. Keep
  checkpoints, accounting and cancellation state. The older binary must refuse
  a newer epoch; replacing it against the same store is not a valid rollback.
  External effects and committed graph history are not undone by a flag change.
- [ ] Write this guide content after verifying the actual C2 epoch. The current
  private `.10` source uses epoch 79; the guide must match both constants and
  the tested fresh-store schema, rather than infer compatibility from a release
  number.

```markdown
# Composer continuation

Continuation lets one authenticated Composer request continue across worker
segments while retaining the original user message, graph history, tool
results and provider accounting. The model still chooses authoring actions.
The same backend serves ordinary sessions and the tutorial.

The waiting indicator describes activity. A segment transition is not a claim
that a graph changed or that work finished. Reconnecting attaches to the same
operation. Stop records cancellation for its owner to reconcile; an in-flight
effect can require settlement before the operation becomes terminal. Revert
uses the normal authenticated graph-history workflow.

This first implementation supports ordinary graph authoring, interpretation
review and immutable attachment/source reads. Pipeline preview and tools that
create or modify stored blob content return explicit unsupported-operation
results. They do not partially dispatch an operation that cannot be recovered.
A malformed existing graph refuses lifecycle-v2 admission before quota, an
operation row or a user message is created. Its stored bytes remain unchanged;
no empty replacement graph is inferred. The legacy graph-recovery path and the
separately provided operator recovery workflow retain their own contracts.

## Activation and supported stores

New continuation admission is disabled by default:

    ELSPETH_WEB__COMPOSER_CONTINUATION_ENABLED=false

The backend advertises the admission version and the client retains the version
selected for each operation. Enabling admission also requires valid explicit
action bounds and a settlement reserve; setting the flag alone does not supply
those deployment measurements.

This implementation uses session schema epoch 79. Fresh initialization creates
the continuation chunks, events, checkpoints, effects and provider-ordinal
tables, together with segment and immutable claim-attempt records. The binary
refuses an incompatible existing store without upgrading it or changing its
persisted bytes. Use an explicitly selected new store or a separately approved
and tested upgrade procedure. This feature provides no in-place migration,
store reset or downgrade procedure.

## Existing requests and rollback

Disabling new admission preserves lifecycle-v2 reads, exact request replay,
Stop and compatible worker draining. An admitted request retains its complete
timing policy. Current security policy and fresh complete-content validation
still apply on every actual use, retry and resume. Old inspection receipts do
not grant new clearance.

Keep compatible readers and workers until admitted requests finish, reconcile
or are cancelled. Keep their checkpoints, accounting and cancellation records.
An older binary must refuse this newer epoch; pointing it at the same store
is not rollback. A flag change does not undo committed graph history or an
external effect.

## Qualification

Focused tests, the frozen integration gate, serial PostgreSQL contention tests,
frontend checks and deployed acceptance establish different facts. The normal
release PR records their actual results and limitations. Approval of this guide
or plan does not enable the feature, spend provider credits or deploy a service.
```
- [ ] Give the release owner the final immutable head, focused/full/PG/frontend
  results, compatibility limits and unresolved actual failures. Use the
  repository's normal PR description and required independent code review;
  do not create sealed plan packages, review receipt sidecars or an approval
  chain for documents. Preserve all unrelated dirty branches and source owners.

**Exit:** the code candidate is reviewable and the release owner can make the
separately authorized deployment/activation decision. Planning and implementation
approval do not grant strategic-credit use, provider configuration, service
custody, deployment or main-branch protection changes.

### Task Q.4: Perform selected live acceptance and protected delivery

**Files:** Existing acceptance procedures and private case inputs selected by
the release owner; existing CI/PR. No private case contents enter this plan.

- [ ] After the release owner authorizes the actual target and ordinary provider
  budget, exercise one ordinary authenticated authoring request across several
  real segments. Reconnect its observer, use one supported worker restart,
  inspect the same original ingress/request, exact graph/configuration,
  effect/accounting records, fresh both-role review coverage and final
  publication. Also exercise authenticated Stop and Revert during continuation;
  verify no stale generation can publish afterwards. Use the same backend for
  the tutorial and ordinary session; do not add a tutorial-specific path.
- [ ] Run the selected complete B1–B3 and C1–C6 ordinary deployed graph assemblies
  against their existing private expected-state inputs. Retain actual saved
  configuration evidence, model-authored transitions and failure evidence.
  These are assemblies: private graph execution or interpretation approval
  is not implied. One-shot remains suspended.
- [ ] Run eligible Gate2 repository examples only under the release owner's
  existing criteria and authority. Keep external-service/endurance exclusions;
  do not widen the battery or spend strategic credits to make it green.
- [ ] Resolve actual product defects before claiming the reported behavior is
  fixed. Obtain protected review and required CI on the exact final head;
  integration after a review requires affected revalidation. The release owner
  performs the authorized merge and verifies the actual main head afterwards.
  Do not use a plan checkbox or a source GO as proof of delivery.

**Exit:** actual local, PostgreSQL, client, deployed and protected-delivery
evidence are separately recorded. An unexecuted or failed gate remains visible
and prevents its corresponding completion claim.

## Resume and completion criteria

Use the actual branch/status and last completed ordinary command to resume at
the first unfinished task. The implementation owner keeps a short private lane
note with the current unit, source HEAD, owned dirty paths, last native exit/log
and next command. Do not stash/reset someone else's work or infer task completion
from a proposed commit message. A coherent checkpoint uses normal path-scoped
staging, branch-safety and hooks; no bespoke checkpoint envelope is needed.

The plan is complete only when all companion packets exist, their imports/types/
ports agree, every spec requirement maps to a concrete task, ordinary commands
reference real or explicitly created paths, and Astra has reviewed this actual
decomposition. Implementation completion additionally requires the relevant Q
exits; writing the plan executes none of them.

## Implementation estimate

This is an engineering forecast, not measured runtime or a capacity reservation.
For one serial implementation owner after P0: allow roughly 10–15 focused
engineering days for the frame/effect/SQL/accounting/client work and 2–4 further
days for integration repairs, PostgreSQL qualification and review. Local `.9`
qualification has completed; remaining CI/release gates and combined runtime
repairs can add calendar time. Deployed acceptance depends on the release owner's target and
ordinary provider budget. The already passing frontend change is a small
checkpoint, not evidence that the full Continuation implementation takes hours.
