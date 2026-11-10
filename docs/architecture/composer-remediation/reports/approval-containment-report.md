# Explicit-approval proposal containment

Date: 10 November 2026. Branch `fix/composer-0812-approval-containment`, cut
from `2f38b807b`. Code commit: `39da736ec`, plus one test-only commit adding
the inline-custody case. Not pushed.

This is the containment for defect A of
`docs/architecture/composer-remediation/reports/astra-followup-defect-research.md`
(reproduced live as failure 1 of
`docs/architecture/composer-remediation/reports/deepseek-followup-tool-probe.md`).
It is deliberately not the cumulative-proposal redesign; what stays broken is
listed under "Scoped follow-up".

## What I verified in the code before building on the research

All read at `2f38b807b`.

| Research claim | Result |
|---|---|
| Each staged edit is previewed against the batch's working `state` and published with `current_state_id` as its base; the staging branch `continue`s without assigning the preview to `state`. | Confirmed in `run_tool_batch`, `src/elspeth/web/composer/tool_batch.py` (the `explicit_approval_required` block and the `proposals_this_turn += 1` branch). |
| Accept refuses a non-null base that is not the current head with `proposal_base_state_changed`, and the message asks for a rebase that no route provides. | Confirmed, `src/elspeth/web/sessions/routes/composer/proposals.py` (accept route, the `current_record.id != proposal.base_state_id` check). |
| `APPROVAL_REQUIRED` is `success=True`, `updated_state=state`, with the validation of the unchanged state. | Confirmed (same branch of `run_tool_batch`). |
| The per-turn cap raises `ComposerConvergenceError` only after the admitted prefix was published. | Confirmed (`_pending_proposal_cap_error`, raised inside the publication branch). |
| A reusable ordinary-refusal seam exists. | Confirmed and used: the `batch_dependency_refused` block in `run_tool_batch` records one audited `finish_success` with a `success=False` rejection result per refused call. |
| The UI lets a user reject a stale proposal. | Confirmed in `src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx` ("You can still reject it"; only Accept is disabled). |

Two things the research did not say, found while reading:

- Blob-only proposals (`update_blob`, `delete_blob`) hit the same accept-time
  base check until their effect is applied (`blob_effect_applied` is the only
  exemption). They therefore share the staleness defect when a composition
  proposal is accepted first. See "Decisions".
- A new `error_code` at a producer site must be a string literal and
  registered in `src/elspeth/web/composer/error_codes.py`
  (`tests/unit/web/composer/test_error_code_redaction.py` censuses both). The
  guard therefore keeps three literal call sites rather than forwarding a code
  through a variable.

## The change

Production diff against `2f38b807b`: `src/elspeth/web/composer/tool_batch.py`
+86/−5, `src/elspeth/web/composer/error_codes.py` +2/−0 (`git diff --numstat`:
88 lines added under `src/`, 81 of them non-blank, comments and docstrings
included). No schema, SQL, write, lock or
envelope-key change.

1. `_stages_composition_proposal(tool_name)`: a mutation tool that is not
   blob-store-only.
2. `_approval_containment_refusal(calls, sessions_service, session_id)`, called
   once per batch before the dispatch loop and only when the session is in
   `explicit_approve`:
   - more than one composition-changing call in the batch, decided from tool
     names alone, returns the "batch" refusal;
   - exactly one such call triggers one read,
     `list_composition_proposals(session_id, status="pending")`; any pending
     row whose tool is not blob-store-only returns the "pending" refusal, which
     names the proposal id and tool name (and how many more are pending).
3. In the loop, at the existing `batch_dependency_refused` site (after the
   audit envelope is open and after JSON/canonicalisation/required-path
   argument errors), a composition-changing call with a containment refusal
   takes the same audited refusal path with `error_code`
   `approval_batch_not_atomic` or `approval_proposal_pending`. Nothing is
   previewed, no candidate is built, no proposal or blob is published.
4. The `APPROVAL_REQUIRED` `data.message` now reads: "The proposal is recorded
   and awaits human approval; it has not been applied. This result's validation
   describes the unchanged current pipeline and does not validate the proposed
   change. Send no further pipeline changes until the user accepts or rejects
   this proposal." The `_ProposalPayload` key set is unchanged.

Model-visible refusal texts:

