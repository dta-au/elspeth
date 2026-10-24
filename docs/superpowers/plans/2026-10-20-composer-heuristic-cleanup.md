# Composer Heuristic Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Finish the bounded Composer heuristic cleanup and complete-original attachment credential inspection while preserving existing authoring, review, custody and resource controls.

**Architecture:** Preserve inherited fixes and the separately prepared frontend packet, move ordinary authoring onto the existing common provider loop, and retire its unreachable private planner facade. Attachment reads receive an explicit deployment inspection bound and freshly inspect their complete immutable original on every read; identity records never cache validation clearance. The existing .9 writer owns both the overlapping narrow guard correction and the separate durable .10 PromptShield route.

**Tech Stack:** Python 3.12+, existing Pydantic/SQLAlchemy/session custody, pytest, React/TypeScript and the existing Vitest/ESLint harness.

---

## Source binding and present boundary

This is an implementation-plan packet; its proposed H2/H3 tests have not run.
The main plan records actual committed/staged source and qualification.
Local `.9` qualification has completed, and isolated `.10` integration exists.
The `.9` owner has priority at measured resource contention. Bespoke check
controllers and capacity reservations remain shelved.

Read-only Git inspection resolves these immutable inputs:

- Composer+ `c299f2e7d852f897d0e34a1178aed4aa732cbd41`, parent
  `67ad14c22b3987a16f0e9a71155102ee451da342`, tree
  `afd97b96a87710722430f5f85b509b8823b603ff`.
- Committed .9 `ce4103539fe97f07fc60fb0cd096256c207c8bc5`. This commit is an
  inspected input, not a substitute for its owner's pending updated checkpoint.
- The owner has now produced `b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree
  `6a34e912debc469e9ce7e2048d433c08d5e606a9`, parent ce410. Normal full
  qualification subsequently completed at its test-only child `2e869240`.
  Its three production changes
  preserve actual Advisor exception classes, separate request-rejection evidence
  and the explicit unconfigured-debug guard size policy. Retain them at P0.
- The two branches diverge from 67ad. Carry c299's exact guidance changes onto
  the actual updated .9 checkpoint only in an authorized isolated integration;
  do not replay inherited history or copy active dirty files.

The chosen source remains version 0.8.9. This document selects no release
version bump, data migration/reset, operator signing, push, merge or deployment.
Preserve the existing .9 writer, security owner, donor files, all old worktrees,
review reports, the frontend checkpoint and planning drafts.

Read the repository's `CONTRIBUTING.md` whole-tree gates before writing code.
Run affected checks and relevant whole-tree gates under ordinary fresh
admission; no historical pass grants present runtime or shared-host capacity.
`docs/design/composer-plus-dot10-carryforward.md` owns the integration and
fresh-validation boundary. `docs/design/composer-continuation-plan.md` remains
the reviewed semantic design; this companion supersedes its old reusable
attachment-clearance interpretation.

## What is already present and what remains

| Design unit | Immutable source observation | Disposition |
| --- | --- | --- |
| H1 hidden pre-state retry | The complete retry branch and its classifier/helper imports are absent from c299 and ce410. | Preserve; add the behavioral regression below without recreating the branch. |
| H1 English intent classifier | `no_tool_policy.py` no longer defines the classifier; no live classifier consumer was found in the committed Composer source. | Preserve; do not rebuild grammar or authority heuristics. |
| H1 frontend waiting view | Ordinary checkpoint `52543bcc195419561b32412c79f4004e3fa8af62`, parent c299, tree `e6cb88e8d1c11de4ea2ae653b5971201f6472c33`; exact tested component SHA256 `d8b7598ee23cc60dd1f2a2481e832fdb72112c3f09b5ab8cdbdbdee8017e910a` and test SHA256 `1b46e25a2ad7520fde533c7ee2ab065f8eb238df9fafc6b09a563ff7c143bf8e`. Scoped 131-test/ESLint pass and 985e closure remain retained; ordinary branch-safety/hooks exited 0 at that checkpoint. | Already carried as `dd288ae72b20e4ceb14accba14e7a5f6b1a452ac` in the current combined source. Preserve it; do not redraft, copy dirty source or replay checks merely for this plan. |
| H2 routing | Both inspected commits still select `PlanningApplication._plan_and_stage_empty_pipeline` for an empty graph plus session/message prerequisites (`service.py:1233`–`:1273`). | Remaining: use the common loop and retire the private facade. |
| H2 chronology | `planning_application.py:193` already preserves every authenticated user request and separate assistant context, without first-plus-last-N truncation or lexical selection. | Move this working projection into a shared owner; preserve it and its direct-library tests. |
| H2 plugin mention debt | `_intent_selected_schema_keys` is absent; `pipeline_planner.py` starts pending information from the actual discovery manifest. | Preserve; no new token-based obligation or duplicate repair. |
| H3b logical/physical guard split | The committed snapshots still construct `_request` before the absent-mount branch (`prompt_guard.py:446`–`:461`). The existing .9 writer owns an active narrow correction. | Dependency only here; do not implement a competing patch from this companion. |
| H3c attachment inspection | `tools/blobs.py` currently scans the complete decoded payload under the generic scanner string bound, and `read_attachment` confuses that with its response bound. | Remaining bounded packet below: an explicit inspection resource and a fresh full-original credential scan on every read. |

The separate Advisor repair is the selected net comparison from donor parent
`2d5c0557e2d3dfcf6f176b94e9d17290d3da7a31` to
`e6b247943f911637f144fd11ba1c8130397fa2a9`. Its final tip and entire b09
parent branch are not that selected repair. URL retention remains excluded.

## File ownership map

| Files | Responsibility |
| --- | --- |
| Existing two `ComposingIndicator` TSX files | Already prepared frontend packet; excluded from new edits in this companion. |
| `src/elspeth/web/composer/service.py`, `request_history.py`, `planning_application.py`, `prompts.py`, `src/elspeth/web/app.py` | Common ordinary entry, complete history projection and dead-facade retirement. |
| `src/elspeth/web/composer/attachment_admission.py` | Pure bounded fresh complete-original inspection and nominal identity receipt; no cache, persistence or provider adapter. |
| `src/elspeth/web/composer/tools/blobs.py`, `tools/_common.py`, `tools/_dispatch.py`, `tool_batch.py`, `protocol.py`, `src/elspeth/web/config.py` | Policy plumbing and actual same-session custody/read/inspection path; existing page and whole-response contracts retained. |
| Existing affected Python tests and new `test_request_history.py`, `test_attachment_admission.py` | Behavioral regressions, real caller wiring, identity/credential/resource negatives. |
| Existing .9 owner's `prompt_guard.py`/related tests and durable .10 route | External dependency with sole owner; this companion makes no implementation edits there. |

## Execution prerequisites and ordinary commands

Do these only when source execution is resumed after actual .9 GREEN. Check
real host contention through normal repository workflow; no numeric admission
or blanket reservation applies. Start in the authorized isolated implementation checkout,
not this historical planning checkout or another owner's active tree.

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
COMPOSER_LOGS="$COMPOSER_WORKTREE/.claude/lanes/composer-heuristic-cleanup/checks"
cd "$COMPOSER_WORKTREE" && mkdir -p "$COMPOSER_LOGS"
```

Use the already admitted checkout-local environment. If the interpreter or
locked frontend prerequisites are absent, stop the affected check and report
the missing prerequisite. Do not run `uv run`, rebuild an incomplete environment,
symlink the primary environment, install packages or claim RED from missing tools.
Before Python checks, ordinary provenance verification is:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
  "$COMPOSER_PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Both printed package paths must resolve inside the implementation checkout.
The commands below call the existing pytest harness directly with `-n 0`,
complete log capture and the native exit code. Expected RED means the named
behavioral assertion fails; tool/environment/collection errors are not a
behavioral RED. Make each new module importable before its specified mutation
control, then restore the correct code and require native GREEN.

