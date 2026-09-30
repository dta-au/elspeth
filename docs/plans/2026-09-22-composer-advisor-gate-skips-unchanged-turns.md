# Composer advisor gate skips an already-blocked, unchanged graph — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A compose turn that changes nothing, on a graph the advisor has
already reviewed and blocked, no longer re-runs the END advisor gate, so a
question about the block gets its answer instead of a forced repair loop.

**Architecture:** The route already holds the prior state row; it parses the
durable `completion_gates` fact and hands it to `compose()`. A pure predicate
decides the skip: the version is unchanged this turn AND the fact's `for_graph`
equals the current graph fingerprint. The END gate returns early on it. The
finalized result then has the same fact folded into its preflight by
`merge_completion_gates`, the function the Run path already uses, so chat and
Run agree by construction. A graph no advisor has seen still gets its review.

**Tech Stack:** Python 3.12, pytest (xdist; `-n 0` for single tests),
structlog. No frontend change, no schema change, no new persisted field.

**Spec:** `docs/reviews/2026-09-22-composer-advisor-block-diagnosis.md`
(§ Rulings; § "The gate is keyed to session state"). Operator approved the
narrow form on 2026-09-22. Ticket: elspeth-032ec69c41.

## Anchors: locate by quoted code, not by line number

The `file:line` references in the tasks below were measured at `971a597b6`.
`release/0.8.1` has since moved (the reply-withholding branch merged as
`0bd0c5e40`, which added about 100 lines above these sites), and other
sessions are still landing work in `service.py`. Find each site by its quoted
code. Re-measured at `0bd0c5e40` for orientation only:

| Site | Find this string | Line at `0bd0c5e40` |
|---|---|---|
| `compose` | `    async def compose(` | `service.py:3961` |
| `_classify_and_budget_turn` | `async def _classify_and_budget_turn(` | `:5566` |
| P5 gate call | `advisor_gate = await self._evaluate_terminal_no_tool_advisor_gate(` (first) | `:5914` |
| `_try_terminate_no_tools` | `async def _try_terminate_no_tools(` | `:6047` |
| P2 gate call | same string (second) | `:6237` |
| finalize tail in `_try_terminate_no_tools` | `result = await self._surface_and_finalize_no_tools(` (the one after the P2 gate call) | `:6339` |
| gate | `async def _evaluate_terminal_no_tool_advisor_gate(` | `:6633` |
| gate's first early return | `if _state_is_structurally_empty(state) or advisor_checkpoint_passes_used >= max_passes:` | `:6700` |
| `_compose_loop` | `async def _compose_loop(` | `:6943` |
| copy, policy module | `on your next message` (6 hits) | `no_tool_policy.py:142,162,185,265,331,351` |
| copy, service | `on your next message` (2 hits) | `service.py:11062,11066` |

`_surface_and_finalize_no_tools` is called three times; Task 3 Step 5 edits
only the call inside `_try_terminate_no_tools`. Before Task 0, run
`git log --oneline -1` and re-run this table's greps; if a string has no hit,
stop and report rather than guessing a new site.

## Global Constraints

- Composer invariant 1: the skip is decided on pipeline state and persisted
  gate facts only. Never read, classify or pattern-match the user's message.
- Composer invariant 2: no tutorial-only or guided-only branch.
- `completion_gates=None` means "no durable fact known" and MUST take today's
  path (the review runs). Every new parameter defaults to `None` for that
  reason: an omitting caller fails toward reviewing, never toward skipping.
- No `# noqa`, `# type: ignore`, or lint suppression. No hand-edited judge
  signatures. No `git stash`. Commit with `git commit -- <pathspec>`.
- Read `CONTRIBUTING.md` § "Whole-tree gates and conventions you will hit"
  before the first edit. Task 4 touches `no_tool_policy.py`, which the
  wire-shape template gate pins.
- Do not merge, push, or fast-forward `release/0.8.1`. Stop at "ready for
  review".
- Work in a dedicated worktree. Inside it a bare `pytest` imports the MAIN
  checkout. Every command below uses the two-root form.
- Copy rule: "on your next message" becomes "after your next pipeline change"
  wherever a notice promises a re-review. Exact strings are in Task 4.

## Non-goals

