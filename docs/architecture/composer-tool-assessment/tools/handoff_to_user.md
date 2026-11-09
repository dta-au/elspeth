# `handoff_to_user` — terminal graph recovery and conversation handoff

Baseline: `3b28d537be98234da8135c418bf185a998c18b68`, restored 0.8.9.
This is a reconnaissance assessment; no production change is proposed as already implemented.

## 1. Purpose and usage

Use this provider-authored terminal control when the user needs a question,
explanation, or a graph-only recovery action instead of further authoring in
the current turn. It ends authoring immediately. `park` retains the visible
draft and creates a bookmark; `reset_to_turn_start` targets the admitted starting
snapshot; `reset_to_safe_checkpoint` selects the latest locally editable saved
version or an empty workspace. “Safe” means representable in the editor, not approved,
valid, executable, or free of missing credentials. The latest editable version
is frequently the current draft itself (see §4 and finding N2).

It is available on every Composer provider surface that can author: the
freeform primary loop, the freeform last-chance call and the recovery-only
reply stage after review cards are staged; the planner's normal and
escape-hatch palettes (the planner is selected only when the graph is
structurally empty); and the recovery-only provider route used when the saved
head cannot hydrate. It is absent from the synchronous registry, canonical
emitter and Composer MCP. A live COMPOSE operation,
owned provider invocation, session authority, and remaining provider budget are
prerequisites; this is not a server-generated fallback around the provider.

## 2. Contract

Arguments are required `action` (`park`, `reset_to_turn_start`, or
`reset_to_safe_checkpoint`) and required `message` (1–16,000 characters,
nonblank after stripping); extra fields are forbidden and strict Pydantic
typing rejects non-string messages. The owned intent also
requires a nonblank provider call identity of at most 1,024 characters. Credential material
in the message is refused. There are no action/message defaults. Every argument
rejection is a `ValueError` subclass (measured; §7).

The control must be the only tool call in its provider response. Parsing yields
an intent, not a committed graph or success receipt. Session settlement selects
a target and then atomically completes the operation with graph outcome,
assistant response, audit cohort and a trusted recovery notice. An unavailable
target — including a `park` candidate that is not representable — produces
`target_unavailable`; the saved assistant text becomes a trusted
refusal rather than the model's prospective message, which is withheld in full
even when it was a question for the user (intended; N7). The editor target
can still be invalid for execution: every recovered checkpoint is stored
`is_valid=False` with no validation results, and only a fingerprinted advisor
sign-off fact is carried forward (it downgrades on graph mismatch). Reset does
not delete prior states, revoke external effects, undo blob writes, or refund
provider spend.

Settlement does have effects beyond the graph that are not disclosed to the
model: every outcome that inserts a new checkpoint (each reset, and `park` when
the candidate differs from the head or the head is malformed) rejects all pending
`set_pipeline` proposals in the session as `superseded`, and the insert runs the
dead-site pending-interpretation sweep (N1). A reset whose target equals the
current head still writes a new, identical version. Blob-effect proposals are
not touched.

There is no separate incremental proposal approval for this conversational
control. Execution and review admission remain independent. It is not cached.
Recovery-only mode makes one model call with the remaining deadline and no
retry; planner and freeform calls retain their enclosing budget/retry policies.
A malformed or mixed recovery call is a terminal turn failure with no repair
round on every route (N3). Replay of an already completed operation returns its
durable response without new writes;
a fresh operation/call is not a globally idempotent command (a repeated `park`
of the same head creates another bookmark row; N10). Session fences,
base identity and final head comparison prevent stale recovery overwriting
another operation's work. Cancellation must settle audit and operation custody.

## 3. Implementation trace

