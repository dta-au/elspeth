# Composer advisor reviewer's note (header + bounded stored note) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** When the END advisor gate blocks a turn, the chat decision panel
shows a backend-validated header (closed category, step ids checked against
the pipeline) over the advisor's own words in a labelled, plain-text, capped
block — in the turn that blocked and on every later `/validate` of the same
graph — so the user can act on the finding instead of reading "Completion
advisory review did not clear."

**Architecture:** The advisor is asked to end its verdict with two machine
lines (`CATEGORY:` from a closed set, `STEPS:` component ids). The parser
reads them leniently; the blocked-result builder validates them against the
composition state (unknown category → `other`, unknown ids dropped) and
writes a backend-authored header sentence into the existing blocker
`detail`. The advisor's prose becomes a bounded plain-text `note` — ONE new
field on `ValidationReadinessBlocker` (wire) and ONE new required key on the
persisted `advisor_signoff` gate fact, so the note survives reload and
appears on `/validate` through `merge_completion_gates` exactly as the
detail does today. The gate-fact parser is strict Tier 1, so the new required
key lands with a `SESSION_SCHEMA_EPOCH` bump (no old rows, no dual
acceptance). The frontend renders `note` under the blocker row as text,
never markdown, and never feeds it into the "Ask the composer" draft.

**Tech Stack:** Python 3.12, pydantic, pytest; React/TypeScript, vitest.

**Spec:** `docs/reviews/2026-09-22-composer-advisor-block-diagnosis.md`
(§ Rulings item 2: "yes, but format it differently" → header + note;
AskUserQuestion 2026-09-22: Header+note / Show it / Store, bounded).
Ticket: elspeth-032ec69c41. Related rulings:
`feedback_advisor_recommendations_belong_in_chat_as_approve_reject` (a
blocking state and its affordance sit at the top level, the chat decision
panel — never a sub-tab) and R2-F13, which this plan NARROWS: advisor words
may reach the user only inside the labelled `note`; `detail`, `suggestion`,
check details and the chat notice stay backend copy.

**Lands after** `fix/advisor-gate-unchanged-turns` (plan 1) and
`fix/reply-on-advisor-block` (plan 2) are merged into `release/0.8.1`; it
edits `_advisor_blocked_result` and `completion_gates.py`, which both
plans touched. Cut the worktree from the merged branch, not from
`0bd0c5e40`.

## Anchors: locate by quoted code, not by line number

| Site | Find this string |
|---|---|
| verdict parser | `def _parse_advisor_checkpoint_guidance(guidance: str) -> AdvisorCheckpointVerdict:` |
| verdict dataclass | `class AdvisorCheckpointVerdict:` |
| END prompt instruction | `"Start your reply with CLEAN or FLAGGED."` (inside `_build_checkpoint_arguments`) |
| wording | `def _advisor_signoff_blocked_wording(` |
| the four in-turn builders | `def _advisor_signoff_blocked_validation(`, `def _advisor_signoff_unverified_validation(`, `def _advisor_signoff_pending_validation(`, `def _advisor_signoff_pending_handoff_validation(` |
| shared red/absent shape | `def _advisor_signoff_fully_blocking_validation(*, detail: str, suggestion: str)` |
| blocked builder | `def _advisor_blocked_result(` |
| gate fact | `class AdvisorSignoffGateDict(TypedDict):` / `class AdvisorSignoffGateFact:` in `src/elspeth/web/execution/completion_gates.py` |
| envelope writer | `def completion_gates_meta_value(` |
| carry-forward writer | `def completion_gates_meta_from_facts(` |
| parser | `def parse_completion_gates(` |
| merge | `def merge_completion_gates(` and `def _reconcile_advisor_blocker(` |
| wire blocker | `class ValidationReadinessBlocker(_StrictResponse):` in `src/elspeth/web/execution/schemas.py` |
| TS type | `export interface ValidationReadinessBlocker {` in `src/elspeth/web/frontend/src/types/index.ts` |
| panel row | `<span className="decision-panel-item-text">{row.detail}` in `src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx` |
| epoch | `SESSION_SCHEMA_EPOCH = 64` in `src/elspeth/web/sessions/models.py` |

## Global Constraints

- Composer invariants 1 and 2: nothing here authors pipeline structure or
  branches on tutorial/guided sessions. Asking the advisor for two machine
  lines is a brief change, not a server-side judgement.
- The note is DATA from an untrusted model that read pipeline data: bounded
  (`ADVISOR_NOTE_MAX_CHARS = 600`), control characters and the two fence
  sentinels stripped, rendered as plain text (React text node inside
  `white-space: pre-wrap`), never markdown, never a link, never prefilled
  into the "Ask the composer about this" draft, never replayed into model
  context (it already is, as the fenced findings; nothing new enters).
