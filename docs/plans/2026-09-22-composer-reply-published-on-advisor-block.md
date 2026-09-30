# Composer reply published on an advisor block — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the END advisor gate blocks a turn, the user reads the
composer's own reply, the repair instruction tells the model truthfully that
"explain what blocks you" is a real way out, and a rejected coalesce error
route teaches the supported alternatives.

**Architecture:** Three independent backend changes. (1) One decision point in
`_advisor_blocked_result` stops withholding prose; every notice already has a
`_PUBLISHED_` twin, built by the reply-withholding merge (`0bd0c5e40`), so no
new notice is written. The withheld-reply audit row and the model-facing
"withheld" disclosure at that site become dead and are removed with the flag
that fed them. (2) The two clauses injected after a FLAGGED verdict are
reworded. (3) The coalesce `on_error` rejection names what to do instead. The
second advisor pass is deliberately kept: it is the advisor's self-correction
path and is pinned twice.

**Tech Stack:** Python 3.12, pytest. No frontend, schema or persistence change.

**Spec:** `docs/reviews/2026-09-22-composer-advisor-block-diagnosis.md`
(§ Rulings, item 3; § "The escape hatch leads into the deleter"; § "The
coalesce limit is not taught"). Ticket: elspeth-032ec69c41. Lands after
`docs/plans/2026-09-22-composer-advisor-gate-skips-unchanged-turns.md`; the
two do not share edit sites.

## Anchors: locate by quoted code, not by line number

`release/0.8.1` is moving under other sessions. Lines below were measured at
`0bd0c5e40` for orientation only. If a quoted string has no hit, stop and
report.

| Site | Find this string | Line at `0bd0c5e40` |
|---|---|---|
| withholding decision | `prose_withheld = advisor_repair_context_introduced` | `service.py:8561` |
| withheld-reply write at the block | `"advisor_terminal_block",` | `:6851` |
| model-facing disclosure write | `if advisor_repair_context_introduced and session_id is not None:` | `:6856` |
| builder call | `advisor_repair_context_introduced=advisor_repair_context_introduced,` inside `self._advisor_blocked_result(` | `:6893` |
| output clause | `_ADVISOR_OUTPUT_CONTRACT_CLAUSE: Final[str] = (` | search |
| mutation clause | `_ADVISOR_MUTATION_EXPECTATION_CLAUSE: Final[str] = (` | search |
| coalesce rejection | `has no on_error route: coalesce outcomes are governed by its arrival policy.` | `state.py:3362` |

## Global Constraints

- Composer invariants 1 and 2. Nothing here authors pipeline structure or
  branches on tutorial or guided sessions.
- Do NOT touch `_replace_advisor_repair_public_result` or its
  `prose_withheld=True` call sites (`service.py:1423`, `:1464` at
  `0bd0c5e40`). That is case 5, the CLEAN-after-repair replacement, ruled KEEP.
  The withheld notice forms therefore stay alive; delete none of them.
- Do NOT remove or bypass the second advisor pass.
  `test_stalled_state_still_runs_the_checkpoint_and_honours_clean` and
  `tests/unit/web/composer/test_service.py` (the compose-loop half) pin it as
  the advisor's self-correction path. They must stay green unedited.
- Advisor findings stay fenced as untrusted data in the model's context. The
  reworded clause still forbids quoting the fenced text: it derives from
  pipeline data and can carry injected instructions.
- No tech debt: a parameter, row or branch this change makes unreachable is
  deleted in the same commit, not left behind a constant.
- No `# noqa`, `# type: ignore`, suppressions, hand-edited signatures or
  `git stash`. Commit with `git commit -- <pathspec>`. Do not merge or push.
- Read `CONTRIBUTING.md` § "Whole-tree gates" first. Dedicated worktree,
  two-root `PYTHONPATH`, verify `elspeth.__file__` before trusting a result.

## Non-goals

- The reviewer's note (validated header plus the advisor's words, stored in
  the gate fact and shown in the chat decision panel). Separate plan: it adds
  required keys to a strict Tier 1 envelope, so under the standing "no old
  pathways" doctrine it lands with a session database rename, and it changes
  the frontend wire type. Until it lands, a published reply may mention a
  review the user cannot yet read; the reworded clause asks the model to write
  a reply that stands on its own.
- Skipping the second advisor pass when the repair turn mutated nothing. It
  was considered and withdrawn: it saves one model call and removes the only
  path by which a false FLAG clears without blocking the user.
