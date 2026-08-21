# WS4 — Collector Executor + Pending-State Re-keying Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build the EXPAND-group closer (the collector executor) and move coalesce/row_union
bookkeeping onto the unified group-lineage rosters, including the four-surface
pending-state re-keying from `(coalesce_name, row_id)` to `(coalesce_name, fork_group_id)`.

**Architecture:** A new `CollectorExecutor` (`engine/executors/collector.py`) closes bound
EXPAND groups: per-group buffers, roster accounting against `group_records` +
`token_lineage_frames`, CAS-fenced idempotent arrivals, `end_of_group`-only flush ordered
by the opener's `token_parents.ordinal`, and empty-group / all-members-lost closes that
never invoke the plugin. Coalesce and row_union keep their merge/union logic and
plugin-visible behaviour byte-identical; only their group *keying* moves onto
`fork_group_id` so that sibling EXPAND members forking into the same coalesce node (legal
under spec §7 rule 5) stop colliding on shared `row_id`.

**Tech Stack:** Python 3.12+, pydantic v2 (config models), SQLAlchemy Core (landscape),
pytest (+hypothesis for the property guards), dataclasses (frozen/slots house style).

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md
(rev 3.2 — rulings 1–28 final; §5 is this workstream's section; §4.2 minting, §4.3
persistence, §6.4 policy semantics, §11 sequencing)

## Global Constraints

- **Shared checkout:** stage by explicit pathspec ONLY (`git add <exact paths>`); never
  `git add -A`/`-u`; a sibling agent's files may be dirty in the same tree. Never commit
  `src/elspeth/web/composer/state.py` or `tests/unit/web/composer/test_state.py` (the
  maintainer is committing them).
- **Hooks:** never bypass pre-commit hooks except under the documented
  `--no-verify`-with-end-of-slice-reconciliation grant; `git stash` is blocked — use
  commits.
- **Full suite at slice boundaries:** run the full `pytest tests/` (CI-equivalent) at
  every slice boundary — whole-tree AST gates (attribute-contracts, masquerade,
  runtime-rejection-parity, wire-shape/serialisation pins) miss scoped runs. Record
  `git rev-parse HEAD` before AND after any long run; if HEAD moved, re-run rather than
  diagnose.
- **Trust-tier corpus diff before/after each slice, add nothing:**
  `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`
  — the gate exits 1 with a large corpus BY DESIGN; compare the finding corpus before vs
  after your slice (COUNT findings, never tail them) and add zero new findings.
- **Wardline gate (from AGENTS.md):**
  `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only`
  — exit 0 = clean and non-inert; fix findings at the boundary, not the sink.
- **No hand-edited judge signatures**, ever. Do not stage a judge-signature bundle across
  this campaign (churn invalidates bundles; 0.7.2 signing sequences after the campaign).
- **Depth cap / fixpoint bound rules (spec §6.3):** the supported guarantee is 5 layers
  of bound-region nesting, enforced fail-closed by the BUILDER (`GraphValidationError`),
  config-overridable. The escalation fixpoint's non-convergence bound is WS2's
  `derive_escalation_fixpoint_bound(depth) = 1000 + 8 * depth`
  (`core/dag/bound_regions.py`), threaded as `PipelineConfig.escalation_fixpoint_bound`
  and already iterated by `leader_drain` (WS2 Task 5; WS3 pins consumption). WS4 must
  not hardcode any depth-derived bound and touches `leader_drain` only for Task 6's
  collector early-return guard.
- **Untouched files (spec §5/§9 row 4):** `src/elspeth/engine/executors/aggregation.py`
  and `src/elspeth/engine/triggers.py` end this plan with an EMPTY diff. Verify at the
  final task.
- **No `getattr`/`hasattr` anywhere in new code** (masquerade gate scans the whole repo,
  tests included). Owned types get direct attribute access; optional facts become real
  fields with defaults.
- **Standing procedures:** docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md
  §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle
  sequencing, and the WS1 STOP rule.

## Preconditions (verify before Task 1)

This plan is written against the POST-WS1 tree (spec §11 sequencing: WS0 → WS1 → WS2 →
(WS3 ∥ WS4)). Verify the WS1a interfaces landed before starting; if any grep below comes
back empty, STOP and surface to the coordinator — do not draft workarounds.

```bash
git grep -n "class LineageFrame" src/elspeth/contracts/          # expect contracts (identity.py or enums-adjacent)
git grep -n "class FrameKind" src/elspeth/contracts/enums.py     # FrameKind.FORK | FrameKind.EXPAND
git grep -n "lineage_path" src/elspeth/contracts/identity.py     # TokenInfo.lineage_path field + derived accessors
git grep -n "token_lineage_frames\|group_records" src/elspeth/core/landscape/schema.py
git grep -n "lineage_path" src/elspeth/contracts/scheduler.py    # TokenWorkItem.lineage_path
git grep -n "pop_closer_frame" src/elspeth/contracts/identity.py # WS1a Task 1 shared pop helper
git grep -n "class CollectorSettings\|class ScopeSettings" src/elspeth/core/config.py  # WS2 Task 2 models
```

Canonical contracts consumed from the WS1a plan
(`docs/superpowers/plans/2026-08-21-unified-lineage-ws1a-model-core.md` — its Interfaces
block carries exactly these signatures; repeated verbatim here so this plan is
self-contained; WS1a task numbers per the 2026-08-22 synthesis canon):

- **WS1a Task 1** (`contracts/identity.py`): `LineageFrame(kind: FrameKind, group_id: str,
  member_key: str)` frozen slots dataclass; `FrameKind.FORK | FrameKind.EXPAND` in
  `contracts/enums.py`; helpers `innermost_fork_frame` / `innermost_expand_frame`, thin
  wrappers `path_branch_name` / `path_fork_group_id` / `path_expand_group_id`
  (`tuple[LineageFrame, ...] -> str | None`), and
  `pop_closer_frame(path: tuple[LineageFrame, ...], *, kind: FrameKind, group_id: str)
  -> tuple[LineageFrame, ...]` — raises `OrchestrationInvariantError` unless `path[-1]`
  matches `kind`+`group_id` exactly. **Every pop in this plan calls `pop_closer_frame`;
  no inline `path[:-1]` release pops.**
- **WS1a Task 2**: `TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()` outermost
  first. Derived accessors (read-only properties): `branch_name` / `fork_group_id`
  (innermost FORK frame), `expand_group_id` (innermost EXPAND frame). `join_group_id`
  leaves `TokenInfo` (WS1a Task 10).
- **WS1a Task 3**: `GroupLossSpec(closer_name, group_id, member_key, token_id, reason)`
  replaces `BranchLossSpec` (`contracts/scheduler.py:70-86`) — the CONTRACT lands in
  WS1a; WS3 builds the ledger machinery around it (below).
- **WS1a Task 4** (schema DDL, epoch 34): table
  `token_lineage_frames(token_id, run_id, depth, kind, group_id, member_key)`
  PK `(token_id, run_id, depth)` INDEX `(run_id, group_id, member_key)`; table
  `group_records(run_id, group_id, kind, opener_token_id, member_count, created_at)`
  PK `(run_id, group_id)`, minted for BOTH kinds — FORK and EXPAND alike (WS1a Task 6
  writers; the FORK roster AUTHORITY stays config, FORK rows are enrichment), empty
  expansions included (`member_count=0` mint gated on `creates_tokens=True`, WS1a
  Task 7); table `group_losses(loss_id PK, run_id FK, closer_name, group_id, member_key,
  token_id, reason, recorded_by, recorded_at, adopted_epoch)` UNIQUE
  `(run_id, closer_name, group_id, member_key)`.
- **WS1a Task 5**: adds `TokenWorkItem.lineage_path`; the `branch_name`/
  `fork_group_id`/`expand_group_id` fields are retired by WS1b's flip (its Tasks 8–9),
  so at WS4's start the landed shape is `lineage_path` + `join_group_id` + the barrier
  binding fields `barrier_key`/`coalesce_node_id`/`coalesce_name`/`row_union_name`;
  `token_from_journal_item` (`payload_codec.py`) reconstructs purely.
- **WS1a Task 6**: the durable frame + `group_records` writers in
  `core/landscape/data_flow/tokens.py` (single frame-write path); **WS1a Task 8**: the
  TokenManager in-memory frame push/strict-pop.

Canonical contracts consumed from the WS3 plan
(`docs/superpowers/plans/2026-08-21-unified-lineage-ws3-settlement.md`) — needed by
Tasks 11–12 ONLY (the `GroupLossSpec` type and `group_losses` DDL are WS1a Tasks 3–4,
above; WS3 supplies the machinery over them):

- The `group_losses` ledger module (`core/landscape/scheduler/group_losses.py`:
  record / list / adopt / takeover replay — WS3 Tasks 2 and 7).
- The settle-member seam (WS3 Task 5) is the sole production caller of the executors'
  loss-notify APIs after integration.