- `approval_batch_not_atomic`: "This session requires approval for pipeline
  changes and this response carried N of them. All N were refused before any
  effect: nothing was applied and no proposal was recorded. Separate proposals
  are each drawn against the same saved pipeline, so the user could only ever
  accept one of them. Submit the whole intended result as one change: a single
  set_pipeline call containing every edit, with no other pipeline mutation in
  the same response."
- `approval_proposal_pending`: "Proposal `<id>` (`<tool>`) is still awaiting
  the user's decision. Nothing from this call was applied and no new proposal
  was recorded. Do not retry or rephrase this change: tell the user they must
  accept or reject the pending proposal first, then ask again."

The persisted tool row keeps the registered `error_code` but redacts the
message text, as for every other rejection; the model sees the full text in
the provider round that follows the refusal.

Composer invariants: the server refuses and asks; the planner still authors
every structure. No tutorial-specific branch.

## Decisions taken (please confirm or overrule)

1. **Blob-only proposals are neither counted nor refused.** The brief says to
   preserve approval-required destructive blob behaviour, and several blob
   deletions in one response have no single-call equivalent to redirect the
   model to. Consequence: "at most one outstanding proposal" is true of
   composition-changing proposals only. A pending `delete_blob`/`update_blob`
   proposal still goes stale if a composition proposal is accepted before it.
2. **Any pending composition proposal blocks**, including a stale leftover
   from before this change and a `set_pipeline` left pending by an
   `auto_commit` review handoff before the user switched to
   `explicit_approve`. The user must reject (or accept) it; the model is told
   so. A session that already holds stale proposals needs each rejected by
   hand before a new change can be staged.
3. **The pending-proposal cap stays.** It is now reachable only through
   blob-only proposals, where it still publishes a prefix before raising.
4. **Not guarded: the empty-pipeline planner publisher**
   (`PlanningApplication._stage_pipeline_plan` in
   `src/elspeth/web/composer/planning_application.py`). It publishes one
   complete pipeline proposal per planning run without consulting pending
   rows. Read, not exercised: a second compose request against a still-empty
   pipeline can plausibly add a second same-base complete proposal. Those are
   whole alternatives, not dependent fragments, so they cannot leave a
   half-applied pipeline, but the losers do go stale.
5. **The pending lookup is check-then-act**, not a database constraint. It
   relies on compose requests for one session being serialised. The proposal
   accept and reject routes take the per-session compose lock, and a comment in
   `src/elspeth/web/sessions/routes/composer/state.py` calls it "the same
   compose exclusion domain as accept/compose"; I did not trace the compose
   route's own acquisition.

## Tests

Interpreter for every run: the worktree's `.venv` python with `PYTHONPATH`
exporting the worktree's `src` and `elspeth-lints/src`, plus the matching
`-o pythonpath`; `elspeth.__file__` and `elspeth_lints.__file__` both printed
paths inside this worktree. Logs are under
`.claude/lanes/approval-containment/` (gitignored).

New: `tests/integration/web/composer/test_explicit_approval_containment.py`
(real compose loop, real SQLite session store, scripted provider), 12 cases:

- two staged edits in one batch (two incremental; incremental plus
  `set_pipeline`; incremental plus a `set_pipeline` carrying an inline blob)
  with a discovery call between them: no proposal, no blob row, no handler
  or candidate builder run for the refused calls, the discovery call still
  succeeds, each refused call gets a `success=false` result carrying
  `approval_batch_not_atomic`, three audited invocations and three persisted
  tool rows; a follow-up single `set_pipeline` then stages exactly one;
- a later provider round while a proposal is pending;
- a later compose request while a proposal is pending (incremental and
  `set_pipeline`), then admitted after the user rejects the pending one;
- a single `set_metadata` and a single `set_pipeline` each stage exactly one
  proposal, with the exact five-key payload and the new wording;
- `auto_commit`: a two-mutation batch applies both immediately even with a
  pending row present, and never performs the pending-status lookup;
- blob deletions stage beside one composition proposal; a pending blob-only
  proposal does not block a composition proposal; ten blob deletions plus
  discovery plus a real `create_blob` sit exactly at the cap without raising.