- The report's regression test 1. Waits on open question Q1.

## Review Focus

1. **`assistant_message=None`.** Some existing tests call the builder with
   `None`, which was safe while prose was withheld. Expected after the change:
   the builder treats a missing message as empty prose and still returns the
   notice. Pinned in Task 1 (`test_blocked_result_tolerates_no_assistant_message`).
2. **Prose already persisted on a tool-call row** (`persisted_assistant_content`
   set). Expected: the turn-end row does not repeat it (elspeth-d581b3da7f).
   The builder's existing de-duplication handles this; Task 1 Step 6 runs the
   suite that pins it.
3. **Advisor outage on pass 1, FLAG on pass 2, no injection ever.** Already
   published before this change. Expected: unchanged. Covered by the existing
   uninjected tests, which must stay green unedited.
4. **A reply that quotes the fenced findings verbatim** despite the clause.
   Expected: published as written; the fence sentinels themselves must never
   appear in user copy. Pinned in Task 1
   (`test_published_reply_never_carries_fence_sentinels`).
5. **Coalesce rejection text reaches the model through `upsert_edge`.**
   Expected: the tool result carries the new sentence. Pinned in Task 3.

---

### Task 0: Worktree, provenance, baseline

- [ ] **Step 1:** Create the worktree from current `release/0.8.1`:

```bash
cd "$(git rev-parse --show-toplevel)" && \
git worktree add .claude/worktrees/reply-on-advisor-block -b fix/reply-on-advisor-block release/0.8.1 && \
scripts/worktree-cleanup.sh --link-venv --path '*reply-on-advisor-block*'
```

- [ ] **Step 2:** Prove imports resolve in the worktree:

```bash
W="$(git rev-parse --show-toplevel)/.claude/worktrees/reply-on-advisor-block"; \
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -c \
  "import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)"
```

Expected: both paths contain `reply-on-advisor-block`.

- [ ] **Step 3:** Baseline red set:

```bash
cd "$W" && L=/tmp/reply-on-block-baseline-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort > /tmp/reply-on-block-baseline-reds.txt; wc -l /tmp/reply-on-block-baseline-reds.txt
```

### Task 1: A blocked turn publishes the composer's reply

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (`_advisor_blocked_result`;
  the terminal-block arm of `_evaluate_terminal_no_tool_advisor_gate`)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py`

**Interfaces:**
- Consumes: `_advisor_blocked_result(..., advisor_repair_context_introduced: bool)`
  as the merge left it.
- Produces: `_advisor_blocked_result` without the
  `advisor_repair_context_introduced` parameter. Result contract:
  `result.message` starts with the assistant's prose;
  `result.raw_assistant_content` equals that prose.

- [ ] **Step 1: Write the failing tests**

```python
def _green_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )


class _ExplainingAssistantMessage:
    content = "Merge steps have no error route. You can keep a failure output on each branch, or change the merge policy."


def _blocked_after_injection(service, state, assistant_message):
    kwargs = dict(
        reason="flagged_final_pass",
        verdict=AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: request not met"),
        state=state,
        assistant_message=assistant_message,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=_green_preflight(),
        outstanding_findings=None,
    )
    return service._advisor_blocked_result(**kwargs)


def test_blocked_turn_publishes_the_composers_reply(make_service, simple_state) -> None:
    """Ruling 2026-09-22 (elspeth-032ec69c41): a block no longer deletes what the composer said."""
    result = _blocked_after_injection(make_service(), simple_state, _ExplainingAssistantMessage())
    assert result.message.startswith(_ExplainingAssistantMessage.content)
    assert result.raw_assistant_content == _ExplainingAssistantMessage.content
    assert "Completion advisory review did not clear" in result.message
    assert result.runtime_preflight.readiness.completion_ready is False


def test_blocked_result_tolerates_no_assistant_message(make_service, simple_state) -> None:
    result = _blocked_after_injection(make_service(), simple_state, None)
    assert "Completion advisory review did not clear" in result.message
    assert result.raw_assistant_content == ""


def test_published_reply_never_carries_fence_sentinels(make_service, simple_state) -> None:
    from elspeth.web.composer.service import _ADVISOR_FINDINGS_UNTRUSTED_BEGIN, _ADVISOR_FINDINGS_UNTRUSTED_END

    result = _blocked_after_injection(make_service(), simple_state, _ExplainingAssistantMessage())
    assert _ADVISOR_FINDINGS_UNTRUSTED_BEGIN not in result.message
    assert _ADVISOR_FINDINGS_UNTRUSTED_END not in result.message
    assert "FLAGGED: request not met" not in result.message