- Publishing the composer's reply on a blocked turn, rewording the repair
  instruction, and teaching the coalesce limit in its rejection message:
  `docs/plans/2026-09-22-composer-reply-published-on-advisor-block.md`.
- Showing the advisor's finding to the user (the reviewer's note). A third
  plan, not yet written: it adds required keys to the strict gate-fact
  envelope and changes the frontend wire type.
- `_replace_advisor_repair_public_result` after a CLEAN re-review (case 5).
  Ruled KEEP.
- The report's regression test 1. It may target the wrong actor; it waits on
  open question Q1.
- **A re-review that can clear a block without a pipeline change.** A state
  row, and the gate fact with it, is saved only when the version changes
  (`sessions/routes/messages.py:838`). So today a retry after an
  advisor outage can return CLEAN in chat while Run stays withheld. This plan
  does not make that worse (it skips the futile review) and does not fix it.
  Flagged for John as a follow-up ticket; the "Retry the request" sentence in
  Task 4 is the user-visible symptom.
- **A block that first arises on an unmutated turn is not covered** (final
  review finding I1, 2026-09-22). The same save rule cuts the other way: an
  END-gate terminal block on a turn whose version did not move writes no
  state row, so no fact persists and the next question re-enters the review.
  The reported incident (`fc112a2f` mutated, then `6ebd025d` asked) is
  covered; a question-only first block is not. Covering it needs a new save
  path (a gate-fact-only state row, or keying the predicate off the
  `AdvisorTerminalPublication` audit row) — a ruling for John, not this plan.
- `service.py:10878` ("Reword your chat message ... then resend") and
  `ADVISOR_SIGNOFF_PENDING_DETAIL` ("Re-run the composer ..."). Same false
  promise, different phrase, separately pinned. Flagged, not changed.
- Base branch. `fix/composer-reply-withholding` is already merged into local
  `release/0.8.1` (`0bd0c5e40`), so branching from `release/0.8.1` includes
  it. It changed when `_advisor_blocked_result` withholds prose; it did not
  change the gate's early returns or the finalize tail this plan edits.

## Review Focus

1. **Never-reviewed graph, unchanged turn** (mutate, time out, then ask a
   question). No fact exists, so the predicate is false and the review must
   still run. Pinned in Task 1 (`..._false_without_a_fact`) and Task 3
   (`test_unchanged_turn_without_a_block_still_reviews`).
2. **Fact for a different graph** (carried forward by a recovery save after
   the content moved). Predicate false; review runs. Pinned in Task 1.
3. **Chat must not say complete over a blocked graph.** A question turn where
   the model ran `preview_pipeline` finalizes with a green preflight. The
   published result must carry `completion_ready=False`. Pinned in Task 3
   (`test_skipped_turn_result_keeps_completion_withheld`).
4. **The P5 last-chance site** (`_classify_and_budget_turn`, `service.py:5841`)
   calls the same gate and must pass the fact too, or it reviews where P2
   skips. Pinned in Task 2 by an AST assertion that both call sites pass
   `completion_gates=`.
5. **Corrupt persisted envelope.** `parse_completion_gates` raises
   `ValueError` on a malformed shape (Tier 1). The route must let it
   propagate, not catch it and compose anyway. No new test: the call is added
   outside every `try` in both routes, and Task 2 Step 3 says where.

---

### Task 0: Worktree and provenance

**Files:** none changed.

- [ ] **Step 1: Create the worktree**

```bash
cd "$(git rev-parse --show-toplevel)" && \
git worktree add .claude/worktrees/advisor-gate-unchanged-turns \
  -b fix/advisor-gate-unchanged-turns release/0.8.1 && \
scripts/worktree-cleanup.sh --link-venv --path '*advisor-gate-unchanged-turns*'
```

Re-run the script with `--execute` if its dry run reports the `.venv` link
missing.

- [ ] **Step 2: Prove imports resolve inside the worktree**

```bash
W="$(git rev-parse --show-toplevel)/.claude/worktrees/advisor-gate-unchanged-turns"; \
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -c \
  "import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)"
```

Expected: both paths contain `advisor-gate-unchanged-turns`. Otherwise stop.

- [ ] **Step 3: Baseline**