- `detail`, `suggestion`, `ValidationCheck.detail` and every chat notice stay
  fixed backend copy. The only surface that may carry advisor words is
  `ValidationReadinessBlocker.note` on the `advisor_signoff_blocked` row. The
  backend-authored pre-scan finding (`findings_backend_authored=True`) keeps
  riding `detail` as today and has NO note (it is not the advisor's words).
- Every `ValidationReadinessBlocker(` constructor sets `note` explicitly
  (REQUIRED, nullable, no default) — 12 sites in `src/elspeth`, 35 in
  `tests/` at `0bd0c5e40`; count again at execution time with
  `grep -rn 'ValidationReadinessBlocker(' src/elspeth tests | grep -v 'class '`.
- The gate fact's new key is REQUIRED (Tier 1 parser raises on absence).
  Therefore bump `SESSION_SCHEMA_EPOCH` by one in the same commit and update
  every literal that pins it (tripwire tests and docs; see Task 5 — the set is
  measured, not remembered).
- No `# noqa`, `# type: ignore`, suppressions, hand-edited signatures or
  `git stash`. Commit with `git commit -- <pathspec>`. Do not merge or push.
- Read `CONTRIBUTING.md` § "Whole-tree gates" first (wire-shape templates,
  notice-pair twins). Dedicated worktree, two-root `PYTHONPATH`, verify
  `elspeth.__file__`.

## Non-goals

- Plan 1's known limit I1 (a block that first arises on an unmutated turn
  persists no fact). Separate ruling.
- Structured `remediation_options` from the advisor (bug report rec. 1).
  The note IS the remediation text, unstructured and labelled as such.
- Changing what the advisor is shown, the repair loop, or pass count.
- Rendering the note anywhere but the decision panel (no Pipeline → Checks
  sub-tab copy; placement ruling stands).

## Review Focus

1. **Advisor omits the machine lines** (older prose habit). Expected: header
   falls back to the `other` sentence with no steps; note still shown.
   Pinned in Task 1 (`test_missing_machine_lines_fall_back_to_other_and_no_steps`).
2. **Advisor names a step id that is not in the state** (hallucinated or
   renamed). Expected: dropped; header lists only ids the state has; if none
   remain, header carries no step sentence. Pinned in Task 3
   (`test_unknown_step_ids_are_dropped_from_the_header`).
3. **Note longer than the cap, or carrying a fence sentinel / control chars.**
   Expected: truncated with a visible `…` marker, sentinels and control chars
   removed, never an empty string when the advisor wrote something. Pinned in
   Task 1 (`test_note_is_bounded_and_sanitised`).
4. **Reload after the block, then `/validate` on the unchanged graph.**
   Expected: the panel shows the same header and note as the blocking turn
   (fact → `merge_completion_gates`). Pinned in Task 4
   (`test_note_survives_reload_and_reaches_validate`) and, on a CHANGED
   graph, the pending wording with `note=None`.
5. **Pre-scan (backend-authored) block.** Expected: unchanged detail, `note`
   is `None`, no "Reviewer's note" block renders. Pinned in Task 3
   (`test_backend_authored_prescan_block_has_no_note`) and Task 6 (frontend).

---

### Task 0: Worktree, provenance, baseline

- [ ] **Step 1:** After plans 1 and 2 are merged, create the worktree from
  the merged `release/0.8.1`:

```bash
cd "$(git rev-parse --show-toplevel)" && \
git worktree add .claude/worktrees/advisor-reviewer-note -b fix/advisor-reviewer-note release/0.8.1 && \
W="$PWD/.claude/worktrees/advisor-reviewer-note" && ln -s "$PWD/.venv" "$W/.venv"
```

(Do NOT run `scripts/worktree-cleanup.sh --execute` on a fresh clean
worktree: it classifies a tree whose HEAD equals the base as REMOVABLE and
removes it — measured 2026-09-22.)

- [ ] **Step 2:** Prove provenance:

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -c \
  "import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)"
```

Expected: both paths contain `advisor-reviewer-note`.

- [ ] **Step 3:** Baseline red set — run in the MAIN checkout (same commit,
  untouched), not in the worktree you are about to edit:

```bash
cd "$(git rev-parse --show-toplevel)" && L=/tmp/reviewer-note-baseline-$$.log; \
.venv/bin/python -m pytest tests/unit/web tests/integration/web -p no:cacheprovider > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort > /tmp/reviewer-note-baseline-reds.txt; wc -l /tmp/reviewer-note-baseline-reds.txt
```

Known reds at `0bd0c5e40`: 2 × `test_freeform_planner_failure_translation`,
and `test_guided_full` ids that flake under parallelism.

### Task 1: The advisor verdict carries a category, step ids and a bounded note

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (`AdvisorCheckpointVerdict`,
  `_parse_advisor_checkpoint_guidance`, the END prompt instruction, new
  module constants)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py`

**Interfaces:**
- Produces:
  ```python
  ADVISOR_NOTE_MAX_CHARS: Final[int] = 600
  ADVISOR_FINDING_CATEGORIES: Final[frozenset[str]] = frozenset({
      "request_not_met", "error_handling", "prompt_defect", "schema_mismatch", "other",
  })
  @dataclass(frozen=True, slots=True)
  class AdvisorCheckpointVerdict:
      ...existing fields...
      category: str = "other"                 # already normalised to the closed set
      affected_step_ids: tuple[str, ...] = () # RAW ids as the advisor wrote them; validated in Task 3
      note: str | None = None                 # bounded, sanitised prose; None when nothing to say
  def _advisor_note_text(findings_text: str) -> str | None
  ```
  `note` is `None` for CLEAN verdicts, for `ok=False` (unavailable /
  malformed: the "findings" are fixed backend copy), and when the prose
  after the verdict token is empty after sanitising.

- [ ] **Step 1: Write the failing tests**