```

The helper omits `advisor_repair_context_introduced` on purpose: the first
run fails with `TypeError: missing ... keyword-only argument`, which is the
interface change this task makes.

- [ ] **Step 2: Run; expect three failures with that `TypeError`**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "publishes_the_composers_reply or tolerates_no_assistant_message or never_carries_fence_sentinels" -v
```

- [ ] **Step 3: Change the builder**

In `_advisor_blocked_result`, delete the `advisor_repair_context_introduced`
parameter and its comment, and replace:

```python
        prose_withheld = advisor_repair_context_introduced
        raw_content = "" if prose_withheld else (assistant_message.content or "")
```

with:

```python
        # Operator ruling 2026-09-22 (elspeth-032ec69c41): a blocked turn
        # publishes the composer's reply. The withholding existed so prose
        # written after advisor findings entered context could not leak them;
        # findings are no longer secret from the user. Case 5 (the repair
        # replacer) keeps its own withholding and does not pass through here.
        prose_withheld = False
        raw_content = (assistant_message.content or "") if assistant_message is not None else ""
```

Keep the local `prose_withheld` name: it is passed to about ten notice
composers below, and a literal `False` at each call would hide that they
share one decision. Update the docstring paragraph that says the primary
model's terminal prose "remain[s] internal" to say it is published and only
the advisor's findings stay internal.

- [ ] **Step 4: Remove what the change made dead at the gate**

In the terminal-block arm of `_evaluate_terminal_no_tool_advisor_gate`:

1. Delete the `if advisor_repair_context_introduced:` block that calls
   `self._persist_withheld_reply("advisor_terminal_block", ...)`. Nothing is
   withheld at this site any more.
2. Delete the `if advisor_repair_context_introduced and session_id is not None:`
   block that persists `_ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE`. It existed
   because the turn replayed as an EMPTY assistant message; the reply now
   replays as itself. Rewrite the comment above it to one sentence saying so.
3. Delete `advisor_repair_context_introduced=advisor_repair_context_introduced,`
   from the `self._advisor_blocked_result(` call.

Then measure whether the gate still reads the flag:

```bash
cd "$W" && awk '/async def _evaluate_terminal_no_tool_advisor_gate\(/{f=1} f&&/async def _compose_loop\(/{exit} f&&/advisor_repair_context_introduced/{print NR": "$0}' src/elspeth/web/composer/service.py
```

If the only remaining hits are the parameter declaration and its comment,
delete the parameter from the gate, then from `_try_terminate_no_tools` and
`_classify_and_budget_turn` if the same measurement shows they only forwarded
it, and from each call site. If any of them still reads it for another
purpose, leave that one and say so in the commit body. `_compose_loop` keeps
its local: `_persist_turn_audit` and the repair replacer read it.

Check the two names this step orphans:

```bash
cd "$W" && grep -rn "_ADVISOR_SIGNOFF_WITHHELD_DISCLOSURE\|advisor_signoff_withheld_control_envelope\|\"advisor_terminal_block\"" src/elspeth | grep -v "^src/elspeth/web/composer/service.py:.*import"
```

A name with no remaining reader is deleted, together with its replay arm in
`control_messages.py` and its `WithheldReplyOrigin` literal member. A name
that still has a reader stays.

- [ ] **Step 5: Run the three tests; expect 3 passed.** Same command as Step 2.

- [ ] **Step 6: Re-seat the tests that pinned the old behaviour**

```bash
cd "$W" && L=/tmp/reply-on-block-task1-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web tests/integration/web \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/reply-on-block-baseline-reds.txt
```

Expected new reds, and the one rule for each:

| Red | Rule |
|---|---|
| `TypeError: unexpected keyword 'advisor_repair_context_introduced'` | delete the argument from that call |
| asserts the blocked message does NOT start with the prose, or asserts `raw_assistant_content == ""`, on an injected turn | the ruling retired it: assert the prose is published, and name the ruling and date in the docstring |
| asserts a `composer_withheld_reply` row with origin `advisor_terminal_block` | delete the test; the origin no longer exists |
| asserts the withheld disclosure row is written on a block | delete the test, or if it also covers case 5, keep the case 5 half |
| anything touching `_replace_advisor_repair_public_result` | STOP. That is case 5 and must not have moved |