- [Recovery tool names](../../../../src/elspeth/web/composer/recovery_controls.py#L18),
  [`HandoffArguments`](../../../../src/elspeth/web/composer/recovery_controls.py#L21),
  [wire projection and description](../../../../src/elspeth/web/composer/recovery_controls.py#L35),
  [parser](../../../../src/elspeth/web/composer/recovery_controls.py#L69) and
  [singleton gate](../../../../src/elspeth/web/composer/recovery_controls.py#L86).
- [Freeform exposure](../../../../src/elspeth/web/composer/provider_gateway.py#L391)
  (inserted before the trailing cache anchor, [L411](../../../../src/elspeth/web/composer/provider_gateway.py#L411))
  and [planner exposure](../../../../src/elspeth/web/composer/pipeline_planner.py#L1647)
  (always appended after the policy-filtered discovery set, [L1667](../../../../src/elspeth/web/composer/pipeline_planner.py#L1667)).
  Both dialects expose the same control names and byte-identical parameter
  schemas; [length keywords are stripped for both dialects](../../../../src/elspeth/web/composer/recovery_controls.py#L55),
  and strict only adds `"strict": true`. The server still enforces lengths at admission.
- [Freeform recovery-only call](../../../../src/elspeth/web/composer/service.py#L1028)
  and [loop terminal result](../../../../src/elspeth/web/composer/service.py#L1087);
  [planner singleton rejection and `PlannerHandoff`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4351),
  caught and converted to a `ComposerResult` in
  [the planning application](../../../../src/elspeth/web/composer/planning_application.py#L1181).
- [Owned intent](../../../../src/elspeth/web/composer/graph_recovery.py#L41) and
  [editable checkpoint qualification](../../../../src/elspeth/web/composer/graph_recovery.py#L190)
  deliberately avoid plugin construction and runtime validation;
  [graph-only checkpoint read and data projection](../../../../src/elspeth/web/sessions/graph_recovery.py#L90).
- [Session admission](../../../../src/elspeth/web/sessions/service.py#L6607)
  binds scalar head identities before graph hydration;
  [target preparation](../../../../src/elspeth/web/sessions/service.py#L6701)
  verifies the COMPOSE fence, selects the snapshot and prepares settlement.
- [Turn settlement and trusted refusal](../../../../src/elspeth/web/sessions/composer_turn.py#L1051)
  hand off to [the terminal composite](../../../../src/elspeth/web/sessions/service.py#L9267),
  which applies [the frozen selection](../../../../src/elspeth/web/sessions/service.py#L9088)
  and [writes the receipt](../../../../src/elspeth/web/sessions/service.py#L9189);
  [browser operation decoder](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L150)
  checks receipt/message, receipt/state and receipt/operation binding before presentation.

### 3.1 Route inventory

Instruments for every membership/absence claim are listed in §7 and in the lane report.

| Route | Entry (file:line) | Gate / policy | Notes |
|---|---|---|---|
| Freeform primary loop | [`_call_model_turn` → `_terminal_recovery_result`](../../../../src/elspeth/web/composer/service.py#L2591) | Non-empty or non-planner turn, head hydrated, COMPOSE lease | Checked on every model call before dispatch; the per-turn tool-call cap is enforced first inside `_call_model_turn` |
| Freeform last-chance call | [B-4D-3 final call](../../../../src/elspeth/web/composer/service.py#L2202) | Composition-turn budget reached | Full loop palette; recovery checked before the cap/dispatch |
| Freeform reply stage | [recovery-only palette](../../../../src/elspeth/web/composer/service.py#L1991), [check](../../../../src/elspeth/web/composer/service.py#L2018) | Review cards staged / pending-interpretation reply | Gateway requires text or a recovery control ([L826](../../../../src/elspeth/web/composer/provider_gateway.py#L826)) |
| Recovery-only provider route | [`compose_recovery`](../../../../src/elspeth/web/composer/service.py#L1028), selected at [composer_turn L1474](../../../../src/elspeth/web/sessions/composer_turn.py#L1474) | `malformed_head` only; availability, chargeable admission, credential check of the user message | No skill text; scalar context only; one call, no retry |
| Planner normal palette | [`planner_tool_definitions`](../../../../src/elspeth/web/composer/pipeline_planner.py#L1667) | Structurally empty state ([service L1233](../../../../src/elspeth/web/composer/service.py#L1233)); discovery policy narrows discovery only | Recovery and terminal names validated by the [capability manifest](../../../../src/elspeth/web/composer/capability_skill.py#L216) |
| Planner escape hatch | [`tools_override`](../../../../src/elspeth/web/composer/pipeline_planner.py#L4268) | Hatch engaged after budget/prose exhaustion | Hatch dialect; same singleton rule |
| Boot probe | [hatch request](../../../../src/elspeth/web/composer/boot_probe.py#L215) | Startup probe, non-`none` hatch dialect | Advertises only; result never executed as recovery |
| Received-argument evidence | [freeform capture](../../../../src/elspeth/web/composer/provider_gateway.py#L919), [planner capture](../../../../src/elspeth/web/composer/pipeline_planner.py#L3893) | Every recorded provider call | Planner labels the slot `unknown_tool` (N4) |
| Settlement | [`settle_post_provider`](../../../../src/elspeth/web/sessions/composer_turn.py#L1051) → [prepare](../../../../src/elspeth/web/sessions/service.py#L6701) → [composite](../../../../src/elspeth/web/sessions/service.py#L9267) | Same COMPOSE fence; head CAS at [L9108](../../../../src/elspeth/web/sessions/service.py#L9108) | `recovery_notice` attached at [L9436](../../../../src/elspeth/web/sessions/service.py#L9436) |
| Async worker / REST | `POST /messages`, `POST /recompose` via the detached [turn](../../../../src/elspeth/web/sessions/composer_turn.py#L1) | Worker-owned lease | Failures project through [`project_composer_operation_error`](../../../../src/elspeth/web/sessions/composer_operation_errors.py#L246) |
| Frontend | [decoder](../../../../src/elspeth/web/frontend/src/api/composerOperationDecoder.ts#L150), [store merge](../../../../src/elspeth/web/frontend/src/stores/sessionStore.ts#L512) | Terminal or state surface | Renders the receipt only; there is no park list or restore affordance in the UI |

Not routes (measured or grepped): the synchronous registry, canonical emitter
and Composer MCP do not contain the name; `tool_batch.py` has no recovery
handling (recovery is parsed before dispatch); `compose_recovery`,
`prepare_graph_recovery` and `load_graph_recovery_context` have no caller
outside `composer_turn.py`; no `tutorial` branch exists in the three recovery
modules or in `service.py`/`composer_turn.py`. The user-initiated state revert
([`revert_state_for_operation_receipt`](../../../../src/elspeth/web/sessions/service.py#L7926))
is an adjacent non-provider rollback with the same proposal-supersession rule,
not a route of this control.

## 4. Alternative paths

Normal `park` can preserve an unpersisted but representable candidate; when
the candidate equals the saved head it only adds a bookmark, otherwise it saves
the candidate as a new version. In the planner route the candidate is the
unchanged (empty) input state. A malformed
head can use provider-selected reset without serializing that damaged candidate.
An empty session can reset to an empty authoring state. `reset_to_safe_checkpoint`
searches all saved versions latest-first for an editable state: it may select
the current draft when that draft is editable, even if runtime validation
rejects it. Because freeform persists a tool-call checkpoint after each
mutation round, that includes this operation's own in-turn checkpoints, so in
freeform it usually re-snapshots the current draft rather than abandoning it
(test-established selection; N2). Turn-start reset is the exact rollback choice when that is the user's intent.

On the recovery-only route there is no candidate and the turn start is the
malformed head: `park` and `reset_to_turn_start` succeed only when the damage
was confined to completion metadata, and otherwise refuse (N5).

Mixed recovery/discovery or recovery/mutation batches reject before dispatch.
Malformed/blank/credential-bearing arguments reject rather than recover. Both
are terminal failures of the whole turn, not repair rounds: freeform raises a
bare `ValueError`, the planner a typed `MALFORMED_RESPONSE` (N3). Missing
or unreadable explicit targets produce refusal. Provider unavailability,
expired budget, lost fences, mismatched base, audit failure or final head change
remain errors, not successful recovery. One corrupt park row fails recovery
admission for every turn of the session (N6). Completed-operation replay uses stored
response identity rather than reconstructing a new receipt.

## 5. Usability assessment

**Intended restriction, high confidence:** this restores editable graph state
only. Its name is broader than rollback semantics; callers must read the actual
receipt and not equate a terminal conversation with completed pipeline work.
Workaround for exact rollback is `reset_to_turn_start`; use preview and review
afterwards for readiness. No demonstrated integrity defect was found in the
focused recovery paths inspected and exercised; the findings below are
disclosure, labelling, fault-typing and resource issues.

**Source-established limitation, high confidence:** the advertised context lists
at most 100 recent parks, with no completeness/cursor indicator
([session query](../../../../src/elspeth/web/sessions/service.py#L6641)). This
does not delete older parks or prove a failed restore; it limits what the model
can discover. The practical impact above 100 parks remains unexercised.

New provisional findings (coordinator renumbers):

- **N1 — demonstrated (probe N1, §7.1), undisclosed effect.** Any settlement that
  inserts a checkpoint rejects every pending `set_pipeline` proposal as
  `superseded` with actor `composer_graph_recovery`
  ([L9117–L9168](../../../../src/elspeth/web/sessions/service.py#L9117)),
  and the insert runs the dead-site pending-interpretation sweep
  ([L1797](../../../../src/elspeth/web/sessions/service.py#L1797)). This matches
  the user state-revert rule, so the behaviour is plausibly intended; the
  description, skill, receipt notice and this sheet did not disclose it, and no
  test in the recovery suites exercises the branch. The probe run showed a
  `reset_to_turn_start` move a pending proposal from `pending` to `rejected`
  with a `proposal.rejected` event by `composer_graph_recovery`. The only
  receipt notice did not mention the proposal. A bookmark-only `park` (the
  control) left the proposal pending. The interpretation-sweep consequence for
  staged review cards is still unverified; the probe did not stage any.
- **N2 — demonstrated at the session-service layer (probe N2, §7.1), semantics/guidance mismatch.** Safe-checkpoint
  selection ([L6787](../../../../src/elspeth/web/sessions/service.py#L6787))
  iterates every version, admitting this operation's own tool-call checkpoints
  ([foreign-only fence, L6730](../../../../src/elspeth/web/sessions/service.py#L6730);
  [per-round insert, L2094](../../../../src/elspeth/web/sessions/service.py#L2094)).
  The common freeform outcome is a duplicate of the current draft, stored
  `is_valid=False`, with pending proposals superseded (N1) — while the skill
  tells the model to use it to “abandon a broken draft”. Not an integrity defect.
  The probe saved two different tool-call checkpoints in one operation.
  Safe-checkpoint then targeted the newer one, and the restored version 3
  carried the newer content with `is_valid=False`. The `reset_to_turn_start`
  control discarded both checkpoints. The probe called the session service
  directly; it did not drive the freeform per-round insert at L2094.
- **N3 — demonstrated (probe N3, §7.1), fault-typing asymmetry.** Rejected recovery calls
  end the turn without repair. Freeform re-raises the `ValueError`
  ([L1097](../../../../src/elspeth/web/composer/service.py#L1097)); the turn
  ladder has no arm for it, so the worker projects the generic
  500 `operation_failed` ([L246](../../../../src/elspeth/web/sessions/composer_operation_errors.py#L246)).
  The planner raises typed
  `MALFORMED_RESPONSE` ([L4356](../../../../src/elspeth/web/composer/pipeline_planner.py#L4356)).
  Measured through `POST /messages` and the detached worker with a scripted
  `[handoff_to_user, set_metadata]` reply: freeform settled as HTTP 500,
  `failure_code=operation_failed`, with a diagnostic id. The turn raised
  `ValueError: Recovery controls must be the sole tool call in a response`.
  The planner, given `[handoff_to_user, list_sources]`, settled as HTTP 502,
  `http_error` / `composer_planner_failure` / `invalid_provider_response` /
  `MALFORMED_RESPONSE`. Each route made exactly one provider call, so neither
  ran a repair round.
  Provider-authored bad input therefore surfaces as an unknown first-party fault on one route.
- **N4 — demonstrated audit labelling inconsistency, low impact.** The planner
  captures received arguments with `known_tools` that exclude recovery names
  ([L3893](../../../../src/elspeth/web/composer/pipeline_planner.py#L3893)),
  so a received `handoff_to_user` slot is stored as `unknown_tool`; freeform
  stores `handoff_to_user`. The raw `provider_tool_name` and arguments are still
  retained, and the label does not change projection for this tool.
- **N5 — source-established guidance gap.** On the recovery-only route,
  `park` and `reset_to_turn_start` target the malformed head
  ([L6758](../../../../src/elspeth/web/sessions/service.py#L6758),
  [L6776](../../../../src/elspeth/web/sessions/service.py#L6776)) and refuse
  when its graph columns cannot be read
  ([L6797](../../../../src/elspeth/web/sessions/service.py#L6797)). Neither the
  shared description nor the [recovery-only prompt](../../../../src/elspeth/web/composer/service.py#L1060)
  says so; the model cannot learn from the terminal refusal.
- **N6 — intended restriction with wide blast radius.** A non-canonical park
  identity or invalid park version raises `AuditIntegrityError` inside admission
  ([L6655](../../../../src/elspeth/web/sessions/service.py#L6655)), which every
  compose turn calls ([composer_turn L902](../../../../src/elspeth/web/sessions/composer_turn.py#L902)).
  Fail-closed Tier-1 behaviour is tested; its consequence is that no turn in
  that session can compose until the row is repaired.
- **N7 — intended restriction, tested.** On refusal the provider message is
  replaced by [fixed trusted text](../../../../src/elspeth/web/composer/no_tool_policy.py#L102),
  so a clarifying question carried by a refused control never reaches the user.
- **N8 — demonstrated (incidentally, in the probe N1 run, §7.1), wording mismatch, minor.** `reset_to_turn_start`
  on a session that was empty at turn start sets `empty_fallback` and publishes
  “No eligible saved draft was available” ([L9211](../../../../src/elspeth/web/sessions/service.py#L9211)),
  although the exact turn-start state was restored. The probe N1 reset and the
  N2 turn-start control both reported `empty_fallback=True`. The N1 receipt
  notice read “No eligible saved draft was available; an empty authoring
  workspace was opened. …”.
- **N9 — source-established resource limitation, unmeasured.** Preparation
  reads every version row without a limit ([L6723](../../../../src/elspeth/web/sessions/service.py#L6723))
  and safe-checkpoint may decode each version until one qualifies.
- **N10 — source-established discoverability limitation.** Each `park` mints a
  new park identity even for an unchanged head ([L6850–L6853](../../../../src/elspeth/web/sessions/service.py#L6850)),
  so repeated parks consume the 100-entry advertisement; entries carry only
  `park_id`, `state_id` and `version` ([context message](../../../../src/elspeth/web/composer/recovery_controls.py#L99)),
  and no REST or UI surface lists parks.
- **N11 — source-established, minor.** The advertised `current_state_id` and
  `current_version` always equal the turn-start values
  ([L6688–L6691](../../../../src/elspeth/web/sessions/service.py#L6688)) and are
  injected once per freeform turn ([L2465](../../../../src/elspeth/web/composer/service.py#L2465)),
  so after the first saved tool round they no longer name the current head.

### 5.1 Guidance parity

The LLM-facing sources are the [tool description](../../../../src/elspeth/web/composer/recovery_controls.py#L42),
[`pipeline_composer.md` § Stop authoring](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L66)
and its completion-state item 7 ([L1359](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L1359)),
the [planner terminal instruction](../../../../src/elspeth/web/composer/pipeline_planner.py#L1941),
[prose nudge](../../../../src/elspeth/web/composer/pipeline_planner.py#L1956),
[escape-hatch notice](../../../../src/elspeth/web/composer/pipeline_planner.py#L1970),
the [reply-stage notice](../../../../src/elspeth/web/composer/service.py#L1975) and
the [recovery-only prompt](../../../../src/elspeth/web/composer/service.py#L1060).
`pipeline_capabilities.md` never mentions the tool, parks or conversational
recovery (its `checkpoint` hits are runtime checkpoints). That is acceptable:
it is the plugin capability core, and the composer skill is the right owner.

Consistent with the code: singleton use, ending the turn, no manifest/review
prerequisites, “do not claim success before the receipt”, availability during
repair, restricted replies and the hatch, `CLARIFY:` being freeform-only, and
“recovery does not undo external effects or grant approval”.

Problems:

- Safe checkpoint is described as abandoning a broken draft
  ([L81–L83](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L81));
  the server picks the latest editable version, usually the current draft (N2).
  This can push the model to a reset that changes nothing but the version and proposals.
- Neither description nor skill discloses pending-proposal supersession (N1);
  a model can tell the user a proposal card is still waiting when it was rejected.
- “Turn-start reset and explicit restore never silently substitute that
  fallback” ([L91](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L91))
  conflicts with the notice for an empty turn start (N8).
- “Use only a parked ID supplied in the session context” ([L84–L86](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L84))
  names no field. Freeform injects the context as an unlabelled JSON system
  message ([L2465](../../../../src/elspeth/web/composer/service.py#L2465)); the
  planner nests it under `graph_recovery_context` ([L3483](../../../../src/elspeth/web/composer/pipeline_planner.py#L3483)).
  Neither text names `parked_graphs[].park_id`.
- The shared description's “Park keeps the visible draft” and
  “reset_to_turn_start restores the exact starting snapshot” are unconditional;
  on the recovery-only route both refuse for graph-level damage (N5).
- The skill says the receipt reports whether the displaced draft was retained
  ([L92–L93](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L92));
  the receipt is only written after the model's terminal call, so the model
  never sees it in-turn. Whether later turns' history includes the system
  notice was not traced (unverified).

## 6. Structural remediation

Preserve the existing intent → fenced settlement → receipt invariant. Integrate
recovery controls into a shared invocation descriptor that explicitly declares
their terminal status, argument contract, required authorities and receipt type;
do not route them through graph-mutation handlers merely to unify names. The
orchestrator should distinguish `conversation_handed_off`, `graph_recovered`,
and `pipeline_ready` in owned outcomes. Recovery/store ownership remains with
Sessions; plugin validation owns readiness, not checkpoint representability.

The same lifecycle distinction affects `restore_parked_graph`, advisor stops,
proposal terminals and user approval. A complete recovery catalogue needs
bounded paging or an explicit incomplete-result signal. These are contract
improvements, not authorization to migrate or erase existing history. Acceptance
must retain valid singleton recovery, invalid mixed-batch rejection, exact
rollback, empty fallback, unavailable-target refusal, fence/CAS rejection,
cancelled audit settlement and write-free completed replay. The receipt should
become the owned place that reports collateral effects (superseded proposals,
retired interpretation reviews) so descriptions can be checked against it.

### 6.1 Simple enhancements

Each is bounded and non-architectural. None is implemented.

1. **Disclose proposal supersession.** What: add a sentence to the description
   ([recovery_controls L42](../../../../src/elspeth/web/composer/recovery_controls.py#L42))
   and the skill ([L77–L96](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L77)),
   and append the superseded count to the receipt notice
   ([L9203](../../../../src/elspeth/web/sessions/service.py#L9203)). Why: N1.
   Acceptance: with one pending `set_pipeline` proposal, a reset leaves it
   `rejected` with a `composer_graph_recovery` event and the notice names it; a
   bookmark-only park leaves it pending.
2. **Test the supersession branch.** Where:
   [`test_graph_recovery_persistence.py`](../../../../tests/unit/web/coordination/test_graph_recovery_persistence.py).
   Acceptance: the test fails when the loop at L9120 is skipped.
3. **Type freeform recovery-argument rejection.** What: convert the
   `ValueError` in [`_terminal_recovery_result`](../../../../src/elspeth/web/composer/service.py#L1097)
   and `compose_recovery` into an owned malformed-provider-response error with
   attached LLM calls. Why: N3. Acceptance: a mixed batch through the detached
   turn settles with a provider-response failure code, not `operation_failed`;
   the existing no-dispatch assertions still hold.
4. **Label planner recovery evidence.** What: include the recovery names in
   `known_tools` at [L3893](../../../../src/elspeth/web/composer/pipeline_planner.py#L3893).
   Acceptance: the captured slot's `tool_name` is `handoff_to_user`.
5. **Correct the safe-checkpoint guidance.** What: say it restores the latest
   editable saved version, which can be the current draft including this
   turn's saved edits, and point to `reset_to_turn_start` for discarding the
   turn ([skill L81](../../../../src/elspeth/web/composer/skills/pipeline_composer.md#L81),
   [description](../../../../src/elspeth/web/composer/recovery_controls.py#L43)).
   Acceptance: text review plus any skill-hash pin updated deliberately.
6. **Disclose malformed-head limits.** What: state in the
   [recovery-only prompt](../../../../src/elspeth/web/composer/service.py#L1062)
   that park and turn-start reset target the unreadable head and may be refused.
   Acceptance: prompt assertion in `test_recovery_control_palettes.py`.
7. **Fix the empty-turn-start notice.** Where: [L9209](../../../../src/elspeth/web/sessions/service.py#L9209).
   Acceptance: `reset_to_turn_start` on an empty session publishes wording
   that the empty turn-start state was restored.
8. **Name the context field.** What: wrap the freeform message under the same
   `graph_recovery_context` key the planner uses
   ([L2465](../../../../src/elspeth/web/composer/service.py#L2465)) and name
   `parked_graphs[].park_id` in the skill. Acceptance: both routes' provider
   requests carry the same key; prompt-cache/transcript pins updated deliberately.

## 7. Evidence, confidence, risk and limits

Read and executed meaningful bodies in
[checkpoint qualification tests](../../../../tests/unit/web/composer/test_graph_recovery.py),
[scripted planner recovery tests](../../../../tests/unit/web/composer/test_planner_graph_recovery.py),
[turn tests](../../../../tests/unit/web/sessions/test_graph_recovery_turn.py), and
[adversarial tests](../../../../tests/unit/web/sessions/test_graph_recovery_adversarial.py).
The parent focused selection also included strict dialect and planner protocol
tests: **86 passed, exit 0**. The separately recorded
[persistence file](../../../../tests/unit/web/coordination/test_graph_recovery_persistence.py)
(**12 passed, exit 0**, [evidence record](../evidence.md)) holds the atomic
settlement, CAS, foreign-checkpoint, rollback, safe-checkpoint selection and
corrupt-park proofs. These prove local/scripted and SQLite behavior,
not remote-provider compliance, PostgreSQL contention, browser rendering or
deployment recovery. Existing browser decoder tests were located, not run.

The freeform palette and mixed-batch tests in
[`test_recovery_control_palettes.py`](../../../../tests/unit/web/composer/test_recovery_control_palettes.py)
were run in the probe pass: 22 passed, exit 0, recorded in §7.1. The sheet
review itself executed only read-only introspection with both worktree source roots (`elspeth.__file__`
resolved into the worktree): registry/canonical/MCP absence (controlled by
`set_pipeline` present), loop and planner membership for both dialects,
byte-identical parameter schemas across dialects, parser rejections (blank,
empty, 16,001-character and non-string messages, extra field, unknown action,
blank or 1,025-character call id) all being `ValueError` subclasses with
16,000 and 1,024 accepted, and planner versus freeform received-argument
labelling (`unknown_tool` versus `handoff_to_user`, with `list_sources` as
the known-name control).

Confidence in the described source/local contracts is high; the >100 park
experience is a source-established limitation needing a controlled boundary test.
N3's user-visible failure code and N1's proposal supersession are now measured
(§7.1). N1's interpretation-review consequence remains source-traced, not executed.
Implementation risk is high if unification weakens fencing or receipt binding;
incremental descriptor adoption is reversible, persisted envelope changes need
explicit compatibility handling.

### 7.1 Probe runs (2026-11-09)

Worktree `$WT` (production source at baseline `3b28d537b`), lane-private
directory `$L`. `$PY` is the main checkout's `.venv/bin/python`. Every run used
the network fail-closed prefix `HTTPS_PROXY=http://127.0.0.1:9 HTTP_PROXY=http://127.0.0.1:9 NO_PROXY= ELSPETH_DISABLE_NETWORK=1`
with `PYTHONPATH=$WT:$WT/src:$WT/elspeth-lints/src` (P0: `$WT/src:$WT/elspeth-lints/src`),
`TMPDIR=$L/tmp`, `-n 0 -p no:cacheprovider` and a matching whitespace-separated
`-o pythonpath`. Every probe file printed `elspeth.__file__` and asserted that
it resolved under `$WT/src/`. A proxy control (`$L/proxy_control.py`) returned
`ConnectError [Errno 111] Connection refused` from the dead proxy. The N3 file
also installed a provider guard: `litellm.acompletion` became a scripted double
that raises on any unscripted call, `litellm.completion` raises, and both
`provider_gateway._litellm_acompletion` and
`pipeline_planner._litellm_acompletion` raise unless the double is installed.
Its positive control showed every unscripted entry point raising.

| Probe | Finding | Outcome | Command (abridged) | Exit | Observed |
|---|---|---|---|---|---|
| P0 | §7 caveat (palette file unrun) | ran, green | `$PY -m pytest tests/unit/web/composer/test_recovery_control_palettes.py tests/unit/web/coordination/test_graph_recovery_persistence.py` | 0 | 34 passed (22 + 12) |
| N1 | N1 | **confirmed** | `$PY -m pytest $L/test_probe_n1_n2.py --rootdir $WT -c $WT/pyproject.toml -p tests.conftest -p tests.unit.web.conftest -p tests.unit.web.coordination.conftest -p tests.unit.web.coordination.test_composer_operation_authority` | 0 (5 passed) | The pending `set_pipeline` proposal was created bound to the running job. After `reset_to_turn_start` it was `rejected` with events `[proposal.created by composer-web:user:alice, proposal.rejected by composer_graph_recovery]`. The only receipt notice did not mention it. The control, a bookmark-only `park` (`state_id == expected_state_id`), left it `pending` with no rejection event |
| N2 | N2 | **confirmed** (session-service layer) | same file and run as N1 | 0 | Two different same-operation `tool_call` checkpoints were saved. Safe-checkpoint targeted the newer one (`empty_fallback=False`). The restored v3 held the newer content, `is_valid=False`, derived from the newer checkpoint. The `reset_to_turn_start` control targeted `None` (`empty_fallback=True`) and discarded both |
| N3 | N3 | **confirmed** | `$PY -m pytest $L/test_probe_n3.py --rootdir $WT -c $WT/pyproject.toml -p tests.conftest -p tests.unit.web.conftest` | 0 (5 passed) | Full `POST /messages` path through the detached worker. Control: a freeform singleton `park` completed. Freeform `[handoff_to_user, set_metadata]`: HTTP 500, `operation_failed`, with a diagnostic id; the raised chain was `ValueError: Recovery controls must be the sole tool call in a response`. Planner `[handoff_to_user, list_sources]`: HTTP 502, `http_error`, `composer_planner_failure` / `invalid_provider_response` / `MALFORMED_RESPONSE`. One provider call per route |

N4 was already demonstrated in process (§7, above) and was not re-run.

## Runtime argument schema and measured exposure

This async/terminal control has no synchronous declaration; its specialized owner is traced above.

| Surface | Exposed |
|---|---|
| Canonical emitter | no |
| Freeform loop | yes |
| Planner default | yes |
| Composer MCP | no |

Both measured provider dialects have the same name membership. Wire schemas, required-null projection, phase/policy filtering and available dependencies can still differ; exposure does not certify execution readiness.
Re-measured during sheet review: the parameter schema below is identical in
both dialects; `openai_strict` adds only `"strict": true` to the function.
The loop places it third from last, before `restore_parked_graph` and the
trailing cache-anchor entry.

This is the emitted recovery transport schema. It deliberately omits string length keywords; the semantic Pydantic parser still enforces the bounded/nonblank fields described above. The schema has no plugin-option fields.

```json
{
  "additionalProperties": false,
  "description": "A terminal request, never an assertion that recovery already succeeded.",
  "properties": {
    "action": {
      "enum": [
        "park",
        "reset_to_turn_start",
        "reset_to_safe_checkpoint"
      ],
      "title": "Action",
      "type": "string"
    },
    "message": {
      "title": "Message",
      "type": "string"
    }
  },
  "required": [
    "action",
    "message"
  ],
  "title": "HandoffArguments",
  "type": "object"
}
```