```python
def test_flagged_verdict_parses_category_steps_and_note() -> None:
    from elspeth.web.composer.service import _parse_advisor_checkpoint_guidance

    verdict = _parse_advisor_checkpoint_guidance(
        "FLAGGED: the request asked for error capture at the merge; both branches now discard.\n"
        "CATEGORY: request_not_met\n"
        "STEPS: merge_ab, eval_a\n"
    )
    assert verdict.blocking is True
    assert verdict.category == "request_not_met"
    assert verdict.affected_step_ids == ("merge_ab", "eval_a")
    assert verdict.note == "the request asked for error capture at the merge; both branches now discard."


def test_missing_machine_lines_fall_back_to_other_and_no_steps() -> None:
    from elspeth.web.composer.service import _parse_advisor_checkpoint_guidance

    verdict = _parse_advisor_checkpoint_guidance("FLAGGED — sink omits the rating column.")
    assert verdict.category == "other"
    assert verdict.affected_step_ids == ()
    assert verdict.note == "sink omits the rating column."


def test_unknown_category_normalises_to_other() -> None:
    from elspeth.web.composer.service import _parse_advisor_checkpoint_guidance

    verdict = _parse_advisor_checkpoint_guidance("FLAGGED: x\nCATEGORY: vibes\nSTEPS: none")
    assert verdict.category == "other"
    assert verdict.affected_step_ids == ()


def test_note_is_bounded_and_sanitised() -> None:
    from elspeth.web.composer.service import (
        _ADVISOR_FINDINGS_UNTRUSTED_BEGIN,
        _ADVISOR_FINDINGS_UNTRUSTED_END,
        ADVISOR_NOTE_MAX_CHARS,
        _advisor_note_text,
    )

    long = "a" * (ADVISOR_NOTE_MAX_CHARS + 50)
    note = _advisor_note_text(f"FLAGGED: {long}")
    assert note is not None
    assert len(note) == ADVISOR_NOTE_MAX_CHARS
    assert note.endswith("…")
    dirty = f"FLAGGED: keep\x00this {_ADVISOR_FINDINGS_UNTRUSTED_BEGIN} and {_ADVISOR_FINDINGS_UNTRUSTED_END}\x1b[31m"
    assert _advisor_note_text(dirty) == "keepthis  and"
    assert _advisor_note_text("FLAGGED:") is None
    assert _advisor_note_text("CLEAN") is None


def test_clean_and_unrendered_verdicts_carry_no_note() -> None:
    from elspeth.web.composer.service import _parse_advisor_checkpoint_guidance

    assert _parse_advisor_checkpoint_guidance("CLEAN").note is None
    malformed = _parse_advisor_checkpoint_guidance("I am not sure")
    assert malformed.ok is False or malformed.blocking is False
    assert malformed.note is None
```

- [ ] **Step 2: Run; expect 5 failures** (`ImportError`/`AttributeError` on
  the new names, then `AttributeError: category`).

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest -n 0 \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py \
  -k "parses_category_steps_and_note or fall_back_to_other or normalises_to_other or bounded_and_sanitised or carry_no_note" -v