Six sites passed `advisor_repair_context_introduced=True` at `0bd0c5e40`
(`test_advisor_checkpoint.py:1617,2535,2743,2809,4568,4654`); start there. Do
not weaken an assertion that is not on this list.

- [ ] **Step 7: Commit** (pathspec: `service.py`, `control_messages.py` and
  `withheld_replies.py` if touched, the test files), message
  `fix(composer): publish the composer's reply when the advisor blocks a turn`.

### Task 2: The repair instruction tells the truth about the way out

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (the two clause constants)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py`

**Interfaces:** none. Two string constants change; both injection sites share
them by reference.

- [ ] **Step 1: Write the failing test**

```python
def test_repair_instruction_offers_a_published_way_out() -> None:
    """Ruling 2026-09-22: 'say what blocks you' must describe an exit that exists."""
    from elspeth.web.composer.service import _ADVISOR_MUTATION_EXPECTATION_CLAUSE, _ADVISOR_OUTPUT_CONTRACT_CLAUSE

    # The anti-lookup wording from elspeth-71617f1d21 stays.
    assert "lookup-only calls is not a fix" in _ADVISOR_MUTATION_EXPECTATION_CLAUSE
    # The exit is named, covers a decision only the user can make, and says the reply is shown.
    assert "decision only the user can make" in _ADVISOR_MUTATION_EXPECTATION_CLAUSE
    assert "make no change" in _ADVISOR_MUTATION_EXPECTATION_CLAUSE
    assert "shown to the user" in _ADVISOR_MUTATION_EXPECTATION_CLAUSE
    # Quoting the fenced text is still forbidden; rebutting or mentioning the review is not.
    assert "do not quote the fenced text" in _ADVISOR_OUTPUT_CONTRACT_CLAUSE
    assert "never reference, quote, or rebut" not in _ADVISOR_OUTPUT_CONTRACT_CLAUSE
```

- [ ] **Step 2: Run; expect FAIL on the second assertion.**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "repair_instruction_offers" -v
```

- [ ] **Step 3: Replace both constants**

```python
_ADVISOR_OUTPUT_CONTRACT_CLAUSE: Final[str] = (
    "Fix the findings via tool calls. The end user has not read these "
    "findings: your final reply is shown to them, so write it to stand on "
    "its own and do not quote the fenced text."
)
```

```python
_ADVISOR_MUTATION_EXPECTATION_CLAUSE: Final[str] = (
    "Resolving these findings requires pipeline MUTATIONS via tool calls "
    "(e.g. patch_node_options, upsert_node, patch_source_options, "
    "patch_output_options). Re-reading state (get_pipeline_state) or other "
    "lookup-only calls is not a fix and wastes this repair pass. If a "
    "finding needs a decision only the user can make, or no tool call can "
    "address it, make no change: tell the user what blocks you and what "
    "their options are. That reply ends the turn and is shown to the user. "
)
```

Keep the comments above each constant and add one line to each citing the
ruling date and elspeth-032ec69c41. The trailing space on the second constant
is load-bearing: the two are concatenated.

- [ ] **Step 4: Run the test, then the clause's other pins**

```bash
cd "$W" && L=/tmp/reply-on-block-task2-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web/composer \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/reply-on-block-baseline-reds.txt
```

A red that compares the injected message to an old literal is updated to the
new text. A red in a prompt-hash or redaction-snapshot pin means these
constants feed a recorded hash: follow that gate's own regeneration command
from `CONTRIBUTING.md`, and confirm only hashes moved.

- [ ] **Step 5: Commit**, message
  `fix(composer): name the published way out in the advisor repair instruction`.

### Task 3: The coalesce rejection says what to do instead

**Files:**
- Modify: `src/elspeth/web/composer/state.py` (the coalesce `on_error` arm)
- Test: `tests/unit/web/composer/test_edge_route_reconciliation.py` (append at
  module level). It already calls `edge_lowering_error` directly (around
  `:627`) but only checks allowed/rejected, never the text: measured, no test
  in `tests/` contains "has no on_error route".

**Interfaces:**
- Consumes: `edge_lowering_error(edge: EdgeSpec, *, from_kind, to_kind) -> str | None`
  and `EdgeSpec(id, from_node, to_node, edge_type, label)` from
  `elspeth.web.composer.state`.