| Run | Command scope | Exit | Result |
|---|---|---|---|
| Red, before the change (`red-new-tests.log`, 10 cases at the time) | new file | 1 | 8 failed, 2 passed (the two that passed were `auto_commit` and pending-blob, which hold with or without the guard) |
| Green, after the change (`final-new.log`) | new file | 0 | 12 passed |
| Affected modules (`affected-3.log`) | `test_compose_loop_persistence.py`, `test_proposal_cap_turn_counts.py`, `test_tool_batch_rejection_llm_call_audit.py`, `test_freeform_proposal_prevalidation.py`, new file; `-n 6` | 0 | 272 passed |
| Broad, on the committed tree (`broad-2.log`) | `tests/unit/web/composer`, `tests/integration/web/composer`, `tests/unit/web/sessions`, `tests/unit/web/test_sessions_composer_attribute_contracts.py`; `-n 6` | 1 | 7 failed, 17603 passed. The failed set is exactly the seven below, which fail on the base commit too |
| `ruff check` / `ruff format --check` on the seven touched files | | 0 / 0 | clean |
| `mypy` on `tool_batch.py`, `error_codes.py` | | 0 | no issues |
| `python -m scripts.check_contracts` | | 0 | census matches the pin |
| Named gate tests (`final-new-and-gates.log`) | `tests/unit/elspeth_lints/test_masquerade_gate.py`, `tests/unit/scripts/test_check_contracts.py`, `tests/unit/architecture/test_pipeline_publication_authority_guards.py`, new file | 0 | 345 passed |
| `tests/unit/architecture` (`gates-1.log`, run together with the three files above) | whole directory | 1 | 57 failed, 2275 passed, 1 xfailed; all 57 are in two files and fail identically with the production files at base content (below) |
| Keyless trust-tier scan, `--rules all --root src/elspeth` | HEAD `39da736ec` and a full `git archive` copy of `2f38b807b` | 1 / 1 | 3722 output lines each; after stripping line numbers and the checkout root the two outputs are identical (`diff` exit 0). No finding added or removed, including for `tool_batch.py` |
| `trust_boundary.tests,trust_boundary.scope,trust_boundary.tier` | HEAD | 0 | exit 0. I ran no positive control for this instrument, and no `@trust_boundary` site or test referenced by one was edited |

The broad run covers the modules the brief named: tool batch, proposal cap and
turn counts, proposal accept
(`tests/unit/web/sessions/routes/composer/`), the envelope gate
(`tests/unit/web/composer/test_tool_result_envelope_gate.py`), error-code
redaction and prevalidation.

### Failures that are not from this change

**Architecture gates (57).** `tests/unit/architecture/test_web_landscape_mutation_fencing.py`
(36 FAILED lines) and `tests/unit/architecture/test_web_reserve_reviewed_contracts.py`
(21) fail. Their findings name `src/elspeth/web/async_workers.py`,
`src/elspeth/web/required_executor.py`, `src/elspeth/core/landscape/*`,
`src/elspeth/web/execution/recovery.py` and similar; none names a file in this
diff (the log has 0 lines matching `tool_batch`, against 23 matching `async_workers`).
Like-for-like control: I wrote the base content of the two production files
back into this worktree (`git diff 2f38b807b -- src` empty), ran the two test
files, and restored from HEAD. Result: exit 1, 57 failed, 862 passed, and the
sorted FAILED set is identical to the one from my tree
(`arch-inplace-base.log`). A first attempt against an extracted `git archive`
copy was not like-for-like (98 failed there), so I do not rely on it.

**Composer and sessions directories (7).**

Seven tests fail in the broad run. The sorted FAILED set of the final run
(`broad-2.log`, HEAD `39da736ec`) is identical to the set from running those
modules alone, and none of them involves explicit approval:

- `tests/unit/web/composer/test_failure_validation_guidance.py::TestFreeformRejectionCarriesCataloguedRepairText::test_invalid_source_options_carry_the_catalogued_fix`
- `...::TestFreeformRejectionCarriesCataloguedRepairText::test_plugin_repair_preserves_explicit_requirements_and_reports_capability_gaps`
- `...::TestValidationGuidanceCustody::test_provider_sees_the_text_but_persistence_sees_a_placeholder`
- `tests/unit/web/composer/test_tool_dispatch_boundary.py::test_source_path_arguments_require_data_dir_for_s2_confinement`
- `tests/unit/web/composer/test_tool_dispatch_boundary.py::test_source_path_arguments_require_data_dir_without_audit_hash`
- `tests/unit/web/composer/test_checkpoint_owner_compatibility.py::test_checkpoint_no_return_retains_projection_until_actual_generation_closure[True]`
- `tests/unit/web/sessions/test_static_direct_writers.py::test_static_direct_writers_match_reviewed_allowlist`