A commit checkbox means an ordinary reviewable checkpoint after its focused
GREEN checks, current staged-path inspection and the repository's normal
`scripts/branch-safety-check.sh --intent commit`. Hooks may execute project code;
no commit step is authorized during this planning-only turn. Run local Ruff
before any later push; integration gates follow the actual change reach.

## Task H0 — Reconcile the selected net Advisor repair once

**Files and implementation packet:** S7.2 in
[`2026-10-20-composer-continuation-client.md`](2026-10-20-composer-continuation-client.md)
owns the exact seven-path selected net repair. Execute that unit once at P0
on the actual .9 GREEN integration base; S7 later verifies preservation rather
than applying it a second time. The source comparison independently reproduced
the owner's 47,088-byte patch, SHA-256
`8af35dea091d2057c23ce1999fa2261252c01d62b07f1001ab22ec7100d5df9e`.

- [ ] Read the selected semantic hunks and curate only missing changes:
  upstream scrape provenance, original/current field identity, normalized-name
  precedence on the same arriving row, conservative alternative-arrival and
  error paths, reviewed accepted-value binding, prompt labels as text and
  factual provider progress. Retain current .9 rejection/guard changes and the
  explicit URL-retention exclusion. The original b09 document is historical
  rationale and contributes no current validation claim.
- [ ] Run the selected repair's affected tests and the independent .9
  rejection/source no-op counterevidence with ordinary pytest:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 \
 tests/unit/web/composer/test_compose_loop_interpretation_review_dispatch.py \
 tests/unit/web/composer/test_progress.py \
 tests/unit/web/sessions/test_interpretation_events_service.py \
 tests/unit/web/test_interpretation_state.py \
 tests/unit/web/composer/test_source_rejection_contracts.py \
 tests/unit/web/composer/test_patch_source_options_noop.py \
 > "$COMPOSER_LOGS/advisor-net-python.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/advisor-net-python.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Review actual resulting files, stage only selected repair hunks and use
  ordinary branch-safety/hooks with commit message
  `fix(composer): reconcile upstream cleanup and factual guidance`.
  A fully represented repair needs only the ordinary PR explanation, not an
  empty commit. Historical test results do not qualify the new combined head.

## Task H1 — Preserve the inherited no-retry contract

**Files:** preserve the inspected c299/ce410 source's `composition_completion.py` and
`no_tool_policy.py`; extend `tests/unit/web/composer/test_rootless_no_tool_recovery.py`.