- Produces: one returned string changes.

- [ ] **Step 1: Write the failing test**

```python
def test_coalesce_on_error_rejection_names_the_supported_alternatives() -> None:
    """elspeth-032ec69c41: the only place the model learns this limit must also teach the way forward."""
    from elspeth.web.composer.state import EdgeSpec, edge_lowering_error

    edge = EdgeSpec(id="e1", from_node="merge_ab", to_node="errors", edge_type="on_error", label=None)
    message = edge_lowering_error(edge, from_kind="coalesce", to_kind="output")
    assert message is not None
    assert "Coalesce 'merge_ab' has no on_error route" in message
    assert "on_error of each branch transform" in message
    assert "'best_effort'" in message
    assert "ask the user" in message
```

- [ ] **Step 2: Run; expect FAIL on the third assertion**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_edge_route_reconciliation.py \
  -k "rejection_names_the_supported_alternatives" -v
```

A failure on the first or second assertion means the call shape is wrong,
not the message: fix the test first.

- [ ] **Step 3: Confirm the message reaches the model.** `edge_lowering_error`
  is the validator; check its text is what `upsert_edge` returns:

```bash
cd "$W" && grep -rn "edge_lowering_error(" src/elspeth/web/composer | grep -v "def edge_lowering_error"
```

Read each caller. If one wraps or replaces the string before it reaches a
tool result, note where in the hand-back; the message change still stands.

- [ ] **Step 4: Change the message**

```python
            return (
                f"Coalesce '{edge.from_node}' has no on_error route: coalesce outcomes are governed by its arrival policy. "
                "To capture a failed branch row, set the on_error of each branch transform to a sink instead; "
                "a branch row diverted that way never reaches the coalesce, so under policy 'require_all' no merged "
                "row is produced for it. To emit a merged row from the branches that did arrive, set the coalesce "
                "policy to 'best_effort', 'quorum' or 'first'. These are different semantics: ask the user which they want."
            )
```

The four policy names are `CoalesceSettings.policy` in `core/config.py`
(`Literal["require_all", "quorum", "best_effort", "first"]`); re-read that
line before committing and keep the message in step with it.

- [ ] **Step 5: Run the test and the rejection-parity gate**

```bash
cd "$W" && L=/tmp/reply-on-block-task3-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web/composer tests/unit/contracts \
  -o pythonpath="$W/src $W/elspeth-lints/src" -k "rejection or on_error or coalesce or parity" > "$L" 2>&1; \
echo "exit=$?"; tail -12 "$L"
```

`CONTRIBUTING.md` § "Gate: runtime-rejection parity" pins composer rejections
against the engine's. If it goes red, the engine message
(`core/config.py`, "does not accept on_error") is the parity partner: read
the gate's message before changing either side.

- [ ] **Step 6: Commit**, message
  `fix(composer): teach the supported alternatives when a coalesce error route is rejected`.

### Task 4: Gates, record, hand back

- [ ] **Step 1:** `ruff check`, `ruff format --check`, and
  `mypy src/elspeth/web` with the two-root `PYTHONPATH`.
- [ ] **Step 2:** Trust-tier corpus with
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing`,
  diffed against the same command on the base. No new finding; line-only
  binding drift is reported, never hand-edited.
- [ ] **Step 3:** Full-suite gate, after checking no other suite is running:

```bash
cd "$W" && scripts/full-suite-gate.sh --execute --detach --root "$W" --stages ruff,mypy,contracts,lints,pytest
```

Read `summary.txt`; `frozen=NO` is not evidence. `testcontainer` is not
required: nothing persisted changes shape (rows stop being written; none
gains a field).

- [ ] **Step 4:** Add a dated line to `docs/agents/recent-code-hints.md`:

```markdown
- 2026-09-22 — A turn blocked by the END advisor gate publishes the
  composer's reply (`_advisor_blocked_result`, ruling on elspeth-032ec69c41).
  The `advisor_terminal_block` withheld-reply origin and the withheld
  disclosure row are gone. Case 5 (`_replace_advisor_repair_public_result`)
  still withholds; do not "align" it.
```

- [ ] **Step 5: Hand back.** Branch, commit hashes, the gate run dir and its
  `summary.txt`, the list of re-seated and deleted tests with the rule applied
  to each, which forwarding parameters were removed, and what Task 3 Step 3
  found about how the rejection text reaches a tool result. Do not merge.