```

- [ ] **Step 3: Implement**

Add beside `_ADVISOR_FINDINGS_MAX_CHARS`:

```python
# elspeth-032ec69c41 (ruling 2026-09-22, "store, bounded"): the advisor's own
# words reach the user as a labelled note. Bounded here, once, before any
# surface or row sees them.
ADVISOR_NOTE_MAX_CHARS: Final[int] = 600
ADVISOR_FINDING_CATEGORIES: Final[frozenset[str]] = frozenset(
    {"request_not_met", "error_handling", "prompt_defect", "schema_mismatch", "other"}
)
_ADVISOR_CATEGORY_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*CATEGORY\s*:\s*([a-z_]+)\s*$", re.IGNORECASE | re.MULTILINE)
_ADVISOR_STEPS_LINE_RE: Final[re.Pattern[str]] = re.compile(r"^\s*STEPS\s*:\s*(.*?)\s*$", re.IGNORECASE | re.MULTILINE)
_ADVISOR_STEP_ID_RE: Final[re.Pattern[str]] = re.compile(r"[A-Za-z0-9_.\-]+")
_ADVISOR_NOTE_CONTROL_RE: Final[re.Pattern[str]] = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]|\x1b\[[0-9;]*[A-Za-z]")


def _advisor_note_text(findings_text: str) -> str | None:
    """The advisor's prose after the verdict token, bounded and sanitised, or None."""
    body = _ADVISOR_VERDICT_LEAD_RE.sub("", findings_text, count=1)  # existing anchored CLEAN/FLAGGED lead
    body = _ADVISOR_CATEGORY_LINE_RE.sub("", body)
    body = _ADVISOR_STEPS_LINE_RE.sub("", body)
    body = body.replace(_ADVISOR_FINDINGS_UNTRUSTED_BEGIN, "").replace(_ADVISOR_FINDINGS_UNTRUSTED_END, "")
    body = _ADVISOR_NOTE_CONTROL_RE.sub("", body).strip()
    if not body:
        return None
    if len(body) > ADVISOR_NOTE_MAX_CHARS:
        body = body[: ADVISOR_NOTE_MAX_CHARS - 1].rstrip() + "…"
    return body
```

(`_ADVISOR_VERDICT_LEAD_RE` is the existing anchored pattern at the
`r"^(CLEAN|FLAGGED)\s*(?:[:.\-–—]|$)"` anchor — reuse its name; if
it has a different name, use that one and say so in the ledger.)

In `_parse_advisor_checkpoint_guidance`, after the verdict is classified,
populate the three fields ONLY on the FLAGGED (`ok=True, blocking=True`)
arm:

```python
    category_match = _ADVISOR_CATEGORY_LINE_RE.search(guidance)
    category = category_match.group(1).lower() if category_match else "other"
    if category not in ADVISOR_FINDING_CATEGORIES:
        category = "other"
    steps_match = _ADVISOR_STEPS_LINE_RE.search(guidance)
    raw_steps = steps_match.group(1) if steps_match else ""
    affected = tuple(s for s in _ADVISOR_STEP_ID_RE.findall(raw_steps) if s.lower() != "none")
    return AdvisorCheckpointVerdict(
        ok=True, blocking=True, findings_text=..., category=category,
        affected_step_ids=affected, note=_advisor_note_text(guidance),
    )
```

CLEAN and the `ok=False` arms keep the defaults.

Append to the END prompt instruction string (the one ending
`"Start your reply with CLEAN or FLAGGED."`):

```python
                "Start your reply with CLEAN or FLAGGED. After a FLAGGED verdict, end with two "
                "lines: 'CATEGORY: <request_not_met|error_handling|prompt_defect|schema_mismatch|other>' "
                "and 'STEPS: <comma-separated step ids from the pipeline excerpt, or none>'. "
                "Your FLAGGED prose is shown to the user as your note: write it for them, "
                "name the step and the option, and do not quote user text or row data."
```

- [ ] **Step 4: Run the five tests; expect PASS.** Then the parser's existing
  pins:

```bash
cd "$W" && PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest \
  -o pythonpath="$W/src $W/elspeth-lints/src" tests/unit/web/composer/test_advisor_checkpoint.py -q
```

A red in a prompt-hash / advisor-prompt snapshot pin means the instruction
string feeds a recorded hash: regenerate per that gate's command in
`CONTRIBUTING.md` and confirm only hashes moved.

- [ ] **Step 5: Commit** — `feat(composer): parse the advisor's category, step ids and bounded note`.

### Task 2: The wire blocker carries `note`

**Files:**
- Modify: `src/elspeth/web/execution/schemas.py` (`ValidationReadinessBlocker`)
- Modify: every `ValidationReadinessBlocker(` constructor in `src/elspeth`
  (12 at `0bd0c5e40`: `completion_gates.py` `_reconcile_advisor_blocker`,
  the composer builders in `service.py`, `execution/validation.py`,
  `execution/service.py`, …) — pass `note=None` everywhere in this task.
- Modify: `src/elspeth/web/frontend/src/types/index.ts`
- Test: `tests/unit/web/execution/test_schemas.py` (or the module that pins
  `ValidationReadinessBlocker`'s field set — find it with
  `grep -rln 'ValidationReadinessBlocker' tests/unit/web/execution`).

**Interfaces:**
- Produces: `ValidationReadinessBlocker(code, component_id, component_type, detail, suggestion, note: str | None)`;
  TS `note: string | null`.

- [ ] **Step 1: Failing test**

```python
def test_readiness_blocker_requires_an_explicit_note() -> None:
    import pytest
    from pydantic import ValidationError
    from elspeth.web.execution.schemas import ValidationReadinessBlocker

    with pytest.raises(ValidationError):
        ValidationReadinessBlocker(code="x", component_id=None, component_type=None, detail="d", suggestion=None)
    blocker = ValidationReadinessBlocker(code="x", component_id=None, component_type=None, detail="d", suggestion=None, note=None)
    assert blocker.model_dump()["note"] is None
```

- [ ] **Step 2: Run; expect FAIL** (no `ValidationError` raised — the field
  does not exist yet, so the first construction succeeds).

- [ ] **Step 3: Implement.** Add to the pydantic model, after `suggestion`:

```python
    # elspeth-032ec69c41: the advisor's own words, bounded and labelled, on the
    # ``advisor_signoff_blocked`` row only. Every other blocker sets None.
    # REQUIRED (no default) so a builder cannot forget the decision.
    note: str | None
```

Set `note=None` at every constructor site in `src/elspeth` (Task 3 sets the
real value at the advisor sites). Add `note: string | null;` to the TS
interface. Fix every test constructor (35) with `note=None`.

- [ ] **Step 4: Run the wire-shape and schema gates**

```bash
cd "$W" && L=/tmp/reviewer-note-task2-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web tests/unit/contracts \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/reviewer-note-baseline-reds.txt
```

Expected new reds: `TypeError`/`ValidationError` at constructor sites you
missed (fix them), and any wire-shape template or OpenAPI/schema snapshot
that pins the blocker's field set (regenerate per `CONTRIBUTING.md`; only the
new field may appear in the diff).

- [ ] **Step 5:** Frontend type check and tests:

```bash
cd "$W/src/elspeth/web/frontend" && npm run typecheck > /tmp/reviewer-note-tsc-$$.log 2>&1; echo "exit=$?"; \
npx vitest run > /tmp/reviewer-note-vitest-$$.log 2>&1; echo "exit=$?"; tail -5 /tmp/reviewer-note-vitest-$$.log
```

Fixture objects typed as `ValidationReadinessBlocker` need `note: null`.

- [ ] **Step 6: Commit** — `feat(web): readiness blockers carry an explicit note field`.

### Task 3: The blocked result writes the validated header and the note

