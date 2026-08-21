# WS3 — Single Settlement Channel Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the three-arm `_notify_barrier_of_lost_branch` and its four bypass
sites with ONE frame-driven settle-member routine, one `GroupLossSpec` staged under one
frame-authenticated guard (claim context AND adoption context), one `group_losses`
ledger with full-table takeover restore, and intake-only escalation with a
build-derived fixpoint bound — spec §6 made literally true.

**Architecture:** Every terminal-disposition path calls one seam that walks the failing
token's `lineage_path` innermost-to-first-BOUND-frame and stages exactly one
`GroupLossSpec` (record-then-notify, staged before any in-memory notify, riding the
claim's disposition transaction). Closer FAIL verdicts wait for roster settlement and
then escalate as ONE loss against the enclosing bound frame, staged leader-only in the
barrier-intake adoption transaction and authenticated against the durable roster
authority. Coalesce/row_union executors stop writing token terminals directly; their
callers terminalize members through the standard channel.

**Tech Stack:** Python 3.12+, SQLAlchemy Core (SQLite + PostgreSQL dialects), pytest
(+ testcontainers for the Postgres proofs), hypothesis untouched but re-run.

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md
(rev 3.2 with the 2026-08-22 synthesis corrections applied — rulings 1–28 final; §6
is this workstream's section, §11 its risk posture).

## Global Constraints

- Shared checkout: stage by pathspec ONLY (`git add <exact paths>`); never `git add -A`
  / `git add .`; never commit hunks you did not author this task.
- Never bypass hooks, except the documented `--no-verify`-with-end-of-slice
  reconciliation grant; `git stash` is hook-blocked — use commits.
- Full `pytest tests/` at slice boundaries (a slice = the commit closing each Task
  marked **[SLICE]** below): whole-tree AST gates (attribute-contracts, masquerade,
  runtime-rejection-parity) miss scoped runs. Record `git rev-parse HEAD` before AND
  after the full run; if they differ, re-run rather than diagnose.
- Trust-tier corpus diff before/after each slice:
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`
  — compare finding counts against the pre-slice corpus; ADD NOTHING (the fail-closed
  corpus is the baseline, not zero). Never hand-edit a `judge_metadata_signature`;
  never stage a signing bundle during this campaign (bundles are exact-source-bound).
- Wardline gate before handing back any task that touches external input:
  `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
  (exit 0 = clean and non-inert).
- Depth cap and fixpoint bound (spec §6.3): the supported guarantee is 5 layers of
  bound-region nesting, builder-enforced fail-closed and config-overridable; the
  end-of-input flush fixpoint's non-convergence bound is DERIVED at build from the
  actual bound depth plus margin, never a bare constant — deeper-than-cap remains
  model-correct, merely unsupported. ONE formula exists, owned by WS2:
  `derive_escalation_fixpoint_bound(depth) = 1_000 + 8 * depth`
  (`core/dag/bound_regions.py`). This plan authors NO competing formula.
- Mutation-test runs use `-n 0` (standing parallelism rule); cap per-agent test
  parallelism on any fan-out.
- Do not touch `src/elspeth/web/composer/state.py` or
  `tests/unit/web/composer/test_state.py` (maintainer is committing them).
- Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md
  §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle
  sequencing, and the WS1 STOP rule.

## Interfaces consumed from sibling plans

WS3 starts only after WS1 and WS2 land (spec §11 sequencing WS0→WS1→WS2→WS3∥WS4).
Repeat of the exact contracts this plan consumes (canonical, from the spec — verify the
sibling plans' Interfaces blocks carry these signatures before starting):

- **WS1a plan** (`docs/superpowers/plans/2026-08-21-unified-lineage-ws1a-model-core.md`):
  - Task 1: `LineageFrame(kind: FrameKind, group_id: str, member_key: str)` — frozen
    slots dataclass; `FrameKind.FORK | FrameKind.EXPAND` (`StrEnum`, values
    `"fork"`/`"expand"`) in `contracts/enums.py`; path helpers
    `path_branch_name` / `path_fork_group_id` / `path_expand_group_id` /
    `pop_closer_frame` in `contracts/identity.py`.
  - Task 3: `GroupLossSpec(closer_name: str, group_id: str, member_key: str, token_id: str, reason: str)`
    — the frozen dataclass ALREADY LANDS THERE (beside `BranchLossSpec`, which WS1a
    leaves untouched — "WS3 retires it"). WS3 does NOT re-create the type.
  - Task 4 (schema DDL, epoch 34): `group_losses_table` + `uq_group_losses_natural` +
    the `database.py` `_REQUIRED_COLUMNS`/index entries ALREADY LAND THERE ("Table
    only in WS1a; record/replay verbs are WS3"). Also
    `token_lineage_frames(token_id, run_id, depth, kind, group_id, member_key)`
    PK `(token_id, run_id, depth)` INDEX `(run_id, group_id, member_key)`;
    `group_records(run_id, group_id, kind, opener_token_id, member_count, created_at)`
    PK `(run_id, group_id)` — minted for BOTH kinds, FORK and EXPAND (WS1a Tasks 6–7
    own the writers; empty expansions included). FORK records are ENRICHMENT: the
    FORK roster AUTHORITY stays config (the fork's declared branch list), never the
    record row.
  - (WS1a task-number canon, for citation: 1 contracts helpers, 2
    `TokenInfo.lineage_path`, 3 `GroupLossSpec`, 4 schema DDL/epoch 34, 5 journal
    plumbing, 6 durable writers, 7 empty-expansion mint, 8 TokenManager
    push/strict-pop, 8a nested fixtures, 9 join carriers, 10 `join_group_id` off
    `TokenInfo`, 11 verification/handoff.)
- **WS1b plan** (`docs/superpowers/plans/2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md`):
  - `TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()` — outermost first (the
    field lands in WS1a Task 2; WS1b Task 8 flips it sole-truth); derived read-only
    accessors `branch_name` / `fork_group_id` (innermost FORK frame),
    `expand_group_id` (innermost EXPAND frame). `join_group_id` has left `TokenInfo`
    (WS1a Task 10).
  - Its D7 decision: `TokenWorkItem.lineage_path: tuple[LineageFrame, ...] = ()`
    (decoded from the `token_work_items.lineage_path_json` column by the codec; the
    retired `branch_name`/`fork_group_id`/`expand_group_id` fields are gone;
    `join_group_id` is KEPT — ruling 20, allowlisted in WS1b's column-deletion
    slice — and the barrier binding fields `coalesce_node_id`/`coalesce_name`/
    `row_union_name`/`barrier_key`/`barrier_adopted_epoch` remain).
- **WS2 plan** (`docs/superpowers/plans/2026-08-21-unified-lineage-ws2-config-validation.md`
  — PUBLISHED; its Task 4 authors the registry, Task 5 the regions/bound):
  - `GroupBindingRegistry` (`core/dag/group_bindings.py`) on the built graph via
    `graph.get_group_bindings()` (empty registry when no bound group exists), with
    `binding_for(frame: LineageFrame) -> GroupBinding | None` (None = inert frame) —
    the keyed lookup the settle-member walk consumes. `GroupBinding` is a frozen
    dataclass carrying (beside its build-side node/name fields)
    `closer_name: str`, `closer_kind: CloserKind`, `policy: str`, and
    `member_roster: tuple[str, ...]` — FORK: the declared branch names (`== fork_to`,
    the roster AUTHORITY); EXPAND: `()` (runtime roster via `token_lineage_frames`).
    There is NO `CloserBinding` type and NO `declared_branches` field.
  - `CloserKind` is a `StrEnum`: `COALESCE = "coalesce"`, `ROW_UNION = "row_union"`,
    `COLLECTOR = "collector"` — string-compatible with every serialized surface; WS3
    compares against the enum members (`binding.closer_kind is CloserKind.COALESCE`).
  - `ExecutionGraph.set_max_bound_region_depth` / `get_max_bound_region_depth`
    (mirroring `set_group_bindings`/`get_group_bindings`) — the deepest bound-region
    nesting the built graph contains (0 for no bound groups), already validated
    against the depth-5 cap/override.
  - `derive_escalation_fixpoint_bound(depth: int) -> int` = `1_000 + 8 * depth`
    (`core/dag/bound_regions.py`) — THE one fixpoint formula. WS2 threads
    `derive_escalation_fixpoint_bound(graph.get_max_bound_region_depth())` into
    `PipelineConfig.escalation_fixpoint_bound` and rewrites the
    `run_end_of_input_barrier_flush` loop to iterate it; WS3 consumes and verifies,
    and authors no formula.
  - The processor's existing `_branch_to_coalesce` / `_branch_to_row_union` maps are
    derived VIEWS of this registry after WS2.

  These names are the ratified cross-plan canon (2026-08-22 synthesis) — the
  pre-flight step below verifies them against the landed tree before Task 1; drift is
  fixed at the OWNING plan, never adapted around here.

## Interfaces this plan produces (for WS4/WS5/WS6)

- `GroupLossSpec(closer_name: str, group_id: str, member_key: str, token_id: str, reason: str)`
  — frozen dataclass in `contracts/scheduler.py`; replaces `BranchLossSpec` everywhere.
- `group_losses` table (schema.py): `(loss_id PK, run_id FK, closer_name, group_id,
  member_key, token_id, reason, recorded_by, recorded_at, adopted_epoch)`, UNIQUE
  `(run_id, closer_name, group_id, member_key)`.
- `core/landscape/scheduler/group_losses.py`:
  `record_group_loss(conn, *, run_id: str, spec: GroupLossSpec, recorded_by: str, now: datetime) -> bool`;
  `GroupLoss` row dataclass;
  `GroupLossRepository.list_unadopted_group_losses(*, run_id: str) -> list[GroupLoss]`;
  `GroupLossRepository.list_group_losses(*, run_id: str, closer_names: frozenset[str] | None = None) -> list[GroupLoss]`
  (§E.4 full-table takeover read);
  `GroupLossRepository.adopt_group_losses(*, run_id: str, loss_ids: Sequence[str], now: datetime, coordination_token: CoordinationToken) -> int`;
  `authenticate_adoption_loss(conn, *, run_id: str, spec: GroupLossSpec, frame_kind: FrameKind, declared_roster: tuple[str, ...] | None) -> None`.
- Disposition verbs (`dispositions.py`, `scheduler_repository.py`,
  `barrier.py::complete_barrier`): parameter `group_losses: tuple[GroupLossSpec, ...] = ()`
  replacing `branch_loss: BranchLossSpec | None` / `branch_losses: Sequence[BranchLossSpec]`.
- `RowProcessor._settle_member_losses(current_token, reason, child_items, *, notify_in_memory=True) -> list[RowResult]`
  — THE settlement seam; `_notify_barrier_of_lost_branch` and both kind-specific
  notifiers are deleted.
- `SchedulerDrain.take_claim_group_losses(claimed: TokenWorkItem) -> tuple[GroupLossSpec, ...]`
  — frame-authenticated claim guard; `take_claim_branch_loss` is deleted.
- `BarrierIntakeCoordinator.note_group_failed(*, closer_name: str, group_id: str, reason: str) -> None`
  and the intake escalation pass (WS4's collector executor plugs its FAIL verdicts into
  `note_group_failed`).
- (consumed, restated for WS5/WS6): the EOF flush loop iterates
  `PipelineConfig.escalation_fixpoint_bound` =
  `derive_escalation_fixpoint_bound(graph.get_max_bound_region_depth())` — both names
  WS2-owned. WS3 produces NO fixpoint names; Task 8 only pins the consumption.
- `tests/integration/pipeline/test_depth5_group_unwrap.py::_nested_settings(depth: int) -> dict`
  — the depth-N nested fork/coalesce topology builder, a plain module-level function
  (no fixture arguments) kept importable: the WS5/WS6 plan's crash+resume depth-5
  variant imports and reuses it (see Task 10).

---

### Pre-flight (before Task 1): mechanical citation check

Same discipline as the WS1b plan's pre-flight. The `file:line` anchors below were
taken from the pre-WS1/WS2 tree and WILL have drifted by the time WS3 starts. Before
touching code:

- [ ] For every `src/...:<line>` citation in this plan, open the cited file and
  confirm the named symbol/block is at (or near) the anchor; where it moved, update
  the citation in THIS plan (docs-only commit, pathspec-staged) — never "fix" code to
  match a stale anchor.
- [ ] Verify each consumed sibling symbol imports from the landed tree exactly as the
  header block states: `FrameKind`, `LineageFrame`, `path_branch_name` /
  `path_fork_group_id` / `path_expand_group_id` / `pop_closer_frame`
  (`contracts/identity.py` / `contracts/enums.py`); `GroupLossSpec`
  (`contracts/scheduler.py`); `group_losses_table` / `token_lineage_frames_table` /
  `group_records_table` (`core/landscape/schema.py`); `GroupBinding` /
  `GroupBindingRegistry.binding_for` / `CloserKind` (`core/dag/group_bindings.py`);
  `derive_escalation_fixpoint_bound` (`core/dag/bound_regions.py`);
  `graph.get_group_bindings()` / `graph.get_max_bound_region_depth()`.
  A quick smoke: `python -c "from elspeth.core.dag.group_bindings import GroupBinding, GroupBindingRegistry, CloserKind; from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound; from elspeth.contracts.scheduler import GroupLossSpec; from elspeth.contracts.identity import LineageFrame, pop_closer_frame"`.
- [ ] Any missing or renamed symbol is sibling-plan drift: STOP and reconcile with the
  owning plan (WS1a/WS1b/WS2) before Task 1 — the canon names above are ratified; do
  not adapt WS3 call sites to a divergent landing.

### Task 1: Retire `BranchLossSpec` and the `coalesce_branch_losses` table **[SLICE]**

WS1a already landed the replacements (its Task 3: `GroupLossSpec`; its Task 4:
`group_losses_table` + `uq_group_losses_natural` + the `database.py`
verification entries). This task VERIFIES the landed contract and deletes the retired
artifacts — do not re-create anything.

**Files:**
- Modify: `src/elspeth/contracts/scheduler.py:70-86` (DELETE `BranchLossSpec`;
  `GroupLossSpec` sits beside it already — leave it exactly as WS1a landed it)
- Modify: `src/elspeth/core/landscape/schema.py:1037-1064` (DELETE
  `coalesce_branch_losses_table` + the `uq_coalesce_branch_losses_natural` index;
  `group_losses_table` already exists further down — leave it)
- Modify: `src/elspeth/core/landscape/database.py:454-463` (DELETE the ten
  `("coalesce_branch_losses", ...)` tuples — the `("group_losses", ...)` entries were
  appended by WS1a) and `:734` (DELETE
  `("coalesce_branch_losses", "uq_coalesce_branch_losses_natural")` — the
  `("group_losses", "uq_group_losses_natural")` entry exists)
- Test: `tests/unit/core/landscape/test_group_losses_schema.py` (create)

**Interfaces:**
- Consumes: WS1a Task 3's `GroupLossSpec`; WS1a Task 4's `group_losses_table`.
- Produces: a tree with ONE loss spec type and ONE loss ledger table — the retirement
  every later task builds on.

Note (ratified, 2026-08-22 synthesis): `BranchLossSpec` carried `recorded_by`; the
canonical `GroupLossSpec` does NOT — `recorded_by` is stamped at write time by the
disposition layer (which knows its fenced identity). WS1a Task 3 fixed the spec
type; this plan's Tasks 2–3 own the verb shape.

- [ ] **Step 1: Write the failing test**

```python
# tests/unit/core/landscape/test_group_losses_schema.py
"""Schema contract for the unified group_losses ledger (spec §4.3/§6.2)."""

import dataclasses

import pytest
from sqlalchemy import inspect

from elspeth.contracts.scheduler import GroupLossSpec
from tests.fixtures.landscape import make_landscape_db


def test_group_loss_spec_is_frozen_and_five_fields():
    spec = GroupLossSpec(
        closer_name="merge_paths",
        group_id="fg_001",
        member_key="path_a",
        token_id="tok_001",
        reason="quarantined",
    )
    assert [f.name for f in dataclasses.fields(spec)] == [
        "closer_name",
        "group_id",
        "member_key",
        "token_id",
        "reason",
    ]
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.reason = "other"  # type: ignore[misc]


def test_group_losses_table_shape_and_natural_key():
    db = make_landscape_db()
    inspector = inspect(db.engine)
    assert "group_losses" in inspector.get_table_names()
    assert "coalesce_branch_losses" not in inspector.get_table_names()
    columns = {c["name"] for c in inspector.get_columns("group_losses")}
    assert columns == {
        "loss_id",
        "run_id",
        "closer_name",
        "group_id",
        "member_key",
        "token_id",
        "reason",
        "recorded_by",
        "recorded_at",
        "adopted_epoch",
    }
    indexes = {ix["name"]: ix for ix in inspector.get_indexes("group_losses")}
    natural = indexes["uq_group_losses_natural"]
    assert natural["unique"]
    assert natural["column_names"] == ["run_id", "closer_name", "group_id", "member_key"]
```

(If `tests/fixtures/landscape.make_landscape_db` exposes the engine under a different
attribute, read that fixture module first and use its accessor — the barrier-suite
tests in `tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py`
show the working pattern.)

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/core/landscape/test_group_losses_schema.py -v`
Expected: `test_group_loss_spec_is_frozen_and_five_fields` PASSES already (WS1a Task 3
landed the type — it stays as the consumed-contract pin);
`test_group_losses_table_shape_and_natural_key` FAILS on
`assert "coalesce_branch_losses" not in inspector.get_table_names()` — the retired
table is still present in the WS1a-landed tree.

- [ ] **Step 3: Delete `BranchLossSpec` from `contracts/scheduler.py`**

Delete the `BranchLossSpec` class (`:70-86`) outright. Leave `GroupLossSpec` (landed
by WS1a Task 3, directly below it) untouched — if its fields differ from the canonical
five `(closer_name, group_id, member_key, token_id, reason)`, STOP and reconcile with
the WS1a plan before proceeding; do not fork the contract.

- [ ] **Step 4: Delete the retired table from `schema.py:1037-1064`**

Delete `coalesce_branch_losses_table` and its `uq_coalesce_branch_losses_natural`
`Index(...)` block. `group_losses_table` + the named unique
`Index("uq_group_losses_natural", run_id, closer_name, group_id, member_key)` already
exist below (WS1a Task 4) — verify their column list matches this task's
test and leave them alone.

Then `git grep -n "coalesce_branch_losses_table" -- src/` and update every import to
`group_losses_table` (they will not compile until Tasks 2–3 rewrite their logic; do
those import flips in the same commit series — this task's commit only lands the
retirement, and the tree must be green at commit time, so: perform Steps 3–5 and the
mechanical import/usage rewrites of Tasks 2–3's files in ONE working session,
committing at each task boundary once green. Pre-release posture: the retired table is
DELETED, dev databases are wiped — `auth.db` NEVER).

- [ ] **Step 5: Prune `database.py` verification lists**

At `:454-463` delete the ten `("coalesce_branch_losses", ...)` tuples; at `:734`
delete `("coalesce_branch_losses", "uq_coalesce_branch_losses_natural")`. The
`("group_losses", ...)` column entries and
`("group_losses", "uq_group_losses_natural")` were appended by WS1a — verify they are
present; add nothing.

- [ ] **Step 6: Run to pass**

Run: `pytest tests/unit/core/landscape/test_group_losses_schema.py -v`
Expected: PASS (once Tasks 2–3's mechanical rewrites in the same session compile).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/contracts/scheduler.py src/elspeth/core/landscape/schema.py \
        src/elspeth/core/landscape/database.py \
        tests/unit/core/landscape/test_group_losses_schema.py
git commit -m "refactor(landscape): retire BranchLossSpec and the coalesce_branch_losses table"
```

---

### Task 2: `group_losses` ledger module (record / list / adopt / takeover read)

**Files:**
- Create: `src/elspeth/core/landscape/scheduler/group_losses.py`
- Delete: `src/elspeth/core/landscape/scheduler/branch_losses.py`
- Modify: `src/elspeth/core/landscape/scheduler/__init__.py:16,43`
- Modify: `src/elspeth/core/landscape/scheduler_repository.py:809-835` (facade methods)
- Test: `tests/unit/core/landscape/test_scheduler_repository_group_losses.py` (create —
  migrated from `test_scheduler_repository_coalesce_branch_losses.py`, which is deleted)
- Test: `tests/testcontainer/core/test_group_loss_reason_postgres.py` (create — migrated
  from `test_coalesce_branch_loss_reason_postgres.py`, which is deleted)

**Interfaces:**
- Consumes: WS1a's `GroupLossSpec` + `group_losses_table` (old artifacts retired in
  Task 1).
- Produces: `record_group_loss(conn, *, run_id, spec, recorded_by, now) -> bool`;
  `GroupLoss(loss_id, run_id, closer_name, group_id, member_key, token_id, reason, recorded_by, recorded_at, adopted_epoch)`;
  `GroupLossRepository` with `list_unadopted_group_losses` / `list_group_losses` /
  `adopt_group_losses` (signatures in the header block);
  `authenticate_adoption_loss(conn, *, run_id, spec, frame_kind, declared_roster) -> None`.

This module is a 1:1 migration of `branch_losses.py` with the widened, group-scoped
key. Carry EVERY behaviour the old module documents: rides the caller's transaction;
idempotent on the natural key; same-key-different-`token_id` raises Tier-1
`AuditIntegrityError`; different `reason` tolerated + logged (first durable record
wins); fail-closed reason length check BEFORE the INSERT, never echoing the content;
sqlite/postgresql `on_conflict_do_nothing` with `NotImplementedError` for other
dialects; append-only; `adopted_epoch` fenced CAS mark; **full-table takeover read
regardless of `adopted_epoch`** (§E.4 — this gets its own stated-requirement test, not
inheritance by analogy).

- [ ] **Step 1: Write the failing tests**

Start from a copy of `tests/unit/core/landscape/test_scheduler_repository_coalesce_branch_losses.py`
(read it fully first — its raw-seed + epoch-1 seat pattern is the working template) and
rewrite every verb/name onto the new key. The four behaviours below must each have an
explicit test; write them exactly:

```python
# tests/unit/core/landscape/test_scheduler_repository_group_losses.py  (excerpts —
# the migrated file also carries the seeded-run/seat fixtures from its predecessor)

def _spec(**overrides):
    base = dict(
        closer_name="merge_paths",
        group_id="fg_001",
        member_key="path_a",
        token_id="tok_a",
        reason="quarantined",
    )
    base.update(overrides)
    return GroupLossSpec(**base)


def test_record_group_loss_is_idempotent_on_group_scoped_natural_key(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        assert record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w1", now=_NOW) is True
        assert record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w2", now=_NOW) is False


def test_same_key_different_token_raises_tier1(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        record_group_loss(conn, run_id=run_id, spec=_spec(), recorded_by="w1", now=_NOW)
        with pytest.raises(AuditIntegrityError, match="token lineage corruption"):
            record_group_loss(conn, run_id=run_id, spec=_spec(token_id="tok_IMPOSTOR"), recorded_by="w1", now=_NOW)


def test_sibling_inner_groups_do_not_collide(seeded_run):
    """The rev-2 key-collision hazard is structurally gone: same closer, same
    member_key, DISTINCT group_ids — two rows, no conflict."""
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        assert record_group_loss(conn, run_id=run_id, spec=_spec(group_id="fg_001"), recorded_by="w1", now=_NOW)
        assert record_group_loss(
            conn, run_id=run_id, spec=_spec(group_id="fg_002", token_id="tok_b"), recorded_by="w1", now=_NOW
        )


def test_takeover_read_returns_full_table_regardless_of_adopted_epoch(seeded_run, seat_token):
    """§E.4 STATED REQUIREMENT (spec §6.2): list_group_losses reads adopted
    AND unadopted rows. A restore filtered by adopted_epoch is the enumerated
    mutant this test kills."""
    db, run_id = seeded_run
    repo = GroupLossRepository(db.tier1_engine)
    with db.engine.begin() as conn:
        record_group_loss(conn, run_id=run_id, spec=_spec(member_key="path_a"), recorded_by="w1", now=_NOW)
        record_group_loss(
            conn, run_id=run_id, spec=_spec(member_key="path_b", token_id="tok_b"), recorded_by="w1", now=_NOW
        )
    unadopted = repo.list_unadopted_group_losses(run_id=run_id)
    repo.adopt_group_losses(
        run_id=run_id, loss_ids=[unadopted[0].loss_id], now=_NOW, coordination_token=seat_token
    )
    assert len(repo.list_unadopted_group_losses(run_id=run_id)) == 1
    assert len(repo.list_group_losses(run_id=run_id)) == 2  # FULL table
```

Adoption-context guard tests (new — no predecessor):

```python
def test_authenticate_adoption_loss_fork_accepts_declared_branch(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        authenticate_adoption_loss(
            conn, run_id=run_id, spec=_spec(member_key="path_a"),
            frame_kind=FrameKind.FORK, declared_roster=("path_a", "path_b"),
        )  # no raise


def test_authenticate_adoption_loss_fork_rejects_undeclared_member(seeded_run):
    db, run_id = seeded_run
    with db.engine.begin() as conn:
        with pytest.raises(AuditIntegrityError, match="roster authority"):
            authenticate_adoption_loss(
                conn, run_id=run_id, spec=_spec(member_key="phantom"),
                frame_kind=FrameKind.FORK, declared_roster=("path_a", "path_b"),
            )


def test_authenticate_adoption_loss_expand_requires_frames_row(seeded_run_with_frames):
    """seeded_run_with_frames seeds one token_lineage_frames row
    (run_id, group_id='eg_001', member_key='tok_child_1')."""
    db, run_id = seeded_run_with_frames
    with db.engine.begin() as conn:
        authenticate_adoption_loss(
            conn, run_id=run_id,
            spec=_spec(group_id="eg_001", member_key="tok_child_1", token_id="tok_child_1"),
            frame_kind=FrameKind.EXPAND, declared_roster=None,
        )  # no raise
        with pytest.raises(AuditIntegrityError, match="roster authority"):
            authenticate_adoption_loss(
                conn, run_id=run_id,
                spec=_spec(group_id="eg_001", member_key="tok_phantom", token_id="tok_phantom"),
                frame_kind=FrameKind.EXPAND, declared_roster=None,
            )
```

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py -v`
Expected: FAIL — `ModuleNotFoundError`/`ImportError` for `group_losses`.

- [ ] **Step 3: Write `group_losses.py`**

Port `branch_losses.py` function-for-function. The deltas, exactly:

```python
# src/elspeth/core/landscape/scheduler/group_losses.py — deltas vs branch_losses.py
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.scheduler import GroupLossSpec
from elspeth.core.landscape.schema import group_losses_table, token_lineage_frames_table


def record_group_loss(
    conn: Connection,
    *,
    run_id: str,
    spec: GroupLossSpec,
    recorded_by: str,
    now: datetime,
) -> bool:
    """Append one group-loss row in the CALLER's transaction (§E.5 carried).

    Idempotent on ``(run_id, closer_name, group_id, member_key)``. A
    natural-key hit with a DIFFERENT token_id is token lineage corruption
    (two distinct tokens claiming one member of one group) and raises
    Tier-1; a different reason is tolerated and logged — first durable
    record wins.
    """
    if len(spec.reason) > _REASON_MAX_LENGTH:
        raise AuditIntegrityError(
            f"Group-loss reason is {len(spec.reason)} chars but the reason column holds a "
            f"category token of at most {_REASON_MAX_LENGTH}; producers must record a bare "
            "token (e.g. 'quarantined') and carry detail via the token outcome's error hash."
        )
    loss_id = f"loss_{generate_id()[:12]}"
    values = {
        "loss_id": loss_id,
        "run_id": run_id,
        "closer_name": spec.closer_name,
        "group_id": spec.group_id,
        "member_key": spec.member_key,
        "token_id": spec.token_id,
        "reason": spec.reason,
        "recorded_by": recorded_by,
        "recorded_at": now,
        "adopted_epoch": None,
    }
    # ... identical sqlite/postgresql insert-or-ignore shape as
    # record_coalesce_branch_loss, with
    # index_elements=["run_id", "closer_name", "group_id", "member_key"],
    # the same returning() check, the same conflict re-read, and:
    if existing["token_id"] != spec.token_id:
        raise AuditIntegrityError(
            f"Group-loss record for run_id={run_id!r} closer_name={spec.closer_name!r} "
            f"group_id={spec.group_id!r} member_key={spec.member_key!r} already exists with "
            f"token_id={existing['token_id']!r}, but this call claims token_id={spec.token_id!r}; "
            "two distinct tokens cannot lose the same member of one group — token lineage corruption."
        )
    # ... reason-drift warning identical in shape.


def authenticate_adoption_loss(
    conn: Connection,
    *,
    run_id: str,
    spec: GroupLossSpec,
    frame_kind: FrameKind,
    declared_roster: tuple[str, ...] | None,
) -> None:
    """Adoption-context frame guard (spec §6.2): no claimed token exists, so the
    spec authenticates against the durable roster authority instead —
    declared branches (FORK) or a token_lineage_frames row at the group
    (EXPAND). Same self-authentication property, different witness."""
    if frame_kind is FrameKind.FORK:
        if declared_roster is None:
            raise AuditIntegrityError(
                f"FORK adoption-context loss for group {spec.group_id!r} supplied no declared roster; "
                "the fork's declared branch list is the roster authority."
            )
        if spec.member_key in declared_roster:
            return
    else:
        witnessed = conn.execute(
            select(token_lineage_frames_table.c.token_id)
            .where(token_lineage_frames_table.c.run_id == run_id)
            .where(token_lineage_frames_table.c.group_id == spec.group_id)
            .where(token_lineage_frames_table.c.member_key == spec.member_key)
            .limit(1)
        ).scalar_one_or_none()
        if witnessed is not None:
            return
    raise AuditIntegrityError(
        f"Adoption-context group loss for closer {spec.closer_name!r} group {spec.group_id!r} "
        f"member {spec.member_key!r} has no durable roster-authority witness "
        f"({frame_kind.name}); losses are staged only for members the group actually minted."
    )
```

`GroupLoss`, `_loss_from_mapping`, `GroupLossRepository.list_unadopted_group_losses`,
`.list_group_losses` (parameter renamed `closer_names`, filtering on
`c.closer_name.in_(...)`, empty-frozenset early return kept), and
`.adopt_group_losses` (fenced `verb="adopt_group_losses"`) are mechanical ports —
same ordering (`recorded_at, loss_id`), same docstrings updated to the new vocabulary.
Delete `branch_losses.py`. Update `scheduler/__init__.py` exports
(`record_group_loss`, `GroupLoss`, `GroupLossRepository`, `authenticate_adoption_loss`)
and the `scheduler_repository.py` facade (`:809-835`): `list_unadopted_group_losses`,
`list_group_losses`, `adopt_group_losses` delegating to `self.group_losses`
(rename the `self.branch_losses` component attribute where it is constructed — grep
`branch_losses` in that file and rename all).

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py tests/unit/core/landscape/test_group_losses_schema.py -v`
Expected: PASS.

- [ ] **Step 5: Migrate the Postgres three-proof suite**

Create `tests/testcontainer/core/test_group_loss_reason_postgres.py` from
`test_coalesce_branch_loss_reason_postgres.py` (read it first; keep its three proofs in
dependency order): (1) the real quarantine arm stages the bare `quarantined` category
token and it records durably on PostgreSQL; (2) `record_group_loss` refuses an
over-wide reason with `AuditIntegrityError` BEFORE the INSERT; (3) a raw INSERT of >64
chars genuinely raises `DataError` at the column. Delete the old file. Proof (1)
depends on Task 5's seam — mark it `pytest.mark.skip(reason="settle-member lands in WS3 Task 5")`
in THIS commit and un-skip it in Task 5's commit.

Run: `pytest tests/testcontainer/core/test_group_loss_reason_postgres.py -v -m testcontainer`
Expected: proofs 2–3 PASS, proof 1 SKIPPED.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/core/landscape/scheduler/group_losses.py \
        src/elspeth/core/landscape/scheduler/__init__.py \
        src/elspeth/core/landscape/scheduler_repository.py \
        tests/unit/core/landscape/test_scheduler_repository_group_losses.py \
        tests/testcontainer/core/test_group_loss_reason_postgres.py
git rm src/elspeth/core/landscape/scheduler/branch_losses.py \
       tests/unit/core/landscape/test_scheduler_repository_coalesce_branch_losses.py \
       tests/testcontainer/core/test_coalesce_branch_loss_reason_postgres.py
git commit -m "feat(landscape): group_losses ledger with full-table takeover read and adoption-context guard"
```

---

### Task 3: Disposition plumbing — singular `branch_loss` to `group_losses` collection **[SLICE]**

**Files:**
- Modify: `src/elspeth/core/landscape/scheduler/dispositions.py:103,139,168,199,228,278,637,665,786,921-935`
- Modify: `src/elspeth/core/landscape/scheduler_repository.py:492-620,695` (wrapper kwargs)
- Modify: `src/elspeth/core/landscape/scheduler/barrier.py:95,482` (`complete_barrier` losses)
- Test: `tests/unit/core/landscape/test_scheduler_repository_group_losses.py` (extend)

**Interfaces:**
- Consumes: Task 2's `record_group_loss`.
- Produces: every `mark_terminal` / `mark_terminal_with_ready_children` / `mark_failed`
  / `mark_failed_with_ready_children` / `mark_pending_sink` /
  `mark_pending_sink_with_ready_children` verb (dispositions + repository facade) takes
  `group_losses: tuple[GroupLossSpec, ...] = ()`; `complete_barrier` takes
  `group_losses: Sequence[GroupLossSpec] = ()`.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/core/landscape/test_scheduler_repository_group_losses.py

def test_mark_failed_records_every_staged_group_loss_in_one_transaction(seeded_claimed_item):
    """The singular §E.5 parameter is now a per-frame collection (spec §6.2):
    every spec in group_losses commits iff the disposition commits."""
    db, repo, claimed = seeded_claimed_item
    losses = (
        _spec(member_key="path_a", token_id=claimed.token_id),
        _spec(group_id="eg_outer", member_key=claimed.token_id, token_id=claimed.token_id,
              closer_name="page_stitcher"),
    )
    repo.mark_failed(
        work_item_id=claimed.work_item_id,
        now=_NOW,
        expected_lease_owner=claimed.lease_owner,
        group_losses=losses,
    )
    ledger = GroupLossRepository(db.tier1_engine).list_group_losses(run_id=claimed.run_id)
    assert {(l.closer_name, l.group_id, l.member_key) for l in ledger} == {
        ("merge_paths", "fg_001", "path_a"),
        ("page_stitcher", "eg_outer", claimed.token_id),
    }
    assert all(l.recorded_by for l in ledger)
```

(`seeded_claimed_item` = the enqueue+claim fixture pattern already used by the
predecessor suite / `test_scheduler_repository_adopt_barrier_item.py`; build it from
those fixtures, do not invent a new seeding path.)

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py::test_mark_failed_records_every_staged_group_loss_in_one_transaction -v`
Expected: FAIL — `TypeError: mark_failed() got an unexpected keyword argument 'group_losses'`.

- [ ] **Step 3: Rewrite the plumbing**

In `dispositions.py`, for every verb listed above replace
`branch_loss: BranchLossSpec | None = None` with
`group_losses: tuple[GroupLossSpec, ...] = ()` and thread it through `_transition`
(`:637`), `_transition_with_ready_children` (`:665`) and `_transition_on` (`:786`).
The recording block at `:921-935` becomes:

```python
        for spec in group_losses:
            # §E.5 record-then-notify carried: each durable loss record
            # commits iff this disposition and every child enqueue commit.
            record_group_loss(
                conn,
                run_id=before["run_id"],
                spec=spec,
                recorded_by=expected_lease_owner if expected_lease_owner is not None else "<unfenced>",
                now=now,
            )
```

Docstrings: replace the "§E.5 fork-lineage branch feeding a coalesce" prose with
"spec §6.2: losses staged by the settle-member seam for any bound closer kind". Update
the `scheduler_repository.py` wrappers (`:492-620` — six signatures) mechanically. In
`barrier.py`, `complete_barrier`'s `branch_losses: Sequence[BranchLossSpec] = ()`
(`:95`) becomes `group_losses: Sequence[GroupLossSpec] = ()`, and the loop at `:482`
calls `record_group_loss(conn, run_id=run_id, spec=spec, recorded_by=..., now=now)`
using the same identity the surrounding verb already threads (read `:460-500` for the
in-scope identity — it is the coordination token's worker attribution; keep exactly
what the old call passed as `recorded_by`).

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py -v`
Expected: PASS (the complete_barrier suite compiles against the renamed kwarg — fix
its call sites in the same commit).

- [ ] **Step 5: Commit, then slice boundary**

```bash
git add src/elspeth/core/landscape/scheduler/dispositions.py \
        src/elspeth/core/landscape/scheduler_repository.py \
        src/elspeth/core/landscape/scheduler/barrier.py \
        tests/unit/core/landscape/test_scheduler_repository_group_losses.py \
        tests/unit/core/landscape/test_scheduler_repository_complete_barrier.py
git commit -m "feat(landscape): disposition verbs carry a group_losses collection"
```

Slice boundary: full `pytest tests/` (HEAD recorded before/after) + trust-tier corpus
diff (add nothing). NOTE: the engine files (`processor.py`, `scheduler_drain.py`,
`barrier_coordination.py`) still reference `BranchLossSpec` at this point and will not
import — Tasks 1–3 and the mechanical engine-side rename in Task 4 Step 3 therefore
land as ONE push sequence; run the full-suite slice check after Task 4's commit if the
tree is not green earlier. Never leave the shared checkout red between pushes.

---

### Task 4: Frame-authenticated CLAIM guard — `take_claim_group_losses`

**Files:**
- Modify: `src/elspeth/engine/scheduler_drain.py:49,293,320,644,655,664,721,758,781,983-1007`
- Modify: `src/elspeth/engine/processor.py:145,647-653,727,3915-3945,4304-4306`
- Test: `tests/unit/engine/test_group_loss_claim_guard.py` (create)

**Interfaces:**
- Consumes: `TokenWorkItem.lineage_path` (WS1a Task 5's journal plumbing, flipped
  sole-truth by WS1b); WS1a Task 3's `GroupLossSpec`.
- Produces: `SchedulerDrain.take_claim_group_losses(claimed: TokenWorkItem) -> tuple[GroupLossSpec, ...]`;
  the drain's `_pending_group_losses: list[GroupLossSpec]` (renamed from
  `_pending_branch_losses`, same producer-transaction discipline at
  `processor.py:3915-3945`).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/engine/test_group_loss_claim_guard.py
"""Claim-context frame guard (spec §6.2): a staged loss is legal iff the
claimed token's own lineage_path contains a frame matching
(group_id, member_key) — self-authenticating because frames are minted by
openers, never asserted by failing code."""

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import GroupLossSpec

# Use the shared builders: _make_processor / _persist_blocked_scheduler_work
# from tests/unit/engine/test_processor (WS1 already migrated them to
# lineage_path). The helpers below assume a `drain` built by _make_processor
# with an accessible _pending_group_losses list and a claimed TokenWorkItem
# factory `claimed_item(lineage_path=...)` added beside the existing
# builders in that module.

FORK_FRAME = LineageFrame(kind=FrameKind.FORK, group_id="fg_1", member_key="path_a")
OUTER_EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_1", member_key="tok_m1")


def _loss(group_id="fg_1", member_key="path_a", token_id="tok_x"):
    return GroupLossSpec(
        closer_name="merge_paths", group_id=group_id, member_key=member_key,
        token_id=token_id, reason="quarantined",
    )


def test_guard_accepts_loss_whose_frame_the_claimed_path_carries(drain, claimed_item):
    claimed = claimed_item(lineage_path=(OUTER_EXPAND, FORK_FRAME))
    drain._pending_group_losses.append(_loss(token_id=claimed.token_id))
    assert drain.take_claim_group_losses(claimed) == (_loss(token_id=claimed.token_id),)
    assert drain._pending_group_losses == []


def test_guard_rejects_group_id_match_with_wrong_member_key(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.append(_loss(member_key="path_b"))
    with pytest.raises(OrchestrationInvariantError, match="lineage path"):
        drain.take_claim_group_losses(claimed)


def test_guard_rejects_member_key_match_with_wrong_group_id(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.append(_loss(group_id="fg_OTHER"))
    with pytest.raises(OrchestrationInvariantError, match="lineage path"):
        drain.take_claim_group_losses(claimed)


def test_guard_rejects_two_losses_for_one_frame(drain, claimed_item):
    claimed = claimed_item(lineage_path=(FORK_FRAME,))
    drain._pending_group_losses.extend([_loss(), _loss(token_id="tok_y")])
    with pytest.raises(OrchestrationInvariantError, match="at most one loss per bound frame"):
        drain.take_claim_group_losses(claimed)


def test_guard_allows_empty_staging(drain, claimed_item):
    assert drain.take_claim_group_losses(claimed_item(lineage_path=())) == ()
```

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/engine/test_group_loss_claim_guard.py -v`
Expected: FAIL — `AttributeError: ... has no attribute 'take_claim_group_losses'`.

- [ ] **Step 3: Implement the guard and the mechanical renames**

Replace `take_claim_branch_loss` (`scheduler_drain.py:983-1007`) with:

```python
    def take_claim_group_losses(self, claimed: TokenWorkItem) -> tuple[GroupLossSpec, ...]:
        """Take the staged losses for the claim being disposed (spec §6.2).

        Frame-authenticated: each staged spec must name a (group_id,
        member_key) frame that the claimed token's own lineage_path carries —
        self-authenticating, because frames are minted by openers and never
        asserted by failing code. At most one loss per bound frame per claim.
        The old token-equality guard is retired with BranchLossSpec: for a
        FORK frame this guard is exactly as strong (the branch token carries
        its own branch frame), and for EXPAND frames it is the correct
        generalization the old guard crashed on.
        """
        if not self._pending_group_losses:
            return ()
        staged = tuple(self._pending_group_losses)
        self._pending_group_losses.clear()
        claimed_frames = {(frame.group_id, frame.member_key) for frame in claimed.lineage_path}
        seen: set[tuple[str, str]] = set()
        for spec in staged:
            key = (spec.group_id, spec.member_key)
            if key not in claimed_frames:
                raise OrchestrationInvariantError(
                    f"Claim disposition for token {claimed.token_id!r} found a staged group-loss "
                    f"record for group {spec.group_id!r} member {spec.member_key!r} that the claimed "
                    "token's lineage path does not carry. Frames are minted by openers, never "
                    "asserted by failing code. Processor bug."
                )
            if key in seen:
                raise OrchestrationInvariantError(
                    f"Claim disposition for token {claimed.token_id!r} staged two losses for group "
                    f"{spec.group_id!r} member {spec.member_key!r}; at most one loss per bound frame "
                    "per claim. Processor bug."
                )
            seen.add(key)
        return staged
```

Mechanical renames in the same commit (grep-driven, exhaustive):
- `scheduler_drain.py:49` import `GroupLossSpec` (drop `BranchLossSpec`); `:293/:320`
  constructor param `pending_group_losses`; call sites `:664/:721/:758/:781` become
  `group_losses=self.take_claim_group_losses(claimed)` (pass the CLAIMED ITEM, not the
  token_id); the `.clear()` calls at `:644/:655` keep their positions.
- `processor.py:145` import; `:647-653` field `self._pending_group_losses: list[GroupLossSpec] = []`
  (keep the §E.5 comment, reworded to §6.2); `:727` kwarg; `:3915-3945` — the flush's
  producer-transaction block renames `branch_losses`→`group_losses` and passes
  `group_losses=group_losses` to `complete_barrier`; `:4304-4306` delegate becomes
  `_take_claim_group_losses(self, claimed: TokenWorkItem)`.
- The staging append sites (`processor.py:3058-3067` and `:3202-3211`) are rewritten by
  Task 5; for THIS commit convert them minimally to `GroupLossSpec` keyed by the
  token's innermost FORK frame so the tree stays green:
  `GroupLossSpec(closer_name=str(coalesce_name), group_id=current_token.fork_group_id, member_key=str(branch_name), token_id=current_token.token_id, reason=reason)`
  (the derived accessor `fork_group_id` is non-None on every path that reaches these
  arms — they begin with `if current_token.branch_name is None: return []`).

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_group_loss_claim_guard.py tests/unit/engine/test_processor.py tests/unit/engine/test_scheduler_drain.py -v`
(if `tests/unit/engine/test_scheduler_drain.py` does not exist under that name, run the
suite `pytest tests/unit/engine/ -x -q` instead)
Expected: PASS.

- [ ] **Step 5: Commit + deferred slice check**

```bash
git add src/elspeth/engine/scheduler_drain.py src/elspeth/engine/processor.py \
        tests/unit/engine/test_group_loss_claim_guard.py tests/unit/engine/test_processor.py
git commit -m "feat(engine): frame-authenticated claim guard replaces the token-equality branch-loss guard"
```

Now run the deferred Task-3 slice boundary: full `pytest tests/` + trust-tier corpus
diff + wardline gate.

---

### Task 5: The settle-member seam **[SLICE]**

**Files:**
- Modify: `src/elspeth/engine/processor.py:1428-1449` (empty-flush loss staging),
  `:1642-1652` (non-empty flush QUARANTINED_AT_SOURCE asymmetry — bypass site 2),
  `:3028-3103` (`_notify_row_union_of_lost_branch` — absorbed), `:3105-3119`
  (`_row_union_group_released` — deleted), `:3121-3155` (`_notify_barrier_of_lost_branch`
  + false docstring — deleted), `:3157-3292` (`_notify_coalesce_of_lost_branch` —
  absorbed), every `_notify_barrier_of_lost_branch` call site
  (`git grep -n "_notify_barrier_of_lost_branch" -- src/` — includes
  `token_traversal.py:198-226`'s zero-row path)
- Test: `tests/unit/engine/test_settle_member_seam.py` (create)

**Interfaces:**
- Consumes: WS1 `TokenInfo.lineage_path` + `LineageFrame` (WS1a Tasks 1–2); WS2
  `GroupBindingRegistry.binding_for(frame) -> GroupBinding | None` and
  `GroupBinding(closer_name, closer_kind, member_roster, …)` with `CloserKind`
  StrEnum members; Task 4's `_pending_group_losses`.
- Produces: `RowProcessor._settle_member_losses(current_token: TokenInfo, reason: str, child_items: list[WorkItem], *, notify_in_memory: bool = True) -> list[RowResult]`
  — THE seam; `RowProcessor._stage_group_loss(spec: GroupLossSpec) -> None`.

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/engine/test_settle_member_seam.py
"""Spec §6.1: ONE settle-member routine — walk the failing token's
lineage_path from the innermost frame to the FIRST BOUND frame, stage one
GroupLossSpec for that frame's member. Record-then-notify: staging is
unconditional and precedes any in-memory notify; followers stage the
innermost bound loss only (notify is leader-only)."""

# Built on tests/unit/engine/test_processor builders (_make_processor with a
# stub GroupBindingRegistry injected). `bindings` maps
# (group_id, member_key) -> GroupBinding; frames absent from it are inert
# (the stub's binding_for(frame) looks up (frame.group_id, frame.member_key)).

INNER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg_inner", member_key="path_a")
OUTER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg_outer", member_key="left")
INERT_EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg_inert", member_key="tok_c1")


def test_walk_stages_loss_for_first_bound_frame_skipping_inert(processor_with_bindings):
    """Innermost-to-first-BOUND: an inert innermost frame is skipped, the
    first BOUND frame gets the one staged loss. Kills the
    walk-stops-at-innermost mutant."""
    proc = processor_with_bindings({("fg_inner", "path_a"): coalesce_binding("merge_inner")})
    token = make_token(lineage_path=(OUTER_FORK, INNER_FORK, INERT_EXPAND))
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    (spec,) = proc._pending_group_losses
    assert (spec.closer_name, spec.group_id, spec.member_key) == ("merge_inner", "fg_inner", "path_a")


def test_walk_is_innermost_first_not_outermost_first(processor_with_bindings):
    """BOTH frames bound: the INNER one is settled. Kills the
    outermost-first mutant."""
    proc = processor_with_bindings({
        ("fg_inner", "path_a"): coalesce_binding("merge_inner"),
        ("fg_outer", "left"): coalesce_binding("merge_outer"),
    })
    token = make_token(lineage_path=(OUTER_FORK, INNER_FORK))
    proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False)
    (spec,) = proc._pending_group_losses
    assert spec.group_id == "fg_inner"


def test_all_inert_path_stages_nothing(processor_with_bindings):
    """Unbound frames are inert provenance (§2): nobody waits, nothing is
    staged — the batch posture, structurally."""
    proc = processor_with_bindings({})
    token = make_token(lineage_path=(OUTER_FORK, INERT_EXPAND))
    assert proc._settle_member_losses(token, "quarantined", [], notify_in_memory=False) == []
    assert proc._pending_group_losses == []


def test_root_token_settles_nothing(processor_with_bindings):
    proc = processor_with_bindings({})
    token = make_token(lineage_path=())
    assert proc._settle_member_losses(token, "quarantined", []) == []


def test_staging_precedes_notify_and_survives_notify_absence(processor_with_bindings):
    """Record-then-notify (processor.py:3191 discipline, carried): the durable
    staging happens even when this worker has no executor (follower)."""
    proc = processor_with_bindings(
        {("fg_inner", "path_a"): coalesce_binding("merge_inner")}, coalesce_executor=None
    )
    token = make_token(lineage_path=(INNER_FORK,))
    assert proc._settle_member_losses(token, "quarantined", []) == []
    assert len(proc._pending_group_losses) == 1


def test_leader_notify_dispatches_to_coalesce_executor(processor_with_bindings, recording_coalesce_executor):
    proc = processor_with_bindings(
        {("fg_inner", "path_a"): coalesce_binding("merge_inner")},
        coalesce_executor=recording_coalesce_executor,
    )
    token = make_token(lineage_path=(INNER_FORK,), row_id="row-1")
    proc._settle_member_losses(token, "quarantined", [])
    assert recording_coalesce_executor.notified == [("merge_inner", "row-1", "path_a", "quarantined")]


def test_quarantined_batch_member_reaches_the_seam(aggregation_flush_processor):
    """Bypass site 2 (spec §6.1 item 2): the non-empty flush's
    QUARANTINED_AT_SOURCE members now call the seam exactly as the
    empty-flush path always did — same reason vocabulary, staged not
    notified in memory."""
    proc, flush = aggregation_flush_processor(
        quarantined_indices={0},
        member_paths=[(INNER_FORK,), ()],
        bindings={("fg_inner", "path_a"): coalesce_binding("merge_inner")},
    )
    flush()
    (spec,) = proc._pending_group_losses
    assert spec.reason == "quarantined"
    assert spec.member_key == "path_a"
```

The fixture helpers (`processor_with_bindings`, `make_token`, `coalesce_binding`,
`recording_coalesce_executor`, `aggregation_flush_processor`) live at the top of this
test file, composed from `tests/unit/engine/test_processor._make_processor`; write them
against the real constructor signature you find there — the recording executor is a
minimal object with `notify_branch_lost(coalesce_name, row_id, lost_branch, reason)`
returning `None` and appending to `.notified`. `coalesce_binding(name)` constructs a
real `GroupBinding` (import it from `elspeth.core.dag.group_bindings`) with
`closer_name=name`, `closer_kind=CloserKind.COALESCE`, and a `member_roster` naming
the frame's member — real type, stub registry.

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/engine/test_settle_member_seam.py -v`
Expected: FAIL — `AttributeError: ... has no attribute '_settle_member_losses'`.

- [ ] **Step 3: Implement the seam**

In `processor.py`, DELETE `_notify_barrier_of_lost_branch` (`:3121-3155`, false
docstring included), `_row_union_group_released` (`:3105-3119` — structurally retired:
ruling 27 pops the frame at release, so a post-release terminal no longer carries it
and the walk can never resolve to the union's group), and fold the two kind-specific
notifiers into:

```python
    def _settle_member_losses(
        self,
        current_token: TokenInfo,
        reason: str,
        child_items: list[WorkItem],
        *,
        notify_in_memory: bool = True,
    ) -> list[RowResult]:
        """THE single settlement seam (spec §6.1) — now actually single.

        Every terminal-disposition path calls this. It walks the failing
        token's lineage_path from the INNERMOST frame outward to the FIRST
        BOUND frame and stages exactly one GroupLossSpec for that frame's
        member (record-then-notify: staged unconditionally, before any
        in-memory notify; the staged record rides this claim's disposition
        transaction via take_claim_group_losses, or the flush's
        complete_barrier). Inert frames — no closer bound — are pure
        provenance and are skipped. Followers stage the innermost bound loss
        only; the in-memory notify is leader-only.
        """
        for frame in reversed(current_token.lineage_path):
            binding = self._group_bindings.binding_for(frame)
            if binding is None:
                continue  # inert frame: no roster watching (spec §2)
            self._stage_group_loss(
                GroupLossSpec(
                    closer_name=binding.closer_name,
                    group_id=frame.group_id,
                    member_key=frame.member_key,
                    token_id=current_token.token_id,
                    reason=reason,
                )
            )
            if not notify_in_memory:
                return []
            return self._notify_closer_of_loss(binding, frame, current_token, reason, child_items)
        return []

    def _stage_group_loss(self, spec: GroupLossSpec) -> None:
        for staged in self._pending_group_losses:
            if (staged.group_id, staged.member_key) == (spec.group_id, spec.member_key):
                raise OrchestrationInvariantError(
                    f"Settlement staged a second loss for group {spec.group_id!r} member "
                    f"{spec.member_key!r} within one claim; at most one loss per bound frame "
                    "per claim. Processor bug."
                )
        self._pending_group_losses.append(spec)
```

`_notify_closer_of_loss` dispatches on `binding.closer_kind`:

- `CloserKind.COALESCE`: the body of today's `_notify_coalesce_of_lost_branch` AFTER
  its staging block (`:3213-3292` — executor-presence check, `notify_branch_lost`,
  merged/failure arms) with `coalesce_name = CoalesceName(binding.closer_name)`,
  `lost_branch=frame.member_key`, and the coalesce node id resolved as today
  (`self._coalesce_node_ids[coalesce_name]`). The in-memory executor call keeps its
  `(coalesce_name, row_id, lost_branch, reason)` signature — the pending-state re-key
  to `(coalesce_name, fork_group_id)` is WS4's (spec §5); `current_token.row_id` is in
  hand here.
- `CloserKind.ROW_UNION`: the body of today's `_notify_row_union_of_lost_branch` after
  its staging block (`:3068-3103`), minus the deleted released-group guard.
- `CloserKind.COLLECTOR`: `raise OrchestrationInvariantError(f"Collector closer {binding.closer_name!r} has no executor wired; collector settlement lands in WS4 integration.")`
  — WS2 forbids building collector bindings until WS4's executor registers, so this arm
  is unreachable until then; the raise keeps it fail-closed rather than silently
  staged-but-never-notified. (The staged loss itself is already correct — WS4 replaces
  only this arm.)

Then rewrite every caller: `git grep -n "_notify_barrier_of_lost_branch" -- src/` and
replace each call with `_settle_member_losses` (same arguments — the signature is
unchanged apart from the name). The empty-flush path (`:1441-1448`) keeps
`notify_in_memory=False`. Fix bypass site 2 by extending the non-empty flush loop
(`:1642-1652`): quarantined members call the seam too —

```python
            for i, token in enumerate(fctx.buffered_tokens):
                if i in quarantined_index_set:
                    outcome = TerminalOutcome.FAILURE
                    path = TerminalPath.QUARANTINED_AT_SOURCE
                    # Spec §6.1 item 2: the quarantined batch member settles
                    # through the same seam as the empty-flush path — the
                    # QUARANTINED_AT_SOURCE asymmetry is retired. After WS2's
                    # ruling-25 ban this stages nothing inside bound regions
                    # (aggregators cannot sit there); for unbound frames the
                    # seam is a structural no-op. Uniformity is the point.
                    results.extend(
                        self._settle_member_losses(token, "quarantined", child_items, notify_in_memory=False)
                    )
                else:
                    outcome = TerminalOutcome.TRANSIENT
                    path = TerminalPath.BATCH_CONSUMED
                self._emit_token_completed(token, outcome=outcome, path=path)
```

The `self._group_bindings` registry reaches the processor from WS2's built graph
(`graph.get_group_bindings()`) — thread it through the constructor the same way
`_branch_to_coalesce` already arrives (read `processor.py:483-499` and the factory in
`engine/orchestrator/processor_factory.py` for the wiring point; after WS2 the
branch maps are derived views of this same registry).

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_settle_member_seam.py tests/unit/engine/test_processor.py tests/unit/engine/test_token_traversal_characterization.py -v`
Expected: PASS. Also un-skip Postgres proof (1) from Task 2 Step 5 and run:
`pytest tests/testcontainer/core/test_group_loss_reason_postgres.py -v -m testcontainer`
Expected: all three proofs PASS.

- [ ] **Step 5: Commit + slice boundary**

```bash
git add src/elspeth/engine/processor.py src/elspeth/engine/token_traversal.py \
        tests/unit/engine/test_settle_member_seam.py \
        tests/testcontainer/core/test_group_loss_reason_postgres.py
git commit -m "feat(engine): settle-member seam replaces the three-arm barrier-loss notifier"
```

Full `pytest tests/` + trust-tier corpus diff + wardline. WS3 will churn manifest
counts for the corpus loss fixtures (scheduler_event/record counts in
`semantic_runtime`/`summary` expectations) even where dispositions stay identical —
adjudicate those rotations under the fixture-oracle protocol as WS3's OWN adjudication
(dated A/B note in `tests/unit/architecture/test_dag_scenario_corpus_contract.py`'s
rotation ledger), never as part of WS1's.

---

### Task 6: Retire the coalesce executor's raw terminals (bypass site 1)

**Files:**
- Modify: `src/elspeth/engine/coalesce_executor.py:837-846` (late arrival), `:1050-1059`
  (group failure), `:1250-1259` (merge-exception cleanup) — and the `CoalesceOutcome`
  contract at `:52` (`outcomes_recorded` flips to `False` on these arms)
- Modify: `src/elspeth/engine/processor.py` (`_notify_closer_of_loss` failure arm; the
  `outcome.failure_reason` consumers), `src/elspeth/engine/barrier_coordination.py:447-482`
  (late-arrival intake arm), `:487-510` (arrival-completes-failure arm), `:792-819`
  (replay failure arm)
- Test: `tests/unit/engine/test_coalesce_executor.py` (extend — read it first; it pins
  `outcomes_recorded`), `tests/unit/engine/test_barrier_coordination.py` (extend)

**Interfaces:**
- Consumes: Task 5's seam.
- Produces: `RowProcessor._record_group_member_terminals(consumed_tokens: tuple[TokenInfo, ...], *, failure_reason: str) -> None`
  — the ONE caller-side terminal writer for closer-consumed members (FAILURE/UNROUTED
  + error hash), used by every arm that previously relied on
  `outcomes_recorded=True`.

The executor's three `record_token_outcome` calls are the audit bypass: a consumed
token dying inside an enclosing bound region without its outer member ever settling.
The executor stops writing `token_outcomes`; callers terminalize through one helper
that ALSO runs the settlement walk for the tokens' REMAINING path (outer frames), which
is what makes escalation (Task 8) observable.

- [ ] **Step 1: Write the failing test**

```python
# append to tests/unit/engine/test_coalesce_executor.py

def test_late_arrival_and_failure_arms_do_not_write_token_outcomes(executor_harness):
    """Spec §6.1 item 1: the executor's failure paths stop writing terminals
    directly. outcomes_recorded=False on the late-arrival, group-failure and
    merge-exception arms; the CALLER records through the settlement channel."""
    executor, data_flow_spy = executor_harness()
    drive_group_to_failure(executor)          # helper: arrive one branch, lose a required one
    drive_late_arrival(executor)              # helper: arrive after completion
    assert data_flow_spy.record_token_outcome_calls == []
```

And the zero-write completeness assertion (recent-code-hints 2026-08-21: the
zero-write direction has NO automatic detection — assert set equality, do not rely on
`ix_token_outcomes_terminal_unique`):

```python
# append to tests/unit/engine/test_barrier_coordination.py

def test_intake_failure_arm_terminalizes_every_consumed_member_exactly_once(intake_harness):
    coordinator, db, tokens = intake_harness.fail_group_via_intake()
    recorded = completed_outcome_token_ids(db, run_id=intake_harness.run_id)
    assert recorded == {t.token_id for t in tokens}  # completeness, not just no-duplicates
```

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/engine/test_coalesce_executor.py -k "do_not_write_token_outcomes" tests/unit/engine/test_barrier_coordination.py -k "terminalizes_every_consumed_member" -v`
Expected: FAIL — the spy records outcome writes / helper names missing.

- [ ] **Step 3: Implement**

In `coalesce_executor.py` delete the three `record_token_outcome` blocks (and their
`data_flow is None` guard raises) at `:837-846`, `:1050-1059`, `:1250-1259`; the
enclosing arms set `outcomes_recorded=False`. Keep node-state completion writes — they
are execution audit, not token terminals.

In `processor.py` add the one helper and use it in the `outcome.failure_reason` arm of
`_notify_closer_of_loss` (which Task 5 carried verbatim from `:3263-3290`) and in the
flush consumers:

```python
    def _record_group_member_terminals(
        self,
        consumed_tokens: tuple[TokenInfo, ...],
        *,
        failure_reason: str,
    ) -> None:
        """Terminalize a closer's consumed members through the standard
        channel (spec §6.1: no closer writes token terminals directly)."""
        error_hash = compute_error_hash(failure_reason)
        for consumed in consumed_tokens:
            self._data_flow.record_token_outcome(
                ref=TokenRef(token_id=consumed.token_id, run_id=self._run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
                error_hash=error_hash,
            )
```

In `barrier_coordination.py` the three intake arms call the coordinator's equivalent
helper (add the twin there — it already holds `_emit_token_completed` and data-flow
access; mirror the processor helper exactly) BEFORE building their `RowResult`s. The
late-arrival arm (`:447-482`) additionally keeps its single-row
`mark_blocked_barrier_terminal` release unchanged. Where each arm formerly trusted
`outcomes_recorded=True`, it now records explicitly; grep
`git grep -n "outcomes_recorded" -- src/ tests/` and update every consumer/pin.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_barrier_coordination.py tests/property/engine/test_coalesce_properties.py -v`
Expected: PASS (the property suite guards plugin-visible merge behaviour — it must stay
green untouched).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/coalesce_executor.py src/elspeth/engine/processor.py \
        src/elspeth/engine/barrier_coordination.py \
        tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_barrier_coordination.py
git commit -m "feat(engine): closers stop writing token terminals; callers record via the settlement channel"
```

---

### Task 7: Ledger replay and full-table takeover restore onto `group_losses`

**Files:**
- Modify: `src/elspeth/engine/barrier_coordination.py:703-828` (`_replay_branch_losses`
  → `_replay_group_losses`), the `BarrierRecoveryCoordinator` restore path (grep
  `list_coalesce_branch_losses` under `src/` — the §E.4 seed site cited by the spec at
  `scheduler/branch_losses.py:193`'s consumer)
- Modify: `tests/e2e/recovery/test_suspended_winner_fences.py` (migrate the
  `adopt_coalesce_branch_losses` fence arm to `adopt_group_losses` — MIGRATED, not
  duplicated)
- Test: `tests/unit/engine/test_barrier_coordination.py` (extend)

**Interfaces:**
- Consumes: Task 2 repository verbs; Task 6 helpers.
- Produces: `BarrierIntakeCoordinator.replay_durable_group_losses() -> tuple[BarrierIntakeDisposition, ...]`
  (renamed from `replay_durable_branch_losses`; same disposition contract);
  `_row_id_for_loss(loss: GroupLoss) -> str` — the transitional group→pending-key
  resolution.

The ledger row no longer carries `row_id`, but the executors' in-memory notify is
still keyed `(closer_name, row_id)` until WS4 re-keys (spec §5). Resolution: the loss
row carries `token_id`; the durable `tokens` row gives `row_id`. This is an intake-path
DB read (leader, once per unadopted loss) — NOT the hot accounting path; the pinned
"never a DB query" commitment (§4.1) binds the COALESCED accounting site, not intake
replay.

- [ ] **Step 1: Write the failing tests**

```python
# append to tests/unit/engine/test_barrier_coordination.py

def test_replay_resolves_row_id_from_the_durable_token_row(replay_harness):
    """group_losses carries no row_id; replay resolves the executor's
    transitional (closer_name, row_id) key via the loss's token_id -> tokens
    row. Covers the all-members-lost shape too: the member token exists
    durably even when it never arrived at the closer."""
    harness = replay_harness()
    harness.record_durable_loss(closer="merge_paths", group_id="fg_1", member_key="path_a",
                                token_id=harness.branch_token.token_id, reason="quarantined")
    dispositions = harness.coordinator.replay_durable_group_losses()
    assert harness.coalesce_executor.notified == [
        ("merge_paths", harness.branch_token.row_id, "path_a", "quarantined")
    ]


def test_takeover_restore_seeds_executor_from_full_table_not_unadopted_subset(replay_harness):
    """Spec §6.2 stated requirement: takeover restore reads the FULL table
    regardless of adopted_epoch. Kills the restore-filtered-by-adopted_epoch
    mutant."""
    harness = replay_harness()
    harness.record_durable_loss(closer="merge_paths", group_id="fg_1", member_key="path_a",
                                token_id=harness.branch_token.token_id, reason="quarantined",
                                adopted_epoch=3)   # already adopted by a dead leader
    harness.record_durable_loss(closer="merge_paths", group_id="fg_1", member_key="path_b",
                                token_id=harness.sibling_token.token_id, reason="error_routed",
                                adopted_epoch=None)
    restored = harness.run_takeover_restore()
    assert restored.lost_branch_count == 2
```

Build `replay_harness` from the existing intake/restore fixtures in this test module
and `tests/integration/pipeline/test_barrier_intake_dispositions.py`'s builders
(`_real_coalesce_executor`, `_coalesce_processor`) — read both before writing.

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/engine/test_barrier_coordination.py -k "replay_resolves or full_table" -v`
Expected: FAIL — `replay_durable_group_losses` missing.

- [ ] **Step 3: Implement**

Rewrite `_replay_branch_losses` (`:703-828`) as `_replay_group_losses`:

```python
        losses = self._scheduler.list_unadopted_group_losses(run_id=self._run_id)
        if not losses:
            return dispositions
        coordination_token = self._require_coordination_token()
        self._scheduler.adopt_group_losses(
            run_id=self._run_id,
            loss_ids=[loss.loss_id for loss in losses],
            now=self._clock.now_utc(),
            coordination_token=coordination_token,
        )
        for loss in losses:
            row_id = self._row_id_for_loss(loss)
            ...
```

with `loss.closer_name` replacing `loss.coalesce_name`, `loss.member_key` replacing
`loss.branch_name`, and `row_id` replacing `loss.row_id` throughout the row_union and
coalesce arms (which are otherwise carried verbatim — including the
`has_recorded_branch_loss` dedup consult, whose executor-side signature keeps
`(name, row_id, branch)` until WS4). Add:

```python
    def _row_id_for_loss(self, loss: GroupLoss) -> str:
        row_id = self._barrier_restore_reads.row_id_for_token(
            run_id=self._run_id, token_id=loss.token_id
        )
        if row_id is None:
            raise AuditIntegrityError(
                f"Group-loss {loss.loss_id!r} names token {loss.token_id!r} with no durable tokens "
                "row; the ledger references a token the audit trail never minted."
            )
        return row_id
```

(`row_id_for_token` is a one-line `select tokens.row_id where token_id/run_id` added to
the same read facade `_barrier_restore_reads` already exposes —
`has_released_row_for_node` shows the pattern; put it beside that.)

Rename the public `replay_durable_branch_losses` (`:826-829`) and its callers
(`processor.py:3943` renamed in Task 4's block; grep for the rest). Point the takeover
restore's full-table read at `list_group_losses(run_id=..., closer_names=...)` — grep
`list_coalesce_branch_losses` for the restore call site and carry its scoping
frozenset semantics.

Migrate the fence-matrix arm in `tests/e2e/recovery/test_suspended_winner_fences.py`:
the `adopt_coalesce_branch_losses` test becomes `adopt_group_losses` with the same
four-part contract (RunLeadershipLostError; one `fence_refusal` event naming
`"adopt_group_losses"`; zero mutation; positive control) — edit the existing test in
place, do not add a sibling.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_barrier_coordination.py -v` and
`pytest tests/e2e/recovery/test_suspended_winner_fences.py -k "group_losses" -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/barrier_coordination.py src/elspeth/engine/processor.py \
        tests/unit/engine/test_barrier_coordination.py tests/e2e/recovery/test_suspended_winner_fences.py
git commit -m "feat(engine): group-loss replay and full-table takeover restore on the unified ledger"
```

---

### Task 8: Escalation — intake-only, verdicts wait for settlement, build-derived fixpoint bound **[SLICE]**

**Files:**
- Modify: `src/elspeth/engine/barrier_coordination.py` (add `note_group_failed` + the
  intake escalation pass; call it from `run_barrier_intake`'s per-iteration sequence
  after loss replay)
- Modify: `src/elspeth/engine/processor.py` (`_notify_closer_of_loss` FAIL arm and the
  flush FAIL arms call `note_group_failed` — bypass sites 3–4 rerouted in Task 9 use
  the same hook)
- Verify (no edit unless WS2 drift): `src/elspeth/engine/orchestrator/leader_drain.py`
  — WS2 already replaced the constant loop with the derived
  `flush_iteration_bound = config.escalation_fixpoint_bound`; WS3 authors NO fixpoint
  code here (see Step 3)
- Test: `tests/unit/engine/test_escalation_intake.py` (create),
  `tests/unit/engine/test_leader_drain_flush_bound.py` (create — consumption pins,
  not a formula)

**Interfaces:**
- Consumes: WS2 `graph.get_max_bound_region_depth()` +
  `derive_escalation_fixpoint_bound` (`core/dag/bound_regions.py`) +
  `PipelineConfig.escalation_fixpoint_bound`; WS2 `GroupBinding.member_roster`; Task 2
  `authenticate_adoption_loss` + `record_group_loss`; Task 7 replay.
- Produces:
  `BarrierIntakeCoordinator.note_group_failed(*, closer_name: str, group_id: str, reason: str) -> None`.
  No fixpoint names — the ONE formula and its plumbing are WS2's.

Semantics implemented here, restated from spec §6.3 as ratified by the 2026-08-22
synthesis:

1. Escalation is leader-only, computed at barrier intake, staged in the adoption
   transaction under the adoption-context guard. A closer FAIL verdict parks an
   in-memory note; each intake pass evaluates parked notes.
2. **Verdicts wait for settlement:** the note escalates only when the failed group's
   roster is durably closed — every minted member has a terminal `token_outcome` or a
   `group_losses` row. Until then the note stays parked (mid-run latency is
   one-pass-per-drain-cycle; the EOF fixpoint guarantees completion).
3. The escalated loss names the ENCLOSING bound frame, resolved from any member
   token's durable `token_lineage_frames` rows: drop the failed group's own frame,
   walk the remainder innermost-first to the first bound frame. No enclosing bound
   frame ⇒ the note is discarded (outermost: today's behaviour verbatim, ruling 19).
   A `best_effort` ENCLOSING closer still receives the loss (settlement propagation
   is policy-independent); absorption is that closer's own verdict, not a staging
   skip.
4. **Escalation loss `token_id` (ratified):** the FAILED group's opener token, read
   from `group_records.opener_token_id`. `group_records` rows exist for BOTH kinds —
   WS1a mints FORK and EXPAND records alike — so there is no fallback path;
   deterministic across re-drives, which the ledger's same-key-different-token Tier-1
   check requires.
5. **Escalation loss `reason` (ratified):** the bare category token `"group_failed"`
   (stays within the categorical vocabulary; the failing group's own detail lives in
   its members' outcomes). `recorded_by` is stamped at write time from the fenced
   leader identity.
6. **Fixpoint bound (consumed, not authored):** the ONE formula is WS2's
   `derive_escalation_fixpoint_bound(depth) = 1_000 + 8 * depth`
   (`core/dag/bound_regions.py`), derived at build from
   `graph.get_max_bound_region_depth()` and threaded to the EOF flush loop as
   `PipelineConfig.escalation_fixpoint_bound`. Depth-5 = 1_040; an
   override-depth-1000 unwind = 9_000 — never a constant collision (spec §6.3). WS3
   defines no `derive_end_of_input_flush_bound` and no second formula anywhere; this
   task only PINS the consumption (no competing formula exists; the loop honors the
   derived value, not the constant).

- [ ] **Step 1: Write the failing tests**

```python
# tests/unit/engine/test_escalation_intake.py
"""Spec §6.3: intake-only escalation. One loss against the ENCLOSING bound
frame, staged in the adoption transaction, authenticated against the durable
roster authority; verdicts wait for settlement; best_effort enclosing
closers still receive the loss."""


def test_fail_verdict_with_settled_roster_stages_enclosing_loss(nested_intake_harness):
    """Inner require_all coalesce fails; all inner members settled durably.
    ONE group_losses row appears against the OUTER frame — (outer closer,
    outer group, outer member) — with reason 'group_failed' and the inner
    group's opener token as token_id. Kills the
    escalation-against-failing-frame mutant (asserting the OUTER group_id,
    not the failed group's)."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    ledger = h.group_losses()
    escalated = [l for l in ledger if l.reason == "group_failed"]
    assert len(escalated) == 1
    (loss,) = escalated
    assert loss.group_id == h.outer_group_id           # enclosing, NOT fg_inner
    assert loss.member_key == h.outer_member_key
    assert loss.closer_name == h.outer_closer_name
    assert loss.token_id == h.inner_opener_token_id


def test_fail_verdict_with_unsettled_roster_defers(nested_intake_harness):
    """A member still live (no terminal, no loss): the verdict parks; no
    escalated row this pass. It stages on a later pass once the member
    settles."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_leaving_one_member_live()
    h.coordinator.run_barrier_intake(h.ctx)
    assert [l for l in h.group_losses() if l.reason == "group_failed"] == []
    h.settle_remaining_member()
    h.coordinator.run_barrier_intake(h.ctx)
    assert len([l for l in h.group_losses() if l.reason == "group_failed"]) == 1


def test_outermost_fail_verdict_discards_note(nested_intake_harness):
    """No enclosing bound frame: today's behaviour verbatim (ruling 19) —
    nothing escalated, members already terminalized FAILURE/UNROUTED."""
    h = nested_intake_harness(nesting=False)
    h.fail_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    assert [l for l in h.group_losses() if l.reason == "group_failed"] == []


def test_best_effort_enclosing_closer_still_receives_the_loss(nested_intake_harness):
    """Settlement propagation is policy-independent (§6.4): the loss is
    staged and notified; the enclosing best_effort closer absorbs it (its
    own group does NOT fail)."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="best_effort")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    assert len([l for l in h.group_losses() if l.reason == "group_failed"]) == 1
    assert h.outer_group_failed() is False


def test_escalation_staging_is_idempotent_across_redrive(nested_intake_harness):
    """Escalated rows are materialized derivations, idempotent on the natural
    key, re-derivable at takeover (§6.3 item 3)."""
    h = nested_intake_harness(inner_policy="require_all", outer_policy="require_all")
    h.fail_inner_group_and_settle_all_members()
    h.coordinator.run_barrier_intake(h.ctx)
    h.coordinator.run_barrier_intake(h.ctx)   # re-derive
    assert len([l for l in h.group_losses() if l.reason == "group_failed"]) == 1
```

```python
# tests/unit/engine/test_leader_drain_flush_bound.py
"""ONE fixpoint formula (2026-08-22 synthesis): WS2 owns
derive_escalation_fixpoint_bound(depth) = 1_000 + 8 * depth in
core/dag/bound_regions.py, and leader_drain iterates
PipelineConfig.escalation_fixpoint_bound (derived at build from
graph.get_max_bound_region_depth()). This suite pins WS3's CONSUMPTION:
no competing formula, and the loop honors the derived value."""

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound
from elspeth.engine.orchestrator import leader_drain


def test_leader_drain_owns_no_competing_formula():
    assert not hasattr(leader_drain, "derive_end_of_input_flush_bound")


def test_depth0_derived_bound_equals_the_historical_constant():
    # Depth-0 behaviour is byte-identical to the pre-campaign loop.
    assert (
        derive_escalation_fixpoint_bound(0)
        == leader_drain.MAX_END_OF_INPUT_FLUSH_ITERATIONS
        == 1_000
    )


def test_depth5_derived_bound_is_1040():
    # The value Task 10's acceptance run flushes inside (consumed-contract pin).
    assert derive_escalation_fixpoint_bound(5) == 1_040


def test_flush_loop_iterates_the_configured_bound_not_the_constant(non_converging_flush_harness):
    """Drive a never-converging flush with escalation_fixpoint_bound=3: the
    OrchestrationInvariantError names 3 — the loop reads the config field,
    not MAX_END_OF_INPUT_FLUSH_ITERATIONS. Build the harness from
    tests/e2e/recovery/test_multi_worker_leader_finalize.py's clock-scripted
    finalize fixtures (read it first); a durable BLOCKED barrier hold that
    never releases is the non-convergence driver."""
    with pytest.raises(OrchestrationInvariantError, match=r"within 3 intake/flush rounds"):
        non_converging_flush_harness.run(escalation_fixpoint_bound=3)
```

- [ ] **Step 2: Run them**

Run: `pytest tests/unit/engine/test_escalation_intake.py tests/unit/engine/test_leader_drain_flush_bound.py -v`
Expected: FAIL — names missing.

- [ ] **Step 3: Implement**

`barrier_coordination.py` — add to `BarrierIntakeCoordinator`:

```python
    def note_group_failed(self, *, closer_name: str, group_id: str, reason: str) -> None:
        """Park a closer FAIL verdict for intake-time escalation (spec §6.3).

        Idempotent in-memory park; re-derivable at takeover because replaying
        the durable losses re-fires the closer's FAIL verdict, which re-parks
        the note, and escalation staging is idempotent on the ledger's
        natural key."""
        self._failed_group_notes[(closer_name, group_id)] = reason

    def _stage_pending_escalations(self) -> None:
        """Leader-only escalation pass, run once per intake iteration AFTER
        durable-loss replay: for each parked FAIL verdict whose roster has
        durably closed, stage ONE loss against the enclosing bound frame in
        the adoption transaction, authenticated against the roster
        authority, then notify the enclosing closer via the normal replay
        machinery on the next pass."""
        for (closer_name, group_id), reason in list(self._failed_group_notes.items()):
            if not self._group_roster_settled(closer_name=closer_name, group_id=group_id):
                continue
            enclosing = self._enclosing_bound_frame(group_id)
            if enclosing is None:
                del self._failed_group_notes[(closer_name, group_id)]
                continue  # outermost: declared terminal handling already ran (ruling 19)
            frame, binding = enclosing
            spec = GroupLossSpec(
                closer_name=binding.closer_name,
                group_id=frame.group_id,
                member_key=frame.member_key,
                token_id=self._opener_token_id_for_group(group_id),
                reason="group_failed",
            )
            self._stage_escalation_loss(spec, frame_kind=frame.kind, binding=binding)
            del self._failed_group_notes[(closer_name, group_id)]
```

Supporting pieces, each small and unit-covered through the harness:

- `_group_roster_settled(*, closer_name, group_id) -> bool`: minted = the binding's
  `member_roster` (FORK — the config roster authority) or `group_records.member_count` cross-checked against
  `DISTINCT member_key` in `token_lineage_frames` (EXPAND, §5). A member is settled iff
  `group_losses` has its `(run, closer, group, member)` row OR the member's token (the
  token whose frames row matches `(group_id, member_key)`; for FORK, the branch child)
  has a completed `token_outcomes` row. Implement as one read-facade query beside
  `has_released_row_for_node`; correctness over elegance — this is intake, not the hot
  path.
- `_enclosing_bound_frame(group_id) -> tuple[LineageFrame, GroupBinding] | None`: read
  any member token's `token_lineage_frames` rows ordered by depth; truncate at the
  failed group's own frame (exclusive); walk the remaining prefix from deepest to
  outermost; return the first frame with a binding.
- `_opener_token_id_for_group(group_id) -> str`: read `group_records.opener_token_id`
  — rows exist for BOTH kinds (WS1a mints FORK and EXPAND records; ratified). Raise
  `AuditIntegrityError` when no record exists (the ledger references a group the
  audit trail never minted). Do NOT port a FORK `token_parents` fallback — under the
  landed behaviour it is dead code, and the nested harness's FORK group pins the
  `group_records` read directly
  (`test_fail_verdict_with_settled_roster_stages_enclosing_loss` asserts
  `loss.token_id == h.inner_opener_token_id`).
- `_stage_escalation_loss(spec, *, frame_kind, binding)`: inside the fenced adoption
  transaction (`fenced_leader_transaction`, `verb="stage_escalation_loss"` — reuse the
  adoption verb plumbing that `adopt_group_losses` uses), call
  `authenticate_adoption_loss(conn, run_id=..., spec=spec, frame_kind=frame_kind, declared_roster=binding.member_roster if frame_kind is FrameKind.FORK else None)`
  then `record_group_loss(conn, run_id=..., spec=spec, recorded_by=self._scheduler_lease_owner, now=...)`.
  The staged row is picked up by the NEXT intake pass's `_replay_group_losses` (which
  notifies the enclosing executor) — that is the one-pass-per-drain-cycle latency the
  spec accepts.

Wire `_stage_pending_escalations()` into `run_barrier_intake`'s per-iteration sequence
immediately after the durable-loss replay step (leader-only guard identical to the
replay's). Call `note_group_failed` from every closer FAIL verdict site: the
`outcome.failure_reason` arms in `processor.py` (`_notify_closer_of_loss` coalesce and
row_union arms) and `barrier_coordination.py` (intake arrival-failure arm `:487+`,
replay failure arms). The failed group's `group_id` at those sites is the consumed
tokens' innermost frame's `group_id` (all consumed members share it —
`consumed_tokens[0].lineage_path[-1].group_id`; for the zero-arrival must-fail the
notifying loss's own `group_id` is in hand).

`leader_drain.py`: NOTHING to author here. WS2 already replaced the constant loop —
`flush_iteration_bound = config.escalation_fixpoint_bound`,
`for _ in range(flush_iteration_bound)`, and a non-convergence raise naming the
derived value — with `PipelineConfig.escalation_fixpoint_bound` set at the
graph-in-hand construction sites to
`derive_escalation_fixpoint_bound(graph.get_max_bound_region_depth())`.
`MAX_END_OF_INPUT_FLUSH_ITERATIONS = 1_000` survives only as the depth-0 default
anchor. VERIFY: `git grep -n "MAX_END_OF_INPUT_FLUSH_ITERATIONS\|derive_end_of_input_flush_bound" src/`
must show the constant only as the anchor/default and the WS3-era name not at all —
any bare-constant loop or competing formula is WS2 drift, fixed in WS2's slice, never
by growing a formula here.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_escalation_intake.py tests/unit/engine/test_leader_drain_flush_bound.py tests/e2e/recovery/test_multi_worker_leader_finalize.py -v`
Expected: PASS (the finalize suite drives the real EOF flush loop — proof the new
escalation pass does not alter behaviour at depth 0, where WS2's derived bound
equals the historical constant).

- [ ] **Step 5: Commit + slice boundary**

```bash
git add src/elspeth/engine/barrier_coordination.py src/elspeth/engine/processor.py \
        tests/unit/engine/test_escalation_intake.py tests/unit/engine/test_leader_drain_flush_bound.py
git commit -m "feat(engine): intake-only escalation with settlement-gated verdicts on WS2's derived fixpoint bound"
```

Full `pytest tests/` + trust-tier corpus diff + wardline.

---

### Task 9: Bypass sites 3–4 — deferral-when-bound for row_union and coalesce failure paths

**Files:**
- Modify: `src/elspeth/engine/processor.py` — the row_union in-line group-failure body
  (Task 5 carried it into `_notify_closer_of_loss`'s ROW_UNION arm; originally
  `:3028-3103`) and the coalesce `outcome.failure_reason` arm (originally
  `:3263-3290`, now calling Task 6's helper)
- Test: `tests/integration/pipeline/test_nested_group_settlement.py` (create)

**Interfaces:**
- Consumes: Task 8's `note_group_failed`; WS2 nesting (fork-in-fork buildable).
- Produces: no new names — behaviour: bound-enclosed closer failures defer their
  verdict to escalation; unbound keep today's behaviour verbatim (ruling 19).

The mechanism is already in place after Tasks 6+8: the FAIL arms terminalize members
and call `note_group_failed`; escalation fires only when an enclosing bound frame
exists. What this task adds is the END-TO-END pin through real pipelines, because the
unit harnesses cannot prove the composition.

- [ ] **Step 1: Write the failing tests**

```python
# tests/integration/pipeline/test_nested_group_settlement.py
"""§6.1 items 3-4 end-to-end: a bound-enclosed closer failure defers its
verdict to the enclosing closer (one escalated loss, one drain cycle after
the inner roster settles); an outermost closer failure behaves exactly as
today. Built on the real Orchestrator over make_landscape_db, following
tests/integration/pipeline/test_barrier_intake_dispositions.py's builders."""


def test_unbound_coalesce_failure_verbatim_today(run_pipeline):
    """Outermost require_all coalesce with a lost branch: members terminalize
    FAILURE/UNROUTED, run completes, ZERO group_failed ledger rows. Pins
    ruling 19's verbatim half against the corpus fork-coalesce-policies
    expectations."""
    result = run_pipeline("fork_coalesce_require_all_lost_branch")
    assert result.failure_outcomes_for_group("merge_paths") == result.consumed_member_ids
    assert result.escalated_losses() == []


def test_nested_inner_failure_settles_outer_member(run_pipeline):
    """fork_outer -> per-branch fork_inner -> merge_inner (require_all)
    -> merge_outer (require_all). One inner branch quarantined:
    - inner loss row (closer=merge_inner, group=fg_inner, member=<branch>);
    - after the inner roster settles, ONE escalated row
      (closer=merge_outer, group=fg_outer, member=<outer branch>, reason=group_failed);
    - merge_outer renders ITS verdict from that settled member (require_all
      => outer group fails, its members terminalize);
    - every token reaches a terminal outcome (zero-write completeness)."""
    result = run_pipeline("nested_fork_coalesce_inner_quarantine")
    inner = [l for l in result.ledger() if l.closer_name == "merge_inner"]
    escalated = [l for l in result.ledger() if l.reason == "group_failed"]
    assert len(inner) == 1 and len(escalated) == 1
    assert escalated[0].closer_name == "merge_outer"
    assert result.non_terminal_tokens() == []


def test_row_union_failure_inside_bound_region_defers(run_pipeline):
    """A row_union closing a fork inside an outer bound region: v1
    fail-closed union failure no longer ends the story in-line — the outer
    member settles via escalation. With NO outer region the in-line
    behaviour is verbatim (second pipeline)."""
    nested = run_pipeline("nested_row_union_lost_branch")
    assert [l.closer_name for l in nested.ledger() if l.reason == "group_failed"] == [nested.outer_closer]
    flat = run_pipeline("row_union_lost_branch_top_level")
    assert flat.escalated_losses() == []
```

The two nested settings fixtures are authored here (YAML under the test's data dir or
built in code via the shared pipeline builders — follow whichever style
`test_barrier_intake_dispositions.py` uses; they require WS2's nested-region
validation to accept fork-in-fork). The result-probe helpers (`ledger()`,
`non_terminal_tokens()`, …) are thin SQL reads over `group_losses` /
`token_outcomes` / `token_work_items` — write them in this module.

- [ ] **Step 2: Run them**

Run: `pytest tests/integration/pipeline/test_nested_group_settlement.py -v`
Expected: FAIL (fixtures/probes missing first; then behaviour).

- [ ] **Step 3: Implement the residual deferral wiring**

Audit the two arms end-to-end against the tests: the row_union arm must call
`note_group_failed(closer_name=..., group_id=<union's FORK group>, reason=row_union_outcome.failure_reason)`
after terminalizing consumed members, and the coalesce arm likewise. The union's
failure reasons are VERIFIED categorical — `row_union_executor.py` produces exactly
these bare tokens: `row_union_branch_lost`, `row_union_timeout`,
`row_union_incomplete_at_flush`, `late_arrival_after_release`, and
`row_union_group_failed` (the conservative point-read closure). All fit the ledger's
64-char category budget; pass them through unchanged — never prose, never a mapping
layer. Delete any remaining in-line assumption that a
group failure is final (grep the arms for comments claiming so; the deleted
`:3121-3155` docstring's siblings). No enclosing frame ⇒ `note_group_failed` is parked
then discarded by Task 8's pass — no code branch needed at the call sites; the
UNIFORMITY is the design.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/integration/pipeline/test_nested_group_settlement.py tests/integration/pipeline/test_barrier_intake_dispositions.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/processor.py tests/integration/pipeline/test_nested_group_settlement.py
git commit -m "feat(engine): bound-enclosed closer failures defer verdicts to the enclosing closer"
```

---

### Task 10: Depth-5 full-unwrap-to-quarantine acceptance test **[SLICE]**

**Files:**
- Test: `tests/integration/pipeline/test_depth5_group_unwrap.py` (create)

**Interfaces:**
- Consumes: everything above; WS2's depth cap (`GraphValidationError` at depth 6
  without override, config-overridable); WS2's `derive_escalation_fixpoint_bound`.
- Produces: the spec §6.3 acceptance evidence — no new runtime names — plus the
  importable topology builder `_nested_settings(depth: int) -> dict` (exported in
  this plan's Interfaces block).

This task stays the LIVE acceptance run. The crash+resume variant of the same
scenario lives in the WS5/WS6 plan
(`docs/superpowers/plans/2026-08-21-unified-lineage-ws5-ws6-resume-observability.md`,
which is adding it): it imports and reuses `_nested_settings(5)` from this module —
so `_nested_settings` and `build_settings_document` must stay plain module-level
functions (no fixture arguments, no test-local state).

- [ ] **Step 1: Write the failing test**

```python
# tests/integration/pipeline/test_depth5_group_unwrap.py
"""Spec §6.3 acceptance scenario at the SUPPORTED depth guarantee: five
nested all-require_all bound regions; a single token failing in the fifth
layer unwraps level by level — each verdict escalating one frame outward —
until the outermost group's declared terminal handling fires. Correctness is
depth-independent by design; 5 is the builder-enforced supported ceiling.

_nested_settings / build_settings_document are IMPORTED by the WS5/WS6
crash+resume variant — keep them plain module-level functions."""

from elspeth.core.dag.bound_regions import derive_escalation_fixpoint_bound

DEPTH = 5


def _nested_settings(depth: int) -> dict:
    """fork_1 -> ... -> fork_N -> quarantining transform on one innermost
    branch -> merge_N -> ... -> merge_1 -> sink. Each fork's whole roster
    closes at its own coalesce (ruling 23), regions well-nested (§7 rule 3),
    every policy require_all. Two branches per level ('go_k' recursing
    deeper, 'ok_k' a passthrough) keeps the tree small: the failure sits on
    the innermost 'go' line; every 'ok' sibling is a survivor.

    Renders the settings DOCUMENT (dict) the run_settings fixture feeds to
    the normal settings loader — mirror the YAML shape of
    examples/fork_coalesce/settings.yaml, which this builder generalizes."""
    gates = []
    coalesces = []
    transforms = []
    for k in range(1, depth + 1):
        inner_next = f"fork_{k + 1}" if k < depth else "poison"
        gates.append(
            {
                "name": f"fork_{k}",
                "plugin": "fork",
                "fork_to": [f"go_{k}", f"ok_{k}"],
                "branch_routes": {
                    f"go_{k}": inner_next,
                    f"ok_{k}": f"tag_ok_{k}",
                },
            }
        )
        transforms.append(
            {"name": f"tag_ok_{k}", "plugin": "passthrough", "on_success": f"merge_{k}"}
        )
        coalesces.append(
            {
                "name": f"merge_{k}",
                "branches": [f"go_{k}", f"ok_{k}"],
                "policy": "require_all",
                "merge": "union",
                "on_success": f"merge_{k - 1}" if k > 1 else "results",
            }
        )
    transforms.append(
        # dag_corpus_always_error-style failing transform: quarantines the
        # innermost go-line token; its on_error discard is the loss producer.
        {"name": "poison", "plugin": "always_error", "on_error": "discard", "on_success": f"merge_{depth}"}
    )
    return build_settings_document(gates=gates, transforms=transforms, coalesces=coalesces, sink="results")


# build_settings_document is a ~30-line helper defined at the top of THIS
# module: it wraps the pieces in the full settings mapping (source: a 2-row
# inline CSV, sinks: {results, plus the discard route}) with key names copied
# from examples/fork_coalesce/settings.yaml — read that file and
# tests/fixtures/dag_scenario_corpus/v1/fork-coalesce-policies/require-all-lost-c.yaml
# first and mirror their exact vocabulary (fork_to / branches / policy /
# on_error), substituting the real corpus plugin names for the placeholders
# 'passthrough'/'always_error' above (dag_corpus_branch_loss /
# dag_corpus_always_error are the loss producers the corpus already ships).


def test_depth5_single_failure_unwraps_to_outermost_quarantine(run_settings):
    result = run_settings(_nested_settings(DEPTH))
    ledger = result.ledger()
    # One primary loss at layer 5, one escalated loss per enclosing layer 4..1.
    assert len([l for l in ledger if l.reason == "quarantined"]) == 1
    escalated = [l for l in ledger if l.reason == "group_failed"]
    assert len(escalated) == DEPTH - 1
    assert [l.closer_name for l in sorted(escalated, key=result.escalation_order)] == [
        "merge_4", "merge_3", "merge_2", "merge_1",
    ]
    # Survivors ran to completion (no cancellation v1) and every token is
    # terminal — the zero-write direction checked by set equality.
    assert result.non_terminal_tokens() == []
    # Outermost declared terminal handling fired: the source row is flagged
    # failed at the run boundary, run itself completes.
    assert result.run_completed()
    assert result.source_row_failed()


def test_depth6_is_rejected_at_build_without_override(build_graph):
    with pytest.raises(GraphValidationError, match="bound-region nesting"):
        build_graph(_nested_settings(6))


def test_flush_bound_scales_with_depth(run_settings):
    """The EOF fixpoint at depth 5 completes well inside WS2's derived bound
    (derive_escalation_fixpoint_bound(5) == 1_040), and the bound in force is
    the derived value, not the constant."""
    result = run_settings(_nested_settings(DEPTH))
    assert result.flush_iterations_used() < derive_escalation_fixpoint_bound(DEPTH)
```

(`escalation_order` sorts by `recorded_at, loss_id` — the ledger's replay order;
`flush_iterations_used` needs a counter probe — patch
`leader_drain.run_end_of_input_barrier_flush`'s loop via a monkeypatched iteration
callback or count `scheduler_events` intake markers; choose whichever the finalize
suite's clock-scripting precedent supports and document it in the module docstring.)

- [ ] **Step 2: Run it**

Run: `pytest tests/integration/pipeline/test_depth5_group_unwrap.py -v`
Expected: FAIL until the composed machinery holds; this test is pure acceptance — if
it fails, the defect is in Tasks 5–9 (or WS2's nesting), never patched around here.

- [ ] **Step 3: Make it pass, commit, slice boundary**

Fix whatever it exposes at the owning task's site (systematic-debugging: reproduce,
localize, fix at the cause).

```bash
git add tests/integration/pipeline/test_depth5_group_unwrap.py
git commit -m "test(engine): depth-5 full-unwrap-to-quarantine acceptance evidence"
```

Full `pytest tests/` + trust-tier corpus diff + wardline. This closes WS3's behaviour
surface; adjudicate any remaining corpus-manifest count rotations now (Task 5 Step 5's
protocol).

---

### Task 11: Frame-guard and settlement mutation matrix (`-n 0`)

**Files:**
- Test: `tests/unit/engine/test_settlement_mutation_matrix.md` — NO new test file: this
  task RUNS the enumerated mutants against the existing killer tests and records the
  ledger below in the commit message. (Fail-closed-analyzer doctrine: corpus agreement
  cannot validate a guard; adversarial mutants can.)

**Interfaces:** none — verification only.

Procedure per mutant: apply the exact edit to the working tree, run the named killer
selection with `-n 0`, CONFIRM RED (the mutant is killed), then
`git checkout -- <file>` to revert. If any mutant survives, STOP: write the missing
killer test in the owning task's test file, commit it, and re-run the matrix. Nothing
from this task is ever committed except added killer tests.

- [ ] **Mutant 1 — guard on `group_id` alone**: in
  `scheduler_drain.take_claim_group_losses` change
  `key = (spec.group_id, spec.member_key)` to `key = (spec.group_id,)` and
  `claimed_frames = {(f.group_id, f.member_key) ...}` to `{(f.group_id,) ...}`.
  Run: `pytest tests/unit/engine/test_group_loss_claim_guard.py -n 0 -v`
  Expected killer: `test_guard_rejects_group_id_match_with_wrong_member_key` FAILS
  under the mutant (i.e. the suite goes red). Revert.
- [ ] **Mutant 2 — guard on `member_key` alone**: same edit with `member_key` only.
  Expected killer: `test_guard_rejects_member_key_match_with_wrong_group_id`. Revert.
- [ ] **Mutant 3 — walk stops at innermost instead of first-bound**: in
  `processor._settle_member_losses` replace the loop with
  `frame = current_token.lineage_path[-1]; binding = self._group_bindings.binding_for(frame); if binding is None: return []`
  (no walk).
  Run: `pytest tests/unit/engine/test_settle_member_seam.py -n 0 -v`
  Expected killer: `test_walk_stages_loss_for_first_bound_frame_skipping_inert`. Revert.
- [ ] **Mutant 4 — outermost-first walk**: change `reversed(current_token.lineage_path)`
  to `current_token.lineage_path`.
  Expected killer: `test_walk_is_innermost_first_not_outermost_first`. Revert.
- [ ] **Mutant 5 — escalation against the failing rather than enclosing frame**: in
  `barrier_coordination._stage_pending_escalations` build the spec from the FAILED
  group (`group_id=group_id`, `closer_name=closer_name`, member from its own frame)
  instead of `enclosing`.
  Run: `pytest tests/unit/engine/test_escalation_intake.py -n 0 -v`
  Expected killer: `test_fail_verdict_with_settled_roster_stages_enclosing_loss`
  (asserts the OUTER group_id). Revert.
- [ ] **Mutant 6 — CAS fence removed**: in `group_losses.adopt_group_losses` delete the
  `.where(...adopted_epoch.is_(None))` predicate.
  Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py tests/e2e/recovery/test_suspended_winner_fences.py -k group_losses -n 0 -v`
  Expected killer: the adopt-idempotency assertions (rowcount of already-adopted rows)
  and the fence-matrix arm. If neither reddens, ADD a killer to the unit suite
  asserting a second `adopt_group_losses` under a NEW epoch marks 0 rows. Revert.
- [ ] **Mutant 7 — restore filtered by `adopted_epoch`**: in
  `group_losses.GroupLossRepository.list_group_losses` add
  `.where(group_losses_table.c.adopted_epoch.is_(None))`.
  Run: `pytest tests/unit/core/landscape/test_scheduler_repository_group_losses.py::test_takeover_read_returns_full_table_regardless_of_adopted_epoch tests/unit/engine/test_barrier_coordination.py -k full_table -n 0 -v`
  Expected killer: both named tests. Revert.
- [ ] **Record the ledger**: commit any killer tests added (by pathspec, into their
  owning test files) with message
  `test(engine): close settlement mutation-matrix survivors` and paste the
  mutant→killer table into the commit body. Confirm working tree clean
  (`git status` — no surviving mutant edits).

---

## Self-review checklist (run before handing the plan's work back)

1. **Spec §6 coverage:** §6.1 seam + bypasses 1–4 (Tasks 5, 6, 9; site 5 is WS2's
   ruling-25 build rejection — only the uniform seam call lands here), §6.2 spec/guard/
   ledger/staging/restore (Tasks 1–4, 7), §6.3 escalation items 1–4 + depth-5
   acceptance (Tasks 8, 10), §6.4 policy semantics pinned where WS3 owns them
   (best_effort absorption Task 8; `scope_group_failed`/`all_members_lost`/
   `empty_expansion` vocabulary is WS6's; roster accounting internals are WS4's).
2. **No dual representation:** `BranchLossSpec`, `branch_losses.py`,
   `take_claim_branch_loss`, `_notify_barrier_of_lost_branch`,
   `_notify_coalesce_of_lost_branch`, `_notify_row_union_of_lost_branch`,
   `_row_union_group_released`, and the `coalesce_branch_losses` table are DELETED —
   `git grep -l "BranchLossSpec\|coalesce_branch_losses\|_notify_barrier_of_lost_branch"`
   must return nothing under `src/` at Task 10's slice.
3. **Zero-write completeness** asserted by set equality wherever a terminal writer
   moved (Tasks 6, 9, 10) — the unique index only self-detects the double-write
   direction.
4. **One fixpoint formula:** `git grep -n "derive_end_of_input_flush_bound" src/ tests/`
   returns nothing; the only formula in the tree is WS2's
   `derive_escalation_fixpoint_bound` and the EOF loop iterates
   `PipelineConfig.escalation_fixpoint_bound`.

## Open Questions

None. The 2026-08-22 synthesis resolved everything this plan previously carried
open: the WS2 plan is published
(`2026-08-21-unified-lineage-ws2-config-validation.md`; its canon names are repeated
in this plan's header and checked by the pre-flight step); `group_records` rows are
minted for BOTH kinds, so Task 8's opener resolution has no FORK fallback; the
escalation reason token `"group_failed"`, opener-token `token_id`, and
recorded_by-at-write-time are ratified; and the row_union failure reasons are
verified bare categorical tokens (named in Task 9). The one campaign-level open item
— the `examples/row_union_ab_experiment/settings_screened.yaml` replacement story —
is a maintainer pedagogy call tracked outside this plan and gates nothing here.
