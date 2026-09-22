# Advisor transient-failure recovery implementation plan

> **For the implementing agent:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Use superpowers:test-driven-development for the regression and superpowers:verification-before-completion before reporting completion.

**Goal:** Let a later compose turn recover from an unavailable, malformed, or message-scoped advisor failure without requiring a pipeline edit, and persist the new result so reload does not restore the old block.

**Architecture:** Carry an explicit, typed END-advisor decision from the service to the route, independently of deterministic runtime validation. Persist cause-bearing blocked facts and replace them only with an actual new advisor decision. Reuse the existing fenced immutable state-save path for advisor-only updates; do not mutate the graph to trigger persistence.

**Tech stack:** Python dataclasses and enums, existing Pydantic validation DTOs, session-service persistence and operation fencing, pytest, SQLite and PostgreSQL testcontainers.

**Prerequisites:**

- Baseline inspected: `5bcd983c9`, on `release/0.8.1`, 2026-09-22. This is the planning baseline, not an instruction to merge or publish to that branch. Reconfirm the integration target before implementation.
- Read `AGENTS.md`, `CONTRIBUTING.md` in full, especially its whole-tree gates, and the current versions of the files named below.
- Work in an isolated implementation worktree. Preserve unrelated changes and the earlier advisor plans/reviews.
- Use a worktree-local environment, or explicitly use the primary environment with BOTH worktree source roots on `PYTHONPATH`; verify import provenance. Do not install into a shared symlinked environment.
- User authorized release-branch implementation and integration on 2026-09-22. No database reset, signing, deployment or external publication is authorized. Subsequent explicit ruling: pre-release, no backwards compatibility; old-format gate envelopes must fail validation.

---

## Evidence and scope

The preceding diagnosis drove the real terminal-gate helper using the existing `test_advisor_checkpoint.py` harness, serialized its result with `completion_gates_meta_value`, parsed it back, and offered a CLEAN advisor on the next unchanged turn. The process exited 0 with these assertions:

```text
unavailable:          retry_calls=0 completion_ready=False
malformed:            retry_calls=0 completion_ready=False
flagged_unrepairable: retry_calls=0 completion_ready=False
no_fact_control:      advisor_calls=1
```

This was a service-harness reproduction, not a database, browser, or live-provider test. The implementation must turn it into maintained tests and add the missing persistence proof.

The relevant current seams are:

| File / symbol | Current behavior | Required change |
|---|---|---|
| `src/elspeth/web/execution/completion_gates.py`: `AdvisorSignoffGateFact`, parser/writers | Stores detail, suggestion, graph fingerprint and note, but no cause | Persist a closed cause vocabulary; reject cause-less facts |
| Same file: `advisor_block_covers_unchanged_graph` | Every matching fact can suppress review | Only an identified graph-scoped rejection qualifies |
| `src/elspeth/web/composer/service.py`: `_evaluate_terminal_no_tool_advisor_gate` | Produces causes transiently, then flattens them into validation | Emit a typed adjudication update for terminal failure or CLEAN |
| Same file: `_try_terminate_no_tools`, `_classify_and_budget_turn` | Two terminal consumers; normal path re-merges old fact | Carry the same decision through both consumers; never reapply superseded facts |
| `src/elspeth/web/sessions/routes/_helpers.py`: `_state_data_from_composer_state` | Uses `ValidationResult` as a proxy for advisor adjudication | Separate advisor authority from runtime validation |
| `src/elspeth/web/sessions/routes/messages.py` and `composer/compose.py` | Ordinary post-compose saves require a state-version change | Also save a changed advisor envelope for the same graph |

The service knows `unavailable`, `malformed`, `flagged_final_pass`, `flagged_no_repair`, and `flagged_unrepairable`. The last is specifically the current user-message pre-scan, which pipeline tools cannot repair. Do not infer these classes from detail text or presence of a reviewer note.

Keep the prior ruling's explanation-only behavior for a known graph block. Do not add a text matcher for “retry”, a tutorial branch, or a server-authored graph. This work changes the END completion advisory, not EARLY checkpoint scheduling or the planner's provider obligation.

## Behavioral contract