**Files:**
- Modify: `src/elspeth/web/composer/service.py` (`_advisor_signoff_blocked_wording`,
  the four `_advisor_signoff_*_validation` builders,
  `_advisor_signoff_fully_blocking_validation`, `_advisor_blocked_result`)
- Test: `tests/unit/web/composer/test_advisor_checkpoint.py`

**Interfaces:**
- Consumes: `AdvisorCheckpointVerdict.category / affected_step_ids / note` (Task 1);
  `ValidationReadinessBlocker.note` (Task 2).
- Produces:
  ```python
  def _validated_advisor_step_ids(state: CompositionState, raw: Sequence[str]) -> tuple[str, ...]
  _ADVISOR_CATEGORY_HEADERS: Final[Mapping[str, str]]  # closed category -> fixed sentence
  def _advisor_signoff_blocked_wording(*, reason, findings, findings_backend_authored=False, notice=..., category="other", step_ids=()) -> tuple[str, str]
  ```
  Each `_advisor_signoff_*_validation` builder gains `category: str`,
  `step_ids: Sequence[str]`, `note: str | None` keyword-only parameters
  (REQUIRED, no defaults) and sets them on the blocker.

- [ ] **Step 1: Failing tests**

```python
def _flagged(note: str, *, category: str = "request_not_met", steps: tuple[str, ...] = ()) -> AdvisorCheckpointVerdict:
    return AdvisorCheckpointVerdict(
        ok=True, blocking=True, findings_text=f"FLAGGED: {note}", category=category, affected_step_ids=steps, note=note
    )


def _advisor_blocker(result) -> ValidationReadinessBlocker:
    # ``ADVISOR_SIGNOFF_BLOCKED_CODE`` and ``ValidationReadinessBlocker`` come
    # from ``elspeth.web.execution.schemas``; add them to the module imports.
    (blocker,) = [b for b in result.runtime_preflight.readiness.blockers if b.code == ADVISOR_SIGNOFF_BLOCKED_CODE]
    return blocker


def test_blocked_result_carries_header_and_note(make_service, clean_runnable_state) -> None:
    """Ruling 2026-09-22: header (backend copy) + the advisor's words as a labelled note."""
    step = clean_runnable_state.nodes[0].id
    result = make_service()._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=_flagged("The merge cannot capture a failed branch; choose per-branch sinks or best_effort.", steps=(step,)),
        state=clean_runnable_state,
        assistant_message=None,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=_green_preflight(),
        outstanding_findings=None,
    )
    blocker = _advisor_blocker(result)
    assert blocker.note == "The merge cannot capture a failed branch; choose per-branch sinks or best_effort."
    assert "The reviewer found the request not fully met" in blocker.detail
    assert f"Steps named by the reviewer: {step}." in blocker.detail
    # R2-F13 narrowed: the words live in note only.
    assert "per-branch sinks" not in blocker.detail
    assert "per-branch sinks" not in (blocker.suggestion or "")
    assert all("per-branch sinks" not in c.detail for c in result.runtime_preflight.checks)
    assert "per-branch sinks" not in result.message


def test_unknown_step_ids_are_dropped_from_the_header(make_service, clean_runnable_state) -> None:
    step = clean_runnable_state.nodes[0].id
    result = make_service()._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=_flagged("x", steps=("ghost_step", step, "DROP TABLE")),
        state=clean_runnable_state,
        assistant_message=None,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=_green_preflight(),
        outstanding_findings=None,
    )
    detail = _advisor_blocker(result).detail
    assert f"Steps named by the reviewer: {step}." in detail
    assert "ghost_step" not in detail and "DROP TABLE" not in detail


def test_no_valid_step_ids_means_no_step_sentence(make_service, clean_runnable_state) -> None:
    result = make_service()._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=_flagged("x", category="other", steps=("ghost",)),
        state=clean_runnable_state,
        assistant_message=None,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=_green_preflight(),
        outstanding_findings=None,
    )
    detail = _advisor_blocker(result).detail
    assert "The reviewer flagged this pipeline" in detail
    assert "Steps named by the reviewer" not in detail


def test_backend_authored_prescan_block_has_no_note(make_service, simple_state) -> None:
    prescan = "FLAGGED: node 'n1' option columns contains advisor-instruction injection text; remove it before the completion advisory review."
    result = make_service()._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=AdvisorCheckpointVerdict(ok=True, blocking=True, findings_text=prescan, findings_backend_authored=True),
        state=simple_state,
        assistant_message=None,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=_green_preflight(),
        outstanding_findings=None,
    )
    blocker = _advisor_blocker(result)
    assert blocker.note is None
    assert prescan in blocker.detail


def _red_preflight() -> ValidationResult:
    return ValidationResult(
        is_valid=False,
        checks=[],
        errors=[
            ValidationError(
                component_id="rate",
                component_type="transform",
                message="node 'rate' requires field 'url' which no upstream emits",
                suggestion=None,
                error_code=None,
            )
        ],
        readiness=ValidationReadiness(authoring_valid=False, execution_ready=False, completion_ready=False, blockers=[]),
    )


@pytest.mark.parametrize(
    "runtime_preflight",
    [_green_preflight(), _red_preflight(), None, _pending_handoff_preflight()],
    ids=["green", "red", "absent", "handoff"],
)
def test_note_rides_every_preflight_shape(make_service, clean_runnable_state, runtime_preflight) -> None:
    result = make_service()._advisor_blocked_result(
        reason="flagged_final_pass",
        verdict=_flagged("x"),
        state=clean_runnable_state,
        assistant_message=None,
        recorder=make_recorder(),
        repair_turns_used=0,
        persisted_assistant_message_id=None,
        persisted_assistant_content=None,
        persisted_tool_call_turn=False,
        runtime_preflight=runtime_preflight,
        outstanding_findings=None,
    )
    assert _advisor_blocker(result).note == "x"
```