```bash
cd "$W" && L=/tmp/advisor-gate-baseline-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest \
  tests/unit/web -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; \
echo "exit=$?"; grep -E "^(FAILED|ERROR)" "$L" | sort > /tmp/advisor-gate-baseline-reds.txt; \
wc -l /tmp/advisor-gate-baseline-reds.txt
```

Expected: `exit=0`, or a recorded red set. Two guided integration tests are
known red on `release/0.8.1`; they are not yours.

### Task 1: The predicate

**Files:**
- Modify: `src/elspeth/web/execution/completion_gates.py` (append after
  `merge_completion_gates`)
- Test: `tests/unit/web/execution/test_completion_gates.py` (append; it
  already has the `_make_state(*, version: int = 1)` helper used below)

**Interfaces:**
- Consumes: `CompletionGateFacts`, `AdvisorSignoffGateFact`,
  `completion_gate_fingerprint(state) -> str` (all in this module).
- Produces:
  `advisor_block_covers_unchanged_graph(facts: CompletionGateFacts | None, state: CompositionState, *, initial_version: int) -> bool`

- [ ] **Step 1: Write the failing tests**

```python
from dataclasses import replace

from elspeth.web.execution.completion_gates import (
    AdvisorSignoffGateFact,
    CompletionGateFacts,
    advisor_block_covers_unchanged_graph,
    completion_gate_fingerprint,
)


def _blocked_facts_for(state) -> CompletionGateFacts:
    return CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail="Completion advisory review did not clear after the available attempts.",
            suggestion=None,
            for_graph=completion_gate_fingerprint(state),
        )
    )


def test_block_covers_an_unchanged_graph_it_was_recorded_for():
    simple_state = _make_state(version=3)
    facts = _blocked_facts_for(simple_state)
    assert advisor_block_covers_unchanged_graph(facts, simple_state, initial_version=simple_state.version) is True


def test_block_does_not_cover_a_turn_that_changed_the_version():
    simple_state = _make_state(version=3)
    facts = _blocked_facts_for(simple_state)
    assert advisor_block_covers_unchanged_graph(facts, simple_state, initial_version=simple_state.version - 1) is False


def test_block_covers_is_false_without_a_fact():
    simple_state = _make_state(version=3)
    assert advisor_block_covers_unchanged_graph(None, simple_state, initial_version=simple_state.version) is False
    no_gate = CompletionGateFacts(advisor_signoff=None)
    assert advisor_block_covers_unchanged_graph(no_gate, simple_state, initial_version=simple_state.version) is False


def test_block_does_not_cover_a_different_graph():
    simple_state = _make_state(version=3)
    stale = CompletionGateFacts(
        advisor_signoff=replace(_blocked_facts_for(simple_state).advisor_signoff, for_graph="0" * 64)
    )
    assert advisor_block_covers_unchanged_graph(stale, simple_state, initial_version=simple_state.version) is False
```

Merge the four imported names into the file's existing import from
`elspeth.web.execution.completion_gates`; do not add a second import line.

- [ ] **Step 2: Run; expect ImportError on the new name**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/execution -k "block_covers or block_does_not_cover" -v
```

- [ ] **Step 3: Implement**

Append to `src/elspeth/web/execution/completion_gates.py`:

```python
def advisor_block_covers_unchanged_graph(
    facts: CompletionGateFacts | None,
    state: CompositionState,
    *,
    initial_version: int,
) -> bool:
    """True when this turn changed nothing AND the advisor already blocked this exact graph.

    Operator ruling 2026-09-22 (elspeth-032ec69c41). A gate fact persists only
    alongside a new state row, so re-reviewing a graph that is unchanged this
    turn and already carries a blocked fact can re-block or trap the turn but
    can never clear anything. ``None`` facts, a fact for other content, or any
    version movement all answer False: a graph no advisor has ruled on still
    gets its review. Decided on state and persisted facts, never on user text.
    """
    if state.version != initial_version:
        return False
    if facts is None or facts.advisor_signoff is None:
        return False
    return facts.advisor_signoff.for_graph == completion_gate_fingerprint(state)
```

- [ ] **Step 4: Run; expect 4 passed.** Same command as Step 2.

- [ ] **Step 5: Commit**

```bash
cd "$W" && scripts/branch-safety-check.sh --intent commit && git status --short && \
git add src/elspeth/web/execution/completion_gates.py tests/unit/web/execution && \
git commit -m "feat(composer): predicate for an advisor block that already covers the unchanged graph" \
  -m "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" \
  -- src/elspeth/web/execution/completion_gates.py tests/unit/web/execution