Base control: I extracted `2f38b807b` (`src`, `tests`, `elspeth-lints`,
`config`, `scripts`, `pyproject.toml`) with `git archive` into the gitignored
lane directory, confirmed `elspeth.__file__` resolved inside that copy, and ran
the four modules there: exit 1, the same seven test ids failed
(`unrelated-base.log`). The copy was deleted afterwards.

That control is like-for-like for the first six. It is **not** like-for-like
for `test_static_direct_writers`: in the partial copy the guard reported a
different finding set (reviewed sites "found 0"), so the copy only shows that
the test was already red, not why. In this worktree its single finding is
`tests/integration/web/test_shareable_reviews_routes.py:606`
(`chat_messages` insert), a file with an empty diff against `2f38b807b`, and
that was still the only finding in the final run. The source-path failures
(five of the seven) expect `plugin_options_invalid` / a data-dir message and
get `source_path_policy_invalid`. I did not diagnose any of the seven.

All 64 are red on the commit this branch was cut from, so they will be red on
whatever lands from it today unless fixed separately.

### Negative control

With the single line that applies the guard replaced by `containment = None`
(nothing else touched), the new file went red: exit 1, 6 failed, 5 passed
(`negative-control.log`; the file had 11 cases then, before the inline-custody
case was added). The six are every batch, later-round, later-request and
blob-beside-composition case. The five that stayed green are the ones that
do not depend on the guard (single edit ×2, `auto_commit`, blob cap, pending
blob). The line was restored and `git status` showed a clean tree afterwards.

The wording assertion has its own control in the red run: both single-edit
cases failed on the old message before the change.

### Not run

- The full `pytest tests/` suite.
- **PostgreSQL contention coverage (`-m testcontainer`) was not run locally.**
  The change does not touch persistence: no schema, SQL, write path or lock.
  It adds one existing read (`list_composition_proposals` with
  `status="pending"`) on an existing service method. I read the one
  testcontainer file that mentions `explicit_approve`
  (`tests/testcontainer/web/test_session_mutation_fencing_postgres.py`): it
  covers trust-mode preference transitions only and pins no multi-proposal
  behaviour, so nothing there needed porting.
- Frontend tests (no frontend file changed).

## Tests whose contract I changed, and why

All of these pinned several same-base composition proposals from one batch, or
a second composition proposal while one was pending. None was skipped, xfailed
or loosened; each asserts the new contract or keeps its original purpose
through blob-only proposals, which are the only way left to reach the cap.

| Test | Old contract | New contract |
|---|---|---|
| `tests/unit/web/composer/test_compose_loop_persistence.py::test_explicit_approval_batch_caps_after_first_ten_admitted_publications` | 10/11 `set_metadata` proposals in one batch; cap publishes a prefix of ten | Same assertions, staged as 10/11 `delete_blob` proposals over distinct blobs (new helper `_blob_deletion_calls`) |
| `...::test_proposal_cap_excludes_schema_errors_and_semantic_refusals` | nine `set_metadata` proposals + a schema-invalid `set_metadata` + a semantically invalid `set_pipeline` | nine `delete_blob` proposals + a schema-invalid `delete_blob` + the same `set_pipeline`; handler-call expectation is now empty because blob deletions are staged without a preview |
| `...::test_proposal_attempt_cap_excludes_discovery_and_immediate_create_blob` | discovery + `create_blob` + ten `set_metadata` proposals | Renamed `test_discovery_and_immediate_create_blob_do_not_block_the_one_staged_proposal`: discovery + `create_blob` + one `set_metadata` stages exactly one. The cap-exclusion property moved to the new integration file, where `create_blob` runs with real custody (this unit harness has no `user_message_id`, so a real `create_blob` cannot run in it) |
| `...::test_proposal_attempt_cap_includes_approval_required_blob_only_mutation` | nine/ten `set_metadata` proposals then a `delete_blob` | nine/ten `delete_blob` proposals then the `delete_blob`; assertions unchanged |
| `tests/unit/web/composer/test_proposal_cap_turn_counts.py::test_proposal_cap_after_a_discovery_turn_reports_turns_used_and_failed_turn` | eleven `set_metadata` | eleven `delete_blob`; assertions unchanged |
| `tests/unit/web/composer/test_tool_batch_rejection_llm_call_audit.py::test_proposal_cap_rejection_is_convergence_error_carrying_llm_calls` | eleven `set_metadata` | eleven `delete_blob`; assertions unchanged |
| `tests/integration/web/composer/test_freeform_proposal_prevalidation.py::test_proposal_cap_counts_only_admitted_changes` | ten `set_metadata` proposals plus a tail; three eleven-call non-admitted cases built from composition tools | ten `delete_blob` proposals plus the same single-call tails. The eleven-call cases `noop`, `refusal`, `argument` are replaced by `blob-refusal` and `blob-argument`: eleven composition calls are now refused whole and would have passed for the wrong reason |
| `...::test_inline_same_text_with_new_blob_authority_is_a_real_change` | a second `set_pipeline` proposal staged while the first was pending | the first proposal is rejected through `reject_pipeline_composition_proposal` before the second request; the "same bytes, new authority is a real change" assertions are unchanged |
| `...::test_publication_resource_race_refuses_once_then_allows_valid_repair` | 3 publishers × {later, same}, with ten same-batch repairs | later × 3 publishers and same × the blob publisher only, one repair each. A same-response composition repair beside a composition publisher is now refused before publication. **Lost:** the "refused publication plus ten admitted share one counter" boundary; the nearest remaining pin is `at-cap-blob-refusal` in the cap test above |