`_pending_handoff_preflight()` is the pending-interpretation-handoff
`ValidationResult` that `test_end_gate_preserves_pending_handoff_shape`
builds inline (a readiness block whose only blocker code is
`INTERPRETATION_REVIEW_PENDING_CODE`, `authoring_valid=True`,
`completion_ready=True`); lift it into a module-level helper of that name in
the same commit so both tests share it.

- [ ] **Step 2: Run; expect failures** on `blocker.note` (None) and on the
  header sentence.

- [ ] **Step 3: Implement**

```python
_ADVISOR_CATEGORY_HEADERS: Final[Mapping[str, str]] = MappingProxyType(
    {
        "request_not_met": "The reviewer found the request not fully met.",
        "error_handling": "The reviewer flagged how failures are handled.",
        "prompt_defect": "The reviewer flagged a prompt.",
        "schema_mismatch": "The reviewer flagged a field or schema mismatch.",
        "other": "The reviewer flagged this pipeline.",
    }
)


def _validated_advisor_step_ids(state: CompositionState, raw: Sequence[str]) -> tuple[str, ...]:
    """Keep only ids the state actually has, in the advisor's order, de-duplicated."""
    known = {s.id for s in state.sources} | {n.id for n in state.nodes} | {o.name for o in state.outputs}
    seen: list[str] = []
    for candidate in raw:
        if candidate in known and candidate not in seen:
            seen.append(candidate)
    return tuple(seen)
```

(Confirm the attribute names — `SourceSpec.id`, `NodeSpec.id`,
`OutputSpec.name` — against `composer/state.py` before writing.)

In `_advisor_signoff_blocked_wording`, when `findings_backend_authored` is
False and `reason` is a FLAGGED reason, prefix `detail` with
`_ADVISOR_CATEGORY_HEADERS[category]` and, when `step_ids` is non-empty,
append `f" Steps named by the reviewer: {', '.join(step_ids)}."` — both are
backend copy; the ids passed here are already validated. Thread
`category=`, `step_ids=`, `note=` through the four builders to the blocker
(`note=note` on the advisor blocker; `note=None` if
`findings_backend_authored`). In `_advisor_blocked_result`, compute once:

```python
        step_ids = _validated_advisor_step_ids(state, verdict.affected_step_ids)
        note = None if verdict.findings_backend_authored else verdict.note
```

and pass `category=verdict.category, step_ids=step_ids, note=note` to
whichever builder the shape selects.

- [ ] **Step 4: Run the tests; then re-seat the R2-F13 pins**

```bash
cd "$W" && L=/tmp/reviewer-note-task3-$$.log; \
PYTHONPATH="$W/src:$W/elspeth-lints/src" .venv/bin/python -m pytest tests/unit/web/composer \
  -o pythonpath="$W/src $W/elspeth-lints/src" > "$L" 2>&1; echo "exit=$?"; \
grep -E "^(FAILED|ERROR)" "$L" | sort | diff - /tmp/reviewer-note-baseline-reds.txt
```

Expected reds and the rule for each:

| Red | Rule |
|---|---|
| asserts a canary is absent from `runtime_preflight.model_dump_json()` or from a blob that includes the blockers (`test_end_gate_final_flag_never_exposes_advisor_findings_on_human_surfaces`, `test_advisor_blocked_result_publishes_an_echoing_reply_and_keeps_backend_surfaces_clean`) | narrow the surface list: the canary MAY appear in `blockers[].note` of the advisor row and nowhere else; name the ruling |
| `_advisor_signoff_blocked_wording` canary pin (`MODEL_FINDINGS_CANARY not in model_detail`) | keep — detail still never carries the words; add `category`/`step_ids` kwargs if the call fails |
| a `TypeError` for the new required builder kwargs | pass them |

Do not weaken any pin about `detail`, `suggestion`, check details or the
chat message.

- [ ] **Step 5: Commit** — `feat(composer): advisor block publishes a validated header and the reviewer's note`.

### Task 4: The note persists in the gate fact and reaches `/validate`

**Files:**
- Modify: `src/elspeth/web/execution/completion_gates.py`
- Test: `tests/unit/web/execution/test_completion_gates.py`,
  `tests/unit/web/sessions/test_routes.py` (the reload/`/validate` seam,
  beside `test_advisor_suggestion_survives_reload_and_clears_on_graph_change`)

**Interfaces:**
- Produces: `AdvisorSignoffGateDict.note: str | None` (REQUIRED key),
  `AdvisorSignoffGateFact.note: str | None`; `parse_completion_gates` raises
  when `note` is absent; `_reconcile_advisor_blocker(..., note: str | None)`;
  `merge_completion_gates` sets `note=fact.note` when `for_graph` matches
  and `note=None` under the pending wording.

- [ ] **Step 1: Failing tests**