| Prior fact and this turn | Call END advisor when otherwise eligible? | Durable outcome |
|---|---|---|
| No fact | Yes | Persist the actual terminal decision if it changes the envelope |
| Same graph, known graph rejection, no version movement | No | Preserve block and explanation; no new state row solely for the skip |
| Same graph, unavailable or malformed | Yes, within existing per-turn limits | CLEAN clears; another failure replaces/preserves its classified block |
| Same graph, message-scoped rejection | Yes, checking the new message | New acceptable message can clear; repeated offending message blocks again |
| Existing cause-less fact | Refuse malformed owned data | No compatibility parser or inferred cause |
| Changed graph | Yes when otherwise eligible | Old fact becomes pending until reviewed; do not attribute its note to new content |
| No advisor decision: deterministic preflight, cancellation, timeout, orphan return, recovery save | No new authority to clear | Preserve prior fact, subject to existing stale-graph projection |
| CLEAN, followed by deterministic validation failure | Advisor fact clears if decision still matches final graph | Validation remains red for its own reasons |
| Decision fingerprint differs from final state | No authority for final state | Reject propagation/persistence as internal drift; never bind it to the new graph |

“Eligible” retains structural-empty, deadline, orphan-review, and budget gates. There is no background retry and no unbounded loop. A new user turn gets the existing configured budget; this change does not replenish a spent budget inside the same turn.

The skip rule remains the established graph-block policy. A future broader question about changed user constraints invalidating an otherwise genuine graph review is outside this repair; do not claim that a graph fingerprint identifies all possible advisor evidence.

## Representation and authority

Add `src/elspeth/web/composer/advisor_decision.py`, a dependency-light owned-type module with no imports from service, state, routes, or validation. Put the cause enum and fact/decision dataclasses there and update imports; avoid an import cycle through `ComposerResult`.

The intended complete type definitions are:

```python
from dataclasses import dataclass
from enum import StrEnum


class AdvisorBlockCause(StrEnum):
    GRAPH_REJECTED = "graph_rejected"
    UNAVAILABLE = "unavailable"
    MALFORMED = "malformed"
    MESSAGE_REJECTED = "message_rejected"


@dataclass(frozen=True, slots=True)
class AdvisorSignoffGateFact:
    detail: str
    suggestion: str | None
    for_graph: str
    note: str | None
    cause: AdvisorBlockCause


@dataclass(frozen=True, slots=True)
class AdvisorGatePassed:
    for_graph: str


@dataclass(frozen=True, slots=True)
class AdvisorGateBlocked:
    fact: AdvisorSignoffGateFact


type AdvisorGateDecision = AdvisorGatePassed | AdvisorGateBlocked
```

`ComposerResult.advisor_gate_decision: AdvisorGateDecision | None = None` and the corresponding terminal-gate outcome field use `None` to mean **no adjudication**, never “passed”. A blocked decision is valid even when a preflight/handoff shape does not contain an advisor readiness blocker. Build its detail/suggestion/note from the existing reason-specific builder, keeping reviewer sanitization in its current boundary. Do not copy these fields from arbitrary model output.

Map terminal causes explicitly: `flagged_final_pass` and `flagged_no_repair` to `GRAPH_REJECTED`; `flagged_unrepairable` to `MESSAGE_REJECTED`; provider failures to their namesake values. Preserve existing fail-closed handling for a non-ok verdict lacking a classified provider failure: it maps to `MALFORMED`, not success.

For persistence, require the current envelope schema version and a valid `cause`; missing, unknown or incorrectly typed fields raise. Unversioned envelopes are rejected, including empty mappings. A missing entire completion-gates key still means that no gate fact was recorded. Keep strict `note` behavior. Do not add migrations, compatibility branches or reset existing data as part of implementation.

Keep this internal to composer metadata and service results. Do not add retry policy to generic public `ValidationReadinessBlocker` or broaden the frontend wire schema just to transport it.

## Task 1: Reproduce the lock and pin the negative controls

**Files:**

- Modify: `tests/unit/web/composer/test_advisor_checkpoint.py`
- Modify: `tests/unit/web/execution/test_completion_gates.py`

1. Add a parameterized two-turn regression for unavailable, malformed and message rejection, using `drive_try_terminate` and the real serializer/parser. First turn produces a terminal block; second turn uses the same graph and a CLEAN advisor. Assert the second advisor actually runs and completion can recover. Run RED before changing production.
2. Add controls: no prior fact invokes the advisor; a known graph rejection skips on an unchanged explanatory turn; changed graph reviews; repeated offending message blocks; no extra repair instruction asks the planner to mutate user text. For message rejection, also exercise the real pre-scan with a replacement benign message rather than relying exclusively on stubbed verdicts.
3. Exercise both normal no-tool termination and the budget-exhaustion last-chance path. Check provider-call deltas per transition, not only a total across the walk.
4. Assert that an unavailable or malformed first turn with green deterministic validation still withholds completion. No retry change may fail open on that first turn.