`tests/unit/web/composer/test_service.py` still contains the old message text
inside a hand-built audit payload fixture. It does not pin the producer and I
left it.

## Proposed skill sentence

I did not edit `src/elspeth/web/composer/skills/*.md` or
`tests/unit/web/composer/test_prompts.py`. For the `APPROVAL_REQUIRED`
paragraph of `pipeline_composer.md`:

> When the session requires approval, make each change as ONE call: a single
> incremental mutation, or — whenever the change needs more than one edit —
> one complete `set_pipeline` carrying all of them. Several pipeline mutations
> in one response are all refused (`approval_batch_not_atomic`) and nothing is
> recorded. `APPROVAL_REQUIRED` means the proposal is recorded, not applied:
> the `validation` beside it describes the unchanged current pipeline, so
> never report the proposed change as validated. While a proposal is pending,
> further pipeline changes are refused (`approval_proposal_pending`); tell the
> user to accept or reject the pending proposal and stop.

The skill's existing advice to prefer narrow tools per edit conflicts with
this in approval mode and should be qualified there.

## Scoped follow-up (still broken after containment)

1. **Already-staged stale proposals.** Sessions that hold same-base siblings
   from before this change keep them as `pending`; accept still returns 409
   `proposal_base_state_changed` and the text still says "Ask ELSPETH to
   rebase", which nothing implements. Under decision 2 they also block new
   changes until rejected one by one. Needed: an explicit retirement path
   (bulk reject or a stale terminal state) and honest 409 wording.
2. **Invalid incremental previews at accept.** A single incremental edit that
   leaves the graph invalid (the probe's gate rewire that orphaned a sink) is
   still staged, and accept still publishes the invalid state. Containment
   removes the forced partial accept, not this. Needed: refuse an invalid
   ordinary preview at staging with the candidate's findings, and an
   accept-time policy for old invalid proposals.
3. **The envelope still carries a green validation of the unchanged state.**
   Only the message says whose it is. Needed: an explicit validation-subject
   field (with the payload type, teaching and census gates updated) or
   candidate validation carried separately.
4. **Cumulative or chained proposals** (research options 1 and 2), so a
   multi-edit request does not have to be re-authored as one `set_pipeline`.
5. **Blob-only proposals** go stale behind an accepted composition proposal,
   and the cap still publishes a prefix for them (decisions 1 and 3).
6. **The planner publisher** is unguarded (decision 4).
7. **Model cost.** A model that sends several edits spends one refused round
   before re-authoring. Whether weaker planners reliably recover with one
   `set_pipeline` was not measured against a live provider.

The accept-time base check is untouched.

## Process note

The first production edits used the Edit tool. The later restructure of the
guard to literal error codes, the two registry lines and the test ports were
applied with exact-match string replacements, each asserted to match exactly
once, and reviewed in the diff. That departs from the brief's "Edit tool for
edits". No `sed`/`awk`, no suppression comments,
no skips or xfails were added.