In `tests/unit/web/execution/test_completion_gates.py`, which already has
`_make_state(...)`, `_green_result()`, `_blocked_facts_for(state)` and
`test_advisor_suggestion_survives_reload_and_clears_on_graph_change`
(plan 1); give `_blocked_facts_for` a `note: str | None = None` keyword that
it forwards to `AdvisorSignoffGateFact(note=...)`:

```python
def test_note_survives_reload_and_reaches_validate() -> None:
    """Ruling 2026-09-22: the note the blocking turn showed is the note /validate shows on the same graph."""
    from elspeth.web.composer.service import _advisor_signoff_pending_validation

    state = _make_state()
    result = _advisor_signoff_pending_validation(
        _green_result(),
        reason="flagged_final_pass",
        findings="FLAGGED: choose per-branch sinks",
        category="error_handling",
        step_ids=(),
        note="choose per-branch sinks",
    )
    assert result.readiness.blockers[0].note == "choose per-branch sinks"
    facts = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_value(result, state)})
    assert facts is not None and facts.advisor_signoff is not None
    assert facts.advisor_signoff.note == "choose per-branch sinks"
    reloaded = merge_completion_gates(_green_result(), facts, state)
    assert reloaded.readiness.blockers[0].note == "choose per-branch sinks"
    # The carry-forward writer keeps it too.
    carried = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_from_facts(facts)})
    assert carried is not None and carried.advisor_signoff is not None
    assert carried.advisor_signoff.note == "choose per-branch sinks"
    # A changed graph gets the pending wording and no note.
    changed = _make_state(node_options={"operations": [{"target": "y", "expression": "2"}]})
    assert merge_completion_gates(_green_result(), facts, changed).readiness.blockers[0].note is None


def test_gate_fact_without_note_key_is_rejected() -> None:
    legacy = {
        COMPLETION_GATES_META_KEY: {
            "advisor_signoff": {"status": "blocked", "detail": "d", "suggestion": None, "for_graph": "f"},
        }
    }
    with pytest.raises(ValueError, match="note is required"):
        parse_completion_gates(legacy)


def test_gate_fact_note_null_round_trips() -> None:
    state = _make_state()
    facts = _blocked_facts_for(state, note=None)
    parsed = parse_completion_gates({COMPLETION_GATES_META_KEY: completion_gates_meta_from_facts(facts)})
    assert parsed is not None and parsed.advisor_signoff is not None
    assert parsed.advisor_signoff.note is None
    assert merge_completion_gates(_green_result(), parsed, state).readiness.blockers[0].note is None
```

The route-level `/validate` seam (`tests/unit/web/sessions/test_routes.py`)
already proves that the endpoint merges the durable fact
(plan 1's `test_send_message_hands_the_prior_rows_advisor_block_to_the_composer`
seeds a row with `_save_test_composition_state`); extend its seeded fact
dict with `"note": "N"` and add one assertion on the `/validate` response:
`response.json()["readiness"]["blockers"][0]["note"] == "N"`.

- [ ] **Step 2: Run; expect FAIL** (`note` unknown on the dict / dataclass;
  the legacy row parses).

- [ ] **Step 3: Implement.** Add `note: str | None` to
  `AdvisorSignoffGateDict` and `AdvisorSignoffGateFact`; both writers emit it
  (`blocked[0].note` / `facts.advisor_signoff.note`); the parser adds, after
  the `suggestion` probe:

```python
    if "note" not in raw_signoff:
        raise ValueError("Tier 1: completion_gates.advisor_signoff.note is required")
    note = raw_signoff["note"]
    if note is not None and (type(note) is not str or not note):
        raise ValueError("Tier 1: completion_gates.advisor_signoff.note must be a non-empty string or null")
```

`_reconcile_advisor_blocker` gains `note: str | None` and sets it;
`merge_completion_gates` passes `fact.note` on a matching fingerprint and
`None` on the pending arm. Update the `ADVISOR_SIGNOFF_PENDING_DETAIL`
docstring/comment if it describes the fact's fields.

- [ ] **Step 4: Run** `tests/unit/web/execution tests/unit/web/sessions tests/unit/web/composer`
  as in Task 3 Step 4; diff against the baseline. Expected: pins that build a
  gate fact dict by hand need `"note": None`.

- [ ] **Step 5: Commit** — `feat(web): persist the reviewer's note in the advisor gate fact`.

### Task 5: Session schema epoch bump (no old rows)

**Files:**
- Modify: `src/elspeth/web/sessions/models.py` (`SESSION_SCHEMA_EPOCH = 64` → `65`)
- Modify: every literal that pins the epoch. MEASURE the set at execution
  time; do not work from memory:

```bash
cd "$W" && grep -rn '\b64\b' src/elspeth/web/sessions/models.py src/elspeth/web/sessions/schema.py \
  tests/unit/web/sessions tests/unit/contracts tests/integration/web docs website 2>/dev/null \
  | grep -i 'epoch\|user_version\|schema' | grep -v '\.venv'
```

Known at 2026-09-21: `_COORDINATION_HARD_CUT_EPOCH` is coupled, ~8
tripwire tests (`test_schema.py`, `test_schema9_epoch.py`,
`test_web_blob_fencing.py`, `test_proposal_blob_effect_receipts_schema.py`,
`test_interpretation_events_table.py`, `test_blob_inline_resolutions_schema.py`,
…) and ~10 docs including `website/get-started.html` (missed once before).