```

### Task 2: Thread the fact from the routes to the gate

**Files:**
- Modify: `src/elspeth/web/composer/protocol.py:1471-1483` (`compose`)
- Modify: `src/elspeth/web/composer/service.py`: `compose` (`:3920`),
  `_compose_loop` (`:6830`), `_try_terminate_no_tools` (`:5949`),
  `_classify_and_budget_turn` (`:5496`),
  `_evaluate_terminal_no_tool_advisor_gate` (`:6539`), and the call sites at
  `:4036`, `:5841`, `:6152`, `:7036`, plus the `_classify_and_budget_turn`
  call inside `_compose_loop`
- Modify: `src/elspeth/web/sessions/routes/messages.py:352`
- Modify: `src/elspeth/web/sessions/routes/composer/compose.py:196`
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py` (append)

**Interfaces:**
- Consumes: `CompletionGateFacts`, `parse_completion_gates` from Task 1's
  module.
- Produces: the keyword `completion_gates: CompletionGateFacts | None = None`
  on all six signatures above. Nothing reads it yet; Task 3 does.

- [ ] **Step 1: Write the failing test**

```python
def test_both_end_gate_call_sites_pass_the_durable_gate_fact():
    """P2 and P5 share one gate; a site that omits the fact reviews where the other skips."""
    import ast
    import inspect

    from elspeth.web.composer import service as service_module

    tree = ast.parse(inspect.getsource(service_module))
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_evaluate_terminal_no_tool_advisor_gate"
    ]
    assert len(calls) == 2
    for call in calls:
        assert "completion_gates" in {kw.arg for kw in call.keywords}
```

- [ ] **Step 2: Run; expect FAIL** (`'completion_gates' not in {...}`). If it
  fails on `len(calls) == 2` instead, the instrument is wrong: print the
  count and fix the test before touching production code.

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "both_end_gate_call_sites" -v
```

- [ ] **Step 3: Thread it**

Add the import to `service.py` beside the existing one at `:224`:

```python
from elspeth.web.execution.completion_gates import CompletionGateFacts, advisor_signoff_check_failed
```

and to `protocol.py` under its `TYPE_CHECKING` block (follow how
`SessionOperationContext` is imported there).

On each of the six signatures add, as the last keyword:

```python
        # Durable advisor gate fact from the prior state row (ruling
        # 2026-09-22). ``None`` = none known: the END gate reviews as before.
        completion_gates: CompletionGateFacts | None = None,
```

On `_compose_loop` it goes in the keyword-only block after `policy_catalog`.
At each call site pass it straight down: `completion_gates=completion_gates,`.

In both routes, above the `try:` that wraps the compose call and outside any
other `try`, so a Tier 1 `ValueError` from a corrupt envelope propagates:

```python
                prior_completion_gates_facts = parse_completion_gates(state_record.composer_meta) if state_record is not None else None
```

and add to the `composer.compose(...)` call:

```python
                            completion_gates=prior_completion_gates_facts,
```

`messages.py` already imports from `completion_gates` (`:146`); add
`parse_completion_gates` to that import. `compose.py` holds the row as
`state_record` (`:123`); add the import there.

- [ ] **Step 4: Run the AST test, then types**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "both_end_gate_call_sites" -v && \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m mypy src/elspeth/web/composer src/elspeth/web/sessions/routes
```

Expected: PASS, and mypy clean. A mypy error on a `ComposerService` fake in
`tests/` means a test double implements the protocol; add the same keyword to
it.

- [ ] **Step 5: Run the web unit suite for signature pins**

```bash
cd "$W" && L=/tmp/advisor-gate-task2-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web tests/unit/elspeth_lints \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/advisor-gate-baseline-reds.txt
```

Expected: no new red. The session-operation-lease gate follows
`self.<member>(...)` call edges; if it reports a new edge, read its message
and follow it, do not suppress it.

- [ ] **Step 6: Commit** (pathspec: the two routes, `protocol.py`,
  `service.py`, the test file), message
  `refactor(composer): hand the durable advisor gate fact to the END gate`.

