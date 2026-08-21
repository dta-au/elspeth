# Unified Lineage WS0 — Docstring / Invariant Corrections Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Land spec §9 row 0 — delete the false "THE single seam" docstring and the
"at most one arm yields results" claim on `_notify_barrier_of_lost_branch`, land truth
comments at the verified bypass sites, land the trap comment on `WorkItem`, and fix the
stale ADR-038 test citation — all standalone-value corrections that make the tree stop
lying to the WS1/WS3 implementers.

**Architecture:** Documentation-truth changes only: zero behaviour change, zero schema
change, zero new dynamic-attribute sites. One small guard test pins the corrected
docstring BY ITS CLAIMS (the false claims absent, the verified bypass classes named) so
the claims cannot silently regress while WS3 is still pending; that test is deleted with
the method when WS3 lands the settle-member seam.

**Tech Stack:** Python 3.12+, pytest (repo venv at `.venv/`), git on the shared
`release/0.7.2` checkout.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md
(rev 3.2 — rulings 1–28 final). The *why* record:
docs/superpowers/specs/2026-08-21-barrier-scope-proposal.md (blocker analysis; its line
"Rewrite the docstring in the same commit. Leave the WorkItem check alone … but flag it
as a trap" is the WS0 charter).

## Global Constraints

- Shared checkout, stage-by-pathspec-only discipline: `git add <explicit file paths>` —
  never `git add -A`, `-u`, or a directory. A sibling agent's files may be staged or
  modified at any moment; commit only YOUR hunks.
- Never bypass hooks, except the documented `--no-verify`-with-end-of-slice-reconciliation
  grant; `git stash` is blocked by hook — do not attempt it.
- Full `pytest tests/` at slice boundaries (this whole plan is one slice), because
  whole-tree AST gates (attribute-contracts, masquerade, serialisation pins,
  runtime-rejection parity) miss scoped runs. Record `git rev-parse HEAD` before and
  after the run; if HEAD moved, a red result is uninterpretable — re-run.
- Trust-tier corpus diff before/after the slice: run the gate, COUNT findings (never
  `tail` them), diff sorted finding identities; add NOTHING to the corpus. Command and
  baseline procedure: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S2.
- Wardline gate (verbatim from AGENTS.md):
  `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
- No hand-edited judge signatures, ever; no judge-bundle staging across the campaign
  (protocols plan §S4).
- Depth cap and fixpoint bound (campaign-wide rules, restated for every plan): the
  supported guarantee is 5 layers of bound-region nesting, builder-enforced fail-closed,
  config-overridable; the escalation fixpoint's non-convergence bound is derived at
  build from the actual depth (+ margin), never a constant.
- Do NOT edit or stage `src/elspeth/web/composer/state.py` or
  `tests/unit/web/composer/test_state.py` — the maintainer is committing them.
- Read docs/agents/recent-code-hints.md before writing code (whole-tree traps).
- Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md
  §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle
  sequencing, and the WS1 STOP rule.

---

### Task 1: Correct the false loss-seam docstring, pinned by its claims

**Files:**
- Test: `tests/unit/engine/test_barrier_loss_seam_docstring.py` (create)
- Modify: `src/elspeth/engine/processor.py:3129-3141` (the docstring of
  `_notify_barrier_of_lost_branch`, whose `def` is at `:3121`)

**Interfaces:**
- Consumes: `elspeth.engine.processor.RowProcessor._notify_barrier_of_lost_branch`
  (existing method; signature untouched:
  `(self, current_token: TokenInfo, reason: str, child_items: list[WorkItem], *, notify_in_memory: bool = True) -> list[RowResult]`).
- Produces: a docstring whose claims the WS3 plan may rely on verbatim (the three
  verified bypass classes), and the guard test module — **the WS3 plan deletes BOTH the
  method and `tests/unit/engine/test_barrier_loss_seam_docstring.py` in the same commit**
  when the settle-member seam replaces them (spec §6.1).

Background (verified 2026-08-21, recorded in the proposal and re-verified against the
live tree for this plan): the docstring claims "THE single seam every early-exit path
calls" and "at most one arm yields results". Both are false. Terminal dispositions that
never reach the method: (1) `(TRANSIENT, BATCH_CONSUMED)` for every non-representative
buffered token of a transform-mode aggregation flush — recorded atomically inside
`expand_token` by `_route_transform_results` (`processor.py:1623`, emission loop
`:1642-1652`) — the NORMAL SUCCESS path; (2) `(FAILURE, QUARANTINED_AT_SOURCE)` inside a
NON-EMPTY flush (same loop), while the EMPTY-emission path
(`_route_empty_emission_results`, notify calls at `:1442-1449`) does notify;
(3) held-sibling failures the CoalesceExecutor writes directly via
`record_token_outcome(FAILURE, UNROUTED)` at `engine/coalesce_executor.py:841`, `:1054`,
`:1254`. And a token can be a fork branch AND an expand child simultaneously, so the
one-arm claim is wrong as stated (the real exclusivity is coalesce-vs-row_union for the
single FORK identity, enforced by the pairwise checks at `processor.py:488-499`).

- [ ] **Step 1: Write the failing guard test**

Create `tests/unit/engine/test_barrier_loss_seam_docstring.py`:

```python
"""Pin the corrected _notify_barrier_of_lost_branch docstring BY ITS CLAIMS.

The old docstring called the method "THE single seam every early-exit path
calls" and claimed "at most one arm yields results" — both false (verified
2026-08-21; see docs/superpowers/specs/2026-08-21-barrier-scope-proposal.md)
and both trusted by design work. Pin the truth so the claims cannot silently
regress while WS3 is pending.

DELETE THIS MODULE with the method itself when WS3 lands the unified
settle-member seam (spec §6.1) — it pins a docstring that dies with its code.
"""

from elspeth.engine.processor import RowProcessor


def test_loss_seam_docstring_does_not_claim_universal_coverage() -> None:
    doc = RowProcessor._notify_barrier_of_lost_branch.__doc__ or ""
    lowered = doc.lower()
    assert "the single seam every early-exit path calls" not in lowered
    assert "at most one arm yields results" not in lowered
    assert "not a single seam" in lowered


def test_loss_seam_docstring_names_the_verified_bypass_classes() -> None:
    doc = RowProcessor._notify_barrier_of_lost_branch.__doc__ or ""
    assert "BATCH_CONSUMED" in doc
    assert "QUARANTINED_AT_SOURCE" in doc
    assert "record_token_outcome" in doc
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `.venv/bin/pytest tests/unit/engine/test_barrier_loss_seam_docstring.py -v`
Expected: BOTH tests FAIL — the first because the old docstring contains
"THE single seam every early-exit path calls" and lacks "not a single seam"; the second
because none of the three bypass tokens appear.

- [ ] **Step 3: Replace the docstring**

In `src/elspeth/engine/processor.py`, replace the docstring of
`_notify_barrier_of_lost_branch`. Exact old text (lines 3129-3141):

```python
        """Notify whichever barrier owns this fork branch that it was lost.

        THE single seam every early-exit path calls. Loss is discovered in
        many places (retry exhaustion, filter drop, quarantine, error
        routing, gate routing, gate discard, batch-flush drop), and each one
        used to name the coalesce notifier directly — so adding a second
        barrier kind silently left every one of those paths unnotified and
        its held siblings waiting for the end-of-source sweep. Dispatching
        here means a future barrier kind is wired by editing one method.

        A branch belongs to at most ONE barrier — enforced at build time and
        re-checked in this constructor — so at most one arm yields results.
        """
```

Exact new text:

```python
        """Notify the barrier (if any) bound to this token's fork branch that
        the branch was lost.

        Dispatches to the coalesce arm, then the row_union arm, so an
        early-exit path names no barrier kind directly. It is NOT a single
        seam: verified 2026-08-21, these terminal dispositions never reach
        this method —

        1. (TRANSIENT, BATCH_CONSUMED) for every non-representative buffered
           token of a transform-mode aggregation flush, recorded atomically
           inside expand_token by _route_transform_results. That is the
           NORMAL SUCCESS path: an aggregation inside a fork branch consumes
           branch tokens no coalesce roster ever hears about.
        2. (FAILURE, QUARANTINED_AT_SOURCE) inside a NON-EMPTY aggregation
           flush (same method). The EMPTY-emission path notifies per
           buffered token via _route_empty_emission_results; the non-empty
           path does not.
        3. Held-sibling failures the CoalesceExecutor writes directly via
           record_token_outcome(FAILURE, UNROUTED) — three sites in
           engine/coalesce_executor.py.

        Any design assuming "every terminal disposition notifies the
        barrier" is wrong in the direction that wedges a run. The unified
        settle-member seam retires this method and the bypasses above
        (docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md
        §6.1).

        Arm exclusivity: a fork branch joins at most one of
        coalesce/row_union (pairwise checks in RowProcessor.__init__), so at
        most one arm stages a loss for the FORK identity — but a token can
        simultaneously be a fork branch AND an expand child, so "one group
        per token" is false and an arm keyed on expand membership would
        legitimately co-fire with a fork arm. Do not extend this dispatcher
        on a one-arm assumption.
        """
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `.venv/bin/pytest tests/unit/engine/test_barrier_loss_seam_docstring.py -v`
Expected: 2 passed.

- [ ] **Step 5: Sanity-run the neighbouring engine suites (docstring-only change, nothing may move)**

Run: `.venv/bin/pytest tests/unit/engine/test_processor.py tests/unit/engine/test_coalesce_executor.py -q`
Expected: all pass, no new failures.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/engine/processor.py tests/unit/engine/test_barrier_loss_seam_docstring.py
git commit -m "docs(engine): correct the false single-seam claim on _notify_barrier_of_lost_branch (WS0)"
```

---

### Task 2: Truth comments at the verified bypass sites

**Files:**
- Modify: `src/elspeth/engine/coalesce_executor.py:841`, `:1054`, `:1254` (the three
  direct `record_token_outcome(FAILURE, UNROUTED)` sites — line numbers pre-edit; each
  edit shifts later anchors, apply bottom-up: `:1254` first, then `:1054`, then `:841`)
- Modify: `src/elspeth/engine/processor.py:1640-1641` (the comment block opening the
  emission loop in `_route_transform_results`)

**Interfaces:**
- Consumes: Task 1's docstring wording (the comments reference the same spec section).
- Produces: in-place truth markers WS3 will search for when retiring the bypasses
  (`grep -n "settle-member" src/elspeth/engine/` finds all four sites).

No behaviour change. Do NOT touch `engine/scheduler_drain.py:996-1006` ("one claim loses
at most one branch") — that comment is TRUE today and is rewritten by WS3 itself when the
singular `branch_loss` becomes a per-frame collection.

- [ ] **Step 1: Comment the three CoalesceExecutor direct-write sites**

At each of the three sites, insert this comment line directly above the
`self._data_flow.record_token_outcome(` call:

```python
            # DIRECT terminal write — bypasses RowProcessor._notify_barrier_of_lost_branch:
            # a held sibling fails here with NO branch loss staged for any enclosing
            # barrier. Retired into the WS3 settle-member seam (barrier-scopes spec §6.1 item 1).
```

Site anchors (each `old_string` must include enough context to be unique):
- `:1254` site — the call preceded by the comment `# Record terminal FAILED outcome for
  consumed token.` and the `OrchestrationInvariantError(...) from merge_exc` guard.
- `:1054` site — the call preceded by the guard raise WITHOUT `from merge_exc`, in the
  method whose `duration_ms=(now - entry.arrival_time) * 1000`.
- `:841` site — the call preceded by the guard raise WITHOUT `from merge_exc`, in the
  method whose `duration_ms=0`.

- [ ] **Step 2: Comment the non-empty-flush asymmetry in processor.py**

Exact old text (`processor.py:1640-1641`):

```python
            # Expansion atomically recorded every parent's terminal disposition;
            # emit the matching telemetry and construct the triggering RowResult.
```

Exact new text:

```python
            # Expansion atomically recorded every parent's terminal disposition;
            # emit the matching telemetry and construct the triggering RowResult.
            #
            # ASYMMETRY (verified 2026-08-21): this non-empty flush path notifies NO
            # barrier — the buffered tokens' (TRANSIENT, BATCH_CONSUMED) and
            # (FAILURE, QUARANTINED_AT_SOURCE) terminals were recorded inside
            # expand_token with no _notify_barrier_of_lost_branch call, while the
            # EMPTY-emission path (_route_empty_emission_results) notifies for every
            # buffered token. Inside a fork branch, a coalesce roster is blind to
            # these losses; ruling 25 (barrier-scopes spec §7 rule 6) bans aggregators
            # in bound regions and the WS3 settle-member seam retires this path.
```

- [ ] **Step 3: Verify comment-only (no executable diff) and suites green**

Run: `git diff -U0 src/elspeth/engine/coalesce_executor.py src/elspeth/engine/processor.py | grep '^+' | grep -v '^+++' | grep -v '^\+\s*#'`
Expected: empty output (every added line is a comment).

Run: `.venv/bin/pytest tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_processor.py tests/property/engine/test_coalesce_properties.py -q`
Expected: all pass.

- [ ] **Step 4: Commit**

```bash
git add src/elspeth/engine/coalesce_executor.py src/elspeth/engine/processor.py
git commit -m "docs(engine): mark the verified loss-seam bypass sites for the WS3 settle-member retirement (WS0)"
```

---

### Task 3: Land the trap comment on WorkItem

**Files:**
- Modify: `src/elspeth/engine/work_items.py:25-28` (the `WorkItem` class docstring)

**Interfaces:**
- Consumes: nothing.
- Produces: the trap text the WS2/WS4 plans must obey — the collector's group→closer
  binding lands in the builder's `group_bindings` registry (spec §3), NEVER as a
  `WorkItem` field; a collector cursor pair is added here only if work items actually
  block at the collector (WS4's call).

Per the proposal's ruling: "Leave the WorkItem check alone (the expand binding rides
`TokenInfo`, not the WorkItem), but flag it as a trap for anyone later tempted to
express the scope binding as a WorkItem field." The `__post_init__` exclusivity checks
are untouched.

- [ ] **Step 1: Replace the class docstring**

Exact old text (`work_items.py:25-28`):

```python
@dataclass(frozen=True, slots=True)
class WorkItem:
    """Item in the work queue for DAG processing."""
```

Exact new text:

```python
@dataclass(frozen=True, slots=True)
class WorkItem:
    """Item in the work queue for DAG processing.

    TRAP — do not express group/scope bindings as WorkItem fields. The
    coalesce_*/row_union_* pairs below are the CURSOR ADDRESS of the barrier
    this item is currently travelling to, not a lineage membership: group
    membership rides TokenInfo (fork/expand lineage), and the group→closer
    binding is a build-time property of the graph (the barrier-scopes spec's
    binding registry, spec §3). The mutual-exclusion check in __post_init__
    therefore constrains only the cursor — one item cannot be blocked at two
    barriers — it does NOT mean a token belongs to at most one group: a token
    can be a fork branch AND an expand child at once. A new barrier kind
    (e.g. the collector) gets a cursor pair here only if work items actually
    block at it; its binding lives in the builder registry, never on this
    dataclass.
    """
```

- [ ] **Step 2: Run the WorkItem suite**

Run: `.venv/bin/pytest tests/unit/engine/test_work_items.py -q`
Expected: all pass (docstring-only change).

- [ ] **Step 3: Commit**

```bash
git add src/elspeth/engine/work_items.py
git commit -m "docs(engine): trap comment on WorkItem — cursor address is not group binding (WS0)"
```

---

### Task 4: Fix the stale ADR-038 test citation

**Files:**
- Modify: `docs/architecture/adr/038-non-terminal-abandoned-path.md:18`

**Interfaces:**
- Consumes: nothing.
- Produces: a correct citation for the WS5 plan, which extends exactly this test pair
  with the group-satisfiability third sibling (spec §8).

ADR-038's Context cites the PRE-FIX test name
`test_aggregation_count_flush_violation_strands_tokens_permanently`. The live tests in
`tests/integration/audit/test_contract_violation_token_outcomes.py` are
`test_aggregation_eof_flush_violation_leaves_genuinely_retryable_tokens` (`:255`) and
`test_aggregation_count_flush_violation_abandons_tokens_at_finalization` (`:290`).

- [ ] **Step 1: Replace the stale name**

Exact old text (ADR-038 line 18):

```
`tests/integration/audit/test_contract_violation_token_outcomes.py::test_aggregation_count_flush_violation_strands_tokens_permanently`):
```

Exact new text:

```
`tests/integration/audit/test_contract_violation_token_outcomes.py::test_aggregation_count_flush_violation_abandons_tokens_at_finalization`
— renamed from `..._strands_tokens_permanently` when the abandoned-path fix landed):
```

- [ ] **Step 2: Verify the stale name is gone from the tree's prose (the test file's own
  docstring note at `:24` legitimately records the rename — that one names the NEW name
  and stays)**

Run: `git grep -n "strands_tokens_permanently" -- docs/architecture/ src/`
Expected: zero hits. (The campaign plan files under docs/superpowers/ legitimately
quote the stale name when describing this very fix — do not widen the grep to them.)

- [ ] **Step 3: Commit**

```bash
git add docs/architecture/adr/038-non-terminal-abandoned-path.md
git commit -m "docs(adr): ADR-038 cites the live abandoned-path test name (WS0)"
```

---

### Task 5: Slice-boundary gates

**Files:** none (verification only).

**Interfaces:**
- Consumes: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S2 (the
  per-slice gate checklist — run it exactly as written there).
- Produces: the WS0 slice closed green; the campaign may start WS1.

- [ ] **Step 1: Full suite with HEAD recorded**

```bash
git rev-parse HEAD | tee /tmp/claude-1000/ws0-head-before
.venv/bin/pytest tests/ -n 12
git rev-parse HEAD | tee /tmp/claude-1000/ws0-head-after
diff /tmp/claude-1000/ws0-head-before /tmp/claude-1000/ws0-head-after
```
Expected: suite green; the two HEADs identical (if they differ, the result is
uninterpretable — re-run, do not diagnose).

- [ ] **Step 2: Trust-tier corpus diff**

Follow protocols plan §S2 step 2 verbatim (full `git archive` baseline export, count
findings, diff sorted identities). Expected: identical finding count and identities —
this slice adds only comments and docstrings.

- [ ] **Step 3: Wardline gate**

Run: `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
Expected: exit 0.

- [ ] **Step 4: Confirm the untouchables were not staged**

Run: `git log --oneline -5 --name-only | grep -E "web/composer/state.py|composer/test_state.py"`
Expected: zero hits in this plan's commits.

---

## Self-review notes

- Spec coverage: §9 row 0 names "Docstring/invariant corrections — processor.py, docs".
  Task 1 = the docstring + one-arm claim; Task 2 = the invariant comments at the bypass
  sites the corrected docstring enumerates; Task 3 = the WorkItem trap (spec §6.1 "the
  trap comment lands there"); Task 4 = the known stale doc citation. No behaviour items
  belong in WS0 by definition.
- The guard test pins CLAIMS (absence of the two false sentences, presence of the three
  bypass tokens), not a full stale string — per the pin-truth-not-existence doctrine.
- All line anchors verified against the live tree at plan-writing time (HEAD add597342
  lineage); Task 2 instructs bottom-up application because the three coalesce_executor
  edits shift line numbers.

## Open Questions

None — every decision here is forced by spec rulings and verified tree facts.