- [ ] **Step 1: Failing test.** The existing tripwire tests ARE the failing
  tests: bump the constant first, run `tests/unit/web/sessions/test_schema.py`,
  read each red, update each literal with the reason in its comment
  ("epoch 65: `completion_gates.advisor_signoff.note` became a required key,
  elspeth-032ec69c41").
- [ ] **Step 2:** Add a one-line entry wherever the epoch history is kept
  (find it: `grep -rn 'epoch 64\|EPOCH 64\|= 64' docs | head`).
- [ ] **Step 3:** Run `tests/unit/web/sessions tests/unit/contracts tests/integration/web -q`
  to a file; diff against the baseline. Expected: only epoch pins moved.
- [ ] **Step 4:** Note for the hand-back: the deployed session DB will refuse
  to open (`SessionSchemaError`) until renamed; local accounts then need the
  bootstrap-admin runbook (`elspeth composer users bootstrap-admin local <account> --note ...`).
- [ ] **Step 5: Commit** — `chore(sessions): schema epoch 65 for the required advisor note key`.

### Task 6: The decision panel renders the note

**Files:**
- Modify: `src/elspeth/web/frontend/src/components/chat/DecisionPanel.tsx`
- Modify: the panel's stylesheet (find it: `grep -rn 'decision-panel-item-text' src/elspeth/web/frontend/src --include=*.css`)
- Test: `src/elspeth/web/frontend/src/components/chat/DecisionPanel.test.tsx`

**Interfaces:**
- Consumes: `ValidationReadinessBlocker.note` (Task 2) on the blocker rows
  the panel already lists.

- [ ] **Step 1: Failing tests** (vitest + testing-library, matching the
  file's existing style):

```tsx
it("renders the reviewer's note under an advisor blocker as plain text", () => {
  render(<DecisionPanel {...baseProps} blockers={[advisorBlocker({ note: "Choose per-branch sinks **or** best_effort." })]} />);
  const note = screen.getByTestId("decision-panel-reviewer-note");
  expect(note).toHaveTextContent("Reviewer's note");
  expect(note).toHaveTextContent("Choose per-branch sinks **or** best_effort.");
  expect(note.querySelector("strong")).toBeNull();          // never markdown
  expect(note.querySelector("a")).toBeNull();
});

it("renders no note block when note is null", () => {
  render(<DecisionPanel {...baseProps} blockers={[advisorBlocker({ note: null })]} />);
  expect(screen.queryByTestId("decision-panel-reviewer-note")).toBeNull();
});

it("does not put the note into the ask-the-composer draft", () => {
  const onAsk = vi.fn();
  render(<DecisionPanel {...baseProps} onAskAboutBlocker={onAsk} blockers={[advisorBlocker({ note: "SECRET_NOTE" })]} />);
  fireEvent.click(screen.getByRole("button", { name: /Ask the composer about this/ }));
  expect(onAsk).toHaveBeenCalledTimes(1);
  expect(JSON.stringify(onAsk.mock.calls[0])).not.toContain("SECRET_NOTE");
});
```

- [ ] **Step 2: Run; expect FAIL** (`getByTestId` finds nothing).

- [ ] **Step 3: Implement.** Under the existing
  `<span className="decision-panel-item-text">{row.detail}…</span>` for a
  blocker row, when `row.note !== null`:

```tsx
{row.kind === "blocker" && row.note !== null && (
  <div className="decision-panel-reviewer-note" data-testid="decision-panel-reviewer-note">
    <span className="decision-panel-reviewer-note-label">Reviewer's note (the advisor's own words — not verified by ELSPETH):</span>
    <p className="decision-panel-reviewer-note-text">{row.note}</p>
  </div>
)}
```

Thread `note` into the row model where `detail`/`suggestion` are copied
from the blocker. CSS: `.decision-panel-reviewer-note-text { white-space: pre-wrap; overflow-wrap: anywhere; }`
plus a muted label; no `dangerouslySetInnerHTML`, no markdown renderer.

- [ ] **Step 4:** `npm run typecheck`, `npx vitest run`, `npm run lint` to
  files; read exit codes.
- [ ] **Step 5: Commit** — `feat(frontend): show the reviewer's note under the advisor blocker`.

### Task 7: Gates, record, hand back

- [ ] **Step 1:** ruff, `ruff format --check`, `mypy src/elspeth/web`, two-root `PYTHONPATH`.
- [ ] **Step 2:** Trust-tier corpus vs the base commit, position-stripped diff.
- [ ] **Step 3:** Full-suite gate: `scripts/full-suite-gate.sh --execute --detach --root "$W" --stages ruff,mypy,contracts,lints,pytest`
  — and because the session schema epoch moved, ALSO
  `pytest tests/ -m testcontainer -n 0` (PostgreSQL identity/epoch probes).
  Read `summary.txt`; `frozen=NO` is not evidence.
- [ ] **Step 4:** Frontend build (`npm run build`) so the deployed bundle
  name changes; note the new `index-*.js` in the hand-back.
- [ ] **Step 5:** Dated line in `docs/agents/recent-code-hints.md` (R2-F13
  narrowed to `note`; epoch 65; the decision-panel note is text-only).
- [ ] **Step 6: Hand back.** Branch, commits, both gate run dirs, the
  `ValidationReadinessBlocker` constructor count you actually touched, the
  epoch-pin set you actually moved, and the deploy note (DB rename +
  bootstrap-admin). Do not merge.