### Task 3: Skip the gate, keep completion withheld

**Files:**
- Modify: `src/elspeth/web/composer/service.py:6598-6600` (gate) and
  `:6245-6262` (`_try_terminate_no_tools` finalize tail)
- Modify: `tests/unit/web/composer/test_advisor_checkpoint.py:2308-2439`
  (`drive_try_terminate`: two new optional parameters)
- Test: same file (append)

**Interfaces:**
- Consumes: `advisor_block_covers_unchanged_graph` (Task 1),
  `merge_completion_gates(result, facts, state) -> ValidationResult`, the
  `completion_gates` keyword (Task 2).
- Produces: behaviour only.

- [ ] **Step 1: Extend the test driver**

In `drive_try_terminate`, add two parameters after `initial_version: int = 1,`:

```python
    completion_gates: CompletionGateFacts | None = None,
    finalize_result: ComposerResult | None = None,
```

Replace the `_surface_and_finalize_no_tools` stub's `return_value` with:

```python
        return_value=finalize_result or ComposerResult(message="Done — the pipeline is ready.", state=state)
```

and pass `completion_gates=completion_gates,` in the
`service._try_terminate_no_tools(...)` call. Import `CompletionGateFacts`,
`AdvisorSignoffGateFact` and `completion_gate_fingerprint` at the top of the
file. `ComposerResult` is imported inside the function today; move that import
above the signature so the annotation resolves.

- [ ] **Step 2: Write the failing tests**

```python
def _blocked_facts_for(state) -> CompletionGateFacts:
    return CompletionGateFacts(
        advisor_signoff=AdvisorSignoffGateFact(
            detail="Completion advisory review did not clear after the available attempts.",
            suggestion="Review the pipeline.",
            for_graph=completion_gate_fingerprint(state),
        )
    )


def _flagging_advisor() -> _AsyncRecorder:
    return _AsyncRecorder(return_value=AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text="FLAGGED: unresolved"))


@pytest.mark.asyncio
async def test_question_on_an_already_blocked_graph_skips_the_review(make_service, clean_runnable_state):
    """Ruling 2026-09-22 (session 6990d39f): asking what a block means must not re-enter the repair loop."""
    service = make_service()
    service._run_advisor_checkpoint = _flagging_advisor()
    outcome = await drive_try_terminate(
        service,
        clean_runnable_state,
        advisor_checkpoint_passes_used=0,
        initial_version=clean_runnable_state.version,
        completion_gates=_blocked_facts_for(clean_runnable_state),
        message="What does this mean, and what are my options?",
    )
    assert service._run_advisor_checkpoint.calls == []
    assert outcome.action == "return"


@pytest.mark.asyncio
async def test_unchanged_turn_without_a_block_still_reviews(make_service, clean_runnable_state):
    """Control and Review Focus 1: a graph no advisor has ruled on keeps its backstop review."""
    service = make_service()
    service._run_advisor_checkpoint = _flagging_advisor()
    outcome = await drive_try_terminate(
        service,
        clean_runnable_state,
        advisor_checkpoint_passes_used=0,
        initial_version=clean_runnable_state.version,
        completion_gates=None,
    )
    assert len(service._run_advisor_checkpoint.calls) == 1
    assert outcome.action == "continue"


@pytest.mark.asyncio
async def test_skipped_turn_result_keeps_completion_withheld(make_service, clean_runnable_state):
    """Review Focus 3: a green preview on a question turn must not publish completion over the durable block."""
    from elspeth.web.composer.protocol import ComposerResult
    from elspeth.web.execution.schemas import ValidationReadiness, ValidationResult

    green = ValidationResult(
        is_valid=True,
        checks=[],
        errors=[],
        readiness=ValidationReadiness(authoring_valid=True, execution_ready=True, completion_ready=True, blockers=[]),
    )
    service = make_service()
    service._run_advisor_checkpoint = _flagging_advisor()
    outcome = await drive_try_terminate(
        service,
        clean_runnable_state,
        advisor_checkpoint_passes_used=0,
        initial_version=clean_runnable_state.version,
        completion_gates=_blocked_facts_for(clean_runnable_state),
        finalize_result=ComposerResult(message="Here are your options.", state=clean_runnable_state, runtime_preflight=green),
    )
    assert outcome.result.message == "Here are your options."
    assert outcome.result.runtime_preflight.readiness.completion_ready is False
    assert outcome.result.runtime_preflight.readiness.execution_ready is True
```