- [ ] Add this test to the existing module, using its existing imports and
  `_make_llm_response`, `_composer_service_with_session`, `_mock_catalog`,
  `_make_settings`, `_empty_state` helpers. The failed tool is deliberately the
  actual interpretation tool, so this proves provider transition behavior rather
  than searching for a deleted helper name.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Build a pipeline that reads CSV and writes JSON.",
        "Please assemble the requested workflow.",
        'Explain the example "build a pipeline" without changing it.',
        "Do not build anything; explain the review error.",
        "Read the complete instruction before replying. " + "neutral detail " * 400,
    ],
)
async def test_pre_state_review_error_does_not_inject_a_host_tool_sequence(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    refusal_reply = "The interpretation review could not be staged; you can ask me to retry."
    replies = [
        _make_llm_response(
            tool_calls=[
                {
                    "id": "review-before-state",
                    "name": "request_interpretation_review",
                    "arguments": {
                        "affected_node_id": "rank",
                        "kind": "vague_term",
                        "user_term": "urgency",
                    },
                }
            ]
        ),
        _make_llm_response(content=refusal_reply),
    ]
    with patch.object(service._provider_gateway, "_call_llm", side_effect=replies) as provider:
        result = await service.compose(user_message, [], _empty_state(), session_id=sid)
    assert provider.call_count == 2
    assert result.repair_turns_used == 0
    assert [entry.tool_name for entry in result.tool_invocations] == ["request_interpretation_review"]
    assert result.tool_invocations[0].status.value == "arg_error"
    assert result.tool_invocations[0].error_message
    assert not result.state.sources
    assert not result.state.nodes
    assert result.message.startswith(refusal_reply)
    sent_text = "\n".join(
        message["content"]
        for call in provider.call_args_list
        for message in call.args[0]
        if isinstance(message.get("content"), str)
    )
    assert "First call set_pipeline or another state-staging tool" not in sent_text
    assert "Do not reply to the user yet" not in sent_text
```

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_rootless_no_tool_recovery.py tests/unit/web/composer/test_no_tool_finalize_disclosure.py > "$COMPOSER_LOGS/h1-inherited.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h1-inherited.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Expected native exit 0 when executed against the integrated source. This
  verifies inherited behavior; no GREEN run was executed while writing the plan.
  do not manufacture a production edit merely to obtain RED. Existing no-tool
  zero-attempt tests provide the no-prior-failure control, and existing pending
  interpretation/orphan tests remain selected at integration.

- [ ] Commit the added regression test after staged-path preflight:

```bash
git -C "$COMPOSER_WORKTREE" add -- tests/unit/web/composer/test_rootless_no_tool_recovery.py
git -C "$COMPOSER_WORKTREE" commit -m "test(composer): pin provider-owned recovery after review refusal"
```

## Task H2.1 — Give every ordinary request the same provider entry

**Files:** modify `src/elspeth/web/composer/service.py`; extend
`tests/unit/web/composer/test_rootless_no_tool_recovery.py`.

- [ ] Add this test. `_insert_user_message` is the existing helper in
  `tests/unit/web/composer/test_service.py`; import it in the existing helper
  import block. Unlike pre-existing tests without a message ID, this test meets
  the old shortcut's actual persisted-message prerequisites.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize(
    "user_message",
    [
        "Build a pipeline that reads CSV and writes JSON.",
        "Please create the requested workflow.",
        "How does CSV parsing work?",
        "Do not change the graph; explain routing.",
        'What does "source:csv" mean?',
        "neutral context " * 400 + "Please describe the available inputs.",
    ],
)
async def test_persisted_empty_request_uses_the_common_provider_entry(user_message: str) -> None:
    service, sid = _composer_service_with_session(_mock_catalog(), _make_settings())
    assert service._session_engine is not None
    request_id = _insert_user_message(service._session_engine, sid, user_message)
    with (
        patch.object(
            service._provider_gateway,
            "_call_llm",
            return_value=_make_llm_response(content="Here is an explanation without changing the graph."),
        ) as provider,
        patch("litellm.acompletion", side_effect=AssertionError("common gateway was bypassed")),
    ):
        result = await service.compose(
            user_message,
            [],
            _empty_state(),
            session_id=sid,
            user_id="test-user",
            user_message_id=request_id,
        )
    assert provider.call_count == 1
    assert result.message.startswith("Here is an explanation without changing the graph.")
    assert result.state.version == 1
    assert not result.state.sources and not result.state.nodes and not result.state.outputs
    assert not result.tool_invocations
    assert result.graph_recovery_intent is None
    prepared_messages, prepared_tools = provider.call_args.args[:2]
    assert any(row["role"] == "user" and row["content"] == user_message for row in prepared_messages)
    names = {tool["function"]["name"] for tool in prepared_tools}
    assert "set_pipeline" in names
    assert "emit_pipeline_proposal" not in names
```

- [ ] Extend the existing actual SDK/END-owner complete-build test with an empty
  start case before changing production routing. Apply these exact edits in
  `tests/unit/web/composer/test_service.py`; its existing tool replies, complete
  inline source/node/output graph, real END provider reply, custody assertions,
  call counts and saved effects remain unchanged:

```diff
     @pytest.mark.asyncio
-    async def test_inline_complete_pipeline_incremental_replay_uses_atomic_tool_shape(self, tmp_path: Path) -> None:
+    @pytest.mark.parametrize("start_empty", [False, True], ids=["existing-graph", "empty-graph"])
+    async def test_inline_complete_pipeline_incremental_replay_uses_atomic_tool_shape(
+        self, tmp_path: Path, start_empty: bool
+    ) -> None:
@@
-        user_message_content = "I want a pipeline that takes the string 'hello' and appends ' world' to it."
+        if start_empty:
+            state = _empty_state()
+        user_message_content = "I want a pipeline that takes the string 'hello' and appends ' world' to it."
```

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_rootless_no_tool_recovery.py::test_persisted_empty_request_uses_the_common_provider_entry 'tests/unit/web/composer/test_service.py::TestComposerSingleToolCall::test_inline_complete_pipeline_incremental_replay_uses_atomic_tool_shape[empty-graph]' > "$COMPOSER_LOGS/h2-common-red.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h2-common-red.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Expected nonzero native exit on the inspected source: the actual empty-state planner route
  does not call the common provider gateway. No external provider is permitted:
  retain the suite's existing external-network prohibition.

- [ ] Delete the complete contiguous block in the inspected c299/ce410 source `service.py` beginning
  with `# Route by graph/session prerequisites.` and ending immediately before
  `return await self._compose_loop(`. The retained common call is exactly:

```python
                return await self._compose_loop(
                    message,
                    messages,
                    state,
                    session_id,
                    current_state_id,
                    user_id,
                    deadline,
                    progress,
                    user_message_id,
                    recorder=recorder,
                    plugin_snapshot=plugin_snapshot,
                    policy_catalog=policy_catalog,
                    session_operation_context=session_operation_context,
                    completion_gates=completion_gates,
                    required_work=required_work,
                    provider_owner=provider_owner,
                    recovery_context=recovery_context,
                    retained_operation_context=retained_operation_context,
                    turn_evidence=turn_evidence,
                )
```

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_rootless_no_tool_recovery.py tests/unit/web/composer/test_no_tool_finalize_disclosure.py tests/unit/web/composer/test_service.py::TestComposerSingleToolCall::test_inline_complete_pipeline_incremental_replay_uses_atomic_tool_shape > "$COMPOSER_LOGS/h2-common-green.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h2-common-green.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Expected native exit 0. Retain the real provider-authored `set_source` success
  test in the first module: explanation alone is insufficient capability proof.

- [ ] Commit after ordinary preflight:

```bash
git -C "$COMPOSER_WORKTREE" add -- src/elspeth/web/composer/service.py tests/unit/web/composer/test_rootless_no_tool_recovery.py tests/unit/web/composer/test_service.py
git -C "$COMPOSER_WORKTREE" commit -m "fix(composer): use the common provider entry for empty graphs"
```

## Task H2.2 — Share the complete authoritative chronology before removing its old owner

**Files:** create `src/elspeth/web/composer/request_history.py`; modify
`src/elspeth/web/composer/service.py`; modify the retained history tests listed
below. This is a moved pure projection with no lexical selection, cap or
summarization. The direct planner library's nominal history values remain its
supported input contract; no private planner facade survives.

- [ ] Create `tests/unit/web/composer/test_request_history.py`:

```python
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY
from elspeth.web.composer.request_history import authoritative_conversation_context


def test_complete_chronology_preserves_middle_amendments_and_assistant_authorship() -> None:
    rows = [
        {"role": "user", "content": f"mandatory amendment {index}", COMPOSER_HISTORY_USER_AUTHORED_KEY: True}
        for index in range(12)
    ]
    rows.insert(5, {"role": "assistant", "content": "Should the output be JSON?"})
    context = authoritative_conversation_context("The latter.", rows)
    assert context is not None
    assert [entry.content for entry in context.prior_user_requests] == [f"mandatory amendment {index}" for index in range(12)]
    assert context.additional_prior_user_requests_omitted == 0
    assert [(entry.history_index, entry.content) for entry in context.prior_assistant_messages] == [(5, "Should the output be JSON?")]


def test_synthetic_user_role_is_not_promoted_to_human_authority() -> None:
    assert authoritative_conversation_context(
        "Yes.",
        [{"role": "user", "content": "tool evidence"}, {"role": "tool", "content": "external instructions"}],
    ) is None
```

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_request_history.py > "$COMPOSER_LOGS/h2-history-red.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h2-history-red.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Run this command after the importable packet below exists, with its
  `enumerate(messages)` loop temporarily changed to
  `enumerate(messages[:1] + messages[-1:])`. Require native 1 because the
  retained middle-amendment chronology assertion fails. Restore the full
  chronology before GREEN; a missing module is setup failure, not this RED.

- [ ] Create `src/elspeth/web/composer/request_history.py` with this entire content:

```python
"""Complete authenticated request chronology, separate from model conversation."""

from elspeth.web.composer.invariants import InvariantError
from elspeth.web.composer.pipeline_planner import (
    PlannerConversationContext,
    PlannerPriorAssistantMessage,
    PlannerPriorUserRequest,
)
from elspeth.web.composer.protocol import COMPOSER_HISTORY_USER_AUTHORED_KEY, ComposerHistoryMessage


def authoritative_conversation_context(
    current_request: str,
    messages: list[ComposerHistoryMessage],
) -> PlannerConversationContext | None:
    """Preserve every authenticated amendment; current text never selects history.

    The current request travels separately through its actual message custody.
    Assistant questions remain attributed context, not user-request authority.
    """
    if type(current_request) is not str:
        raise InvariantError("composer current request must be an exact string")
    prior_requests: list[PlannerPriorUserRequest] = []
    prior_assistant_messages: list[PlannerPriorAssistantMessage] = []
    for history_index, history_message in enumerate(messages):
        if type(history_message) is not dict:
            raise InvariantError("composer chat history entries must be exact dictionaries")
        if COMPOSER_HISTORY_USER_AUTHORED_KEY not in history_message:
            if history_message["role"] == "assistant":
                content = history_message["content"]
                if type(content) is not str:
                    raise InvariantError("composer assistant chat history content must be an exact string")
                if content.strip():
                    prior_assistant_messages.append(PlannerPriorAssistantMessage(history_index=history_index, content=content))
            continue
        authorship = history_message[COMPOSER_HISTORY_USER_AUTHORED_KEY]
        if authorship is not True or "role" not in history_message or history_message["role"] != "user":
            raise InvariantError("composer user-authorship marker is malformed")
        content = history_message["content"] if "content" in history_message else None
        if type(content) is not str or not content.strip():
            raise InvariantError("composer user chat history content must be a non-empty exact string")
        prior_requests.append(PlannerPriorUserRequest(history_index=history_index, content=content))
    if not prior_requests and not prior_assistant_messages:
        return None
    return PlannerConversationContext(
        prior_user_requests=tuple(prior_requests),
        prior_assistant_messages=tuple(prior_assistant_messages),
    )
```

- [ ] Import `authoritative_conversation_context` in `service.py`. Replace the
  complete `_chat_ingress_inputs_for_compose` function with:

```python
def _chat_ingress_inputs_for_compose(
    message: str,
    messages: list[ComposerHistoryMessage],
    *,
    user_message_id: str | None,
    own_compartment_id: str | None,
) -> list[ChatIngressInput]:
    """Carry every authenticated request through state writes without paraphrasing."""
    inputs: list[ChatIngressInput] = []
    history = authoritative_conversation_context(message, messages)
    if history is not None:
        for request in history.prior_user_requests:
            row = messages[request.history_index]
            if COMPOSER_HISTORY_USER_MESSAGE_ID_KEY not in row:
                raise AuditIntegrityError("persisted human chat history is missing its message id")
            inputs.append(
                chat_ingress_input(
                    row[COMPOSER_HISTORY_USER_MESSAGE_ID_KEY],
                    request.content,
                    own_compartment_id=own_compartment_id,
                )
            )
    if user_message_id is not None:
        inputs.append(chat_ingress_input(user_message_id, message, own_compartment_id=own_compartment_id))
    return inputs
```

  Keep `prompts.build_messages`' entire chronological chat-history projection;
  it already transmits every earlier user row and assistant question and removes
  only internal custody marker keys. H3a consumes this same owned projection
  for review evidence. No first-plus-last-N copy or inferred summary is added.

- [ ] Move the retained test imports in `test_service.py`,
  `test_compose_loop_anti_anchor.py`, `test_information_boundary_recovery.py`,
  `test_planner_conversation_history.py`, and the local import inside
  `tests/unit/web/sessions/test_routes.py::test_composer_chat_history_marks_edited_fork_user_but_not_fork_system_row`
  from the deleted facade to:

```python
from elspeth.web.composer.request_history import (
    authoritative_conversation_context as _freeform_planner_conversation_context,
)
```

  This test-local alias preserves the existing calls while their owner is moved;
  the new production name has no freeform or routing semantics. The complete
  test bodies remain, including real direct-library input-byte refusal and
  assistant-authorship controls. `current_request` never gates retained history.

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_request_history.py tests/unit/web/composer/test_compose_loop_anti_anchor.py tests/unit/web/composer/test_information_boundary_recovery.py tests/unit/web/composer/test_planner_conversation_history.py > "$COMPOSER_LOGS/h2-history-green.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h2-history-green.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Expected native exit 0. The direct-library tests prove full-history preservation
  and real input refusal separately from the ordinary-entry tests.

- [ ] Commit after ordinary preflight:

```bash
git -C "$COMPOSER_WORKTREE" add -- src/elspeth/web/composer/request_history.py src/elspeth/web/composer/service.py tests/unit/web/composer/test_request_history.py tests/unit/web/composer/test_service.py tests/unit/web/composer/test_compose_loop_anti_anchor.py tests/unit/web/composer/test_information_boundary_recovery.py tests/unit/web/composer/test_planner_conversation_history.py tests/unit/web/sessions/test_routes.py
git -C "$COMPOSER_WORKTREE" commit -m "refactor(composer): share complete authenticated request history"
```

## Task H2.3 — Retire the private planner facade and only its obsolete tests

**Files:** delete `src/elspeth/web/composer/planning_application.py`; modify
`service.py`, `web/app.py` and the tests below. Preserve
`pipeline_planner.py`, its direct `plan_pipeline` API, direct discovery,
budget/cost/transport tests, nominal context types and all current candidate
repairs. In particular, the inspected c299/ce410 source already removed token-derived schema debt;
do not reintroduce it while moving history.

- [ ] Delete the entire `PlanningApplication` constructor assignment in
  `ComposerServiceImpl.__init__`, from `self._planning_application = PlanningApplication(`
  through its closing parenthesis. Delete only its `PlanningApplication` import.
  In `web/app.py` delete exactly
  `app.state.planning_application = app.state.composer_service._planning_application`.
  In parity `conftest.py` delete exactly
  `app.state.planning_application = composer._planning_application`.

  Replace `project_server_owned_option_metadata`'s docstring phrase in
  `prompts.py` with `the direct planner library's provider state`, replacing
  the obsolete `provider_current_state in planning_application.py` reference.
  Delete the now-unused `COMPOSER_HISTORY_USER_AUTHORED_KEY` import from
  `service.py` if its only previous consumer was the moved ingress projection.

- [ ] Delete `src/elspeth/web/composer/planning_application.py` in full. Its
  methods and private helper functions become unreachable after H2.1. The moved
  history projection is already covered under H2.2, so no lost history behavior
  is justified by this deletion.

- [ ] Remove these exact facade-specific test units, including their decorators;
  remove imports and local fixtures used only by the deleted units. Keep every
  other test in a mixed module. These tests exercise a retired adapter, not the
  retained direct planner library:

| File under `tests/` | Exact unit to delete |
| --- | --- |
| `unit/web/composer/test_audit_cohort_settlement.py` | `test_planner_success_path_cohort_is_all_or_nothing_at_every_write_index`, `_plan_and_stage_empty`, `test_planner_decline_path_cohort_is_all_or_nothing_at_every_write_index`, `test_planner_cohort_cancelled_mid_settlement_lands_whole_and_creates_no_proposal` |
| `unit/web/composer/test_compose_loop_persistence.py` | `test_current_planner_persistence_rejects_malformed_bound_content_hash`, `test_planner_audit_cohort_carries_the_prose_the_planner_refused_to_publish` |
| `unit/web/composer/test_private_evidence_persistence.py` | `test_received_and_accepted_one_shot_candidates_settle_in_actual_planner_cohort` |
| `unit/web/composer/test_planner_graph_recovery.py` | `test_application_handoff_defers_audit_and_omits_optional_candidate_capture` |
| `unit/web/composer/test_provider_context_planner_integration.py` | `test_application_projection_boundary_preserves_parts_and_complete_byte_admission` |
| `unit/web/composer/test_runtime_preflight_pending_review_verification.py` | class `TestPlannerPreviewPreflightStructuralCallback` |
| `unit/web/composer/test_required_control_autowire.py` | class `TestServiceFinalizerFactory`; in `TestAutoWireIdempotence.test_every_no_op_path_is_identity_preserving`, delete the entire trailing service-finalizer block from `# REQUIRED posture through the service finalizer factory` through `assert finalize(frozen) is frozen`; retain direct `wire_required_controls` identity assertions |
| `unit/web/composer/test_visible_tool_event_persistence.py` | `test_actual_planner_tools_and_public_call_gaps_survive_joined_cohort_reload` |
| `unit/web/sessions/test_run_composer_turn.py` | `test_actual_planner_decline_review_shares_call_budget_and_saved_terminal` |
| `unit/web/sessions/test_pipeline_rejection_callers.py` | `test_actual_planner_creation_cancel_retains_nonzero_child_rejection_scope` |
| `integration/web/composer/test_freeform_pipeline_planner.py` | `test_freeform_compose_routes_to_the_planner_without_pipeline_side_effects` |

- [ ] Delete `tests/unit/web/composer/test_planner_stage_runtime_preflight.py`
  completely: its `_stage` helper is only a private-facade caller, and every
  behavior under that module requires the removed staging adapter. Retain the
  common compose-loop preflight/review tests in
  `test_runtime_preflight_pending_review_verification.py` and
  `test_compose_loop_persistence.py`; do not delete those modules.

- [ ] In `test_provider_context_planner_integration.py`, keep
  `test_nonempty_compose_sends_current_amended_parts_through_real_provider_boundary`
  and delete only its `patch.object(service._planning_application, ... )` context
  manager and `empty_planner.assert_not_called()` line. Its actual SDK count,
  full prepared prompt-part inspection and no-mutation assertions remain.

- [ ] In `test_chargeable_admission.py` delete only assignments to
  `service._planning_application._sessions_service_optional` and
  `service._planning_application._chargeable_admission` from the three surviving
  public-entry/diagnostics tests. Delete only the `planning_application.plan_pipeline`
  patch and `planner.assert_not_called()` from the public refusal test. Rename
  `test_same_valid_request_reaches_planner_only_when_admitted` to
  `test_same_valid_request_reaches_common_entry_only_when_admitted` and replace
  that whole test, including decorators, with:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("entry", ["rootless", "signoff"])
@pytest.mark.parametrize("allowed", [True, False])
async def test_same_valid_request_reaches_common_entry_only_when_admitted(
    composer_service_with_real_sessions: ComposerServiceImpl, entry: str, allowed: bool
) -> None:
    service = composer_service_with_real_sessions
    sessions = service._sessions_service
    assert sessions is not None

    async def assert_scoped_entry(*args: object, **kwargs: object) -> None:
        scope = provider_quota._SCOPE.get()
        assert scope is not None and scope.service is sessions and scope.context is _CONTEXT
        raise _PlannerReached

    authority = _AdmissionService(None if allowed else AdmissionRefusalReason.IDENTITY_DISABLED)
    state = CompositionState(nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    with (
        patch.object(sessions, "assess_chargeable_operation", new=authority.assess_chargeable_operation),
        patch.object(service, "_compose_loop", autospec=True, side_effect=assert_scoped_entry) as common,
        patch.object(service._advisor_checkpoint, "_run_advisor_checkpoint", autospec=True, side_effect=assert_scoped_entry) as advisor,
        pytest.raises(_PlannerReached if allowed else ComposerAdmissionRefused),
    ):
        if entry == "rootless":
            await service.compose(
                "Build a CSV pipeline",
                [],
                state,
                session_id=_SESSION_ID,
                user_id="owner",
                user_message_id="00000000-0000-0000-0000-000000000002",
                session_operation_context=_CONTEXT,
            )
        else:
            await service._advisor_checkpoint.run_signoff_checkpoint(
                state=state,
                session_id=_SESSION_ID,
                recorder=BufferingRecorder(),
                session_operation_context=_CONTEXT,
            )
    assert authority.operations == [ChargeableOperation.COMPOSER]
    assert common.await_count == int(allowed and entry == "rootless")
    assert advisor.await_count == int(allowed and entry == "signoff")
    assert provider_quota._SCOPE.get() is None
```

  This is an admission-scope test stopping at the first owned common entry.
  H2.1 separately exercises the actual common provider boundary with persisted
  message custody. Do not represent this logical test as physical guard proof.

- [ ] In `test_llm_call_audit_summary.py` delete `_PlannerAuditHost`, class
  `TestAllThreeDrainSitesShareOneProjection` and
  `test_freeform_planner_persistence_atomically_interleaves_physical_calls_and_semantic_attempts`.
  Retain all literal legacy-summary/finish-reason tests and add this surviving
  actual writer test using the module's existing `_CapturingSessionService`,
  `_call`, `_compose_context`, `_persist_llm_calls`, `uuid4`, `cast` and
  `SessionServiceProtocol` definitions:

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("finish_reason", (None, *ROUTINE, *ABNORMAL, "provider_specific_halt"))
async def test_actual_llm_writer_uses_the_shared_summary(finish_reason: str | None) -> None:
    service = _CapturingSessionService()
    session_id = uuid4()
    call = _call(finish_reason=finish_reason)
    await _persist_llm_calls(
        cast(SessionServiceProtocol, service),
        session_id,
        (call,),
        None,
        plugin_crash_pending=False,
        session_operation_context=_compose_context(session_id),
    )
    assert len(service.messages) == 1
    assert service.messages[0].content == llm_call_audit_summary(call)
```

- [ ] Run the existing harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/composer/test_llm_call_audit_summary.py tests/unit/web/composer/test_provider_context_planner_integration.py tests/unit/web/composer/test_pipeline_planner.py tests/unit/web/composer/test_planner_graph_recovery.py tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py > "$COMPOSER_LOGS/h2-retire-green.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h2-retire-green.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```
  Expected native exit 0. The ordinary integration checkpoint must also run the
  affected persistence suites and whole-tree attribute/masquerade gates; deleting
  dead adapter tests never substitutes for common-path atomicity evidence.

  Delete stale top-level imports from the removed facade in
  `test_llm_call_audit_summary.py`, `test_provider_context_planner_integration.py`
  and `test_pipeline_rejection_callers.py`; their only runtime consumers were
  the exact deleted units above. Update the first module's opening docstring to
  `The surviving LLM-call writer shares llm_call_audit_summary; literal legacy
  bytes and finish-reason tests pin its operator-facing summary.` Remove
  `_attempt` there because its only callers were retired planner-audit tests.

- [ ] Commit after ordinary preflight with explicit affected pathspecs and the entire
  `planning_application.py` removal reviewed:

```bash
git -C "$COMPOSER_WORKTREE" add -- src/elspeth/web/composer/planning_application.py src/elspeth/web/composer/service.py src/elspeth/web/composer/prompts.py src/elspeth/web/app.py tests/unit/web/composer/test_audit_cohort_settlement.py tests/unit/web/composer/test_compose_loop_persistence.py tests/unit/web/composer/test_private_evidence_persistence.py tests/unit/web/composer/test_planner_graph_recovery.py tests/unit/web/composer/test_provider_context_planner_integration.py tests/unit/web/composer/test_runtime_preflight_pending_review_verification.py tests/unit/web/composer/test_required_control_autowire.py tests/unit/web/composer/test_visible_tool_event_persistence.py tests/unit/web/sessions/test_run_composer_turn.py tests/unit/web/sessions/test_pipeline_rejection_callers.py tests/integration/web/composer/test_freeform_pipeline_planner.py tests/integration/web/composer/parity/conftest.py tests/unit/web/composer/test_planner_stage_runtime_preflight.py tests/unit/web/composer/test_chargeable_admission.py tests/unit/web/composer/test_llm_call_audit_summary.py
git -C "$COMPOSER_WORKTREE" commit -m "refactor(composer): retire the unreachable private planner facade"
```

## Task H3b — Consume the existing owner's guard correction without duplicating it

**Files:** read the actual b003 checkpoint's `prompt_guard.py`,
`prompt_guard_clients.py`, `prompt_guard_rendering.py`, `provider_gateway.py`
and their changed tests. This companion does not edit those files.

- [ ] Wait for the existing .9 writer's actual completed qualification of b003
  or its resulting repair checkpoint, then integrate that narrow explicit
  debug/unconfigured correction under P0. Preserve its exact boolean enforcement
  and policy fingerprint; do not replay the old ce410/c299 guard body.
- [ ] Verify the actual diff retains complete structural/provenance/Unicode and
  credential checks before dispatch, treats absent-mount required mode as
  unavailable, and applies no nonexistent physical service's 10K envelope in
  explicit unconfigured debug mode. Configured service limits still refuse
  indivisible oversized semantic values and require every batch to allow.
- [ ] Preserve the existing complete-current-request producer and exact
  guard/journal/SDK input. Do not add a raw slicer, padding, trusted-label upgrade
  or semantic-equivalence claim. Audit and fingerprint actual validity policy
  separately from configured physical adapter policy as the owner implements it.
- [ ] At the admitted integration checkpoint, run the owner's changed tests and
  the existing focused harness directly:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 \
 tests/unit/web/composer/test_prompt_guard_admission.py \
 tests/unit/web/composer/test_prompt_guard_clients.py \
 tests/unit/web/composer/test_prompt_guard_producer_parts.py \
 tests/unit/web/composer/test_prompt_guard_content_parts.py \
 tests/unit/web/composer/test_prompt_guard_advisor_projection.py \
 tests/unit/web/composer/test_prompt_guard_wiring.py \
 tests/unit/web/composer/test_provider_context.py \
 > "$COMPOSER_LOGS/h3b-owner-integration.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h3b-owner-integration.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

Expected native exit 0 on the integrated source. Long valid debug input,
long required input, malformed/credential input, seam credentials, failed
later batches and immutable physical input remain the behavioral boundaries.
An unavailable owner checkpoint holds this dependency; it is not permission
to implement an overlapping correction here.

The selected durable .10 PromptShield route is also that owner's dependency:
typed immutable units and use/context, complete bounded projections,
lossless materialisation, fenced publication/invalidation, model-independent
legacy recovery and both-role qualification. This plan creates none of those
competing stores, adapters or contracts. Every reuse, retry and resume must
freshly validate its actual use and assembled context even if all hashes and
stored receipts appear unchanged. A credential-inspection receipt below is
identity/history evidence only and cannot authorize PromptShield bypass or
whole-graph CLEAN for unseen content.

## Task H3c.1 — Add a bounded fresh complete-original inspection primitive

**Files:**
- Create: `src/elspeth/web/composer/attachment_admission.py`
- Create: `tests/unit/web/composer/test_attachment_admission.py`

- [ ] Create this complete test module. Repeating identical input deliberately
  requires two real detector calls; the later credential is a negative control
  against an accidental identity/receipt clearance cache.

```python
import hashlib
from unittest.mock import patch

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer import attachment_admission as admission
from elspeth.web.composer.attachment_admission import (
    AttachmentInspectionPolicy,
    AttachmentInspectionRefused,
    inspect_attachment_original,
)
from elspeth.web.credential_guard import CredentialMaterialRefused


def test_identical_reuse_freshly_inspects_the_complete_original_each_time() -> None:
    raw = b"neutral record\n" * 25_000
    assert len(raw) > 262_144
    digest = hashlib.sha256(raw).hexdigest()
    policy = AttachmentInspectionPolicy(max_bytes=len(raw))
    with patch.object(admission, "require_no_credential_material", wraps=admission.require_no_credential_material) as scan:
        first = inspect_attachment_original(session_id="s", blob_id="b", verified_hash=digest, raw=raw, policy=policy)
        second = inspect_attachment_original(session_id="s", blob_id="b", verified_hash=digest, raw=raw, policy=policy)
    assert first == second and first is not second
    assert scan.call_count == 2
    assert [call.args for call in scan.call_args_list] == [(raw.decode("utf-8"),)] * 2
    assert all(call.kwargs["max_string_bytes"] == len(raw) for call in scan.call_args_list)


@pytest.mark.parametrize("prefix", [4_095, 262_140, 300_000])
def test_late_or_page_seam_credentials_are_refused_before_receipt(prefix: int) -> None:
    raw = b"x" * prefix + b"\n" + b"sk-" + b"A" * 48
    with pytest.raises(CredentialMaterialRefused):
        inspect_attachment_original(
            session_id="s", blob_id="b", verified_hash=hashlib.sha256(raw).hexdigest(),
            raw=raw, policy=AttachmentInspectionPolicy(max_bytes=len(raw)),
        )


def test_safe_prior_inspection_does_not_clear_changed_or_credential_content() -> None:
    safe = b"neutral"
    unsafe = b"neutral\n" + b"sk-" + b"A" * 48
    policy = AttachmentInspectionPolicy(max_bytes=1_000)
    inspect_attachment_original(session_id="s", blob_id="b", verified_hash=hashlib.sha256(safe).hexdigest(), raw=safe, policy=policy)
    with pytest.raises(CredentialMaterialRefused):
        inspect_attachment_original(
            session_id="s", blob_id="b", verified_hash=hashlib.sha256(unsafe).hexdigest(), raw=unsafe, policy=policy,
        )


def test_explicit_resource_policy_and_utf8_failures_remain_precise() -> None:
    raw = b"neutral"
    with pytest.raises(AttachmentInspectionRefused, match="inspection policy"):
        inspect_attachment_original(
            session_id="s", blob_id="b", verified_hash=hashlib.sha256(raw).hexdigest(), raw=raw,
            policy=AttachmentInspectionPolicy(max_bytes=len(raw) - 1),
        )
    malformed = b"\xff"
    with pytest.raises(UnicodeDecodeError):
        inspect_attachment_original(
            session_id="s", blob_id="b", verified_hash=hashlib.sha256(malformed).hexdigest(), raw=malformed,
            policy=AttachmentInspectionPolicy(max_bytes=10),
        )


def test_identity_receipt_never_substitutes_for_current_byte_verification() -> None:
    raw = b"before"
    with pytest.raises(AuditIntegrityError, match="verified bytes"):
        inspect_attachment_original(
            session_id="s", blob_id="b", verified_hash=hashlib.sha256(raw).hexdigest(), raw=b"after!",
            policy=AttachmentInspectionPolicy(max_bytes=100),
        )


def test_policy_identity_records_reference_names_and_all_traversal_bounds() -> None:
    original = AttachmentInspectionPolicy(max_bytes=100)
    refs = AttachmentInspectionPolicy(max_bytes=100, env_ref_names=frozenset({"SYNTHETIC_REF"}))
    fields = AttachmentInspectionPolicy(max_bytes=100, additional_credential_fields=frozenset({"custom_auth"}))
    assert len({original.fingerprint, refs.fingerprint, fields.fingerprint}) == 3
```

- [ ] After the importable production packet below exists, temporarily replace
  its complete-original scan input with `content[:128]`. Run this behavioral RED;
  the late-credential assertion must fail. Restore `content` before GREEN:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_attachment_admission.py > "$COMPOSER_LOGS/h3c-primitive-red.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h3c-primitive-red.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```

Require native exit 1 for the late-credential assertion under the stated
mutation. An environment, import or collection failure does not satisfy RED.

- [ ] Create `src/elspeth/web/composer/attachment_admission.py` with this complete
  policy/receipt/function implementation. It has no cache, context variable,
  registry, process lock, persisted state or reuse fast path.

```python
"""Fresh bounded credential inspection of each complete attachment original."""

import hashlib
import hmac
import json
from dataclasses import dataclass

from elspeth.contracts.credential_material import (
    CREDENTIAL_DETECTOR_VERSION,
    DEFAULT_CREDENTIAL_TRAVERSAL_POLICY,
)
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.credential_guard import require_no_credential_material


class AttachmentInspectionRefused(ValueError):
    """The original exceeds the operator's bounded inspection resource."""


@dataclass(frozen=True, slots=True)
class AttachmentInspectionPolicy:
    max_bytes: int
    env_ref_names: frozenset[str] = frozenset()
    additional_credential_fields: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if type(self.max_bytes) is not int or self.max_bytes <= 0:
            raise ValueError("Attachment inspection requires a positive exact byte limit")
        for names in (self.env_ref_names, self.additional_credential_fields):
            if type(names) is not frozenset or any(type(name) is not str or not name for name in names):
                raise ValueError("Attachment inspection reference policy requires exact nonempty strings")

    @property
    def fingerprint(self) -> str:
        traversal = DEFAULT_CREDENTIAL_TRAVERSAL_POLICY
        value = {
            "schema_version": 1,
            "content_interpretation": "complete-original-strict-utf-8",
            "max_bytes": self.max_bytes,
            "max_string_bytes": self.max_bytes,
            "max_depth": traversal.max_depth,
            "max_nodes": traversal.max_nodes,
            "max_width": traversal.max_width,
            "env_ref_names": sorted(self.env_ref_names),
            "additional_credential_fields": sorted(self.additional_credential_fields),
        }
        return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class AttachmentReadAdmission:
    """Identity/history of this inspection only; never reusable clearance."""

    session_id: str
    blob_id: str
    content_hash: str
    size_bytes: int
    detector_version: str
    policy_fingerprint: str


def inspect_attachment_original(
    *,
    session_id: str,
    blob_id: str,
    verified_hash: str,
    raw: bytes,
    policy: AttachmentInspectionPolicy,
) -> AttachmentReadAdmission:
    if type(policy) is not AttachmentInspectionPolicy or type(raw) is not bytes:
        raise TypeError("Attachment inspection requires owned policy and exact immutable bytes")
    if type(session_id) is not str or not session_id or type(blob_id) is not str or not blob_id:
        raise AuditIntegrityError("Attachment inspection requires actual session/blob identities")
    actual_hash = hashlib.sha256(raw).hexdigest()
    if type(verified_hash) is not str or not hmac.compare_digest(actual_hash, verified_hash):
        raise AuditIntegrityError("Attachment inspection does not match verified bytes")
    if len(raw) > policy.max_bytes:
        raise AttachmentInspectionRefused(
            f"Attachment exceeds the {policy.max_bytes}-byte complete-original inspection policy; no content was returned."
        )
    content = raw.decode("utf-8")
    require_no_credential_material(
        content,
        surface="composer_attachment_original",
        env_ref_names=policy.env_ref_names,
        additional_credential_fields=policy.additional_credential_fields,
        max_string_bytes=policy.max_bytes,
    )
    return AttachmentReadAdmission(
        session_id=session_id,
        blob_id=blob_id,
        content_hash=verified_hash,
        size_bytes=len(raw),
        detector_version=CREDENTIAL_DETECTOR_VERSION,
        policy_fingerprint=policy.fingerprint,
    )
```

- [ ] Run the same existing module and require exit 0:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_attachment_admission.py > "$COMPOSER_LOGS/h3c-primitive-green.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h3c-primitive-green.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] After ordinary preflight, stage these two files only and checkpoint:

```bash
git -C "$COMPOSER_WORKTREE" add -- src/elspeth/web/composer/attachment_admission.py tests/unit/web/composer/test_attachment_admission.py
git -C "$COMPOSER_WORKTREE" commit -m "feat(composer): inspect every complete attachment original under explicit bounds"
```

## Task H3c.2 — Thread the operator bound into real custody and both readers

**Files:** modify `src/elspeth/web/config.py`, `composer/protocol.py`,
`composer/service.py`, `composer/tool_batch.py`, `composer/tools/_common.py`,
`composer/tools/_dispatch.py`, `composer/tools/blobs.py`; test
`tests/unit/web/test_config.py` and `tests/unit/web/composer/test_tools_blobs.py`.

- [ ] Add this test to `tests/unit/web/test_config.py`, using its existing
  `WebSettings` and pytest imports:

```python
def test_attachment_inspection_default_tracks_configured_upload_limit() -> None:
    assert WebSettings(max_upload_bytes=123_456).composer_attachment_inspection_max_bytes == 123_456
    assert WebSettings(max_upload_bytes=123_456, composer_attachment_inspection_max_bytes=999).composer_attachment_inspection_max_bytes == 999
    with pytest.raises(ValueError):
        WebSettings(composer_attachment_inspection_max_bytes=0)
```

- [ ] Add these complete methods to the existing `TestGetBlobContentGuards`
  class. They reuse its real database/file setup, `_replace_content`,
  `_blob_operation`, `_empty_state`, `_mock_catalog` and existing `execute_tool`
  wrapper. The selected response serializer remains the size oracle.

```python
    @pytest.mark.parametrize("tool_name", ["get_blob_content", "read_attachment"])
    def test_both_attachment_readers_freshly_scan_identical_reuse(self, tool_name: str) -> None:
        from unittest.mock import patch

        from elspeth.web.composer import attachment_admission

        original = "neutral record\n" * 20
        digest = self._replace_content(original)
        with (
            _blob_operation(self.engine, self.session_id) as (authority, operation),
            patch.object(
                attachment_admission,
                "require_no_credential_material",
                wraps=attachment_admission.require_no_credential_material,
            ) as scan,
        ):
            for _ in range(2):
                result = execute_tool(
                    tool_name,
                    {"blob_id": self.blob_id, "expected_content_hash": digest},
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=str(self.data_dir),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=authority,
                    session_operation_context=operation,
                    attachment_inspection_max_bytes=len(original.encode("utf-8")),
                )
                assert result.success, result.validation.errors
                assert result.data["content"] == original
            assert scan.call_count == 2
            assert [call.args for call in scan.call_args_list] == [(original,), (original,)]

    def test_large_original_pages_reinspect_every_read_and_reconstruct_exactly(self) -> None:
        from unittest.mock import patch

        from elspeth.web.composer import attachment_admission
        from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

        original = "neutral\x00🙂 record\n" * 22_000
        digest = self._replace_content(original)
        assert len(original.encode("utf-8")) > 262_144
        offset = 0
        chunks = []
        with (
            _blob_operation(self.engine, self.session_id) as (authority, operation),
            patch.object(
                attachment_admission,
                "require_no_credential_material",
                wraps=attachment_admission.require_no_credential_material,
            ) as scan,
        ):
            for _ in range(2_000):
                result = execute_tool(
                    "get_blob_content",
                    {"blob_id": self.blob_id, "offset_chars": offset, "expected_content_hash": digest},
                    _empty_state(),
                    _mock_catalog(),
                    data_dir=str(self.data_dir),
                    session_engine=self.engine,
                    session_id=self.session_id,
                    session_operation_authority=authority,
                    session_operation_context=operation,
                    attachment_inspection_max_bytes=len(original.encode("utf-8")),
                )
                assert result.success, result.validation.errors
                serialized = serialize_admitted_discovery_result(admit_discovery_result("get_blob_content", result))
                assert len(serialized.encode("utf-8")) <= 4_096
                assert result.data["start_char"] == offset and result.data["content_hash"] == digest
                chunks.append(result.data["content"])
                next_offset = result.data["next_offset_chars"]
                if next_offset is None:
                    break
                assert next_offset > offset
                offset = next_offset
            else:
                raise AssertionError("Bounded page loop did not finish the original")
            assert scan.call_count == len(chunks)
            assert all(call.args == (original,) for call in scan.call_args_list)
        assert "".join(chunks) == original

    def test_operator_inspection_bound_refuses_before_any_page(self) -> None:
        original = "neutral record\n" * 22_000
        digest = self._replace_content(original)
        with _blob_operation(self.engine, self.session_id) as (authority, operation):
            result = execute_tool(
                "get_blob_content",
                {"blob_id": self.blob_id, "expected_content_hash": digest},
                _empty_state(),
                _mock_catalog(),
                data_dir=str(self.data_dir),
                session_engine=self.engine,
                session_id=self.session_id,
                session_operation_authority=authority,
                session_operation_context=operation,
                attachment_inspection_max_bytes=len(original.encode("utf-8")) - 1,
            )
        assert not result.success and result.data is None
        assert "complete-original inspection policy" in result.validation.errors[0].message
```

- [ ] Run the two new wiring tests before the plumbing edit:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 \
 tests/unit/web/test_config.py::test_attachment_inspection_default_tracks_configured_upload_limit \
 tests/unit/web/composer/test_tools_blobs.py::TestGetBlobContentGuards::test_large_original_pages_reinspect_every_read_and_reconstruct_exactly \
 > "$COMPOSER_LOGS/h3c-wiring-red.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h3c-wiring-red.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
```

Expected a nonzero native exit because the declared setting/tool keyword does
not exist yet, not because the environment lacks pytest or its dependencies.

- [ ] In `WebSettings`, add this field immediately after `max_upload_bytes`:

```python
    composer_attachment_inspection_max_bytes: int = Field(
        default_factory=lambda validated: validated["max_upload_bytes"],
        ge=1,
    )
```

  In `ComposerSettings` in `protocol.py`, add this property next to the existing
  blob-quota property:

```python
    @property
    def composer_attachment_inspection_max_bytes(self) -> int: ...
```

- [ ] Add the bounded policy input to `ToolContext` in `tools/_common.py` and
  `execute_tool` in `tools/_dispatch.py` with these exact signature/field hunks:

```diff
     max_blob_storage_per_session_bytes: int | None = None
+    attachment_inspection_max_bytes: int = 100 * 1024 * 1024
```

```diff
     max_blob_storage_per_session_bytes: int | None = None,
+    attachment_inspection_max_bytes: int = 100 * 1024 * 1024,
```

  Add the corresponding exact constructor argument to its single
  `ToolContext(...)` construction:

```python
        attachment_inspection_max_bytes=attachment_inspection_max_bytes,
```

  No cache or prior receipt is accepted by `ToolContext` or `execute_tool`.
  The default supports direct library callers; the production path below
  supplies the deployment's validated policy explicitly.

- [ ] Add `attachment_inspection_max_bytes: int = 100 * 1024 * 1024` as the last
  field of `ToolBatchCustody` in `tool_batch.py`. In its actual `execute_tool`
  dispatch next to `max_blob_storage_per_session_bytes`, add:

```python
                attachment_inspection_max_bytes=ctx.custody.attachment_inspection_max_bytes,
```

  In `service.py`'s `ToolBatchCustody(...)` construction, add:

```python
                attachment_inspection_max_bytes=self._settings.composer_attachment_inspection_max_bytes,
```

  Preserve the .9 owner's unrelated `tool_batch.py` rejection corrections.
  This patch adds one custody input and one dispatch argument; it replaces no
  whole file and changes no Advisor error payload or persisted receipt.

- [ ] In `tools/blobs.py`, import the three owned symbols:

```python
from elspeth.web.composer.attachment_admission import (
    AttachmentInspectionPolicy,
    AttachmentInspectionRefused,
    inspect_attachment_original,
)
```

  Add this keyword-only parameter to `_locked_read_ready_blob` after its
  existing `session_operation_authority` parameter:

```python
    attachment_inspection_policy: AttachmentInspectionPolicy | None = None,
```

  Immediately after its existing `_verify_blob_content_integrity(record, data)`
  inside the actual ready-blob custody lock, insert:

```python
            if len(data) != record["size_bytes"]:
                raise AuditIntegrityError("Ready attachment byte length does not match its stored metadata")
            if attachment_inspection_policy is not None:
                verified_hash = record["content_hash"]
                if verified_hash is None:
                    raise AuditIntegrityError("Ready attachment is missing its verified content hash")
                inspect_attachment_original(
                    session_id=session_id,
                    blob_id=blob_id,
                    verified_hash=verified_hash,
                    raw=data,
                    policy=attachment_inspection_policy,
                )
```

  Each read acquires present custody and verifies actual hash and length before
  fresh inspection. The receipt returned by the primitive is not a capability
  and is not fed back to skip this branch. Existing callers that merely read
  bytes for the separately owned durable route retain their current interface;
  their actual use/context validation remains that owner's responsibility.

- [ ] Add this complete helper immediately after `_locked_read_ready_blob`:

```python
def _read_admitted_attachment(
    blob_id: str,
    state: CompositionState,
    context: ToolContext,
) -> tuple[BlobToolRecord | None, bytes | None] | ToolResult:
    engine = context.session_engine
    session_id = context.session_id
    if engine is None or session_id is None:
        return _failure_result(state, "Blob tools require session context.")
    try:
        return _locked_read_ready_blob(
            engine,
            session_id,
            blob_id,
            data_dir=context.data_dir,
            session_operation_context=context.session_operation_context,
            session_operation_authority=context.session_operation_authority,
            attachment_inspection_policy=AttachmentInspectionPolicy(max_bytes=context.attachment_inspection_max_bytes),
        )
    except AttachmentInspectionRefused as exc:
        return _failure_result(state, str(exc))
    except CredentialMaterialRefused:
        return _failure_result(state, CREDENTIAL_REFUSAL_DETAIL, error_code="credential_material_rejected")
    except UnicodeDecodeError as exc:
        return _failure_result(state, f"Blob '{blob_id}' is not valid UTF-8 text ({exc.reason} at byte offset {exc.start}).")
```

- [ ] Replace the complete `_locked_read_ready_blob(...)` call in each of
  `_execute_get_blob_content` and `_execute_read_attachment` with:

```python
    observed = _read_admitted_attachment(blob_id, state, context)
    if type(observed) is ToolResult:
        return observed
    blob, data = observed
```

  Keep their existing not-found, nonready, missing-file, strict UTF-8,
  expected-content-hash and cursor checks. A fresh complete-original credential
  refusal returns no successful prefix. No whole-value PromptShield verdict is
  inferred from this credential scan or from later page serialization.

- [ ] In `_execute_get_blob_content`, replace its old full-payload scanner block
  at the `# Inspect the FULL decoded content before slicing` comment with the
  separate normal-policy metadata scan below. The original was already freshly
  inspected under custody; scanning this entire long payload with the generic
  256KiB string limit would reintroduce the defect.

```python
    refusal = _credential_safe_blob_discovery_result(
        state,
        {key: value for key, value in payload.items() if key != "content"},
        surface="composer_blob_content_metadata",
    )
    if refusal is not None:
        return refusal
```

  Add this local helper immediately before its existing `page` function:

```python
    def admitted_page(candidate: ToolResult) -> ToolResult:
        refusal = _credential_safe_blob_discovery_result(state, candidate.data, surface="composer_blob_content_page")
        return candidate if refusal is None else refusal
```

  Replace the immediate `return candidate` after the existing
  `size <= _BLOB_CONTENT_PAGE_MAX_UTF8_BYTES` condition with
  `return admitted_page(candidate)` and its final `return best` with
  `return admitted_page(best)`. Keep the actual serializer/binary search,
  existing `AttachmentContentPayload` versus `BlobContentPayload` shape and
  the complete 4096-byte serialized page envelope bound.

- [ ] In `_execute_read_attachment`, remove only the old pre-decode
  `if len(data) > READ_ATTACHMENT_MAX_UTF8_BYTES:` block that calls this a shared
  inspection limit. After its existing expected-hash check, add:

```python
    if len(data) > READ_ATTACHMENT_MAX_UTF8_BYTES:
        return _failure_result(
            state,
            f"The attachment's complete serialized response exceeds {READ_ATTACHMENT_MAX_UTF8_BYTES} UTF-8 bytes; "
            "no content was returned. Use get_blob_content for selected pages.",
        )
```

  This response lower-bound refusal occurs after complete-original inspection.
  Keep the later actual serialized-response measurement and normal output scan
  unchanged: JSON escaping and metadata can make a raw-short value fail too.
  Complete-original inspection and whole-response delivery are separate bounds.

- [ ] Run the existing focused checks and require the recorded native exit 0:

```bash
cd "$COMPOSER_WORKTREE" &&
PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" \
 "$COMPOSER_PYTHON" -m pytest -n 0 \
 tests/unit/web/composer/test_attachment_admission.py \
 tests/unit/web/composer/test_tools_blobs.py \
 tests/unit/web/composer/test_blob_response_contracts.py \
 tests/unit/web/test_config.py \
 > "$COMPOSER_LOGS/h3c-wiring-green.log" 2>&1
COMPOSER_CHECK_EXIT=$?
cat "$COMPOSER_LOGS/h3c-wiring-green.log"
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

Existing late/page-seam credential, non-UTF8, mismatched-hash, same-session
custody, escaped-envelope and cursor tests remain selected. The new positive
instrument requires one full scan **per read**, even for unchanged content.

- [ ] After ordinary preflight, checkpoint only these plumbing/tests paths:

```bash
git -C "$COMPOSER_WORKTREE" add -- src/elspeth/web/config.py src/elspeth/web/composer/protocol.py src/elspeth/web/composer/service.py src/elspeth/web/composer/tool_batch.py src/elspeth/web/composer/tools/_common.py src/elspeth/web/composer/tools/_dispatch.py src/elspeth/web/composer/tools/blobs.py tests/unit/web/composer/test_tools_blobs.py tests/unit/web/test_config.py
git -C "$COMPOSER_WORKTREE" commit -m "fix(composer): freshly inspect attachment originals before every read"
```

## Integration exit and handoff

- [ ] Reconcile the exact selected source with the .9 owner's actual committed
  narrow guard correction, selected b09 hunks, c299 guidance and the separate
  frontend checkpoint. Do not infer inclusion from a dirty checkout or old pass.
- [ ] Run ordinary affected tests and every whole-tree gate whose scanned inputs
  changed, including attribute/masquerade contracts, config contracts and the
  generated tool/skill inventory when relevant. Use the canonical full-suite gate
  only at the admitted integration checkpoint; one broad suite at a time. Schema,
  SQL, session-persistence or lock changes in the integrated source also require
  the serial PostgreSQL testcontainer selection. This bounded attachment packet
  itself changes no persistent schema, migration or store lifecycle.
- [ ] Retain keyless lint/signature limitations honestly; agents neither hold
  operator keys nor claim signature clearance. Keep any honest policy mismatch
  ready for the established operator workflow rather than adding suppressions.
- [ ] Verify neutral replies and actual provider-authored full builds on the
  common route, preserved review/rejection/accounting boundaries, and freshly
  inspected attachments without a receipt-based bypass. Complete the separate
  durable PromptShield owner's tests before claiming its complete-value/context
  validation or whole-graph review behavior.

An exit from this companion means these bounded changes are implemented and
their appropriate admitted checks pass on the actual combined source. It does
not mark Continuation, deployed B/C assemblies, eligible Gate2, CI/main, signing,
release or deployment complete. After authoring this plan, stand by; do not
execute any task from this document without the then-current development scope
and normal capacity admission.