**Run:** use the focused command in Validation below with `-k 'advisor_recovery or unchanged or already_blocked or completion_withheld'`; name the new tests with `advisor_recovery`. Expected RED is an assertion about missing second-turn review, not collection/import errors. Keep the existing no-fact and graph-block controls green.

**Done:** the reported loop has a maintained failing regression with positive controls. Keep red tests local until the corresponding implementation commit is ready.

## Task 2: Introduce classified facts and explicit decisions

**Files:**

- Create: `src/elspeth/web/composer/advisor_decision.py`
- Modify: `src/elspeth/web/composer/protocol.py`
- Modify: `src/elspeth/web/execution/completion_gates.py`
- Modify: `tests/unit/web/execution/test_completion_gates.py`
- Modify: `tests/unit/web/sessions/test_completion_gate_roundtrip.py`

1. Write failing tests for every cause's JSON round-trip, rejection of unversioned envelopes, missing cause, unknown cause/version, wrong scalar types, frozen metadata, current empty envelopes, and malformed-note rejection.
2. Add the owned types above. Move the fact type and update real imports and test constructors explicitly; do not retain aliases solely to appease static gates.
3. Implement versioned strict parsing and serialization. Update `completion_gates_meta_from_facts` and the envelope TypedDicts together. Keep fingerprint computation unchanged.
4. Replace validation-derived adjudication with one explicit decision-to-envelope helper. Inputs are a prior parsed fact, optional decision, and final state. `None` carries forward; passed yields empty gates; blocked writes its typed fact. Validate the decision fingerprint before applying it. Use that helper at the writer seam in Task 4.
5. Narrow skip eligibility to `fact.cause is AdvisorBlockCause.GRAPH_REJECTED` plus the existing version/fingerprint conditions. Do not use the skip predicate to decide whether an unresolved old fact must remain visible: retryable blocks also stay visible until actually superseded.
6. Run the gate unit and DB round-trip tests. Expected GREEN: each admitted representation round-trips and corrupt current data raises.

**Done:** no persisted failure is interpreted from prose; no green deterministic validation grants advisor authority. Suggested commit boundary: types, readers/writers and their tests together once all callers are updated enough to remain green.

## Task 3: Thread the actual END decision through both terminal paths

**Files:**

- Modify: `src/elspeth/web/composer/service.py`
- Modify: `src/elspeth/web/composer/protocol.py`
- Modify: `tests/unit/web/composer/test_advisor_checkpoint.py`
- Modify: `tests/unit/web/composer/test_advisor_terminal_publication.py`
- Modify: `tests/unit/web/composer/test_compose_loop_persistence.py`

1. Add failing assertions that CLEAN and terminal block carry an explicit fingerprint-bound decision, while skip, continue, exhausted-before-review and orphan early-return do not claim a new decision.
2. Populate `AdvisorGateBlocked` in `_build_advisor_signoff_blocked_result` using its typed reason and existing sanitized presentation fields. Terminal publication and the new decision must describe the same branch, including outage-after-FLAG and FLAG-after-outage.
3. Populate `AdvisorGatePassed` only after an actual CLEAN END checkpoint. EARLY CLEAN and successful preflight do not qualify. Carry it through `_TerminalNoToolAdvisorGateOutcome` and into the finalized `ComposerResult` in both `_try_terminate_no_tools` and `_classify_and_budget_turn`.
4. Replace the normal path's unconditional old-fact merge with decision-aware projection shared by both terminal consumers: no decision retains the fact, passed removes the old advisor block, blocked uses the new block. Preserve all unrelated validation errors and readiness blockers. Assert fingerprint identity after any finalization that can change graph content; do not stamp a new fingerprint onto an old decision.
5. Preserve the latest effective fact across skipped or early-return paths. A fresh review that cannot run because another gate intercepts must not erase the old fact.
6. Run the Task 1 regression and terminal-publication/loop-persistence suites. Assert reviewer note behavior, raw assistant content, publication cardinality, and budget counters remain correct.

**Done:** all three retry causes can recover in memory; graph-block explanations still skip; the alternate terminal path has identical authority and projection semantics. Commit the coherent service change with focused tests.

## Task 4: Persist advisor-only changes through the existing fenced save path

**Files:**

- Modify: `src/elspeth/web/sessions/routes/_helpers.py`
- Modify: `src/elspeth/web/sessions/routes/messages.py`
- Modify: `src/elspeth/web/sessions/routes/composer/compose.py`
- Modify: `tests/unit/web/sessions/test_completion_gate_roundtrip.py`
- Create: `tests/unit/web/sessions/test_advisor_recovery_routes.py`
- Extend if needed: `tests/testcontainer/web/test_session_operation_fence_postgres.py`