- [ ] **Step 3: Run; expect the first and third to FAIL, the control to PASS**

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "already_blocked_graph_skips or without_a_block_still_reviews or keeps_completion_withheld" -v
```

A failing control means the driver edit is wrong. Fix that first.

- [ ] **Step 4: Implement the skip**

In `_evaluate_terminal_no_tool_advisor_gate`, directly after the existing
early return at `:6599-6600`:

```python
        # Operator ruling 2026-09-22 (elspeth-032ec69c41): this turn changed
        # nothing and the advisor has already blocked this exact graph. A
        # gate fact persists only with a new state row, so another review
        # could re-block or trap the turn but never clear anything. Observed
        # live (session 6990d39f): "what does this block mean?" was answered,
        # FLAGGED over the unchanged graph, and the repair injection ordered
        # pipeline edits. Unlike the proof gate above (whose version guard was
        # removed because a resumed session can carry an unreported blocker),
        # the last advisor ruling is already durable on the state row.
        if advisor_block_covers_unchanged_graph(completion_gates, state, initial_version=initial_version):
            slog.info(
                "composer_advisor_end_gate_skipped",
                reason="unchanged_graph_already_blocked",
                state_version=state.version,
                session_id=session_id,
            )
            return _TerminalNoToolAdvisorGateOutcome(action="fall_through")
```

Extend the import from Task 2 with `advisor_block_covers_unchanged_graph` and
`merge_completion_gates`.

Before keeping the `slog.info`, load the `logging-telemetry-policy` skill. The
precedent is `composer_authoring_surface_selected` (`service.py:4002`): closed
vocabulary, no user text. If the policy forbids it, delete those six lines.

- [ ] **Step 5: Implement the withheld completion**

In `_try_terminate_no_tools`, directly after the
`result = await self._surface_and_finalize_no_tools(...)` call (`:6245-6262`):

```python
        # The END gate stood aside for a graph the advisor already blocked
        # (ruling 2026-09-22). Fold the same durable fact into this turn's
        # preflight with the read-side merge the Run path uses, so the chat
        # never reports completion that /validate and Run still withhold.
        if result.runtime_preflight is not None and advisor_block_covers_unchanged_graph(
            completion_gates, state, initial_version=initial_version
        ):
            result = replace(
                result,
                runtime_preflight=merge_completion_gates(result.runtime_preflight, completion_gates, state),
            )
```

`replace` is already imported in `service.py` (used at `:6692`).

- [ ] **Step 6: Run the three tests; expect 3 passed.** Same command as Step 3.

- [ ] **Step 7: Measure the wider red set**

```bash
cd "$W" && L=/tmp/advisor-gate-task3-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web tests/integration/web \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/advisor-gate-baseline-reds.txt
```

Expected: no new red. Existing tests pass no `completion_gates`, so they take
today's path. A new red here is a real finding; read it, do not re-seat it.

- [ ] **Step 8: Commit** (pathspec: `service.py`, the test file), message
  `fix(composer): do not re-review an unchanged graph the advisor already blocked`,
  body citing session 6990d39f, the ruling date and the ticket.

### Task 4: Correct the copy that promises a re-review on the next message

**Files:**
- Modify: `src/elspeth/web/composer/no_tool_policy.py:134,153,174,231,275,286`
- Modify: `src/elspeth/web/composer/service.py:10884,10888`
- Modify: `tests/unit/web/composer/test_no_tool_policy_segments.py:110`
- Test: same test file (append)

**Interfaces:** none. Eight string literals change. Template structure must
not: the wire-shape gate round-trips every template in `no_tool_policy.py`.

- [ ] **Step 1: Write the failing test, with its control**

```python
def _composer_sources_containing(phrase: str) -> list[str]:
    from pathlib import Path

    import elspeth.web.composer as composer_pkg

    root = Path(composer_pkg.__file__).parent
    return sorted(str(p.relative_to(root)) for p in root.rglob("*.py") if phrase in p.read_text(encoding="utf-8"))