Task dependency order: Tasks 1–7 (collector lane) need WS1a plus WS2 Task 2's landed
config models (spec §11 sequences WS2 before WS4) and are independent of WS3. Tasks 8–10
(coalesce re-key, loss-free surfaces) need WS1a only. Tasks 11–12 need WS3's
`group_losses` ledger machinery — if WS3 has not landed it when you reach Task 11, STOP
at the Task 10 boundary and coordinate (this is the spec's "WS3+WS4 integration, own
line item"). Task 13 is last.

**The behaviour guard for everything coalesce/row_union in this plan** (spec §5: "merge
logic and plugin behaviour UNTOUCHED — the existing coalesce tests are the guard"):

- `tests/unit/engine/test_coalesce_executor.py`, `test_coalesce_policy.py`,
  `test_coalesce_pipeline_row.py`, `test_coalesce_contract_bug.py`,
  `test_journal_restore.py`, `test_row_union_executor.py`
- `tests/property/engine/test_coalesce_properties.py`,
  `test_processor_coalesce_equivalence_properties.py`
- `tests/property/audit/test_fork_coalesce_flow.py`, `test_fork_join_balance.py`
- `tests/integration/pipeline/test_coalesce_process_recovery.py`,
  `test_coalesce_rename_collision.py`, `test_barrier_intake_dispositions.py`,
  `test_row_union_ab_experiment.py`, `test_row_union_branch_cardinality.py`,
  `test_row_union_branch_loss.py`, `test_row_union_identity_branches.py`
- `tests/testcontainer/core/test_barrier_recovery_postgres.py`,
  `test_coalesce_effect_lock_order_postgres.py`,
  `test_coalesce_branch_loss_reason_postgres.py` (run with `-m testcontainer` where Docker
  is available)

These suites must stay green modulo the key shape (`(name, row_id)` →
`(name, fork_group_id)` in constructor kwargs/assertions). Any assertion change beyond the
key shape is a regression — stop and investigate, don't re-pin.

---

### Task 1: Consume-and-verify WS2's landed CollectorSettings / ScopeSettings

**This task authors NOTHING** (2026-08-22 synthesis canon, item 5): the ONE authored
copy of `CollectorSettings`/`ScopeSettings` is **WS2 Task 2**
(`docs/superpowers/plans/2026-08-21-unified-lineage-ws2-config-validation.md`), which
lands before WS4 starts (spec §11 sequencing). This task verifies the landed models
match the contract every later WS4 task builds against, and STOPs on any mismatch —
do NOT patch `core/config.py` from this plan; a mismatch is a WS2 defect to surface
to the coordinator.

**Files:**
- None created or modified. Read-only verification against
  `src/elspeth/core/config.py` and WS2's model suite
  `tests/unit/core/test_config_collectors_scopes.py`.

**Interfaces:**
- Consumes (WS2 Task 2, `src/elspeth/core/config.py`):
  - `CollectorSettings(name: str, plugin: str, input: str, on_success: str, on_error: str | None = None, options: dict[str, Any] = {})`
    — frozen, `extra="forbid"`. `input` and `on_success` are REQUIRED; `on_error`
    defaults to `None`, which means the error route DERIVES from structure per spec §7
    rule 9 (ratified). No `trigger`, no `output_mode` — collectors flush on
    `end_of_group` only and are transform-only (spec §5).
  - `ScopeSettings(name: str, opener: str, closer: str, policy: Literal["require_all", "best_effort"], on_group_failure: Literal["quarantine", "escalate"] = "quarantine")`
    — frozen, `extra="forbid"`. `policy` is REQUIRED with no default (spec §3);
    `on_group_failure` DEFAULTS to `"quarantine"` (ratified — quarantine is the only
    value legal at an outermost group, spec ruling 2).
  - WS2 also owns `ElspethSettings.collectors`/`.scopes`, the binding registry
    (`GroupBindingRegistry.binding_for`), and the composer three-pin — none of it is
    WS4's to touch.
- Produces: a verified premise for Tasks 4/5 (executor registration types) and every
  fixture in this plan. **Every WS4 fixture constructs `CollectorSettings` WITH
  `on_success` — it is a required field** (e.g.
  `CollectorSettings(name="stitch", plugin="recording_stitch", input="pages_in", on_success="assembled_out")`).

- [ ] **Step 1: Verify the models landed**

Run: `git grep -n "class CollectorSettings\|class ScopeSettings" src/elspeth/core/config.py`
Expected: both classes present. Empty output ⇒ WS2 Task 2 has not landed — STOP and
surface to the coordinator.

- [ ] **Step 2: Probe the field contract** (one-shot, not committed):

```python
# .venv/bin/python - <<'EOF'
from pydantic import ValidationError

from elspeth.core.config import CollectorSettings, ScopeSettings

s = CollectorSettings(name="page_stitcher", plugin="stitch_pages", input="pages_in", on_success="assembled_out")
assert s.on_error is None, "on_error must default to None (derive-from-structure, spec §7 rule 9)"
assert s.options == {}
for missing_kwargs in (
    dict(name="c", plugin="p", input="i"),                     # on_success REQUIRED
    dict(name="c", plugin="p", on_success="o"),                # input REQUIRED
):
    try:
        CollectorSettings(**missing_kwargs)
        raise SystemExit(f"FAIL: {missing_kwargs} must be rejected")
    except ValidationError:
        pass
for forbidden in (dict(trigger={"count": 5}), dict(output_mode="passthrough")):
    try:
        CollectorSettings(name="c", plugin="p", input="i", on_success="o", **forbidden)
        raise SystemExit(f"FAIL: extra='forbid' must reject {forbidden}")
    except ValidationError:
        pass
try:
    ScopeSettings(name="s", opener="o", closer="c")            # policy REQUIRED
    raise SystemExit("FAIL: policy must be required")
except ValidationError:
    pass
assert ScopeSettings(name="s", opener="o", closer="c", policy="require_all").on_group_failure == "quarantine"
print("WS2 models verified for WS4 consumption")
# EOF
```

Expected: `WS2 models verified for WS4 consumption`. Any assertion failure or
unexpected acceptance ⇒ STOP (WS2 contract drift — coordinate, do not adapt).

- [ ] **Step 3: Run WS2's model suite as the standing guard**

Run: `pytest tests/unit/core/test_config_collectors_scopes.py -v`
Expected: PASS. These are WS2's tests; WS4 adds none here (no duplicate pins of a
sibling's authored surface).

---

### Task 2: Group roster reads on BarrierRestoreReadModel

**Files:**
- Modify: `src/elspeth/core/landscape/scheduler/restore_read_model.py` (insert after
  `has_completed_row_for_node`, `:165-183` at pre-WS1 HEAD — anchor on the method, the
  line numbers will have drifted with WS1)
- Test: `tests/unit/core/landscape/test_barrier_restore_read_model.py` (extend — this
  file already builds the read model against `make_landscape_db()`; follow its seeding
  style)

**Interfaces:**
- Consumes (WS1a): tables `token_lineage_frames` and `group_records` (exact columns in
  Preconditions), `token_parents(token_id, parent_token_id, run_id, ordinal)`
  (`schema.py:1126-1136`, pre-existing).
- Produces (used by Tasks 4, 5, 7, 8, 10, 12):

```python
@dataclass(frozen=True, slots=True)
class GroupRecordRow:
    run_id: str
    group_id: str
    kind: str            # "fork" | "expand" — group_records.kind verbatim
    opener_token_id: str
    member_count: int

class BarrierRestoreReadModel:
    def get_group_record(self, *, run_id: str, group_id: str) -> GroupRecordRow | None: ...
    def get_group_member_keys(self, *, run_id: str, group_id: str) -> frozenset[str]: ...
    def get_group_member_ordinals(self, *, run_id: str, opener_token_id: str) -> dict[str, int]: ...
    def has_completed_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool: ...
    def has_released_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool: ...
    def get_completed_group_ids_for_nodes(self, run_id: str, node_ids: frozenset[str]) -> set[tuple[str, str]]: ...
    # (kind-agnostic: group_id discriminates FORK and EXPAND groups alike — the
    # frames table's group_id is globally unique per run)
```

- [ ] **Step 1: Write the failing tests** (extend
  `tests/unit/core/landscape/test_barrier_restore_read_model.py`; reuse that file's
  existing fixtures for run/node/token seeding — read the file's helpers first and seed
  through the same raw-SQL/production-writer path it already uses; frames and group
  records are seeded through WS1a's production writer, `RecorderFactory.data_flow`, never
  raw INSERTs into the new tables):

```python
def test_group_member_reads_roster_from_frames_and_group_record(read_model_env) -> None:
    # Seed: opener token O expands into members m1, m2 (ordinals 0, 1) in group "g-exp-1".
    # WS1a's expand write mints group_records(member_count=2) and one
    # token_lineage_frames row per member (kind='expand', member_key=child token_id).
    env = read_model_env  # existing fixture: (read_model, db, run_id, ...)
    opener, members = env.seed_expansion(group_id="g-exp-1", child_count=2)

    record = env.read_model.get_group_record(run_id=env.run_id, group_id="g-exp-1")
    assert record is not None
    assert record.member_count == 2
    assert record.opener_token_id == opener.token_id

    keys = env.read_model.get_group_member_keys(run_id=env.run_id, group_id="g-exp-1")
    assert keys == frozenset(m.token_id for m in members)

    ordinals = env.read_model.get_group_member_ordinals(run_id=env.run_id, opener_token_id=opener.token_id)
    assert ordinals == {members[0].token_id: 0, members[1].token_id: 1}


def test_get_group_record_returns_none_for_unknown_group(read_model_env) -> None:
    assert read_model_env.read_model.get_group_record(run_id=read_model_env.run_id, group_id="no-such") is None


def test_has_completed_group_for_node_discriminates_sibling_groups_on_one_row(read_model_env) -> None:
    # THE collision this workstream exists for: two sibling fork groups share
    # row_id at one coalesce node; completing one must not mark the other.
    env = read_model_env
    node_id = env.seed_node("merge_x")
    g1_token = env.seed_forked_token(row_id="row-1", fork_group_id="g-fork-1", branch="left")
    g2_token = env.seed_forked_token(row_id="row-1", fork_group_id="g-fork-2", branch="left")
    env.complete_node_state(token=g1_token, node_id=node_id)   # completed_at set

    assert env.read_model.has_completed_group_for_node(run_id=env.run_id, node_id=node_id, group_id="g-fork-1") is True
    assert env.read_model.has_completed_group_for_node(run_id=env.run_id, node_id=node_id, group_id="g-fork-2") is False


def test_get_completed_group_ids_for_nodes_pairs(read_model_env) -> None:
    env = read_model_env
    node_id = env.seed_node("merge_x")
    g1_token = env.seed_forked_token(row_id="row-1", fork_group_id="g-fork-1", branch="left")
    env.complete_node_state(token=g1_token, node_id=node_id)
    pairs = env.read_model.get_completed_group_ids_for_nodes(env.run_id, frozenset({node_id}))
    assert pairs == {(node_id, "g-fork-1")}
```

The `read_model_env` helpers (`seed_expansion`, `seed_forked_token`, `seed_node`,
`complete_node_state`) are thin wrappers over the file's EXISTING seeding code — factor
them from what the current `has_completed_row_for_node` tests in that file already do,
calling WS1a's `RecorderFactory.data_flow.fork_token`/`expand_token` writers so the
frames rows are production-written. If the file has no fixture object, add one local to
the new test class; do not restructure existing tests.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/landscape/test_barrier_restore_read_model.py -v -k group`
Expected: FAIL with `AttributeError: ... has no attribute 'get_group_record'`

- [ ] **Step 3: Implement** (in `restore_read_model.py`; import
  `token_lineage_frames_table`, `group_records_table`, `token_parents_table` from
  `elspeth.core.landscape.schema` alongside the existing table imports; add the
  dataclass near the module's other row types):

```python
@dataclass(frozen=True, slots=True)
class GroupRecordRow:
    """One durable group roster-authority row (spec §4.3)."""

    run_id: str
    group_id: str
    kind: str
    opener_token_id: str
    member_count: int
```

```python
    def get_group_record(self, *, run_id: str, group_id: str) -> GroupRecordRow | None:
        """Durable roster authority for one group (spec §5 'minted')."""
        query = select(
            group_records_table.c.run_id,
            group_records_table.c.group_id,
            group_records_table.c.kind,
            group_records_table.c.opener_token_id,
            group_records_table.c.member_count,
        ).where(
            group_records_table.c.run_id == run_id,
            group_records_table.c.group_id == group_id,
        )
        row = self._ops.execute_fetchone(query)
        if row is None:
            return None
        return GroupRecordRow(
            run_id=row.run_id,
            group_id=row.group_id,
            kind=row.kind,
            opener_token_id=row.opener_token_id,
            member_count=row.member_count,
        )

    def get_group_member_keys(self, *, run_id: str, group_id: str) -> frozenset[str]:
        """DISTINCT member identities minted into one group (identity set, never a count)."""
        query = (
            select(token_lineage_frames_table.c.member_key)
            .where(
                token_lineage_frames_table.c.run_id == run_id,
                token_lineage_frames_table.c.group_id == group_id,
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return frozenset(row.member_key for row in rows)

    def get_group_member_ordinals(self, *, run_id: str, opener_token_id: str) -> dict[str, int]:
        """member token_id → opener expansion ordinal (spec §5 flush order, decision 11).

        Resolved from the OPENER's ``token_parents`` rows — never from an
        arriving token's own parent chain (a member whose subtree
        forked-and-coalesced arrives as a merged token with a fresh token_id).
        """
        query = select(token_parents_table.c.token_id, token_parents_table.c.ordinal).where(
            token_parents_table.c.run_id == run_id,
            token_parents_table.c.parent_token_id == opener_token_id,
        )
        rows = self._ops.execute_fetchall(query)
        return {row.token_id: row.ordinal for row in rows}

    def has_completed_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool:
        """Group-keyed sibling of has_completed_row_for_node.

        Two sibling groups can share a row_id at one closer node (spec §5,
        arch M1), so completion must be tested per GROUP: any completed
        node_state at the node whose token carries a lineage frame in the
        group.
        """
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def has_released_group_for_node(self, *, run_id: str, node_id: str, group_id: str) -> bool:
        """Status-COMPLETED variant (row_union release discrimination)."""
        query = (
            select(node_states_table.c.state_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id == node_id,
                token_lineage_frames_table.c.group_id == group_id,
                node_states_table.c.completed_at.isnot(None),
                node_states_table.c.status == NodeStateStatus.COMPLETED.value,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None

    def get_completed_group_ids_for_nodes(
        self,
        run_id: str,
        node_ids: frozenset[str],
    ) -> set[tuple[str, str]]:
        """Completed ``(node_id, group_id)`` pairs — the group-keyed restore sweep."""
        if not node_ids:
            return set()
        query = (
            select(node_states_table.c.node_id, token_lineage_frames_table.c.group_id)
            .select_from(
                node_states_table.join(
                    token_lineage_frames_table,
                    (node_states_table.c.token_id == token_lineage_frames_table.c.token_id)
                    & (node_states_table.c.run_id == token_lineage_frames_table.c.run_id),
                )
            )
            .where(
                node_states_table.c.run_id == run_id,
                node_states_table.c.node_id.in_(node_ids),
                node_states_table.c.completed_at.isnot(None),
            )
            .distinct()
        )
        rows = self._ops.execute_fetchall(query)
        return {(row.node_id, row.group_id) for row in rows}
```

Join subtlety pinned by the sibling-groups test: the join naturally matches the closer's
own group frame on arriving member tokens (the innermost frame at the closer IS the
group's frame — §7 rule 5 guarantees it), and released/merged output tokens no longer
carry the frame (strict pop), so a completed hold is attributed to exactly its group.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/landscape/test_barrier_restore_read_model.py -v`
Expected: PASS (all pre-existing + 4 new)

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/core/landscape/scheduler/restore_read_model.py tests/unit/core/landscape/test_barrier_restore_read_model.py
git commit -m "feat(landscape): group-keyed roster and completion reads for unified barrier restore (WS4)"
```

---

### Task 3: `TokenManager.collect_tokens` — the strict-pop N→M release mint

**Files:**
- Modify: `src/elspeth/engine/tokens.py` (add after `coalesce_tokens`, def `:307` at
  pre-WS1 HEAD)
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` (add the durable write beside
  the expand-write machinery, `:1254-1330` idempotency region at pre-WS1 HEAD — this file
  is WS1's hot zone; anchor on the landed WS1a method names, not line numbers)
- Test: `tests/unit/engine/test_collect_tokens.py` (create),
  `tests/unit/core/landscape/test_token_recording.py` (extend — the mint-replay
  predicate home per the test-harness scout §4.1)

**Interfaces:**
- Consumes (WS1a): `LineageFrame`/`FrameKind` and the shared strict-pop helper
  `pop_closer_frame(path, *, kind, group_id)` from `contracts/identity.py` (WS1a
  Task 1) — the release pop below CALLS it, never re-implements the pop inline; the
  frames-aware durable token-INSERT path in `core/landscape/data_flow/tokens.py`
  (WS1a Task 6's `token_lineage_frames` writer) and its expand-idempotency pattern
  against `batches.expansion_group_id` extended to `group_records` (spec §4.4).
  **Read the landed WS1a code in that file before writing this task's durable half;
  reuse its private frame-write helper rather than a second INSERT path —
  `token_lineage_frames` must keep a single write path (spec §11 guard).**
- Produces (used by Task 5):

```python
# engine/tokens.py
def collect_tokens(
    self,
    members: Sequence[TokenInfo],          # arrived members, opener-ordinal order
    output_rows: Sequence[PipelineRow],    # plugin outputs; may be empty (no-mint close)
    node_id: NodeID,
    run_id: str,
    group_id: str,                         # the collector's own EXPAND group being closed
) -> tuple[TokenInfo, ...]: ...
```

Semantics (spec §4.2 "collector release", ruling 24/28): every member's INNERMOST frame
must be `LineageFrame(EXPAND, group_id, member_key=<its own member key>)` and all members
must share the identical remaining path — violation is `OrchestrationInvariantError`
(engine/validation bug, unreachable from a valid build). The released base path is that
shared remainder, computed per member by the SHARED `pop_closer_frame` helper
(`contracts/identity.py`, WS1a Task 1 — it raises `OrchestrationInvariantError` unless
`path[-1]` matches `kind`+`group_id` exactly; no inline `path[:-1]` here). Emission
model — **RATIFIED** (2026-08-22 synthesis, canon item 10): the flush emission follows
the aggregation-flush precedent (§4.2: `expand_token` covers "EVERY expansion ...
aggregation flush emission included"): output tokens are minted as a NEW EXPAND group
over the popped base path — child *i* gets
`base + (LineageFrame(EXPAND, <fresh release_group_id>, member_key=child_i.token_id),)`
with a universal `group_records` mint (inert unless a WS2 binding binds it; inside an
enclosing bound region §7 rule 5 governs buildability). `output_rows == ()` mints
nothing and returns `()` (the DROP_FILTERED close — durable `group_records` for the
release group is still NOT minted, because no expansion happened).

- [ ] **Step 1: Write the failing unit test**

```python
# tests/unit/engine/test_collect_tokens.py
"""TokenManager.collect_tokens strict-pop release mint (WS4, spec §4.2)."""

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.contracts.identity import LineageFrame, TokenInfo
# Reuse the existing TokenManager test scaffolding from tests/unit/engine —
# test_processor.py's _make_factory builds a real RecorderFactory over
# make_landscape_db(); follow the same construction here.


def _member(token_id: str, group_id: str, base=(), row_id="row-1", row_data=None) -> TokenInfo:
    path = tuple(base) + (LineageFrame(kind=FrameKind.EXPAND, group_id=group_id, member_key=token_id),)
    return TokenInfo(row_id=row_id, token_id=token_id, row_data=row_data, lineage_path=path)


class TestCollectTokensPathAlgebra:
    def test_release_pops_the_collector_frame_and_opens_a_fresh_expand_group(self, token_env) -> None:
        # token_env: fixture building TokenManager + data_flow over a real
        # landscape DB, with two durably-minted members of group "g-exp-1"
        # (seeded through the production expand write so replay predicates hold).
        members, rows = token_env.seed_members(group_id="g-exp-1", count=2)
        released = token_env.token_manager.collect_tokens(
            members=members,
            output_rows=rows[:1],           # N→1
            node_id=token_env.node_id,
            run_id=token_env.run_id,
            group_id="g-exp-1",
        )
        assert len(released) == 1
        child = released[0]
        # The collector's own frame is POPPED; the emission is a fresh EXPAND group.
        assert child.lineage_path[-1].kind is FrameKind.EXPAND
        assert child.lineage_path[-1].group_id != "g-exp-1"
        assert child.lineage_path[:-1] == members[0].lineage_path[:-1]

    def test_all_members_must_carry_the_closers_innermost_frame(self, token_env) -> None:
        members, rows = token_env.seed_members(group_id="g-exp-1", count=2)
        intruder = members[1]
        # Simulate a member whose innermost frame is NOT the closer's group —
        # statically impossible under §7 rule 5, so it must crash loudly.
        with pytest.raises(OrchestrationInvariantError):
            token_env.token_manager.collect_tokens(
                members=[members[0], intruder],
                output_rows=rows[:1],
                node_id=token_env.node_id,
                run_id=token_env.run_id,
                group_id="g-other",
            )

    def test_empty_output_mints_nothing(self, token_env) -> None:
        members, _rows = token_env.seed_members(group_id="g-exp-1", count=2)
        released = token_env.token_manager.collect_tokens(
            members=members,
            output_rows=(),
            node_id=token_env.node_id,
            run_id=token_env.run_id,
            group_id="g-exp-1",
        )
        assert released == ()

    def test_requires_at_least_one_member(self, token_env) -> None:
        with pytest.raises(OrchestrationInvariantError):
            token_env.token_manager.collect_tokens(
                members=[], output_rows=(), node_id=token_env.node_id,
                run_id=token_env.run_id, group_id="g-exp-1",
            )
```

Plus, in `tests/unit/core/landscape/test_token_recording.py`, the replay-predicate
sibling beside the existing fork/expand replay tests (`:413`/`:764`):

```python
def test_collect_replay_returns_existing_children_without_reminting(recorder_env) -> None:
    # Drive the durable collect write twice with identical inputs; the second
    # call must return the SAME children (idempotent against group_records for
    # the release group), never mint a second group — spec §4.4.
    ...  # mirror test_exact_replay_returns_existing_children_without_reminting's structure

def test_collect_replay_with_divergent_outputs_refuses_without_mutation(recorder_env) -> None:
    # Divergent replay = AuditIntegrityError, DB image unchanged — mirror
    # test_incompatible_replay_refuses_without_mutation.
    ...
```

(Write these two by copying the structure of their fork/expand siblings in the same file
— they are real tests, the `...` above stands only for the copied scaffolding you will
paste from the sibling; the assertions are: same child token_ids on re-drive, and
`AuditIntegrityError` + byte-identical `tokens`/`token_lineage_frames`/`group_records`
row sets on divergence.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_collect_tokens.py tests/unit/core/landscape/test_token_recording.py -v -k collect`
Expected: FAIL with `AttributeError: 'TokenManager' object has no attribute 'collect_tokens'`

- [ ] **Step 3: Implement the TokenManager half** (`engine/tokens.py`, after
  `coalesce_tokens`; frame logic lives HERE, never at call sites — rev 2.1 ruling,
  spec §4.2; import `pop_closer_frame` from `elspeth.contracts.identity` beside the
  module's existing `LineageFrame` import):

```python
    def collect_tokens(
        self,
        members: Sequence[TokenInfo],
        output_rows: Sequence[PipelineRow],
        node_id: NodeID,
        run_id: str,
        group_id: str,
    ) -> tuple[TokenInfo, ...]:
        """Close a bound EXPAND group: strict-pop the closer's frame, mint outputs.

        spec §4.2 (ruling 24 as amended by 28): every member's innermost frame
        MUST be the closer's own EXPAND frame and all members share the
        remaining path — §7 rule 5 makes violation a genuine engine invariant.
        The pop is the SHARED ``pop_closer_frame`` (contracts/identity.py,
        WS1a Task 1): it raises OrchestrationInvariantError unless
        ``path[-1]`` matches kind+group_id exactly (empty paths included).
        The emission is RATIFIED (2026-08-22 synthesis): the aggregation-flush
        precedent — outputs form a fresh EXPAND group over the popped base
        path (inert unless bound).
        """
        if not members:
            raise OrchestrationInvariantError("collect_tokens requires at least one member token")
        # pop_closer_frame owns the innermost-frame validation (kind, group_id,
        # non-empty path); this method adds only the cross-member consistency check.
        base_path = pop_closer_frame(members[0].lineage_path, kind=FrameKind.EXPAND, group_id=group_id)
        for member in members[1:]:
            popped = pop_closer_frame(member.lineage_path, kind=FrameKind.EXPAND, group_id=group_id)
            if popped != base_path:
                raise OrchestrationInvariantError(
                    f"collect_tokens: member {member.token_id} does not share the group's "
                    f"remaining path after the strict pop of EXPAND group {group_id!r} — "
                    f"{popped!r} != {base_path!r} (spec §4.2). Engine/validation bug."
                )
        if not output_rows:
            return ()

        step = self._step_resolver(node_id)
        committed = self._data_flow.collect_tokens(
            member_refs=[TokenRef(token_id=m.token_id, run_id=run_id) for m in members],
            group_id=group_id,
            collector_node_id=str(node_id),
            output_payloads=[row.to_dict() for row in output_rows],
            output_contracts=[row.contract for row in output_rows],
            step_in_pipeline=step,
        )
        release_frames = tuple(
            base_path + (LineageFrame(kind=FrameKind.EXPAND, group_id=committed.release_group_id, member_key=child.token_id),)
            for child in committed.children
        )
        return tuple(
            TokenInfo(
                row_id=members[0].row_id,
                token_id=child.token_id,
                row_data=row,
                lineage_path=path,
            )
            for child, row, path in zip(committed.children, output_rows, release_frames, strict=True)
        )
```

- [ ] **Step 4: Implement the durable half**
  (`core/landscape/data_flow/tokens.py::collect_tokens`). This is Tier-1 audit code;
  pattern-match WS1a's landed expand write in the SAME file:
  - mint `release_group_id`, INSERT the `group_records` row for the release group
    (kind `expand`, opener = representative member = `member_refs[0]`,
    `member_count=len(output_payloads)`);
  - INSERT one child token per output payload (parent = representative member via
    `token_parents` with dense ordinals from 0), writing each child's
    `token_lineage_frames` rows through WS1a's single frame-write helper: the shared
    base path (member frames minus the popped innermost — the same strict-pop contract
    `pop_closer_frame` enforces on the engine half, re-derived here from the stored
    frames in depth order) plus the child's own release frame;
  - idempotency: re-drive detection keyed on `(run_id, collector_node_id,
    group_id)` against the release `group_records` row, mirroring the expand
    idempotency at `:1254-1330` — an exact replay returns the existing children
    (loaded with their frames); a divergent replay raises `AuditIntegrityError`
    before any write;
  - return a small frozen result:
    `CommittedCollect(release_group_id: str, children: tuple[CommittedChild, ...])`
    (`CommittedChild(token_id: str)` is enough — declare both beside the module's
    existing committed-result types).

- [ ] **Step 5: Run to pass**

Run: `pytest tests/unit/engine/test_collect_tokens.py tests/unit/core/landscape/test_token_recording.py -v`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/engine/tokens.py src/elspeth/core/landscape/data_flow/tokens.py \
        tests/unit/engine/test_collect_tokens.py tests/unit/core/landscape/test_token_recording.py
git commit -m "feat(engine): TokenManager.collect_tokens strict-pop N→M release mint with replay idempotency (WS4, spec §4.2/§4.4)"
```

---

### Task 4: CollectorExecutor — registration, arrivals, losses, roster closure

**Files:**
- Create: `src/elspeth/engine/executors/collector.py`
- Modify: `src/elspeth/contracts/node_state_context.py` (only if a collector flush
  context type is wanted — reuse `AggregationFlushContext` instead; NO change expected)
- Modify: `src/elspeth/engine/executors/state_guard.py:54` — extend
  `_NODE_STATE_AUTO_FAIL_PHASES` (and the `NodeStateAutoFailPhase` type it validates
  against — follow the type to its declaration) with `"collector_flush"`. A new
  `NodeStateGuard` site REQUIRES deliberate vocabulary extension plus caller-path tests
  (recent-code-hints 2026-08-15).
- Test: `tests/unit/engine/test_collector_executor.py` (create)

**Interfaces:**
- Consumes: WS2 Task 2's `CollectorSettings`/`ScopeSettings` (landed models, verified
  by Task 1 — never re-authored here); Task 2 `GroupRecordRow` +
  `get_group_record`/`get_group_member_keys`/`get_group_member_ordinals`/
  `has_completed_group_for_node`; Task 3 `TokenManager.collect_tokens`; WS1a
  `LineageFrame`/`FrameKind`/`TokenInfo.lineage_path`.
- Produces (consumed by Task 5/7, the WS3 settle-member seam, and the WS3+WS4
  integration item):

```python
@dataclass(frozen=True, slots=True)
class CollectorOutcome:
    held: bool
    released_tokens: tuple[TokenInfo, ...] = ()
    consumed_tokens: tuple[TokenInfo, ...] = ()
    collector_name: str | None = None
    group_id: str | None = None
    failure_reason: str | None = None       # "collector_missing_members" | "collector_transform_error" | "empty_expansion" | None
    closed_without_plugin: str | None = None  # "all_members_lost" | "empty_expansion" | None
    outcomes_recorded: bool = False

class CollectorExecutor:
    def __init__(self, execution, span_factory, token_manager, run_id, step_resolver,
                 data_flow, clock=None, max_completed_keys=10000,
                 barrier_restore_reads=None) -> None: ...
    def register_collector(self, settings: CollectorSettings, scope: ScopeSettings,
                           node_id: NodeID, transform: BatchTransformProtocol) -> None: ...
    def get_registered_names(self) -> list[str]: ...
    # ctx threads to the plugin flush exactly as AggregationExecutor.execute_flush
    # receives it — loss/empty notifications can close a roster and flush too,
    # so all three settlement entry points carry it.
    def accept(self, token: TokenInfo, collector_name: str, ctx: PluginContext, *,
               arrival_time: float | None = None) -> CollectorOutcome: ...
    def notify_member_lost(self, collector_name: str, group_id: str, member_key: str,
                           reason: str, ctx: PluginContext) -> CollectorOutcome | None: ...
    def notify_empty_group(self, collector_name: str, group_id: str) -> CollectorOutcome: ...
    def has_recorded_member_loss(self, collector_name: str, group_id: str,
                                 member_key: str) -> bool: ...
    def buffered_member_count(self) -> int: ...
```

- [ ] **Step 1: Write the failing tests.** Build the executor directly, the way
  `tests/unit/engine/test_coalesce_executor.py` builds `CoalesceExecutor` (copy its
  fixture approach: real `make_landscape_db()`-backed repositories OR its fake-repository
  pattern — read that file first and mirror it; members are seeded durably through the
  production expand write so `group_records`/frames/ordinals exist).

  Fixture contract used by every test in this file (Tasks 4/5/7): `env.executor` is a
  thin test adapter over the real `CollectorExecutor` that injects `env.ctx` into
  `accept`/`notify_member_lost` unless the call passes `ctx=` explicitly (a ~6-line
  wrapper class in the test module, NOT production code) — the real signatures take
  `ctx: PluginContext` positionally (see Interfaces). This keeps arrival/loss tests
  terse while the flush tests can override the context. The registration fixture
  constructs the WS2 models with EVERY required field — `on_success` included:
  `CollectorSettings(name="stitch", plugin="recording_stitch", input="pages_in",
  on_success="assembled_out")` (bare `name`/`plugin`/`input` constructions do not
  validate — `on_success` is required, canon item 5).

```python
# tests/unit/engine/test_collector_executor.py
"""CollectorExecutor arrival/loss/roster semantics (WS4, spec §5)."""

import pytest

from elspeth.contracts.errors import AuditIntegrityError, OrchestrationInvariantError
from elspeth.engine.executors.collector import CollectorExecutor, CollectorOutcome


class TestArrivals:
    def test_members_are_held_until_roster_closes(self, collector_env) -> None:
        env = collector_env  # registers collector "stitch" bound to scope policy=require_all
        members = env.seed_group(group_id="g-1", count=3)
        assert env.executor.accept(members[0], "stitch").held is True
        assert env.executor.accept(members[1], "stitch").held is True
        outcome = env.executor.accept(members[2], "stitch")   # roster closes
        assert outcome.held is False
        assert outcome.group_id == "g-1"

    def test_arrival_resolves_member_key_from_the_innermost_expand_frame(self, collector_env) -> None:
        env = collector_env
        members = env.seed_group(group_id="g-1", count=1)
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.held is False   # count=1 roster closes on the single arrival

    def test_duplicate_same_token_arrival_is_an_idempotent_skip(self, collector_env) -> None:
        # spec §5: lease-expiry redelivery is by design — CAS-fenced skip, not a crash.
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.accept(members[0], "stitch")
        again = env.executor.accept(members[0], "stitch")
        assert again.held is True
        assert env.executor.buffered_member_count() == 1

    def test_distinct_token_for_a_settled_member_is_an_integrity_error(self, collector_env) -> None:
        # Build-time impossible everywhere (§7 rule 5) — runtime occurrence is a bug.
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.accept(members[0], "stitch")
        impostor = env.reissue_with_new_token_id(members[0])
        with pytest.raises(AuditIntegrityError):
            env.executor.accept(impostor, "stitch")

    def test_arriving_token_whose_innermost_frame_is_not_expand_crashes(self, collector_env) -> None:
        env = collector_env
        stray = env.plain_token()   # lineage_path == ()
        with pytest.raises(OrchestrationInvariantError):
            env.executor.accept(stray, "stitch")

    def test_roster_cross_check_member_count_vs_frames(self, collector_env) -> None:
        # spec §5 'minted': group_records.member_count cross-checked against
        # DISTINCT member_key in token_lineage_frames — mismatch = integrity error.
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        env.corrupt_group_record_member_count(group_id="g-1", member_count=3)
        with pytest.raises(AuditIntegrityError):
            env.executor.accept(members[0], "stitch")

    def test_every_arrival_journals_a_durable_hold(self, collector_env) -> None:
        # RATIFIED pin (2026-08-22 synthesis, canon item 11): collector arrivals
        # ALWAYS journal. WS5's satisfiability gate's "arrived" limb reads the
        # durable record, so a memory-only buffered member would make that gate
        # lie after takeover. accept() calls begin_node_state BEFORE the buffer
        # insert — every held member has an open PENDING node_state at the
        # collector node (the state_id later completed at flush/failure).
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        outcome = env.executor.accept(members[0], "stitch")
        assert outcome.held is True
        # env.open_hold_token_ids: token_ids of node_states at the collector
        # node with completed_at IS NULL, read from the landscape DB.
        assert env.open_hold_token_ids(node="stitch") == [members[0].token_id]
        # The idempotent redelivery skip does NOT journal a second hold:
        env.executor.accept(members[0], "stitch")
        assert env.open_hold_token_ids(node="stitch") == [members[0].token_id]


class TestLosses:
    def test_loss_settles_a_member_and_can_close_the_roster_best_effort(self, best_effort_env) -> None:
        env = best_effort_env
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.notify_member_lost("stitch", "g-1", members[1].token_id, "quarantined")
        assert outcome is not None and outcome.held is False   # arrived member flushes

    def test_require_all_group_with_a_loss_fails_only_at_closure(self, collector_env) -> None:
        # Verdicts wait for settlement (spec §6.3 item 3): a loss alone does
        # not fail a 3-member group while a member is still unsettled.
        env = collector_env
        members = env.seed_group(group_id="g-1", count=3)
        env.executor.accept(members[0], "stitch")
        mid = env.executor.notify_member_lost("stitch", "g-1", members[1].token_id, "quarantined")
        assert mid is None                                     # roster not closed yet
        final = env.executor.accept(members[2], "stitch")      # closure
        assert final.failure_reason == "collector_missing_members"
        assert final.outcomes_recorded is True

    def test_duplicate_loss_dedup(self, collector_env) -> None:
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.notify_member_lost("stitch", "g-1", members[0].token_id, "quarantined")
        assert env.executor.has_recorded_member_loss("stitch", "g-1", members[0].token_id) is True
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", "g-1", members[0].token_id, "quarantined")

    def test_loss_for_a_member_outside_the_roster_crashes(self, collector_env) -> None:
        env = collector_env
        env.seed_group(group_id="g-1", count=2)
        with pytest.raises(OrchestrationInvariantError):
            env.executor.notify_member_lost("stitch", "g-1", "tok-not-a-member", "quarantined")
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_collector_executor.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'elspeth.engine.executors.collector'`

- [ ] **Step 3: Implement the executor core.** Structure (full file; flush internals
  land in Task 5 — the closure evaluation calls `self._close_group(...)`, implemented
  there; for THIS task `_close_group` performs the loss-only/failure/no-plugin arms and
  raises `NotImplementedError` only for the plugin-flush arm, which Task 5 replaces
  before this branch is ever green end-to-end — the two tasks land as ONE slice
  boundary):

```python
"""CollectorExecutor: closes bound EXPAND groups (spec §5, WS4).

The collector is the EXPAND-group closer — roster-flushed on end_of_group
only, no trigger config, transform-only output. It shares the aggregator's
batch-transform plugin contract but is NOT an aggregator (standing ruling:
aggregator is a window, never a closer; ``executors/aggregation.py`` and
``engine/triggers.py`` are untouched by this module's existence).
"""

from collections import OrderedDict
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

import structlog

import elspeth.contracts.errors as contract_errors
from elspeth.contracts import BatchTransformProtocol, PipelineRow, TokenInfo, TransformResult
from elspeth.contracts.audit import TokenRef
from elspeth.contracts.enums import FrameKind, NodeStateStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.errors import (
    AuditIntegrityError,
    ExecutionError,
    OrchestrationInvariantError,
    PluginContractViolation,
)
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.types import NodeID, StepResolver
from elspeth.core.config import CollectorSettings, ScopeSettings
from elspeth.core.landscape.data_flow_repository import DataFlowRepository
from elspeth.core.landscape.execution_repository import ExecutionRepository
from elspeth.engine._error_hash import compute_error_hash
from elspeth.engine.aggregation_result import validated_quarantined_indices
from elspeth.engine.clock import DEFAULT_CLOCK
from elspeth.engine.executors.state_guard import NodeStateGuard
from elspeth.engine.spans import SpanFactory

if TYPE_CHECKING:
    from elspeth.core.landscape.scheduler import BarrierRestoreReadModel
    from elspeth.engine.clock import Clock
    from elspeth.engine.tokens import TokenManager

slog = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class CollectorOutcome:
    """Result of a collector arrival/loss/close operation.

    Mutual exclusivity mirrors CoalesceOutcome: held ⟹ no release, no failure;
    a release and a failure never coexist.
    """

    held: bool
    released_tokens: tuple[TokenInfo, ...] = ()
    consumed_tokens: tuple[TokenInfo, ...] = ()
    collector_name: str | None = None
    group_id: str | None = None
    failure_reason: str | None = None
    closed_without_plugin: str | None = None
    outcomes_recorded: bool = False

    def __post_init__(self) -> None:
        if self.held and (self.released_tokens or self.failure_reason is not None):
            raise OrchestrationInvariantError("CollectorOutcome: held=True excludes release/failure")
        if self.released_tokens and self.failure_reason is not None:
            raise OrchestrationInvariantError("CollectorOutcome: release and failure are mutually exclusive")


@dataclass(frozen=True, slots=True)
class _MemberEntry:
    token: TokenInfo
    arrival_time: float
    state_id: str
    ordinal: int


@dataclass
class _PendingGroup:
    """One open EXPAND group's roster ledger at this collector.

    Identity sets, never arithmetic (spec §2 'roster accounting'):
    closure = roster == arrived_keys | lost_keys.
    """

    roster: frozenset[str]              # minted member_keys (cross-checked authorities)
    opener_token_id: str
    ordinals: dict[str, int]            # member_key -> opener expansion ordinal
    arrived: dict[str, _MemberEntry] = field(default_factory=dict)
    lost: dict[str, str] = field(default_factory=dict)     # member_key -> reason
    first_arrival: float = 0.0
```

Executor `__init__`/registration (mirror `CoalesceExecutor.__init__` at
`coalesce_executor.py:454-520`, including the `barrier_restore_reads is None` refusal
and `max_completed_keys` FIFO bound):

```python
class CollectorExecutor:
    def __init__(
        self,
        execution: ExecutionRepository,
        span_factory: SpanFactory,
        token_manager: "TokenManager",
        run_id: str,
        step_resolver: StepResolver,
        data_flow: DataFlowRepository,
        clock: "Clock | None" = None,
        max_completed_keys: int = 10000,
        barrier_restore_reads: "BarrierRestoreReadModel | None" = None,
    ) -> None:
        if max_completed_keys <= 0:
            raise OrchestrationInvariantError(f"max_completed_keys must be > 0, got {max_completed_keys}")
        if barrier_restore_reads is None:
            raise OrchestrationInvariantError("barrier_restore_reads is required for collector roster/restore reads")
        self._execution = execution
        self._barrier_restore_reads = barrier_restore_reads
        self._data_flow = data_flow
        self._spans = span_factory
        self._token_manager = token_manager
        self._run_id = run_id
        self._step_resolver = step_resolver
        self._clock = clock if clock is not None else DEFAULT_CLOCK
        self._settings: dict[str, CollectorSettings] = {}
        self._scopes: dict[str, ScopeSettings] = {}
        self._node_ids: dict[str, NodeID] = {}
        self._transforms: dict[str, BatchTransformProtocol] = {}
        # (collector_name, group_id) -> _PendingGroup
        self._pending: dict[tuple[str, str], _PendingGroup] = {}
        self._completed_keys: OrderedDict[tuple[str, str], None] = OrderedDict()
        self._max_completed_keys = max_completed_keys

    def register_collector(
        self,
        settings: CollectorSettings,
        scope: ScopeSettings,
        node_id: NodeID,
        transform: BatchTransformProtocol,
    ) -> None:
        if scope.closer != settings.name:
            raise OrchestrationInvariantError(
                f"Scope {scope.name!r} closer {scope.closer!r} does not name collector {settings.name!r}"
            )
        self._settings[settings.name] = settings
        self._scopes[settings.name] = scope
        self._node_ids[settings.name] = node_id
        self._transforms[settings.name] = transform

    def get_registered_names(self) -> list[str]:
        return list(self._settings.keys())

    def buffered_member_count(self) -> int:
        """Total in-memory buffered members across open groups (EOF diagnostics)."""
        return sum(len(g.arrived) for g in self._pending.values())
```

`accept` (arrival accounting; the frame resolution is the spec's decision — innermost
frame IS the closer's group frame at the closer, §7 rule 5):

```python
    def accept(
        self, token: TokenInfo, collector_name: str, ctx: PluginContext, *, arrival_time: float | None = None
    ) -> CollectorOutcome:
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        if not token.lineage_path or token.lineage_path[-1].kind is not FrameKind.EXPAND:
            raise OrchestrationInvariantError(
                f"Token {token.token_id} arrived at collector '{collector_name}' without an "
                f"innermost EXPAND frame (path={token.lineage_path!r}). Under §7 rule 5 every "
                f"member presents exactly one token whose innermost frame is the closer's own."
            )
        frame = token.lineage_path[-1]
        group_id = frame.group_id
        member_key = frame.member_key
        key = (collector_name, group_id)
        now = arrival_time if arrival_time is not None else self._clock.monotonic()
        node_id = self._node_ids[collector_name]

        if key in self._completed_keys or self._check_landscape_for_completion(collector_name, group_id):
            raise AuditIntegrityError(
                f"Token {token.token_id} (member {member_key!r}) arrived at collector "
                f"'{collector_name}' after group {group_id!r} closed. One token per member "
                f"is build-time guaranteed (§7 rule 5); a post-closure DISTINCT arrival is "
                f"an engine bug. (Same-token lease-expiry redelivery is skipped upstream by "
                f"the barrier_adopted_epoch CAS and the pending-entry check below.)"
            )

        pending = self._pending.get(key)
        if pending is None:
            pending = self._open_group(collector_name, group_id, first_arrival=now)
            self._pending[key] = pending
        if now < pending.first_arrival:
            pending.first_arrival = now

        if member_key not in pending.roster:
            raise AuditIntegrityError(
                f"Token {token.token_id} claims member {member_key!r} of group {group_id!r} "
                f"at collector '{collector_name}' but the durable roster is {sorted(pending.roster)!r}."
            )
        existing = pending.arrived.get(member_key)
        if existing is not None:
            if existing.token.token_id == token.token_id:
                # CAS-fenced idempotent skip: lease-expiry redelivery is by design
                # (spec §5, decision 10). The durable fence is barrier_adopted_epoch;
                # this is the in-memory mirror.
                slog.info(
                    "collector_duplicate_arrival_skipped",
                    collector=collector_name, group_id=group_id,
                    member_key=member_key, token_id=token.token_id, run_id=self._run_id,
                )
                return CollectorOutcome(held=True, collector_name=collector_name, group_id=group_id)
            raise AuditIntegrityError(
                f"Two DISTINCT tokens for member {member_key!r} of group {group_id!r} at "
                f"collector '{collector_name}': {existing.token.token_id!r} then "
                f"{token.token_id!r}. Build-time impossible (§7 rule 5) — engine bug."
            )
        if member_key in pending.lost:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} of group {group_id!r} both arrived and was reported "
                f"lost at collector '{collector_name}' — a token cannot both arrive and be error-routed."
            )

        step = self._step_resolver(node_id)
        state = self._execution.begin_node_state(
            token_id=token.token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=token.row_data.to_dict(),
            attempt=token.resume_attempt_offset,
            resume_checkpoint_id=token.resume_checkpoint_id,
        )
        ordinal = pending.ordinals.get(member_key)
        if ordinal is None:
            raise AuditIntegrityError(
                f"Member {member_key!r} of group {group_id!r} has no token_parents ordinal "
                f"under opener {pending.opener_token_id!r} — expansion audit inconsistency."
            )
        pending.arrived[member_key] = _MemberEntry(token=token, arrival_time=now, state_id=state.state_id, ordinal=ordinal)

        if self._roster_settled(pending):
            return self._close_group(collector_name, key, pending, ctx)
        return CollectorOutcome(held=True, collector_name=collector_name, group_id=group_id)
```

Roster helpers + `_open_group` (the spec-§5 cross-check) + loss/empty APIs:

```python
    @staticmethod
    def _roster_settled(pending: _PendingGroup) -> bool:
        return frozenset(pending.arrived) | frozenset(pending.lost) == pending.roster

    def _open_group(self, collector_name: str, group_id: str, *, first_arrival: float) -> _PendingGroup:
        record = self._barrier_restore_reads.get_group_record(run_id=self._run_id, group_id=group_id)
        if record is None:
            raise AuditIntegrityError(
                f"Collector '{collector_name}' opened group {group_id!r} with no group_records "
                f"row — the opener's expansion transaction mints it unconditionally (spec §4.3)."
            )
        roster = self._barrier_restore_reads.get_group_member_keys(run_id=self._run_id, group_id=group_id)
        if len(roster) != record.member_count:
            raise AuditIntegrityError(
                f"Group {group_id!r} roster mismatch at collector '{collector_name}': "
                f"group_records.member_count={record.member_count} but "
                f"{len(roster)} DISTINCT member_key rows in token_lineage_frames (spec §5)."
            )
        ordinals = self._barrier_restore_reads.get_group_member_ordinals(
            run_id=self._run_id, opener_token_id=record.opener_token_id
        )
        return _PendingGroup(
            roster=roster,
            opener_token_id=record.opener_token_id,
            ordinals=ordinals,
            first_arrival=first_arrival,
        )

    def has_recorded_member_loss(self, collector_name: str, group_id: str, member_key: str) -> bool:
        key = (collector_name, group_id)
        pending = self._pending.get(key)
        if pending is not None:
            return member_key in pending.lost
        return False

    def notify_member_lost(
        self, collector_name: str, group_id: str, member_key: str, reason: str, ctx: PluginContext
    ) -> CollectorOutcome | None:
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        key = (collector_name, group_id)
        if key in self._completed_keys or self._check_landscape_for_completion(collector_name, group_id):
            return None
        pending = self._pending.get(key)
        if pending is None:
            pending = self._open_group(collector_name, group_id, first_arrival=self._clock.monotonic())
            self._pending[key] = pending
        if member_key not in pending.roster:
            raise OrchestrationInvariantError(
                f"Lost member {member_key!r} not in group {group_id!r} roster at collector "
                f"'{collector_name}': {sorted(pending.roster)!r}"
            )
        if member_key in pending.arrived:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} already arrived at collector '{collector_name}' "
                f"but was reported lost — processor bug."
            )
        if member_key in pending.lost:
            raise OrchestrationInvariantError(
                f"Member {member_key!r} already marked lost at collector '{collector_name}' — "
                f"duplicate loss notification (dedup with has_recorded_member_loss first)."
            )
        pending.lost[member_key] = reason
        if self._roster_settled(pending):
            return self._close_group(collector_name, key, pending, ctx)
        return None

    def notify_empty_group(self, collector_name: str, group_id: str) -> CollectorOutcome:
        """Close a member_count=0 group (spec §6.4): no plugin, ever.

        require_all ⇒ group failure 'empty_expansion'; best_effort ⇒ silent
        close (parent keeps SUCCESS/FILTER_DROPPED — the caller's disposition).
        """
        if collector_name not in self._settings:
            raise OrchestrationInvariantError(f"Collector '{collector_name}' not registered")
        record = self._barrier_restore_reads.get_group_record(run_id=self._run_id, group_id=group_id)
        if record is None or record.member_count != 0:
            raise AuditIntegrityError(
                f"notify_empty_group for {group_id!r} at '{collector_name}': group_records says "
                f"{'absent' if record is None else record.member_count} — not an empty group."
            )
        key = (collector_name, group_id)
        self._mark_completed(key)
        scope = self._scopes[collector_name]
        if scope.policy == "require_all":
            return CollectorOutcome(
                held=False, collector_name=collector_name, group_id=group_id,
                failure_reason="empty_expansion", closed_without_plugin="empty_expansion",
            )
        return CollectorOutcome(
            held=False, collector_name=collector_name, group_id=group_id,
            closed_without_plugin="empty_expansion",
        )

    def _check_landscape_for_completion(self, collector_name: str, group_id: str) -> bool:
        if collector_name not in self._node_ids:
            return False
        node_id = self._node_ids[collector_name]
        if self._barrier_restore_reads.has_completed_group_for_node(
            run_id=self._run_id, node_id=str(node_id), group_id=group_id
        ):
            self._mark_completed((collector_name, group_id))
            return True
        return False

    def _mark_completed(self, key: tuple[str, str]) -> None:
        self._completed_keys[key] = None
        while len(self._completed_keys) > self._max_completed_keys:
            self._completed_keys.popitem(last=False)
```

`_close_group` — the closure dispatch (loss arms in this task; the plugin-flush arm is
Task 5's `_execute_flush`):

```python
    def _close_group(
        self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, ctx: PluginContext
    ) -> CollectorOutcome:
        """Roster closed (minted == settled as identity sets) — render the verdict.

        Verdicts wait for settlement (spec §6.3 item 3): this is only ever
        called from a settlement event that completed the roster.
        """
        scope = self._scopes[collector_name]
        group_id = key[1]
        if pending.lost and scope.policy == "require_all":
            return self._fail_group(collector_name, key, pending, failure_reason="collector_missing_members")
        if not pending.arrived:
            # best_effort, every member lost: engine closes WITHOUT the plugin
            # (spec §6.4 'all_members_lost'; not a failure under best_effort).
            del self._pending[key]
            self._mark_completed(key)
            return CollectorOutcome(
                held=False, collector_name=collector_name, group_id=group_id,
                closed_without_plugin="all_members_lost",
            )
        return self._execute_flush(collector_name, key, pending, ctx)

    def _fail_group(
        self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, *, failure_reason: str
    ) -> CollectorOutcome:
        """require_all group failure: engine-performed, plugin never invoked (spec §6.4)."""
        group_id = key[1]
        consumed = tuple(entry.token for entry in sorted(pending.arrived.values(), key=lambda e: e.ordinal))
        error_hash = compute_error_hash(failure_reason)
        now = self._clock.monotonic()
        error = ExecutionError(
            exception=(
                f"Collector group {group_id!r} failed: lost members "
                f"{sorted(pending.lost)!r} under require_all"
            ),
            exception_type="CollectorGroupFailure",
            phase="collector_flush",
        )
        for entry in pending.arrived.values():
            self._execution.complete_node_state(
                state_id=entry.state_id,
                status=NodeStateStatus.FAILED,
                error=error,
                duration_ms=(now - entry.arrival_time) * 1000,
            )
            self._data_flow.record_token_outcome(
                ref=TokenRef(token_id=entry.token.token_id, run_id=self._run_id),
                outcome=TerminalOutcome.FAILURE,
                path=TerminalPath.UNROUTED,
                error_hash=error_hash,
            )
        del self._pending[key]
        self._mark_completed(key)
        return CollectorOutcome(
            held=False, collector_name=collector_name, group_id=group_id,
            consumed_tokens=consumed, failure_reason=failure_reason, outcomes_recorded=True,
        )
```

NOTE for the WS3 integration reviewer: `_fail_group`'s direct `record_token_outcome`
writes are the collector's engine-performed disposition for ARRIVED members; the group
verdict itself (escalation vs quarantine per `scope.on_group_failure`, survivor
termination as `scope_group_failed`) is staged by the WS3 settle-member seam consuming
this `CollectorOutcome` — the executor renders, WS3 settles. Do not add escalation logic
here.

- [ ] **Step 4: Run the Task 4 tests** (closure tests that reach the plugin-flush arm
  stay red until Task 5 — run the arrival/loss subset):

Run: `pytest tests/unit/engine/test_collector_executor.py -v -k "duplicate or distinct or frame or cross_check or dedup or outside"`
Expected: PASS for the arrival/loss invariants; the flush-reaching tests fail on
`NotImplementedError` (expected — Task 5 completes them; Tasks 4+5 are ONE slice).

- [ ] **Step 5: DO NOT COMMIT YET** — Tasks 4 and 5 land as one commit (Task 5 Step 6).

---

### Task 5: CollectorExecutor flush — ordinal order, guards, quarantine, no-plugin closes

**Files:**
- Modify: `src/elspeth/engine/executors/collector.py` (Task 4's file — `_execute_flush`)
- Modify: `src/elspeth/engine/executors/state_guard.py` (`"collector_flush"` phase, from
  Task 4's file list)
- Test: `tests/unit/engine/test_collector_executor.py` (extend)

**Interfaces:**
- Consumes: Task 3 `TokenManager.collect_tokens(members, output_rows, node_id, run_id, group_id) -> tuple[TokenInfo, ...]`;
  `validated_quarantined_indices(result, *, buffered_token_count, aggregation_name) -> set[int]`
  (`engine/aggregation_result.py:14-51` — REUSED, not copied);
  `NodeStateGuard(execution, token_id=..., node_id=..., run_id=..., step_index=..., input_data=..., attempt=..., resume_checkpoint_id=..., auto_fail_phase="collector_flush")`
  (`executors/state_guard.py`).
- Produces: `CollectorOutcome` release/failure arms (Task 4's dataclass, now fully
  reachable); flush ordering contract: **arrived members are flushed in opener expansion
  ordinal order — never arrival order** (spec decision 11).

- [ ] **Step 1: Write the failing tests** (extend `test_collector_executor.py`):

```python
class TestFlush:
    def test_flush_orders_members_by_opener_ordinal_not_arrival_order(self, collector_env) -> None:
        env = collector_env
        members = env.seed_group(group_id="g-1", count=3)     # ordinals 0,1,2
        env.executor.accept(members[2], "stitch")             # arrive out of order
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")
        assert outcome.held is False
        # The recording plugin observed rows in ordinal order 0,1,2:
        assert env.transform.seen_rows == [m.row_data.to_dict() for m in members]
        assert outcome.consumed_tokens == tuple(members)

    def test_flush_order_resolves_a_merged_member_through_the_opener_ordinals(self, collector_env) -> None:
        # A member whose subtree forked-and-coalesced arrives as a merged token
        # with a FRESH token_id; its member_key (the opener child's token_id)
        # still resolves the ordinal via token_parents (spec §5, arch minor 3).
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        merged = env.reissue_with_new_token_id(members[0])    # fresh token_id, same frame
        env.executor.accept(members[1], "stitch")
        outcome = env.executor.accept(merged, "stitch")
        assert outcome.held is False
        assert env.transform.seen_rows[0] == merged.row_data.to_dict()   # ordinal 0 first

    def test_group_relative_quarantine_indices(self, collector_env) -> None:
        # Indices are relative to the FLUSHED GROUP in ordinal order —
        # validated_quarantined_indices reused with buffered_token_count=len(group).
        env = collector_env  # env.transform quarantines index 1
        env.transform.quarantine_indices = [1]
        members = env.seed_group(group_id="g-1", count=3)
        for m in members:
            outcome = env.executor.accept(m, "stitch")
        assert outcome.held is False
        assert env.quarantined_token_ids() == [members[1].token_id]

    def test_all_quarantined_with_output_is_an_invariant_violation(self, collector_env) -> None:
        # aggregation.py:546-550 guard, replicated with the same semantics.
        env = collector_env
        env.transform.quarantine_indices = [0, 1]
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.accept(members[0], "stitch")
        with pytest.raises(OrchestrationInvariantError, match="all .* members were quarantined"):
            env.executor.accept(members[1], "stitch")

    def test_success_with_neither_row_nor_rows_is_a_contract_violation(self, collector_env) -> None:
        # aggregation.py:527-532 guard, replicated with the same semantics.
        env = collector_env
        env.transform.return_empty_success = True
        members = env.seed_group(group_id="g-1", count=1)
        with pytest.raises(PluginContractViolation, match="neither row nor rows"):
            env.executor.accept(members[0], "stitch")

    def test_passthrough_quarantine_is_prohibited(self, collector_env) -> None:
        # Collectors are transform-only; the passthrough prohibition
        # (aggregation_result.py:83-84) holds by construction — a collector
        # flush ALWAYS passes OutputMode-free group-relative indices, and a
        # plugin that returns per-row passthrough shape is rejected by the
        # roster/output validation. Pinned via a plugin returning
        # len(rows)==len(members) with quarantine metadata:
        env = collector_env
        env.transform.quarantine_indices = [0]
        env.transform.echo_rows = True   # rows out == rows in
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.accept(members[0], "stitch")
        outcome = env.executor.accept(members[1], "stitch")
        # transform-mode semantics: quarantined member excluded, output released
        assert outcome.held is False
        assert env.quarantined_token_ids() == [members[0].token_id]

    def test_empty_group_close_never_invokes_plugin(self, collector_env, best_effort_env) -> None:
        for env, expect_failure in ((collector_env, True), (best_effort_env, False)):
            group_id = env.seed_empty_group()      # group_records row, member_count=0
            outcome = env.executor.notify_empty_group("stitch", group_id)
            assert env.transform.call_count == 0
            assert outcome.closed_without_plugin == "empty_expansion"
            assert (outcome.failure_reason == "empty_expansion") is expect_failure

    def test_all_members_lost_best_effort_closes_without_plugin(self, best_effort_env) -> None:
        env = best_effort_env
        members = env.seed_group(group_id="g-1", count=2)
        env.executor.notify_member_lost("stitch", "g-1", members[0].token_id, "quarantined")
        outcome = env.executor.notify_member_lost("stitch", "g-1", members[1].token_id, "quarantined")
        assert outcome is not None
        assert outcome.closed_without_plugin == "all_members_lost"
        assert env.transform.call_count == 0
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_collector_executor.py -v -k Flush`
Expected: FAIL (NotImplementedError from Task 4's stub / missing guard phase)

- [ ] **Step 3: Extend the guard vocabulary.** In
  `src/elspeth/engine/executors/state_guard.py`, add `"collector_flush"` to
  `_NODE_STATE_AUTO_FAIL_PHASES` (`:54`) AND to the `NodeStateAutoFailPhase` type it
  validates (follow the import — the closed vocabulary is runtime-and-static). The
  caller-path test is `test_collector_flush_plugin_exception_autofails_with_phase` below.

- [ ] **Step 4: Implement `_execute_flush`** (replaces Task 4's stub; the shape is
  aggregation's `execute_flush` (`aggregation.py:651-810`) adapted to per-group buffers,
  minus triggers/output_mode/window, plus member-hold completion in the style of
  coalesce's `_execute_merge` (`coalesce_executor.py:1084-1265`)):

```python
    def _execute_flush(
        self, collector_name: str, key: tuple[str, str], pending: _PendingGroup, ctx: PluginContext
    ) -> CollectorOutcome:
        """end_of_group flush: opener-ordinal order, transform-only, audit-guarded."""
        group_id = key[1]
        node_id = self._node_ids[collector_name]
        transform = self._transforms[collector_name]
        step = self._step_resolver(node_id)
        entries = sorted(pending.arrived.values(), key=lambda e: e.ordinal)
        members = tuple(entry.token for entry in entries)
        pipeline_rows: list[PipelineRow] = []
        for entry in entries:
            contract = entry.token.row_data.contract
            if contract is None:
                raise OrchestrationInvariantError(
                    f"Token {entry.token.token_id} has no contract — cannot flush collector "
                    f"'{collector_name}' group {group_id!r}."
                )
            pipeline_rows.append(PipelineRow(entry.token.row_data.to_dict(), contract))

        batch_input = {"batch_rows": [row.to_dict() for row in pipeline_rows]}
        representative = members[0]
        now = self._clock.monotonic()

        with NodeStateGuard(
            self._execution,
            token_id=representative.token_id,
            node_id=node_id,
            run_id=self._run_id,
            step_index=step,
            input_data=batch_input,
            attempt=representative.resume_attempt_offset,
            resume_checkpoint_id=representative.resume_checkpoint_id,
            auto_fail_phase="collector_flush",
        ) as guard:
            result = transform.process(pipeline_rows, ctx)
            if result.status != "success":
                guard.complete(
                    NodeStateStatus.FAILED,
                    duration_ms=(self._clock.monotonic() - now) * 1000,
                    error=ExecutionError(
                        exception=str(result.reason) if result.reason else "Collector transform returned error",
                        exception_type="TransformError",
                    ),
                )
                return self._fail_group(collector_name, key, pending, failure_reason="collector_transform_error")

            if result.row is None and result.rows is None:
                raise PluginContractViolation(
                    f"Collector transform '{transform.name}' returned success status but "
                    f"neither row nor rows contains data. Batch-aware transforms must return "
                    f"output via TransformResult.success(row) or TransformResult.success_multi(rows)."
                )
            output_rows: tuple[PipelineRow, ...] = (result.row,) if result.row is not None else tuple(result.rows or ())
            quarantined = validated_quarantined_indices(
                result, buffered_token_count=len(members), aggregation_name=collector_name
            )
            surviving = tuple(entry for index, entry in enumerate(entries) if index not in quarantined)
            if output_rows and not surviving:
                raise OrchestrationInvariantError(
                    f"Collector {collector_name!r} emitted output but all group members were quarantined"
                )

            released = self._token_manager.collect_tokens(
                members=tuple(entry.token for entry in surviving),
                output_rows=output_rows,
                node_id=node_id,
                run_id=self._run_id,
                group_id=group_id,
            )

            duration_ms = (self._clock.monotonic() - now) * 1000
            for index, entry in enumerate(entries):
                if index in quarantined:
                    self._execution.complete_node_state(
                        state_id=entry.state_id,
                        status=NodeStateStatus.FAILED,
                        error=ExecutionError(
                            exception=f"quarantined_in_group:{group_id}:{index}",
                            exception_type="CollectorMemberQuarantine",
                            phase="collector_flush",
                        ),
                        duration_ms=duration_ms,
                    )
                    self._data_flow.record_token_outcome(
                        ref=TokenRef(token_id=entry.token.token_id, run_id=self._run_id),
                        outcome=TerminalOutcome.FAILURE,
                        path=TerminalPath.QUARANTINED_AT_SOURCE,
                        error_hash=compute_error_hash(f"quarantined_in_group:{group_id}:{index}"),
                    )
                else:
                    self._execution.complete_node_state(
                        state_id=entry.state_id,
                        status=NodeStateStatus.COMPLETED,
                        output_data={},
                        duration_ms=duration_ms,
                    )
            guard.complete(NodeStateStatus.COMPLETED, duration_ms=duration_ms)

        del self._pending[key]
        self._mark_completed(key)
        return CollectorOutcome(
            held=False,
            released_tokens=released,
            consumed_tokens=members,
            collector_name=collector_name,
            group_id=group_id,
            outcomes_recorded=True,
        )
```

Two implementation notes the coder must honour (both verified against the live tree):
1. **`PluginContext` threading**: `ctx` arrives as a parameter end to end
   (`accept`/`notify_member_lost` → `_close_group` → `_execute_flush`), mirroring how
   `AggregationExecutor.execute_flush` receives it — the executor NEVER constructs its
   own context. `notify_empty_group` does not take one (it can never reach the plugin).
   The WS3+WS4 integration passes the processor's real ctx.
2. **Member terminal outcomes**: the surviving members' TERMINAL outcomes
   (`BATCH_CONSUMED`-analogue) are NOT written here — under the unified system the
   member terminals ride the WS3 settlement/disposition seam consuming
   `CollectorOutcome.consumed_tokens` (the `(TRANSIENT, BATCH_CONSUMED)` write pattern
   at `processor.py:1642-1652` is retired by ruling 25 for bound regions and its
   collector analogue is a WS3 decision). Quarantined-member FAILURE outcomes ARE
   written here because they parallel coalesce's in-executor cleanup writes
   (`coalesce_executor.py:1054`) which WS3 §6.1 retires into the seam in ITS
   workstream — leave the WS3 plan the single list of writes to lift.

- [ ] **Step 5: Add the guard caller-path test:**

```python
def test_collector_flush_plugin_exception_autofails_with_phase(collector_env) -> None:
    env = collector_env
    env.transform.raise_on_process = RuntimeError("boom")
    members = env.seed_group(group_id="g-1", count=1)
    with pytest.raises(RuntimeError):
        env.executor.accept(members[0], "stitch", ctx=env.ctx)
    # NodeStateGuard auto-failed the flush state with the new phase:
    assert env.latest_node_state_error_phase(node="stitch") == "collector_flush"
```

- [ ] **Step 6: Run the whole collector suite to pass, then the guard's own suite:**

Run: `pytest tests/unit/engine/test_collector_executor.py tests/unit/engine/test_executors.py -v`
Expected: PASS

- [ ] **Step 7: Commit (Tasks 4+5 as one slice)**

```bash
git add src/elspeth/engine/executors/collector.py src/elspeth/engine/executors/state_guard.py \
        tests/unit/engine/test_collector_executor.py
git commit -m "feat(engine): CollectorExecutor — roster-closed EXPAND-group closer with ordinal flush and no-plugin closes (WS4, spec §5)"
```

- [ ] **Step 8: Slice boundary** — full `pytest tests/`, trust-tier corpus diff
  (before/after counts equal), wardline gate. Record HEAD before/after the full run.

---

### Task 6: Collector work-item address — `collector_name` through contract, schema, codecs

**Files:**
- Modify: `src/elspeth/contracts/scheduler.py` — `TokenWorkItem` (add
  `collector_name: str | None = None` beside `row_union_name`, `:144` pre-WS1) and
  `BarrierEmission` (same addition beside `:197`)
- Modify: `src/elspeth/core/landscape/schema.py` — `token_work_items_table`: add
  `Column("collector_name", String(128))` directly after `row_union_name` (`:731`
  pre-WS1)
- Modify: `src/elspeth/engine/scheduler_work_codec.py` — `ScheduledWorkFields` (+field),
  `ready_fields`, `ready_emission`, `work_item_from_scheduler` (all three mappings gain
  the field; anchor on `row_union_name` in each)
- Modify: `src/elspeth/core/landscape/scheduler/queue.py` and
  `core/landscape/scheduler/work_items.py` — every column list that enumerates
  `row_union_name` gains `collector_name` (grep: `git grep -n "row_union_name" src/elspeth/core/landscape/scheduler/`)
- Modify: `src/elspeth/core/landscape/database.py` — the `token_work_items`
  required-column verification list (`:394-397` pre-WS1; consumer-roster risk note 4)
- Modify: `src/elspeth/engine/work_items.py` — the in-memory `WorkItem` gains
  `collector_name` mirroring `row_union_name` (and the `create_work_item` factory
  Protocol in `scheduler_work_codec.py:40-47`)
- Test: `tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py`
  (extend), `tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py`
  (create)

**Interfaces:**
- Consumes: WS1a Task 5's landed `TokenWorkItem` shape (with `lineage_path`).
- Produces: `TokenWorkItem.collector_name: str | None` — the collector's barrier BINDING
  address (closer's address, not lineage — spec §4.3); BLOCKED collector rows are
  therefore counted by `has_blocked_barrier_work` (`processor.py:4199` →
  `list_blocked_barrier_items`, which is non-filtered by design —
  `core/landscape/scheduler/barrier.py:1125`) with ZERO drain-loop code change. The WS3+WS4
  integration's intake arm (`BarrierIntakeCoordinator`) and the processor's
  `mark_blocked` call for collector arrivals consume this field. Stated contract:
  collector BLOCKED rows carry `barrier_key = "collector:<collector_name>:<group_id>"`
  (never the bare closer name) — the shape this task's fixtures seed and the WS3+WS4
  integration item's `mark_blocked` call must emit; WS5's satisfiability gate relies on
  it for its `collector_name` disjunct.

- [ ] **Step 1: Write the failing tests.**

Extend `test_scheduler_repository_adopt_barrier_item.py` (follow its existing seeding
pattern — raw-SQL runs/nodes/rows/tokens + epoch-1 seat):

```python
def test_adopt_blocked_collector_item_is_cas_fenced(adopt_env) -> None:
    """Collector arrivals ride the SAME barrier_adopted_epoch CAS as coalesce.

    spec §5 decision 10: duplicate same-token arrival via lease-expiry
    redelivery is a CAS-fenced idempotent skip.
    """
    env = adopt_env
    item = env.seed_blocked_work_item(collector_name="stitch", barrier_key="collector:stitch:g-1")
    first = env.repo.adopt_blocked_barrier_item(
        work_item_id=item.work_item_id, coordination_token=env.token, membership=None, buffered_outcome=None,
    )
    assert first.adopted is True
    second = env.repo.adopt_blocked_barrier_item(
        work_item_id=item.work_item_id, coordination_token=env.token, membership=None, buffered_outcome=None,
    )
    assert second.adopted is False          # idempotent skip — the double-accept guard


def test_blocked_collector_item_counts_as_blocked_barrier_work(adopt_env) -> None:
    """leader_drain.py:511 gate: collector-buffered members keep the EOF fixpoint alive.

    This is the journal-row half of the ratified always-journal pin (canon
    item 11, executor half in test_collector_executor.py): every collector
    arrival is a durable BLOCKED row, and WS5's satisfiability gate's
    "arrived" limb reads exactly these rows.
    """
    env = adopt_env
    env.seed_blocked_work_item(collector_name="stitch", barrier_key="collector:stitch:g-1")
    items = env.repo.list_blocked_barrier_items(run_id=env.run_id)
    assert [i.collector_name for i in items] == ["stitch"]
```

Create `tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py`:

```python
"""EOF fixpoint counts collector-buffered members (WS4, spec §5 / leader_drain.py:511)."""

import pytest

from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.engine.orchestrator.leader_drain import run_end_of_input_barrier_flush
# Reuse the port-shaped fake from the existing leader-drain/orchestrator unit tests
# (grep tests/unit/engine/orchestrator for EndOfInputBarrierProcessorPort fakes and
# extend that fake rather than writing a new one).


def test_flush_loop_does_not_exit_while_collector_holds_remain(drain_env) -> None:
    # Fake processor: no aggregations, no coalesce, no row_union; intake pass
    # resolves one collector hold per iteration; has_blocked_barrier_work
    # reports the remaining durable BLOCKED collector rows.
    env = drain_env(collector_blocked_rows=2, resolved_per_intake=1)
    run_end_of_input_barrier_flush(
        config=env.config, processor=env.processor, ctx=env.ctx,
        counters=env.counters, pending_tokens=env.pending_tokens,
        coalesce_executor=None, coalesce_node_map={},
    )
    assert env.processor.intake_calls == 2   # looped until the collector drained


def test_flush_loop_raises_on_nonconverging_collector_holds(drain_env) -> None:
    env = drain_env(collector_blocked_rows=1, resolved_per_intake=0)
    with pytest.raises(OrchestrationInvariantError, match="did not converge"):
        run_end_of_input_barrier_flush(
            config=env.config, processor=env.processor, ctx=env.ctx,
            counters=env.counters, pending_tokens=env.pending_tokens,
            coalesce_executor=None, coalesce_node_map={},
        )
```

IMPORTANT: `run_end_of_input_barrier_flush` currently EARLY-RETURNS when
`config.aggregation_settings` is empty and both coalesce and row_union executors are
absent (`leader_drain.py:450-452`) — a collector-only pipeline would skip the flush loop
entirely and strand its holds. The first drain test above must catch this: give the fake
processor a `collector_executor` and extend the early-return guard.

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py -k collector tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py -v`
Expected: FAIL (`TypeError: ... unexpected keyword argument 'collector_name'`, and the
drain loop exiting early)

- [ ] **Step 3: Implement.** Field additions are mechanical — every site that names
  `row_union_name` in the files listed above gains `collector_name` with identical
  optionality (contract default `None`, column nullable `String(128)`, codec
  passthrough, `database.py` required-column entry). In `leader_drain.py`, extend the
  early-return guard:

```python
    row_union_executor = processor.row_union_executor
    collector_executor = processor.collector_executor
    if (
        not config.aggregation_settings
        and coalesce_executor is None
        and row_union_executor is None
        and collector_executor is None
    ):
        return
```

with `collector_executor` added to `EndOfInputBarrierProcessorPort`
(`engine/orchestrator/ports.py` — beside `row_union_executor`, and returning `None` on
the processor until the WS3+WS4 integration wires the real one:
`RowProcessor.collector_executor` property returning the constructor-injected executor
or `None`). No other drain change: `has_blocked_barrier_work` is already non-filtered,
which the repository test pins.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py tests/unit/engine/test_scheduler_drain_characterization.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/scheduler.py src/elspeth/core/landscape/schema.py \
        src/elspeth/engine/scheduler_work_codec.py src/elspeth/core/landscape/scheduler/queue.py \
        src/elspeth/core/landscape/scheduler/work_items.py src/elspeth/core/landscape/database.py \
        src/elspeth/engine/work_items.py src/elspeth/engine/orchestrator/leader_drain.py \
        src/elspeth/engine/orchestrator/ports.py src/elspeth/engine/processor.py \
        tests/unit/core/landscape/test_scheduler_repository_adopt_barrier_item.py \
        tests/unit/engine/orchestrator/test_leader_drain_collector_fixpoint.py
git commit -m "feat(scheduler): collector_name barrier-binding address through work-item contract, schema, codecs; EOF fixpoint counts collector holds (WS4)"
```

- [ ] **Step 6: Slice boundary** — full `pytest tests/` (schema + contract churn has
  wide blast radius: `database.py` verification, codec round-trip suites), trust-tier
  diff, wardline.

---

### Task 7: CollectorExecutor.restore_from_journal — takeover buffer restore

**Files:**
- Modify: `src/elspeth/engine/executors/collector.py`
- Modify: `src/elspeth/engine/journal_restore.py` (add `CollectorJournalRestorer` beside
  `CoalesceJournalRestorer` — same validate-before-mutate boundary split)
- Test: `tests/unit/engine/test_collector_executor.py` (extend),
  `tests/unit/engine/test_journal_restore.py` (extend)

**Interfaces:**
- Consumes: Task 6 `TokenWorkItem.collector_name`; WS1a Task 5's
  `token_from_journal_item(item, *, attempt_offset, resume_checkpoint_id) -> TokenInfo`
  (`payload_codec.py` — codec-pure, reconstructs `lineage_path`); Task 2 group reads.
- Produces:

```python
def restore_from_journal(
    self,
    *,
    items: Sequence[TokenWorkItem],       # ALL collector BLOCKED rows, one call per resume
    state_ids: Mapping[str, str],         # token_id -> PENDING hold state_id (audit-derived)
    attempt_offsets: Mapping[str, int],
    resume_checkpoint_id: str,
) -> None: ...
```

Collector restore needs NO checkpoint scalars: arrived members are journal BLOCKED rows,
losses are durable `group_losses` rows replayed by WS3's intake (full-table on takeover,
spec §6.2), and the roster is `group_records` — there is no underivable in-memory-only
state, so `BarrierScalars` gains no collector component (state this in the docstring).

- [ ] **Step 1: Write the failing tests:**

```python
class TestRestore:
    def test_restore_rebuilds_buffers_and_ordinal_flush_survives_takeover(self, collector_env) -> None:
        # Decision 11's point: arrival order is unrecoverable after takeover,
        # ordinal order is not. Restore from journal items listed in a
        # scrambled order; a subsequent closure must still flush 0,1,2.
        env = collector_env
        members = env.seed_group(group_id="g-1", count=3)
        items = [env.blocked_item_for(m, collector_name="stitch") for m in (members[1], members[2])]
        fresh = env.fresh_executor()
        fresh.restore_from_journal(
            items=list(reversed(items)),
            state_ids=env.state_ids_for(items),
            attempt_offsets={m.token_id: 0 for m in members},
            resume_checkpoint_id="ckpt-1",
        )
        assert fresh.buffered_member_count() == 2
        outcome = fresh.accept(members[0], "stitch", ctx=env.ctx)
        assert outcome.held is False
        assert env.transform.seen_rows == [m.row_data.to_dict() for m in members]  # ordinal order

    def test_restore_rejects_unknown_collector_and_duplicate_member_claims(self, collector_env) -> None:
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        item = env.blocked_item_for(members[0], collector_name="not-registered")
        with pytest.raises(AuditIntegrityError):
            env.fresh_executor().restore_from_journal(
                items=[item], state_ids=env.state_ids_for([item]),
                attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1",
            )

    def test_restore_requires_empty_executor_and_covers_every_state_id(self, collector_env) -> None:
        env = collector_env
        members = env.seed_group(group_id="g-1", count=2)
        item = env.blocked_item_for(members[0], collector_name="stitch")
        with pytest.raises(AuditIntegrityError):
            env.fresh_executor().restore_from_journal(
                items=[item], state_ids={},          # missing hold — audit inconsistency
                attempt_offsets={members[0].token_id: 0}, resume_checkpoint_id="ckpt-1",
            )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_collector_executor.py -v -k Restore`
Expected: FAIL with `AttributeError: ... no attribute 'restore_from_journal'`

- [ ] **Step 3: Implement.** `CollectorJournalRestorer.restore` mirrors
  `CoalesceJournalRestorer.restore` (`journal_restore.py:128-345`) with these
  substitutions: group key = `(item.collector_name, <innermost EXPAND frame group_id of
  item.lineage_path>)`; member identity = that frame's `member_key`; per-key validation
  = registered collector, non-NULL `barrier_blocked_at`, no duplicate member claims,
  every token covered by `state_ids`/`attempt_offsets`; completed-key reconstruction via
  Task 2's `get_completed_group_ids_for_nodes`. No scalars parameter, no lost-member
  restore (losses replay through WS3's intake). The executor method applies the returned
  frozen state wholesale after `if self._pending: raise OrchestrationInvariantError`
  (the row_union `:213-214` precedent), rebuilding each `_PendingGroup` through
  `_open_group` (re-deriving roster/ordinals from the durable authorities) and
  `token_from_journal_item` for the tokens.

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_collector_executor.py tests/unit/engine/test_journal_restore.py -v`
Expected: PASS

- [ ] **Step 5: Commit, then slice boundary** (full suite + trust-tier diff + wardline):

```bash
git add src/elspeth/engine/executors/collector.py src/elspeth/engine/journal_restore.py \
        tests/unit/engine/test_collector_executor.py tests/unit/engine/test_journal_restore.py
git commit -m "feat(engine): collector journal restore — takeover-invariant buffers with ordinal flush (WS4, decision 11)"
```

---

### Task 8: Coalesce re-key — accept path, pending dict, landscape completion

**Files:**
- Modify: `src/elspeth/engine/coalesce_executor.py` — `:511-517` (pending/completed-key
  docs + types), `:698-724` (`_check_landscape_for_completion`), `:744-914` (`accept`),
  `:1014`/`:1298-1344`/`:1381-1439` (key docstrings, `decide_coalesce` kwarg, flush
  invariant message)
- Modify: `src/elspeth/engine/coalesce_policy.py` — `decide_coalesce(..., row_id=None)`
  parameter renamed `group_id` (`:107-114`)
- Test: `tests/unit/engine/test_coalesce_executor.py`,
  `tests/unit/engine/test_coalesce_policy.py` (mechanical key/kwarg migration),
  plus one NEW discriminating test (below)

**Interfaces:**
- Consumes: WS1a `TokenInfo.fork_group_id` derived accessor (innermost FORK frame's
  `group_id`); Task 2 `has_completed_group_for_node`.
- Produces: coalesce pending state keyed `(coalesce_name, fork_group_id)` on ALL
  in-memory surfaces. `accept(token, coalesce_name, *, arrival_time=None)` signature
  UNCHANGED (the key is derived from the token). Merge logic, policies, timeouts,
  plugin-visible behaviour byte-identical — guarded by the suite list in the header.

- [ ] **Step 1: Write the NEW failing test** (in `test_coalesce_executor.py`; this is
  the arch-M1 collision — the reason for the re-key):

```python
def test_sibling_fork_groups_sharing_row_id_are_distinct_pending_groups(coalesce_env) -> None:
    """spec §5 (arch M1): expand siblings share row_id; each forks into the
    same coalesce NODE as a DISTINCT concurrent FORK group. Under the old
    (coalesce_name, row_id) key the second group collides; under the group
    key both merge independently."""
    env = coalesce_env  # registers coalesce "merge_x" over branches ("left", "right")
    base_a = (LineageFrame(kind=FrameKind.EXPAND, group_id="g-exp", member_key="m-a"),)
    base_b = (LineageFrame(kind=FrameKind.EXPAND, group_id="g-exp", member_key="m-b"),)
    tokens = {
        ("g-fork-a", "left"): env.make_token("t-al", row_id="row-1", base=base_a, fork_group="g-fork-a", branch="left"),
        ("g-fork-a", "right"): env.make_token("t-ar", row_id="row-1", base=base_a, fork_group="g-fork-a", branch="right"),
        ("g-fork-b", "left"): env.make_token("t-bl", row_id="row-1", base=base_b, fork_group="g-fork-b", branch="left"),
        ("g-fork-b", "right"): env.make_token("t-br", row_id="row-1", base=base_b, fork_group="g-fork-b", branch="right"),
    }
    assert env.executor.accept(tokens[("g-fork-a", "left")], "merge_x").held is True
    assert env.executor.accept(tokens[("g-fork-b", "left")], "merge_x").held is True   # OLD KEY: raises duplicate-arrival
    merged_a = env.executor.accept(tokens[("g-fork-a", "right")], "merge_x")
    merged_b = env.executor.accept(tokens[("g-fork-b", "right")], "merge_x")
    assert merged_a.merged_token is not None
    assert merged_b.merged_token is not None
    assert merged_a.merged_token.token_id != merged_b.merged_token.token_id
```

(`env.make_token` builds `TokenInfo(..., lineage_path=base + (LineageFrame(FORK,
fork_group, branch),))` and seeds the frames durably where the fixture is DB-backed —
follow the file's existing token construction, updated by WS1.)

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_coalesce_executor.py -v -k sibling_fork_groups`
Expected: FAIL with `OrchestrationInvariantError: Duplicate arrival for branch 'left'`

- [ ] **Step 3: Implement.** Exact edits:

`coalesce_executor.py:511-512`:
```python
        # Pending tokens: (coalesce_name, fork_group_id) -> _PendingCoalesce
        self._pending: dict[tuple[str, str], _PendingCoalesce] = {}
```
(`_PendingCoalesce` docstring `:107`: "Tracks pending tokens for a single fork group at
a coalesce point."; `_completed_keys` comment likewise.)

`accept` `:802-804`:
```python
        # Get or create pending state for this FORK GROUP (spec §5 re-keying:
        # sibling EXPAND members share row_id, so row_id cannot key the group).
        fork_group_id = token.fork_group_id
        if fork_group_id is None:
            raise OrchestrationInvariantError(
                f"Token {token.token_id} has branch_name={token.branch_name!r} but no "
                f"fork_group_id — lineage corruption (branch and group ride one frame)."
            )
        key = (coalesce_name, fork_group_id)
```

`:811` and the late-arrival arm: `self._check_landscape_for_completion(coalesce_name,
fork_group_id)`.

`_check_landscape_for_completion` `:698-724` — parameter `row_id: str` becomes
`fork_group_id: str`; body swaps `has_completed_row_for_node(...)` for:
```python
        if self._barrier_restore_reads.has_completed_group_for_node(
            run_id=self._run_id, node_id=str(node_id), group_id=fork_group_id
        ):
            self._mark_completed((coalesce_name, fork_group_id))
            return True
        return False
```

`_resolve_pending` `:1325` and `_evaluate_after_loss` `:1592`:
`decide_coalesce(..., group_id=key[1])`; `coalesce_policy.py:113` renames the kwarg
(`row_id: str | None = None` → `group_id: str | None = None`) and any use inside
`decide_coalesce`'s diagnostics follows.

`flush_pending` `:1425` / `:1434-1439`: `coalesce_name, _fork_group_id = key` and the
invariant message reads `(fork group {_fork_group_id})`.

Docstrings that say "(coalesce_name, row_id) tuple" (`:998`, `:1014`, `:1090`,
`:1314`, `:1567`, `:1581`) all become "(coalesce_name, fork_group_id) tuple". Do NOT
touch `_execute_merge`/`_merge_data`/`build_coalesce_merge`/policy decisions — merge
behaviour is the frozen guard.

- [ ] **Step 4: Migrate the mechanical key shapes** in `test_coalesce_executor.py` /
  `test_coalesce_policy.py` (constructor kwargs and key-tuple literals only), run:

Run: `pytest tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_coalesce_policy.py tests/unit/engine/test_coalesce_pipeline_row.py tests/unit/engine/test_coalesce_contract_bug.py tests/property/engine/test_coalesce_properties.py -v`
Expected: PASS — any failure that is not a bare key-shape mismatch is a merge-behaviour
regression: STOP and fix the code, not the test.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/coalesce_executor.py src/elspeth/engine/coalesce_policy.py \
        tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_coalesce_policy.py
git commit -m "feat(engine): re-key coalesce pending state (coalesce_name,row_id)->(coalesce_name,fork_group_id) — accept + landscape completion (WS4, spec §5)"
```

---

### Task 9: Coalesce re-key — checkpoint scalars surface

**Files:**
- Modify: `src/elspeth/contracts/barrier_scalars.py` — `BARRIER_SCALARS_VERSION` (`:57`),
  `CoalescePendingScalars` docstring (`:168-181`), `BarrierScalars` docstrings +
  `to_dict`/`from_dict` local names (`:244-364`)
- Modify: `src/elspeth/engine/coalesce_executor.py` — `get_barrier_scalars` docstring
  (`:560-584`)
- Test: `tests/unit/contracts/test_barrier_scalars.py` (extend/migrate)

**Interfaces:**
- Consumes: Task 8's key semantics.
- Produces: `BarrierScalars.coalesce: Mapping[tuple[str, str], CoalescePendingScalars]`
  keyed `(coalesce_name, fork_group_id)`; wire shape unchanged
  (`[[name, group_id], scalars]` pairs); `BARRIER_SCALARS_VERSION = "2.0"` so any
  pre-re-key checkpoint fails CLOSED (`AuditIntegrityError: unsupported version`) instead
  of silently reinterpreting row_ids as group ids. Quarantine, do not migrate
  (recent-code-hints 2026-08-20 doctrine); pre-release wipe posture makes this cheap.
  The fail-closed bump is RATIFIED (2026-08-22 synthesis, canon item 10) — implement it
  without further maintainer confirmation.

- [ ] **Step 1: Write the failing test:**

```python
def test_barrier_scalars_version_2_rejects_v1_payloads() -> None:
    """The v1→v2 bump is the fail-closed guard against a stale checkpoint whose
    coalesce keys are (name, row_id): identical wire SHAPE, changed key
    SEMANTICS — silent reinterpretation is the failure this version gate exists
    to prevent."""
    v1_payload = {
        "_version": "1.0",
        "aggregation": {},
        "coalesce": [[["merge_x", "row-1"], {"_version": "1.0", "lost_branches": {}}]],
    }
    with pytest.raises(AuditIntegrityError, match="unsupported version"):
        BarrierScalars.from_dict(v1_payload)


def test_barrier_scalars_round_trips_group_keyed_coalesce_entries() -> None:
    scalars = BarrierScalars(
        aggregation={},
        coalesce={("merge_x", "g-fork-1"): CoalescePendingScalars(lost_branches={"left": "quarantined"})},
    )
    restored = BarrierScalars.from_dict(scalars.to_dict())
    assert restored.coalesce == scalars.coalesce
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/contracts/test_barrier_scalars.py -v -k "version_2 or group_keyed"`
Expected: FAIL (v1 payload currently accepted)

- [ ] **Step 3: Implement.** `BARRIER_SCALARS_VERSION = "2.0"` (the shared
  `_validate_envelope` then rejects both stale top-level and stale nested envelopes —
  note `AggregationNodeScalars`/`CoalescePendingScalars` serialize the same constant, so
  the bump is one edit). Rename the `from_dict` local `coalesce_row_id` →
  `coalesce_group_id` (`:356-357`) and rewrite the three docstrings: the
  `CoalescePendingScalars` header becomes "Scalar checkpoint state for one pending
  coalesce `(coalesce_name, fork_group_id)` key."; `BarrierScalars.coalesce` attr doc
  and the `to_dict` comment swap `row_id` for `fork_group_id`. In `coalesce_executor.py`
  `get_barrier_scalars` (`:577`), the Returns doc becomes "Mapping of
  `(coalesce_name, fork_group_id)` -> CoalescePendingScalars..." (the CODE is already
  key-shape-agnostic — it iterates `self._pending`, re-keyed in Task 8; verify no other
  change needed).

- [ ] **Step 4: Migrate existing version-literal pins** in
  `tests/unit/contracts/test_barrier_scalars.py` (and any `"1.0"` literals in
  `tests/unit/contracts/test_checkpoint.py`, `tests/unit/core/checkpoint/test_recovery.py`
  — `git grep -n '"1.0"' tests/unit/contracts tests/unit/core/checkpoint`), run:

Run: `pytest tests/unit/contracts/test_barrier_scalars.py tests/unit/contracts/test_checkpoint.py tests/unit/core/checkpoint/ -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/contracts/barrier_scalars.py src/elspeth/engine/coalesce_executor.py \
        tests/unit/contracts/test_barrier_scalars.py tests/unit/contracts/test_checkpoint.py
git commit -m "feat(contracts): re-key coalesce checkpoint scalars to fork_group_id; bump BARRIER_SCALARS_VERSION to fail closed on v1 checkpoints (WS4)"
```

---

### Task 10: Coalesce re-key — journal restore into the group-keyed dict

**Files:**
- Modify: `src/elspeth/engine/journal_restore.py` — `RestoredPendingCoalesce.key`
  (`:66-79`), the grouping key (`:219`), scalar-only handling (`:300-334`),
  `_reconstruct_completed_keys_from_landscape` (`:346-369`)
- Modify: `src/elspeth/engine/coalesce_executor.py` — `restore_from_journal` docstring
  (`:586-653`; the apply loop `:672-688` is key-shape-agnostic — verify, don't edit)
- Test: `tests/unit/engine/test_journal_restore.py` (extend/migrate)

**Interfaces:**
- Consumes: WS1a `TokenWorkItem.lineage_path` (the journal row's lineage truth) +
  `LineageFrame`/`FrameKind`; Task 2 `get_completed_group_ids_for_nodes`; Task 8/9 key
  semantics.
- Produces: `CoalesceJournalRestorer.restore(...)` returns state keyed
  `(coalesce_name, fork_group_id)`; `RestoredPendingCoalesce.key` documents the new
  tuple. Restore call signature unchanged.

- [ ] **Step 1: Write the failing test** (in `test_journal_restore.py`, following its
  existing item-construction helpers, which WS1 migrated to `lineage_path`):

```python
def test_restore_groups_journal_items_by_fork_group_not_row(journal_env) -> None:
    """Two sibling fork groups share row_id; restore must rebuild TWO pending
    entries, not raise a duplicate-branch claim."""
    env = journal_env  # coalesce "merge_x" over ("left", "right")
    items = [
        env.blocked_item(token_id="t-al", row_id="row-1", fork_group="g-a", branch="left"),
        env.blocked_item(token_id="t-bl", row_id="row-1", fork_group="g-b", branch="left"),
        env.blocked_item(token_id="t-ar", row_id="row-1", fork_group="g-a", branch="right"),
    ]
    restored = env.restorer.restore(
        items=items, scalars={}, state_ids=env.state_ids(items),
        attempt_offsets=env.offsets(items), resume_checkpoint_id="ckpt-1", now=env.now,
    )
    keys = {group.key for group in restored.pending}
    assert keys == {("merge_x", "g-a"), ("merge_x", "g-b")}


def test_restore_rejects_journal_item_without_a_fork_frame(journal_env) -> None:
    env = journal_env
    item = env.blocked_item(token_id="t-x", row_id="row-1", fork_group=None, branch=None)
    with pytest.raises(AuditIntegrityError, match="innermost FORK frame"):
        env.restorer.restore(
            items=[item], scalars={}, state_ids=env.state_ids([item]),
            attempt_offsets=env.offsets([item]), resume_checkpoint_id="ckpt-1", now=env.now,
        )
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_journal_restore.py -v -k fork_group`
Expected: FAIL (duplicate-branch-claim raise from the row-keyed grouping)

- [ ] **Step 3: Implement.** In the validation/grouping loop (`:149-232`, as landed by
  WS1 — WS1 already rewrote the branch derivation to the lineage path; reconcile with
  its landed shape), derive both facts from ONE frame:

```python
            innermost = item.lineage_path[-1] if item.lineage_path else None
            if innermost is None or innermost.kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"Journal BLOCKED row for token {item.token_id!r} at coalesce "
                    f"{item.coalesce_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has no innermost FORK frame — only forked "
                    f"branch tokens hold at a coalesce."
                )
            branch_name = innermost.member_key
            # ... existing branch-in-settings.branches validation, against branch_name ...
            key = (item.coalesce_name, innermost.group_id)
```

with the duplicate-claim message updated to name the fork group instead of the row.
Scalar-only handling (`:300-334`): the unpacking `coalesce_name, row_id = scalar_key`
becomes `coalesce_name, fork_group_id = scalar_key` and messages follow ("Checkpoint
lost_branches for coalesce {name!r} fork group {fork_group_id!r} ...").
`_reconstruct_completed_keys_from_landscape` swaps
`get_completed_row_ids_for_nodes` for `get_completed_group_ids_for_nodes` (same
node-id→name mapping shape). `RestoredPendingCoalesce.key` comment: `# (coalesce_name,
fork_group_id)`.

- [ ] **Step 4: Migrate the mechanical key shapes** in `test_journal_restore.py`, run:

Run: `pytest tests/unit/engine/test_journal_restore.py tests/unit/engine/test_coalesce_executor.py -v`
Expected: PASS

- [ ] **Step 5: Commit, then slice boundary** (full suite — the restore path feeds the
  e2e recovery suites; trust-tier diff; wardline):

```bash
git add src/elspeth/engine/journal_restore.py src/elspeth/engine/coalesce_executor.py \
        tests/unit/engine/test_journal_restore.py
git commit -m "feat(engine): coalesce journal restore groups by fork_group_id (WS4, spec §5)"
```

---

### Task 11: Coalesce re-key — loss dedup + notify surfaces and their callers

**DEPENDS ON WS3:** the `group_losses` ledger machinery (record / list / adopt /
replay — WS3 Tasks 2 and 7; the `GroupLossSpec` CONTRACT and the `group_losses` TABLE
are WS1a Tasks 3–4, already landed) must be landed (see Preconditions). If absent,
STOP here.

**Files:**
- Modify: `src/elspeth/engine/coalesce_executor.py` — `has_recorded_branch_loss`
  (`:1460-1481`), `notify_branch_lost` (`:1483-1562`), `_evaluate_after_loss` docstring
- Modify: `src/elspeth/engine/processor.py` — `_notify_coalesce_of_lost_branch`'s
  executor call (`:3219-3224`) and the `BranchLossSpec` staging block (`:3202-3211`,
  which WS3 turns into `GroupLossSpec` — coordinate: whichever plan lands second updates
  the construction; the executor-call edit below is THIS plan's)
- Modify: `src/elspeth/engine/barrier_coordination.py` — the §E.5 replay pair
  (`:778-783`: `has_recorded_branch_loss(loss.coalesce_name, loss.row_id,
  loss.branch_name)` / `notify_branch_lost(...)`)
- Test: `tests/unit/engine/test_coalesce_executor.py`,
  `tests/unit/engine/test_barrier_coordination.py` (migrate call shapes; add the
  discriminating test below)

**Interfaces:**
- Consumes: WS1a Task 3's `GroupLossSpec` (fields in Preconditions) through WS3's
  ledger replay records (they expose `.closer_name`, `.group_id`, `.member_key`).
- Produces:

```python
def has_recorded_branch_loss(self, coalesce_name: str, fork_group_id: str, branch_name: str) -> bool: ...
def notify_branch_lost(self, coalesce_name: str, fork_group_id: str, lost_branch: str, reason: str) -> CoalesceOutcome | None: ...
```

(parameter position 2 changes MEANING from row_id to fork_group_id — the rename in the
signature is the mypy-visible fence; every caller is updated in this task, verified by
`git grep -n "notify_branch_lost\|has_recorded_branch_loss" src/elspeth` returning ONLY
group-passing sites.)

- [ ] **Step 1: Write the failing test** (in `test_coalesce_executor.py`):

```python
def test_loss_notification_is_group_scoped(coalesce_env) -> None:
    """A loss in fork group A must not settle fork group B on the same row."""
    env = coalesce_env
    a_left = env.make_token("t-al", row_id="row-1", fork_group="g-a", branch="left")
    b_left = env.make_token("t-bl", row_id="row-1", fork_group="g-b", branch="left")
    env.executor.accept(a_left, "merge_x")
    env.executor.accept(b_left, "merge_x")
    outcome = env.executor.notify_branch_lost("merge_x", "g-a", "right", "quarantined")
    # require_all: group A fails; group B is untouched and still pending.
    assert outcome is not None and outcome.failure_reason is not None
    assert {t.token_id for t in outcome.consumed_tokens} == {"t-al"}
    assert env.executor.has_recorded_branch_loss("merge_x", "g-b", "right") is False
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_coalesce_executor.py -v -k group_scoped`
Expected: FAIL (signature still takes row_id; both groups collide or the wrong group
settles)

- [ ] **Step 3: Implement.** `has_recorded_branch_loss` (`:1460-1481`): parameter
  `row_id` → `fork_group_id`; `key = (coalesce_name, fork_group_id)`; the completion
  fallback calls `self._check_landscape_for_completion(coalesce_name, fork_group_id)`.
  `notify_branch_lost` (`:1483-1562`): same substitution throughout (`key = (coalesce_name,
  fork_group_id)` at `:1516`, completion check at `:1520`, docstring Args). Caller edits:
  - `processor.py:3219-3224`:
    ```python
        lost_group_id = current_token.fork_group_id
        if lost_group_id is None:
            raise OrchestrationInvariantError(
                f"Token {current_token.token_id} has branch_name={current_token.branch_name!r} "
                f"but no fork_group_id — lineage corruption."
            )
        outcome = self._coalesce_executor.notify_branch_lost(
            coalesce_name=coalesce_name,
            fork_group_id=lost_group_id,
            lost_branch=current_token.branch_name,
            reason=reason,
        )
    ```
  - `barrier_coordination.py:778-783`: the replayed durable record is WS3's
    `GroupLossSpec`-shaped row —
    ```python
            if self._coalesce_executor.has_recorded_branch_loss(loss.closer_name, loss.group_id, loss.member_key):
                continue
            outcome = self._coalesce_executor.notify_branch_lost(
                coalesce_name=loss.closer_name,
                fork_group_id=loss.group_id,
                lost_branch=loss.member_key,
                reason=loss.reason,
            )
    ```
    (reconcile attribute names against WS3's landed replay-record type; the dedup
    doctrine comment at `:711` stands.)

- [ ] **Step 4: Run to pass** (plus the §E.5 seam suites):

Run: `pytest tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_barrier_coordination.py tests/unit/engine/test_processor.py tests/integration/pipeline/test_barrier_intake_dispositions.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/coalesce_executor.py src/elspeth/engine/processor.py \
        src/elspeth/engine/barrier_coordination.py \
        tests/unit/engine/test_coalesce_executor.py tests/unit/engine/test_barrier_coordination.py
git commit -m "feat(engine): group-scope coalesce loss dedup and notify surfaces onto fork_group_id (WS4/WS3 integration)"
```

---

### Task 12: Row_union re-key onto the unified rosters

**DEPENDS ON WS3** (same gate as Task 11 — `group_losses` point reads).

**Files:**
- Modify: `src/elspeth/engine/row_union_executor.py` — `_pending` (`:158`),
  `_completed_keys` (`:164`), `_recorded_losses`/`_recorded_loss_groups` (`:169-172`),
  `restore_from_journal` grouping (`:197-280`), `restore_branch_losses` (`:281`),
  `accept` keying (`:351+`), `has_recorded_branch_loss` (`:601`), `is_group_released`
  (`:605`), `notify_branch_lost` (`:614-657`), `_check_landscape_for_completion`
  (`:660-685`), `_remember_branch_loss` (`:692-703`)
- Modify: `src/elspeth/core/landscape/scheduler/restore_read_model.py` —
  `has_branch_loss_for_group` (`:239-250`) re-pointed at WS3's `group_losses`
  (`closer_name` + `group_id` predicate), renamed `has_group_loss`
- Modify: `src/elspeth/engine/processor.py` — `_notify_row_union_of_lost_branch`'s
  executor call (`:3070`) passes `current_token.fork_group_id` (same None-guard shape as
  Task 11's)
- Modify: `src/elspeth/engine/barrier_coordination.py` — the row_union replay pair
  (`:737-741`), same substitution as Task 11's coalesce pair
- Test: `tests/unit/engine/test_row_union_executor.py` (migrate + one discriminating
  test), `tests/unit/core/landscape/test_barrier_restore_read_model.py`

**Interfaces:**
- Consumes: WS1a accessors; the `group_losses` ledger (WS1a Task 4 table, WS3 Tasks 2/7
  machinery); Task 2 group reads
  (`has_completed_group_for_node`, `has_released_group_for_node`).
- Produces: all row_union group state keyed `(row_union_name, fork_group_id)`; loss
  identities `(row_union_name, fork_group_id, member_key)`;
  `is_group_released(row_union_name, fork_group_id)`. Union release semantics, declared
  branch order, v1 fail-closed policy — untouched (guarded by the four
  `tests/integration/pipeline/test_row_union_*` suites + `test_row_union_executor.py`).
  NOTE: ruling 27 (release pops frames — a WS1 delta) removes the released-token
  re-entry into the loss path (a released token's `branch_name` is None, so
  `processor.py:3149`'s guard short-circuits); do NOT delete `is_group_released` here —
  WS3's seam rewrite owns its caller's fate.

- [ ] **Step 1: Write the failing test** (mirror Task 8's discriminator):

```python
def test_sibling_fork_groups_sharing_row_id_release_independently(row_union_env) -> None:
    env = row_union_env  # row_union "variant_union" over ("control", "treatment")
    a1 = env.make_token("t-a1", row_id="row-1", fork_group="g-a", branch="control")
    a2 = env.make_token("t-a2", row_id="row-1", fork_group="g-a", branch="treatment")
    b1 = env.make_token("t-b1", row_id="row-1", fork_group="g-b", branch="control")
    assert env.executor.accept(a1, "variant_union").released_tokens == ()
    assert env.executor.accept(b1, "variant_union").released_tokens == ()   # OLD KEY: duplicate arrival
    released = env.executor.accept(a2, "variant_union")
    assert {t.token_id for t in released.released_tokens} == {"t-a1", "t-a2"}
```

- [ ] **Step 2: Run to verify failure**

Run: `pytest tests/unit/engine/test_row_union_executor.py -v -k sibling`
Expected: FAIL (duplicate-arrival raise)

- [ ] **Step 3: Implement.** Mechanical substitution with the SAME pattern as Tasks
  8/11 (derive `fork_group_id = token.fork_group_id` with the None-guard in `accept`
  and in `restore_from_journal`'s entry loop; every `(name, row_id)` key/message becomes
  `(name, fork_group_id)`; `restore_branch_losses` tuples become
  `(row_union_name, fork_group_id, member_key)`). `_check_landscape_for_completion`
  (`:660-685`) swaps its three reads: `has_completed_group_for_node`,
  `has_released_group_for_node`, and `has_group_loss(run_id=..., closer_name=name,
  group_id=...)`. In `restore_read_model.py`, replace `has_branch_loss_for_group` with:

```python
    def has_group_loss(self, *, run_id: str, closer_name: str, group_id: str) -> bool:
        """Whether the unified ledger records any loss for one bound group."""
        query = (
            select(group_losses_table.c.loss_id)
            .where(
                group_losses_table.c.run_id == run_id,
                group_losses_table.c.closer_name == closer_name,
                group_losses_table.c.group_id == group_id,
            )
            .limit(1)
        )
        return self._ops.execute_fetchone(query) is not None
```

(`group_losses_table` from WS3's landed schema; delete the
`coalesce_branch_losses`-backed predecessor in the same edit — no dual read.)

- [ ] **Step 4: Migrate mechanical shapes and run:**

Run: `pytest tests/unit/engine/test_row_union_executor.py tests/integration/pipeline/test_row_union_ab_experiment.py tests/integration/pipeline/test_row_union_branch_cardinality.py tests/integration/pipeline/test_row_union_branch_loss.py tests/integration/pipeline/test_row_union_identity_branches.py -v`
Expected: PASS (release semantics unchanged; only key shapes moved)

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/engine/row_union_executor.py src/elspeth/core/landscape/scheduler/restore_read_model.py \
        src/elspeth/engine/processor.py src/elspeth/engine/barrier_coordination.py \
        tests/unit/engine/test_row_union_executor.py tests/unit/core/landscape/test_barrier_restore_read_model.py
git commit -m "feat(engine): re-key row_union group state onto fork_group_id and the unified group_losses ledger (WS4)"
```

---

### Task 13: Guard sweep, untouched-file proof, and the full-matrix boundary

**Files:**
- Modify (mechanical only, if red): `tests/property/engine/test_coalesce_properties.py`,
  `tests/property/engine/test_processor_coalesce_equivalence_properties.py`,
  `tests/integration/pipeline/test_barrier_intake_dispositions.py` (shared builders —
  cross-tier load-bearing; migrate ONCE, three tiers inherit)
- Test: everything in the header's guard list

- [ ] **Step 1: Run the property guards with capped parallelism** (standing rule: `-n 0`
  for mutation-adjacent/property-heavy runs):

Run: `pytest tests/property/engine/test_coalesce_properties.py tests/property/audit/test_fork_coalesce_flow.py tests/property/audit/test_fork_join_balance.py -n 0 -v`
Expected: PASS. `test_coalesce_properties.py` pins merge policies, merge strategies, the
`_max_completed_keys` FIFO bound, and late-arrival consistency — "plugin-visible merge
behaviour unchanged" (spec §5) is MEASURED by this suite staying green modulo the key
shape. Fix mechanical key shapes only.

- [ ] **Step 2: Prove the untouched files are untouched:**

Run: `git diff origin/release/0.7.2...HEAD --stat -- src/elspeth/engine/executors/aggregation.py src/elspeth/engine/triggers.py`
Expected: EMPTY output. (Use the campaign's WS4 start commit as the base if the branch
tip has moved.) Any diff here is a spec violation (spec §9 row 4) — revert it.

- [ ] **Step 3: Full-matrix slice boundary:**

```bash
git rev-parse HEAD   # record
pytest tests/
git rev-parse HEAD   # must equal the recording, or re-run
```
Expected: green (modulo the branch's pre-existing known-red state — diff failures
against a pre-WS4 baseline run, never against zero).

Then the trust-tier corpus diff (counts equal before/after WS4 — COUNT, don't tail) and
the wardline gate command from Global Constraints (expect exit 0).

- [ ] **Step 4: Postgres qualification** (where Docker is available):

Run: `pytest tests/testcontainer/core/ -m testcontainer -v`
Expected: PASS. The `group_losses`-backed suites are WS3's; the coalesce-recovery suite
(`test_barrier_recovery_postgres.py`) exercises Task 8/10's re-keyed restore against a
real dialect. The collector's committed-flush-recovery Postgres twin and the e2e
death-matrix/timing-invariance `collector` families are the **WS3+WS4 integration**
item's tests (test-harness scout §1.1/§1.2 names the exact extension points:
`_real_collector_executor` builder + a `_complete_collector_fire` pause seam) — record
them as the integration plan's opening tasks, not here.

- [ ] **Step 5: Final commit (test migrations only, if any):**

```bash
git add tests/property/engine/test_coalesce_properties.py \
        tests/property/engine/test_processor_coalesce_equivalence_properties.py \
        tests/integration/pipeline/test_barrier_intake_dispositions.py
git commit -m "test(engine): migrate shared barrier builders and property guards to group-keyed coalesce state (WS4 closeout)"
```

---

## Handed to the WS3+WS4 integration line item (spec §11 — NOT this plan)

Recorded so nothing silently drops: (1) processor wiring — `RowProcessor` constructs
`CollectorExecutor`, routes arrivals via `mark_blocked` with `collector_name`, adds the
`BarrierIntakeCoordinator` collector adoption arm (coalesce-shaped: no batch membership
at arrival), threads `ctx`, and consumes `CollectorOutcome` in the WS3 settle-member
seam (member terminals, `scope_group_failed` survivors, escalation per
`ScopeSettings.on_group_failure`); (2) `notify_empty_group` wiring at the opener's
zero-row path; (3) the e2e death-matrix `collector` family, timing-invariance
takeover-composition arm, Postgres committed-flush twin, and the
suspended-winner fence arm for the collector arrival CAS; (4) WS5's
`has_blocked_barrier_work`/satisfiability interactions.

## Ratified decisions (2026-08-22 synthesis — formerly Open Questions; do not re-litigate)

Every question this plan's first draft flagged was adjudicated by the cross-plan
synthesis. Recorded here so the implementer treats them as decided, not as assumptions:

1. **Collector release-emission model (Task 3): RATIFIED.** The M-output flush emission
   follows the aggregation-flush precedent — outputs form a fresh EXPAND group over the
   popped base path, with a universal `group_records` mint and representative-parent
   `token_parents` rows. The WS3+WS4 integration freezes its audit fixtures on this
   shape.
2. **`ScopeSettings.on_group_failure` defaults to `"quarantine"`: RATIFIED** — and the
   field is AUTHORED by WS2 Task 2 (this plan only consumes it; Task 1). An outermost
   `on_group_failure: quarantine` yields the COMPLETED-family run status (quarantine is
   handled failure, as today).
3. **Group-failure reason vocabulary: ACCEPTED as categorical.**
   `"collector_missing_members"` and `"collector_transform_error"` are ratified reason
   tokens and reach `group_losses.reason` through WS3 unchanged (`empty_expansion`,
   `all_members_lost`, `scope_group_failed` were already specced).
4. **`BARRIER_SCALARS_VERSION` fail-closed bump (Task 9): RATIFIED.** Stale
   `(name, row_id)`-keyed checkpoints fail closed (quarantine, don't migrate); the
   pre-release wipe posture absorbs the one-stroke dev-checkpoint invalidation.
5. **Sibling plan filenames: RESOLVED** — verified on disk and cited by full name in
   the Preconditions section: `2026-08-21-unified-lineage-ws1a-model-core.md`,
   `2026-08-21-unified-lineage-ws2-config-validation.md`,
   `2026-08-21-unified-lineage-ws3-settlement.md`.

No open questions remain in this plan. (The one campaign-wide item still open — the
`examples/row_union_ab_experiment/settings_screened.yaml` replacement story — is a
maintainer pedagogy call tracked outside this plan.)