1. Write route tests for send-message and recompose: seed a durable retryable block, return an unchanged graph with a real CLEAN decision, reload the latest record, and assert the block is absent. Assert graph fingerprint/content equality before and after, a new persisted state id/version, correct assistant linkage, and no extra tool mutation.
2. Add the negative controls: no decision plus green preflight preserves the block; skipped known graph block creates no advisor-only row; rejected decision fingerprint cannot save; repeated identical failure need not append another state solely for an unchanged envelope; changed failure details/cause do save; a lost operation fence leaves the previous durable fact intact.
3. Introduce a shared route helper to compute the effective envelope and whether it differs semantically from the prior one. Compare typed facts so only a changed decision triggers an advisor-only save. Save when graph version changed OR an explicit advisor decision changes the durable envelope. Do not increment `CompositionState.version` in the service to force the branch.
4. Extend `_state_data_from_composer_state` with explicit advisor-decision input and a required/explicit prior-fact input for adjudicating paths. Audit all existing callers, including recovery: deterministic recomputation must carry prior facts, never clear them by virtue of returning `ValidationResult`. Remove the old proxy semantics and update their comments/tests.
5. Use `save_composition_state(..., provenance="post_compose", session_operation_context=...)` for ordinary advisor-only saves. Its authority allocates the next storage version and checks the COMPOSE fence. Keep it immutable; do not update JSON in place or add an unfenced SQL shortcut.
6. Preserve guided settlement precedence. If a guided transition is being committed, combine its metadata and the advisor decision into that single existing settlement. Do not use `commit_transition_response` for an ordinary freeform review: it requires guided transition state. Do not create a second state snapshot after an already-settled pipeline intent. Prove that any such intent can carry a decision for its actual final graph, or explicitly reject an impossible combination.
7. Return a state response for advisor-only changes so the client sees the new readiness immediately. Link the turn-end assistant message to the persisted state as existing routes do. Keep LLM audit cohort linkage on the original compose base. In send-message, `compose_base_state_id` is not necessarily the user's historical `pre_send_state_id`; preserve that distinction. Preserve already-persisted terminal-publication rows and avoid duplicate prose. Progress text for an advisor-only save must say review status was saved, not claim the pipeline was edited.
8. Verify the existing lease covers the entire read/compose/save sequence. Add the PostgreSQL fence regression at the save seam: a stale owner cannot replace a newer fact or clear the block after ownership loss. Reuse the current authority rather than introduce a second locking scheme. `save_composition_state` has no expected-tip argument; do not describe it as a current-state compare-and-swap. The existing `commit_composition_response` does have that check, but switching to it would require carrying the latest mid-turn persisted id, not blindly passing the pre-compose id. Retain the existing ordinary-save pattern for this bounded repair.
9. Build the new route tests using the real fixture/persistence patterns in `tests/unit/web/sessions/test_routes.py`, not only a mocked save. Include first-ever blocked decision on an unchanged graph, mid-turn persistence before final save, and guided consumption plus advisor recovery. Preserve guided custody refusal, using `tests/unit/web/sessions/test_guided_custody_gate.py` as the existing control.

**Done:** the recovery survives a real DB round-trip and both HTTP route paths; GET state/readiness and revalidation agree with the just-returned result. A read/recompute alone still cannot clear a gate. Commit route/helper changes and their proofs together.

## Task 5: Align notices and complete regression coverage

**Files:**

- Modify: `src/elspeth/web/composer/no_tool_policy.py`
- Modify: `src/elspeth/web/composer/service.py` (reason-specific suggestions)
- Modify: `tests/unit/web/composer/test_no_tool_policy_segments.py`
- Modify: `tests/unit/web/composer/test_advisor_checkpoint.py`
- Modify if the maintained hint becomes stale: `docs/agents/recent-code-hints.md`

1. Write failing copy tests by cause. Unavailable/malformed notices should offer another message/request after resolving provider problems; message-scoped notices should request a rephrased message; graph-rejection notices should retain their pipeline-change guidance.
2. Update existing reason-specific templates and their pinned twins without altering wire interpolation structure. Replace the old global prohibition on “on your next message” with cause-specific assertions; blanket wording bans encode the bug.
3. Test all existing preflight shapes: green, pending interpretation handoff, absent, and red. A CLEAN advisor removes only its own gate; it must not mark unresolved interpretation reviews or runtime errors complete. Persisted failures retain the pending-interpretation handoff shape on reload: append advisor check evidence without replacing the review-card readiness discriminator. Execution remains blocked by the unresolved review.
4. Test durable recovery: current classified block -> bounded review -> CLEAN -> current empty gates -> reload. Old-format and malformed current data fail loudly. No database reset is a test prerequisite.
5. Update the dated advisor-skip hint to describe graph-rejected facts specifically and explicit-decision persistence. Preserve reviewer-note sanitization and the existing disclosure policy.