def test_no_notice_promises_a_review_on_the_next_message():
    """Ruling 2026-09-22: a blocked graph is re-reviewed after the next pipeline CHANGE."""
    # Control: the instrument must find a phrase known to be present.
    assert _composer_sources_containing("Composer completion is withheld") != []
    assert _composer_sources_containing("on your next message") == []
```

- [ ] **Step 2: Run; expect FAIL on the second assertion**, naming
  `no_tool_policy.py` and `service.py`.

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_no_tool_policy_segments.py \
  -k "promises_a_review_on_the_next_message" -v
```

- [ ] **Step 3: Change the eight strings** with the Edit tool, one occurrence
  at a time, never `sed`:

| Old | New |
|---|---|
| `run again on your next message` | `run again after your next pipeline change` |
| `runs again on your next message` | `runs again after your next pipeline change` |
| `again on your next message` (split across lines at `no_tool_policy.py:275,286`) | `again after your next pipeline change` |

Line 174 keeps its opening "Retry the request, or check the advisor model
configuration;". That clause is the known-false sentence listed under
Non-goals; leave it and say so in the hand-back.

Apply the same replacement to the pinned expectation at
`test_no_tool_policy_segments.py:110`.

- [ ] **Step 4: Run the policy, segment and wire-shape tests**

```bash
cd "$W" && L=/tmp/advisor-gate-task4-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web/composer \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; tail -15 "$L"
```

Expected: `exit=0`. A red in
`test_the_round_trip_parametrization_covers_every_wrapped_template` or a
notice-twin test means a template's structure moved; revert that edit and redo
it touching only the phrase.

- [ ] **Step 5: Look for the phrase outside the composer package**

```bash
cd "$W" && grep -rn "on your next message" src/elspeth/web/frontend/src docs/guides docs/reference website 2>/dev/null; echo "grep_exit=$?"; \
grep -rn "Composer completion is withheld" src/elspeth/web/composer | head -1
```

Expected: the first prints nothing (`grep_exit=1`); the second prints a line,
proving the search reaches the tree. Any hit gets the same replacement and
joins the commit's pathspec.

- [ ] **Step 6: Commit**, message
  `fix(composer): say the advisory review re-runs after a pipeline change`.

### Task 5: Gates, record, hand back

- [ ] **Step 1: Lint and types**

```bash
cd "$W" && .venv/bin/ruff check src tests && .venv/bin/ruff format --check src tests && \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m mypy src/elspeth/web
```

- [ ] **Step 2: Trust-tier corpus, compared and not zeroed**

```bash
cd "$W" && export PYTHONPATH="$W/src:$W/elspeth-lints/src" && \
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  .venv/bin/elspeth-lints check --rules all --root src/elspeth > /tmp/advisor-gate-lints-after-$$.txt 2>&1; echo "exit=$?"
```

Exit 1 is the deliberate fail-closed state. Produce the before-corpus with
the same command in the main checkout and diff. Expected: no new finding.
Line-only drift in `service.py` bindings is an honest re-pin for the
operator's next signing pass: report it, never hand-edit it.

- [ ] **Step 3: Full-suite gate**

Shared composer runtime behaviour and two route call sites changed, so the
full suite is owed. Check no other suite is running on the host first, then:

```bash
cd "$W" && scripts/full-suite-gate.sh --execute --detach --root "$W" \
  --stages ruff,mypy,contracts,lints,pytest
```

Poll the printed `.done` path; read `summary.txt`. `frozen=NO` is not
evidence. `testcontainer` is not required: no schema, SQL, persistence or
lock changed (the fact is read with an existing parser, nothing new is
written).

- [ ] **Step 4: Dated hint line** under the newest date in
  `docs/agents/recent-code-hints.md`:

```markdown
- 2026-09-22 — The END advisor gate stands aside when the turn changed
  nothing AND the prior state row's `completion_gates` fact was recorded for
  this exact graph (`advisor_block_covers_unchanged_graph`; ruling on session
  6990d39f, elspeth-032ec69c41). `completion_gates=None` always reviews. A new
  caller of `compose()` that holds a state row should pass
  `parse_completion_gates(record.composer_meta)`.
```

- [ ] **Step 5: Hand back.** Report the branch, commit hashes, the gate run
  dir and its `summary.txt`, the lint-corpus diff, and the three flagged copy
  strings. Do not merge.