**Done:** user instructions match implemented recovery behavior and no notice promises an unavailable path. Commit copy/tests after service and route behavior exist.

## Validation and integration

Run commands from the implementation worktree. Initialize `W` to its root and `V` to the explicitly selected environment; neither may be an unresolved placeholder when executing. These variables are local shell setup, not paths to commit into source.

```bash
cd <implementation-worktree> && W="$PWD" && V=<selected-venv>
export PYTHONPATH="$W/src:$W/elspeth-lints/src"
"$V/bin/python" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

For each RED/GREEN step, use a unique log and inspect the recorded exit code and full failure details. The consolidated focused selection is:

```bash
cd "$W" && run_log=$(mktemp /tmp/advisor-recovery-focused.XXXXXX)
"$V/bin/python" -m pytest -n 0 -o pythonpath="$W/src $W/elspeth-lints/src" \
  tests/unit/web/execution/test_completion_gates.py \
  tests/unit/web/composer/test_advisor_checkpoint.py \
  tests/unit/web/composer/test_advisor_terminal_publication.py \
  tests/unit/web/composer/test_compose_loop_persistence.py \
  tests/unit/web/composer/test_no_tool_policy_segments.py \
  tests/unit/web/sessions/test_completion_gate_roundtrip.py \
  tests/unit/web/sessions/test_advisor_recovery_routes.py \
  tests/unit/web/test_sessions_composer_attribute_contracts.py \
  > "$run_log" 2>&1
test_exit=$?
printf 'exit=%s log=%s\n' "$test_exit" "$run_log"
cat "$run_log"
```

Expected after implementation: exit 0, all selected tests completed. The new route file intentionally does not exist until Task 4; omit it in earlier focused invocations. For RED runs use only the relevant new cases and record their exact failed assertions. Add negative mutations temporarily in the isolated tree: restore unconditional skipping; drop the explicit decision on either terminal path; restore version-only saving; let green preflight clear facts. Each must make its corresponding regression fail, then be removed.

Before broad validation inspect active pytest processes and host load; coordinate one broad suite. This change touches shared Composer behavior and session persistence, so the complete final gate includes PostgreSQL:

```bash
cd "$W" && scripts/full-suite-gate.sh --execute --detach --root "$W" \
  --stages ruff,mypy,contracts,lints,pytest,testcontainer
```

Read the printed run directory's `.done` and `summary.txt`. Require frozen-tree evidence and inspect each exit code. The canonical script/environment must use the intended interpreter and both source roots. If Docker is unavailable, report PostgreSQL proof as outstanding, not passed. Before any commit run `git status --short`, inspect only the intended staged paths, and run `scripts/branch-safety-check.sh --intent commit`.

Compare the trust-tier finding corpus to the frozen base; the known global signature failure is not this task's obligation to clear. Do not hand-edit signatures or acquire the operator key. Run all affected whole-tree AST, contract and wire-template gates described in CONTRIBUTING; extend exact pins only where the actual semantic change justifies it. No lint suppressions or mock masquerades.

If live acceptance is requested for implementation, first inspect the existing Composer harness. Exercise unavailable -> provider restored -> unchanged-message retry -> reload, and message rejection -> benign new message -> reload. Record provider calls per transition, persisted state ids, graph fingerprints and readiness. A controlled stubbed route test proves recovery semantics; it does not prove deployed-provider recovery.

## Completion criteria and handoff

- All three reported causes recover without graph mutation when a later review succeeds.
- The new outcome survives reload, and no skipped/failed-to-run advisor or deterministic recompute clears a durable block.
- Known graph blocks retain explanation-only skips and completion withholding.
- Both terminal service paths, both route writers, guided precedence, recovery saves and operation fencing are covered.
- Old-format and malformed metadata raise; no backwards-compatibility code is introduced.
- Checks are reported with exact scope, exit codes and frozen candidate SHA. Report full-suite, PostgreSQL, trust-tier/signing, merge, push and live acceptance separately.
- No implementation is claimed by this plan. Do not stop after adding cause metadata: the service retry and durable clear are one deliverable.

**Rollback:** Revert the implementation only with a reader that understands any v2 rows already written, or perform an explicitly approved development-store reset. An older binary must not be pointed at new envelopes under an assumption of compatibility. Do not delete session history as an automatic rollback step.
