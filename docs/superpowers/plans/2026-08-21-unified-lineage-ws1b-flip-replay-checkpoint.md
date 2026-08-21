# Unified Lineage WS1b — Consumers, Atomic Flip, Replay, Checkpoint — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rewrite every consumer of the retired tri-field lineage (`fork_group_id` / `expand_group_id` / `branch_name`, plus `join_group_id` leaving `TokenInfo`) to be path-aware, then execute the ONE atomic representation flip (four-table column retirement, journal codec, all replay predicates, fixture regeneration), guard the flipped state with a lint/AST gate, and run the WS1 frozen-oracle checkpoint with its STOP rule.

**Architecture:** Phase A lands behaviour-neutral consumer rewrites one green commit at a time — each site stops reading the stored tri-fields and reads either the `lineage_path` (dual-written by WS1a) or the WS1a join-context carriers, with §4.1a decision-pinning tests. Phase B is the atomic flip: a single reviewable change that deletes the stored fields/columns, moves lineage onto `token_lineage_frames` + the `lineage_path_json` work-item column (landed by WS1a, made sole-truth here), rewrites the Tier-1 replay predicates to frame equality + strict pop, and regenerates the one adjudicated fixture. Phase C pins the flipped state (AST guard) and executes the WS1 checkpoint diff.

**Tech Stack:** Python 3.12+ (uv venv), SQLAlchemy Core (SQLite + PostgreSQL testcontainers), pytest (+xdist), dataclasses (frozen/slots), the Landscape audit schema, the DAG scenario corpus harness, TypeScript (one frontend type file + two test fixtures).

**Spec:** docs/superpowers/specs/2026-08-21-barrier-scopes-full-nesting-spec.md (rev 3.2 — rulings 1–28 final; §4.1a is the delta contract, §4.3 the retirement matrix, §4.4 the replay predicates, §11 the checkpoint and STOP rule).

## Global Constraints

- **Shared checkout:** stage by explicit pathspec ONLY (`git add <path> <path>`), never `git add -A`/`-u`; sibling agents' hunks must never ride your commit. Do NOT touch `src/elspeth/web/composer/state.py` or `tests/unit/web/composer/test_state.py` (maintainer is committing them).
- **Hooks:** never bypass pre-commit hooks except under the documented `--no-verify`-with-end-of-slice-reconciliation grant; `git stash` is blocked — use commits.
- **Full suite at slice boundaries:** whole-tree AST gates (attribute-contracts, masquerade, rejection-parity, serialisation-contract) miss scoped runs. Run the full `pytest tests/` at the end of Phase A, after the flip commit, and at the checkpoint. Record `git rev-parse HEAD` before AND after every full run; if they differ the result is uninterpretable — re-run.
- **Trust-tier corpus:** diff the `elspeth-lints` finding corpus before/after each slice. Baseline command: `ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing elspeth-lints check --rules all --root src/elspeth`. The gate is deliberately fail-closed with a standing corpus — compare counts against the pre-slice corpus, ADD NOTHING. Never hand-edit a `judge_metadata_signature`; do not stage signing bundles across this campaign.
- **Wardline gate:** `wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only` (exit 0 = clean and non-inert) before handing back any slice that touches external input.
- **Depth cap / fixpoint bound (spec §6.3):** the supported bound-nesting guarantee is 5 layers, build-enforced fail-closed, config-overridable; the escalation fixpoint's non-convergence bound is derived at build from the actual depth (+ margin), never a constant. Nothing in this plan may hard-code a depth constant into runtime logic.
- **Atomic flip discipline (spec §11):** Phase B is ONE commit. Schema columns are never half-deleted; no dual reads land at any commit boundary; every landed state leaves the tree consistent with a single representation per surface.
- Work on `release/0.7.2` directly (release-branch discipline). Commit promptly per task in Phases A and C; Phase B commits exactly once.
- `-n 0` for any mutation-style or timing-sensitive run; cap test parallelism if fanning out subagents.
- Standing procedures: docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md §S1–§S5 govern fixture freezing, slice gates, casualty retirement, judge-bundle sequencing, and the WS1 STOP rule.

## Canonical contracts (verbatim — copy into code exactly; do not rename)

- `LineageFrame(kind: FrameKind, group_id: str, member_key: str)` frozen slots dataclass; `FrameKind.FORK | FrameKind.EXPAND` in `contracts/enums.py`.
- `TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()` outermost first. Derived accessors (read-only properties): `branch_name` / `fork_group_id` (innermost FORK frame), `expand_group_id` (innermost EXPAND frame). `join_group_id` leaves `TokenInfo`.
- `GroupLossSpec(closer_name, group_id, member_key, token_id, reason)` replaces `BranchLossSpec` (WS3 — NOT this plan; `BranchLossSpec` and `coalesce_branch_losses` survive this plan unchanged and are allowlisted in the Task 13 guard).
- Tables: `token_lineage_frames(token_id, run_id, depth, kind, group_id, member_key)` PK`(token_id, run_id, depth)` INDEX`(run_id, group_id, member_key)`; `group_records(run_id, group_id, kind, opener_token_id, member_count, created_at)` PK`(run_id, group_id)`, minted for BOTH kinds — FORK and EXPAND, empty expansions included (WS1a is authoritative; the FORK roster AUTHORITY stays config); `group_losses` (DDL lands with WS1a Task 4; the ledger is written from WS3, not here).
- Strict pop: a closer pops exactly its own (innermost) frame; violation = `OrchestrationInvariantError` (live) / `AuditIntegrityError` (replay divergence, §4.4).
- Ruling 27: row_union pops the branch frame on EACH released token. Ruling 28: undeclared expand inside a bound region = `GraphValidationError` (WS2, not here — but the replay predicates written here are entitled to assume one-token-per-member).

## Consumed interfaces from sibling plans

**From the WS1a plan** (`docs/superpowers/plans/2026-08-21-unified-lineage-ws1a-model-core.md` — its "What WS1b consumes from this plan" section is the handoff contract; task numbers below are that plan's, canon numbering ratified 2026-08-22: 1 contracts helpers, 2 `TokenInfo.lineage_path`, 3 `GroupLossSpec`, 4 schema DDL/epoch 34, 5 journal plumbing, 6 durable writers, 7 empty-expansion mint, 8 TokenManager push/strict-pop, 8a nested fixtures, 9 join carriers, 10 `join_group_id` off `TokenInfo`, 11 verification/handoff):

- **WS1a Task 1:** `elspeth.contracts.enums.FrameKind` — `class FrameKind(StrEnum): FORK = "fork"; EXPAND = "expand"`; `elspeth.contracts.identity.LineageFrame` — `@dataclass(frozen=True, slots=True)` with `kind: FrameKind`, `group_id: str`, `member_key: str`; the JSON codec `lineage_path_to_json(path: tuple[LineageFrame, ...]) -> str` / `lineage_path_from_json(raw: str) -> tuple[LineageFrame, ...]` in `elspeth.contracts.identity` (raises `ValueError` on corrupt input — the journal mapping wraps it to `AuditIntegrityError`, Task 5); `innermost_fork_frame(path) -> LineageFrame | None` / `innermost_expand_frame(path) -> LineageFrame | None`; the path helpers `path_branch_name(path: tuple[LineageFrame, ...]) -> str | None` (innermost FORK frame's `member_key`), `path_fork_group_id(path) -> str | None`, `path_expand_group_id(path) -> str | None` (thin wrappers over the two innermost-frame functions), and `pop_closer_frame(path: tuple[LineageFrame, ...], *, kind: FrameKind, group_id: str) -> tuple[LineageFrame, ...]` (strict pop — raises `OrchestrationInvariantError` unless `path[-1]` is exactly the closer's own frame). This plan does NOT define sibling helpers — the flip's `TokenInfo` properties, the MCP projections, and the row_union release pop call these.
- **WS1a Task 2:** `TokenInfo.lineage_path: tuple[LineageFrame, ...] = ()` — present alongside the still-stored tri-fields through Phase A ("prep: consumers migrate while still backed by stored fields", spec §11); WS1a Task 10 leaves the final pre-flip field set `(row_id, token_id, row_data, branch_name, fork_group_id, expand_group_id, lineage_path, resume_attempt_offset, resume_checkpoint_id)`.
- **WS1a Task 4 (epoch 34):** the three new tables — `token_lineage_frames_table` — `(token_id String(64), run_id String(64), depth Integer, kind String(16), group_id String(64), member_key String(128))`, PK `(token_id, run_id, depth)`, `Index("ix_token_lineage_frames_group", run_id, group_id, member_key)`; `group_records_table` — `(run_id, group_id, kind, opener_token_id, member_count Integer, created_at DateTime)`, PK `(run_id, group_id)`, unique opener index `uq_group_records_opener`; `group_losses_table` — `(loss_id PK, run_id, closer_name, group_id, member_key, token_id, reason String(64), recorded_by, recorded_at, adopted_epoch)` (written from WS3; DDL is Task 4's) — plus `token_work_items.lineage_path_json Text NOT NULL` with NO server default (WS1a's Task 4/5 combined-commit fallback covered the DDL-before-threading window — decision D7 records that this plan has no default to drop).
- **WS1a Task 5:** journal plumbing — `TokenWorkItem.lineage_path: tuple[LineageFrame, ...] = ()`; `BarrierEmission.lineage_path`; `ScheduledWorkFields.lineage_path`; every enqueue verb and `scheduler_repository` wrapper accepts `lineage_path: tuple[LineageFrame, ...] = ()`; `item_from_mapping` decodes `lineage_path_json` via Task 1's codec and wraps its `ValueError` in `AuditIntegrityError`; `token_from_journal_item` already reconstructs `lineage_path` codec-purely; `IncompleteTokenSpec.lineage_path: tuple[LineageFrame, ...]` (loaded from `token_lineage_frames` in `_get_incomplete_token_work`) with `resume_incomplete_token`'s TokenInfo reconstruction already threading it — this plan's Task 4 rewrites the DISPATCH over that field.
- **WS1a Task 6:** durable writers — every fork/expand child and coalesce-merged token gets its full frame stack in `token_lineage_frames`, written in the SAME transaction as its token INSERT; every fork/expand opener mints one `group_records` row (BOTH kinds — canon: WS1a is authoritative; FORK roster authority stays config); replay/idempotent paths (`_reconcile_fork_replay`, `_reconcile_expansion_replay`, existing-coalesce-effect) return BEFORE the mint; the durable coalesce strict pop refuses (`AuditIntegrityError`) parents whose innermost durable frame is not a shared-group FORK frame. Private helpers `_insert_lineage_frames(..., frames: Sequence[LineageFrame])` and `_load_lineage_frames(...) -> tuple[LineageFrame, ...]` (already `LineageFrame`-typed — this plan's flip only renames the crafted-token seam kwarg, Task 9 step 3); the crafted-token seam `create_token(..., lineage_frames: Sequence[LineageFrame] = ())` (Task 6e). NOTE: `fork_token`/`expand_token` load the parent's frames durably via `_load_lineage_frames` — WS1a has NO `parent_lineage_path` kwarg and NO supplied-path cross-check; this plan adds both at the flip (`_assert_parent_lineage`, Task 10 step 5, decision D8).
- **WS1a Task 7:** empty-expansion mint — `DataFlowRepository.record_empty_expansion(parent_ref: TokenRef) -> str` (idempotent per opener via `uq_group_records_opener`; divergent replay raises `AuditIntegrityError`) + `TokenManager.record_empty_expansion`, minting the `member_count=0` EXPAND `group_records` row, gated on `creates_tokens=True` (2026-08-22 spec correction).
- **WS1a Task 8:** in-memory push/strict-pop — every `TokenInfo` minted by `TokenManager` carries its full `lineage_path`; `TokenManager.coalesce_tokens` performs the in-memory strict pop (`OrchestrationInvariantError` — the engine-layer twin of Task 6's durable check); the three shared cross-tier test-builder modules already construct frames ALONGSIDE the tri-fields (in-memory path AND durable frames via the Task 6e seam). Row_union release pop is deliberately NOT in WS1a (ruling 27 — this plan's Task 10 step 4).
- **WS1a Task 8a:** the nested differential fixtures — fork-in-fork depth-2 AND expand-in-fork nested scenario dirs under `tests/fixtures/dag_scenario_corpus/v1/`, wired into `EXPECTED_SCENARIOS` and `oracle_freeze.SCENARIO_CLASSIFICATION` as FROZEN. §4.1a rows 2–4 get corpus coverage from exactly these (fixture-oracle §6).
- **WS1a Task 9:** the §4.1 join-event carrier fields and threading: `RowResult.join_group_id: str | None = None` (required non-empty iff COALESCED, forbidden otherwise), `PendingOutcome.join_group_id` (same rule; NOT in the sink-flush grouping key), the `TokenManager.coalesce_tokens(...) -> tuple[TokenInfo, str]` tuple return, and the `WorkItem.join_group_id`/`CoalesceOutcome.join_group_id` threading. **WS1a Task 10** lands the reader rewires: `SinkExecutor.write(..., *, join_group_id_by_token: Mapping[str, str | None])` (keyword, REQUIRED, no default — WS1a Task 10c), `outcomes.py:257`, the `sink_flush.py` per-token map, `executors/sink.py`, and the `payload_codec`/drain kwarg drops. This plan does NOT re-land any of it (Task 3 verifies and finishes the one residual contract).
- **WS1a Task 10:** `TokenInfo` WITHOUT `join_group_id` (ruling 20).
- **WS1a Task 8 (mint-integration pin):** `tests/unit/engine/test_token_lineage_path.py` — the §4.1a rows pinned over REAL minting sequences via the path helpers. **This plan re-points that suite's stored-field assertions onto the flip's accessors** (Task 12 step 4) and owns §4.1a row 6 (ruling 27), which WS1a deliberately excludes.

**From the frozen-oracle PROTOCOLS plan** (`docs/superpowers/plans/2026-08-21-unified-lineage-protocols.md`):

- **Protocols Tasks 1–3:** the oracle-freeze registry `tests/fixtures/dag_scenario_corpus/oracle_freeze.py` (`frozen_surface(evidence)`, `invariant_subset(surface)`, `snapshot_path(scenario_id, case_id)`, `canonical_bytes(surface)`, per-scenario `SCENARIO_CLASSIFICATION`); the compare gate `tests/integration/core/dag/test_oracle_freeze.py` (write mode `ELSPETH_ORACLE_FREEZE=write`); the executed pre-WS1 freeze — snapshots committed under `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/` at a recorded HEAD. The rewritten corpus harness is checked against those stored bytes, never its own regeneration (fixture-oracle risk 6).
- **Protocols §S1:** the regeneration procedure this plan's Task 12 step 3 follows for `row-union-interleave` (targeted `ELSPETH_ORACLE_FREEZE=write` run, snapshot diff review, manifest hand-edit + dated rotation-ledger note).
- The new genuinely-nested frozen fixtures are authored by **WS1a Task 8a** (fork-in-fork depth-2 AND expand-in-fork) and frozen by **protocols Task 3** — in that order; without them §4.1a rows 2–4 have zero corpus coverage (fixture-oracle §6). Protocols §S1 executes before the WS1b flip (WS1b Task 7). **Do not begin Task 7 until WS1a Task 8a's fixture commit AND the protocols plan's Task 3 freeze commit exist on the branch.**

## Inline decisions (settled here, per the spec's "settled when the plan touches the file")

- **D1 — `token_work_items.join_group_id` COLUMN STAYS** (resolves the §4.1-vs-§4.3 tension, consumer-roster risk note 3). Ruling 20 keeps the CONTRACT field for merged tokens; a contract field that does not survive the journal breaks codec purity (`token_from_journal_item` reconstructs purely) and makes the COALESCED pending-sink redrive (`scheduler_drain.py:918` rebuild) unable to rebuild its `RowResult` join context after a crash. The `schema.py:851` COALESCED-arm predicate therefore keeps its `join_group_id` evidence clauses verbatim; "re-point at the work item's barrier fields" is satisfied vacuously because the retained column IS work-item state, not token lineage. Deleted from `token_work_items`: `branch_name`, `fork_group_id`, `expand_group_id` only. Guard allowlist (Task 13): `tokens.join_group_id` + `token_work_items.join_group_id`. (Ratified 2026-08-22, ruling 20: the column is KEPT; this plan's column-deletion slice explicitly allowlists it.)
- **D2 — `token_outcomes.expected_branches_json` is DELETED with the tri-columns** (consumer-roster risk note 11). Its sole Tier-1 consumer is `_reconcile_fork_replay`, which after the rewrite derives the recorded roster from the children's persisted FORK frames (written in the same atomic transaction as the outcome row, so integrity is equivalent). Exports and `database.py:300` drop it.
- **D3 — export shape:** `TokenExportRecord` replaces the three retired fields with `lineage_path: list[list[str]]` (each frame `[kind, group_id, member_key]`, outermost first) and keeps `join_group_id`. `TokenOutcomeExportRecord` drops `fork_group_id`/`expand_group_id`/`join_group_id`/`expected_branches_json`. No separate frames record type — the token record is self-describing, which is what the corpus harness needs (Task 12). The three epoch-34 tables enter the portable export at THIS flip (ratified 2026-08-22): `token_lineage_frames` rides inside `TokenExportRecord.lineage_path`; `group_records` gets a new `GroupRecordExportRecord`; `group_losses` gets a new `GroupLossExportRecord` (stream empty until WS3 writes the ledger). Manifest sha churn from the new/reshaped records is an allowed §11 delta; the oracle diff runs on the four stable projection classes, never on manifest blobs (fixture-oracle risk 4); Task 12 step 3a rotates the corpus manifest for the export-surface change.
- **D4 — ADR-019 pair matrix re-specification** (consumer-roster risk note 2): `_DISCRIMINATOR_FIELDS` shrinks to `("sink_name", "batch_id", "error_hash")`. `(TRANSIENT, FORK_PARENT)` and `(TRANSIENT, EXPAND_PARENT)` become `required=()` — their roster/group evidence is the children's `token_lineage_frames` rows plus (EXPAND) the `group_records` row, both written in the same transaction and enforced by the Task 10 replay predicates. `(SUCCESS, COALESCED)` loses its `join_group_id` requirement at the OUTCOME-row level; the merge event's durable anchor is `tokens.join_group_id` + the `coalesce_effects` composite FK, both kept. ADR-019 gets an amendment note in the same commit (`docs/architecture/adr/019-*.md`).
- **D5 — join-context carrier: LANDED BY WS1a Tasks 9–10** (recorded here because later tasks cite it): Task 9 lands the carriers — `RowResult.join_group_id: str | None = None` and `PendingOutcome.join_group_id: str | None = None` (NOT part of the sink-flush grouping key, so sink batch composition is unchanged), the `TokenManager.coalesce_tokens(...) -> tuple[TokenInfo, str]` return, and the `WorkItem`/`CoalesceOutcome` threading; Task 10 lands the reader rewires, including `SinkExecutor.write(..., *, join_group_id_by_token: Mapping[str, str | None])` (keyword, REQUIRED, no default — WS1a Task 10c). Never a DB query on the accounting path (§4.1 pinned commitment). This plan consumes these; Task 3 verifies them and finishes the `SinkEffectFinalizationMember` shrink.
- **D6 — diagnostics wire shape:** `RunDiagnosticToken` replaces the three retired fields with `lineage: list[RunDiagnosticLineageFrame]` (`kind`/`group_id`/`member_key`) and keeps `join_group_id`; `src/elspeth/web/frontend/src/types/index.ts` mirrors it. Pre-1.0 break posture — no compat shim.
- **D7 — journal lineage encoding:** the column is WS1a Task 4's `token_work_items.lineage_path_json Text NOT NULL` holding `contracts.identity.lineage_path_to_json(path)` (WS1a Task 1's codec; canonical: `"[]"` for empty); `TokenWorkItem.lineage_path: tuple[LineageFrame, ...] = ()` is the decoded contract field (WS1a Task 5). What THIS plan changes at the flip: the three retired columns/fields are deleted. (An earlier draft owed a `server_default="[]"` drop here — WS1a Task 4 landed the column NOT NULL with no default, its Task 4/5 combined-commit fallback covering the DDL-before-threading window, so there is nothing to drop; Task 9 step 1 verifies no default crept in.)
- **D8 — release-aware parent-lineage cross-check under ruling 27** (RATIFIED 2026-08-22: the journal-row release witness stands — no hedging remains). WS1a's writers derive a parent's frames durably (`_load_lineage_frames`) rather than trusting the caller, which implicitly assumes in-memory path == durable mint frames. The Task 10 step 4 release pop breaks that assumption for exactly one shape, so the flip threads the engine-supplied `parent_lineage_path` into the child-mint verbs (Task 9 step 3) and introduces `_assert_parent_lineage` (Task 10 step 5): the durable cross-check is `supplied == mint_frames` OR (`supplied == mint_frames[:-1]` AND `mint_frames[-1].kind is FrameKind.FORK` AND the token has a row_union release witness). The witness is the token's own completed barrier journal row: a `token_work_items` row for `(run_id, token_id)` with `row_union_name IS NOT NULL` whose status has left `BLOCKED` — row_union release is journal-first, so the completed BLOCKED row is durable release evidence. This is REACHABLE, not theoretical: `row-union-interleave` feeds released tokens into a downstream aggregation whose flush calls `expand_token` (processor.py:1623), which cross-checks the parent's path. Coalesce needs no weakening — its merged token is a NEW token whose mint frames ARE the popped path. Task 10 step 5 implements this with both-direction tests.

---

### Task 0: Mechanical pre-flight — diff every sibling-plan citation against its Produces block

> The consumed-interfaces section above was regenerated against the 2026-08-22 canon
> numbering, but plans drift as siblings land. Before ANY implementation work, verify
> mechanically that every artifact this plan consumes is what the cited task actually
> produced — a citation that names the wrong task, module, signature, or exception type
> turns a "consume, never re-land" boundary into a silent re-land or an AttributeError
> deep in Phase B.

**Files:** none modified in `src/` (read-only gate; citation fixes land in THIS plan file only)

- [ ] **Step 1: Enumerate the citations**

```bash
grep -n "WS1a Task [0-9]*a\?\|protocols plan's Task [0-9]\|Protocols Task" \
  docs/superpowers/plans/2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md
```

- [ ] **Step 2: Diff each citation against its Produces block.** For every hit, open the cited task in the sibling plan (`2026-08-21-unified-lineage-ws1a-model-core.md` / `2026-08-21-unified-lineage-protocols.md`) and compare the artifact this plan names — symbol name, module path, signature, exception type, table/column shape — against that task's **Produces** block AND against the live tree (WS1a is landed at this plan's entry). Canon numbering (ratified 2026-08-22): 1 contracts helpers, 2 `TokenInfo.lineage_path`, 3 `GroupLossSpec`, 4 schema DDL/epoch 34, 5 journal plumbing, 6 durable writers, 7 empty-expansion mint, 8 TokenManager push/strict-pop, 8a nested fixtures, 9 join carriers, 10 `join_group_id` off `TokenInfo`, 11 verification/handoff.

- [ ] **Step 3: Resolve mismatches before Task 1.** A citation mismatch → fix the citation in this plan and commit the doc fix (`git add docs/superpowers/plans/2026-08-21-unified-lineage-ws1b-flip-replay-checkpoint.md`). A genuine handoff gap (the sibling landed something different from its own Produces block) → STOP and surface to the maintainer; do not paper over it with a local shim.

---

### Task 1: Batch lineage-path reader — `load_lineage_paths` on the frames-table owner

> Path helpers (`path_branch_name` etc.) and the JSON codec (`contracts.identity.lineage_path_to_json` /
> `lineage_path_from_json`) are WS1a Task 1 deliverables (the journal threading is Task 5's) — this plan defines NO siblings.
> What WS1b needs and WS1a does not provide is a BATCH, self-connecting, public reader:
> WS1a Task 6's `_load_lineage_frames(conn, *, token_id, run_id)` is per-token and
> connection-scoped, while the resume workset, MCP projections, web diagnostics, the
> exporter, and the Task 10 replay predicates all need many tokens' paths in one query.

**Files:**
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` (beside WS1a Task 6's `_load_lineage_frames`)
- Test: `tests/unit/core/landscape/test_load_lineage_paths.py` (create)

**Interfaces:**
- Consumes: `LineageFrame`/`FrameKind` (WS1a Task 1), `token_lineage_frames_table` (WS1a Task 4), `fork_token` (WS1a Task 6 — it loads the parent's frames durably; no path kwarg exists pre-flip) as the test's frame producer.
- Produces (Tasks 4, 5, 6, 10, 11, 12 all call this — exact signature):
  - `RowTokenRepository.load_lineage_paths(run_id: str, token_ids: Sequence[str]) -> dict[str, tuple[LineageFrame, ...]]` — every requested id is a key; tokens with no frames rows map to `()`; frames ordered outermost first; non-dense depths raise `AuditIntegrityError`.

- [ ] **Step 1: Write the failing test**

```python
"""tests/unit/core/landscape/test_load_lineage_paths.py"""
import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.identity import LineageFrame
from elspeth.core.landscape.schema import token_lineage_frames_table

# Reuse the recording suite's real-DB builders (the shared-builder pattern the
# e2e suites already use). Verify these names against the module before writing.
from tests.unit.core.landscape.test_token_recording import _make_row, _setup


def test_load_lineage_paths_batches_and_orders_by_depth() -> None:
    db, factory = _setup()
    row, root = _make_row(factory)
    children, fork_group = factory.data_flow.fork_token(
        parent_ref=TokenRef(token_id=root.token_id, run_id="run-1"),
        row_id=row.row_id,
        branches=["path-a", "path-b"],
        step_in_pipeline=1,
    )
    paths = factory.data_flow.load_lineage_paths(
        "run-1", [root.token_id, children[0].token_id, children[1].token_id, "tok-absent"]
    )
    assert paths[root.token_id] == ()          # root token: no frames rows
    assert paths["tok-absent"] == ()           # unknown id: still present, empty
    assert paths[children[0].token_id] == (
        LineageFrame(kind=FrameKind.FORK, group_id=fork_group, member_key="path-a"),
    )
    assert paths[children[1].token_id] == (
        LineageFrame(kind=FrameKind.FORK, group_id=fork_group, member_key="path-b"),
    )


def test_load_lineage_paths_rejects_non_dense_depths() -> None:
    db, factory = _setup()
    row, root = _make_row(factory)
    with db.write_connection() as conn:  # match the module's write-connection helper name
        conn.execute(
            token_lineage_frames_table.insert().values(
                token_id=root.token_id, run_id="run-1", depth=1,  # gap: no depth 0
                kind="fork", group_id="fg-x", member_key="a",
            )
        )
    with pytest.raises(AuditIntegrityError, match="non-dense"):
        factory.data_flow.load_lineage_paths("run-1", [root.token_id])
```

(Adapt the `TokenRef` import, the run id literal, and the connection helper to `test_token_recording.py`'s actual builders — read that module first; WS1a Task 6 extended it, so the shapes above mirror its own frame tests.)

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/core/landscape/test_load_lineage_paths.py -v`
Expected: FAIL with `AttributeError: ... has no attribute 'load_lineage_paths'`.

- [ ] **Step 3: Implement** — add to `RowTokenRepository` in `core/landscape/data_flow/tokens.py` (the module already imports `select`, the table, and `AuditIntegrityError` after WS1a Task 6):

```python
    def load_lineage_paths(self, run_id: str, token_ids: Sequence[str]) -> dict[str, tuple[LineageFrame, ...]]:
        """Batch-load lineage paths from token_lineage_frames (outermost first).

        Every requested token_id is a key; tokens with no frames rows map to ().
        Depth gaps or duplicate depths are audit corruption (the frames are
        written atomically with the token INSERT — WS1a Task 6).
        """
        paths: dict[str, list[tuple[int, LineageFrame]]] = {token_id: [] for token_id in token_ids}
        if token_ids:
            with self._db.connection() as conn:
                rows = conn.execute(
                    select(
                        token_lineage_frames_table.c.token_id,
                        token_lineage_frames_table.c.depth,
                        token_lineage_frames_table.c.kind,
                        token_lineage_frames_table.c.group_id,
                        token_lineage_frames_table.c.member_key,
                    )
                    .where(token_lineage_frames_table.c.run_id == run_id)
                    .where(token_lineage_frames_table.c.token_id.in_(list(token_ids)))
                    .order_by(token_lineage_frames_table.c.token_id, token_lineage_frames_table.c.depth)
                ).all()
            for row in rows:
                paths[str(row.token_id)].append(
                    (int(row.depth), LineageFrame(kind=FrameKind(row.kind), group_id=row.group_id, member_key=row.member_key))
                )
        result: dict[str, tuple[LineageFrame, ...]] = {}
        for token_id, entries in paths.items():
            depths = [depth for depth, _frame in entries]
            if depths != list(range(len(depths))):
                raise AuditIntegrityError(
                    f"token_lineage_frames for token {token_id!r} (run {run_id!r}) has non-dense depths {depths} — audit corruption"
                )
            result[token_id] = tuple(frame for _depth, frame in entries)
        return result
```

(If `token_ids` can exceed the SQLite parameter budget, chunk by the existing `_TOKEN_ID_CHUNK_SIZE` pattern from `restore_read_model.py:322`.)

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/core/landscape/test_load_lineage_paths.py -v` — Expected: PASS.
Also run `pytest tests/unit/core/landscape/test_token_recording.py -q` (no collateral damage).

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/core/landscape/data_flow/tokens.py tests/unit/core/landscape/test_load_lineage_paths.py
git commit -m "feat(lineage): batch load_lineage_paths reader over token_lineage_frames"
```

---

### Task 2: §4.1a accessor-equivalence table, pinned as unit tests

**Files:**
- Test: `tests/unit/contracts/test_lineage_path_delta_table.py` (create)

**Interfaces:**
- Consumes: `path_branch_name` / `path_fork_group_id` / `path_expand_group_id` (WS1a Task 1). These tests are written against the HELPERS (not `TokenInfo` properties), so they are green in Phase A and stay green through the flip — they are the durable PURE-PATH pin of the §4.1a table. They complement, not duplicate, WS1a Task 8's `tests/unit/engine/test_token_lineage_path.py` (which pins the same rows over REAL minting sequences against a live DB): this file is the topology truth-table with zero scaffolding, Task 8's is the mint-integration half, and Task 7 adds the property-level twins that go green only at the flip. Row 6 (ruling 27) is owned HERE — WS1a deliberately excludes it.
- Produces: the case-by-case §4.1a delta record every reviewer diffs against.

- [ ] **Step 1: Write the test (passes immediately — it pins the CONTRACT, each case labeled with its §4.1a row)**

```python
"""tests/unit/contracts/test_lineage_path_delta_table.py

The spec §4.1a delta table, case by case. Each test names the topology row and
states BOTH truths: what today's destructive tri-field said (in-memory /
durable-row) and what the preservative accessor says. Rulings 26/27 are final:
the accessor value is the contract; the "today" values are recorded in comments
as the adjudicated delta, not asserted.
"""
from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import (
    LineageFrame,
    path_branch_name,
    path_expand_group_id,
    path_fork_group_id,
)

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")
OUTER_FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="left")


def _accessors(path: tuple[LineageFrame, ...]) -> tuple[str | None, str | None, str | None]:
    # exactly the three helpers the flip exposes as TokenInfo properties (WS1a Task 1)
    return (path_branch_name(path), path_fork_group_id(path), path_expand_group_id(path))


def test_row_plain_and_single_frame_topologies_match_both_truths() -> None:
    # §4.1a row 1: plain / fork-only child / expand-only child — identical to both truths.
    assert _accessors(()) == (None, None, None)
    assert _accessors((FORK,)) == ("path_a", "fg-1", None)
    assert _accessors((EXPAND,)) == (None, None, "eg-1")


def test_row_expand_child_inside_a_fork_branch_adopts_the_in_memory_truth() -> None:
    # §4.1a row 2: today in-memory said branch_name="path_a" (inherited) while the
    # durable tokens row said None (data_flow/tokens.py:1374-1385 wrote neither);
    # fork_group_id was None in BOTH truths (expand_token dropped it). The accessor
    # adopts the in-memory branch AND regains the fork group.
    assert _accessors((FORK, EXPAND)) == ("path_a", "fg-1", "eg-1")


def test_row_fork_child_inside_an_expand_regains_the_outer_group() -> None:
    # §4.1a row 3: today expand_group_id was None (fork_token dropped it).
    assert _accessors((EXPAND, FORK)) == ("path_a", "fg-1", "eg-1")


def test_row_merged_token_under_outer_frames_sees_outer_membership() -> None:
    # §4.1a row 4: post-coalesce merged token. Strict pop removed the inner FORK
    # frame; the outer frames remain visible (today all None). Required for
    # whole-roster settlement at the outer closer.
    assert _accessors((OUTER_FORK,)) == ("left", "fg-outer", None)
    assert _accessors((OUTER_FORK, EXPAND)) == ("left", "fg-outer", "eg-1")


def test_row_merged_token_top_level_is_all_none() -> None:
    # §4.1a row 5: identical to today.
    assert _accessors(()) == (None, None, None)


def test_row_row_union_released_token_has_no_branch_identity() -> None:
    # §4.1a row 6 / ruling 27: the release PORTs each of the N tokens through a
    # strict pop, so branch_name/fork_group_id become None (today deliberately
    # retained, processor.py:3043-3048 — a WS1 delta; downstream reads audit rows).
    released_path = (FORK,)[:-1]
    assert released_path == ()
    assert _accessors(released_path) == (None, None, None)
```

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/contracts/test_lineage_path_delta_table.py -v` — Expected: PASS (6 passed).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/contracts/test_lineage_path_delta_table.py
git commit -m "test(lineage): pin the spec 4.1a accessor-equivalence table case by case"
```

---

### Task 3: Verify the WS1a join carriers, then shrink `SinkEffectFinalizationMember`

> The join-context carriers themselves — `RowResult.join_group_id`, `PendingOutcome.join_group_id`,
> `WorkItem.join_group_id`, `CoalesceOutcome.join_group_id`, `TokenManager.coalesce_tokens ->
> tuple[TokenInfo, str]`, `SinkExecutor.write(..., join_group_id_by_token)`, and the rewritten
> consumers (`outcomes.py:257`, `sink_flush.py`, `executors/sink.py:760`, the `scheduler_drain.py`
> pending-sink rebuild) — are **WS1a Task 9 deliverables. Do not re-land them.** This task
> verifies they are present (hard entry gate for the rest of Phase A) and finishes the one
> contract WS1a leaves untouched: the finalization member still carries two dead tri-fields.

**Files:**
- Modify: `src/elspeth/contracts/sink_effects.py:572-574` (delete `fork_group_id`/`expand_group_id` from `SinkEffectFinalizationMember`; keep `join_group_id`)
- Modify: `src/elspeth/core/landscape/execution/sink_effect_finalization.py:169-171`, `:313-315` (drop the two kwargs — they only ever forwarded None, verified in the consumer roster)
- Test: the existing finalization suite — locate with `grep -rln "SinkEffectFinalizationMember" tests/unit/` (update in the same commit)

**Interfaces:**
- Consumes: WS1a Task 9's carriers (exact signatures in the consumed-interfaces section above).
- Produces: `SinkEffectFinalizationMember` with `join_group_id: str | None` as its ONLY group field (ruling 20); the outcome writer keeps accepting its own tri-field kwargs until the flip (Task 9 deletes them there).

- [ ] **Step 1: Verify the WS1a Task 9 carriers are on the branch (entry gate — STOP if any check fails and land WS1a Task 9 first)**

```bash
git grep -n "join_group_id" src/elspeth/contracts/results.py            # RowResult field + __post_init__ rule
git grep -n "join_group_id" src/elspeth/contracts/engine.py             # PendingOutcome field + rule
git grep -n "join_group_id_by_token" src/elspeth/engine/executors/sink.py
git grep -n "result.join_group_id" src/elspeth/engine/orchestrator/outcomes.py   # :257 reads the carrier, not token.join_group_id
pytest tests/unit/contracts/ -q -k "join_group_id"                      # WS1a Task 9's carrier tests
```

Expected: every grep hits; the carrier tests pass.

- [ ] **Step 2: Write the failing test** — append to the located finalization suite:

```python
def test_sink_effect_finalization_member_carries_join_context_only() -> None:
    import dataclasses

    from elspeth.contracts.sink_effects import SinkEffectFinalizationMember

    names = {f.name for f in dataclasses.fields(SinkEffectFinalizationMember)}
    assert "join_group_id" in names  # merge event — kept (ruling 20)
    assert {"fork_group_id", "expand_group_id"} & names == set()
```

(If `SinkEffectFinalizationMember` is a pydantic model rather than a dataclass, use `set(SinkEffectFinalizationMember.model_fields)` — read `contracts/sink_effects.py:560-580` first.)

- [ ] **Step 3: Run it**

Run: `pytest <located finalization suite> -q -k "join_context_only"`
Expected: FAIL — the two retired fields are still declared.

- [ ] **Step 4: Implement the deletions** — remove the two fields from `SinkEffectFinalizationMember` (`contracts/sink_effects.py:572-574`) and the two forwarding kwargs at `sink_effect_finalization.py:169-171` and `:313-315`. Fix any test constructing the member with the deleted kwargs (give the fake the real contract — masquerade discipline).

- [ ] **Step 5: Run**

Run: `pytest tests/unit/core/landscape/execution/ tests/unit/engine/ -q` — Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/contracts/sink_effects.py src/elspeth/core/landscape/execution/sink_effect_finalization.py
git add <the finalization test file you edited, by name>
git commit -m "refactor(lineage): SinkEffectFinalizationMember carries join context only"
```

---

### Task 4: The path-aware resume filter and resume-start dispatch (pinned arm order)

**Files:**
- Modify: `src/elspeth/engine/orchestrator/resume.py:352-360` (filter)
- Modify: `src/elspeth/engine/processor.py:2840-2945` (`resume_incomplete_token`)
- Test: `tests/unit/engine/test_resume_start_dispatch.py` (create)

**Interfaces:**
- Consumes: `IncompleteTokenSpec.lineage_path: tuple[LineageFrame, ...]` — landed by WS1a Task 5 (loaded from `token_lineage_frames` in `_get_incomplete_token_work`, already threaded into `resume_incomplete_token`'s TokenInfo reconstruction). This task rewrites the DECISIONS over it; it does not touch the field or its loading.
- Produces:
  - The PINNED dispatch-arm order: **merged (join) FIRST, then innermost-EXPAND, then innermost-FORK, then raise** — this is the §4.1a "pin arm selection" obligation; the merged-first order is what keeps a future merged-token-under-outer-frames from being misdispatched into the expand arm.
  - `classify_resume_start(*, lineage_path, join_group_id) -> ResumeStartArm` (module level in `engine/processor.py`).

- [ ] **Step 1: Write the failing dispatch-arm test**

```python
"""tests/unit/engine/test_resume_start_dispatch.py

Pins resume-start ARM SELECTION (spec §4.1a: decisions, not field values).
Uses classify_resume_start — the pure arm-selection function extracted in this
task — so the pin needs no orchestrator scaffolding.
"""
import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.engine.processor import ResumeStartArm, classify_resume_start

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")
OUTER = LineageFrame(kind=FrameKind.FORK, group_id="fg-outer", member_key="left")


@pytest.mark.parametrize(
    ("path", "join_group_id", "expected"),
    [
        # today's three depth-1 shapes — arm selection identical to the tri-field dispatch
        ((EXPAND,), None, ResumeStartArm.EXPAND_CHILD),
        ((FORK,), None, ResumeStartArm.FORK_CHILD),
        ((), "jg-1", ResumeStartArm.MERGED),
        # §4.1a nested shapes — the NEW pins
        ((FORK, EXPAND), None, ResumeStartArm.EXPAND_CHILD),   # expand child inside a fork branch: expand wins (innermost)
        ((EXPAND, FORK), None, ResumeStartArm.FORK_CHILD),     # fork child inside an expand: fork wins (innermost)
        ((OUTER,), "jg-1", ResumeStartArm.MERGED),             # merged token under an outer frame: MERGED wins over the frame
        ((OUTER, EXPAND), "jg-1", ResumeStartArm.MERGED),      # merged under outer expand: still MERGED, never EXPAND_CHILD
    ],
)
def test_arm_selection(path, join_group_id, expected) -> None:
    assert classify_resume_start(lineage_path=path, join_group_id=join_group_id) is expected


def test_no_lineage_and_no_join_is_an_invariant_error() -> None:
    with pytest.raises(Exception, match="no resume-start"):
        classify_resume_start(lineage_path=(), join_group_id=None)
```

- [ ] **Step 2: Run it**

Run: `pytest tests/unit/engine/test_resume_start_dispatch.py -v`
Expected: FAIL — `ImportError: cannot import name 'classify_resume_start'`.

- [ ] **Step 3: Implement the classifier in `engine/processor.py`** (module level, above `RowProcessor`; import `FrameKind`, `LineageFrame` from contracts):

```python
class ResumeStartArm(Enum):
    """Resume-start dispatch arms (spec §4.1a — arm selection is pinned, not derived)."""

    MERGED = "merged"
    EXPAND_CHILD = "expand_child"
    FORK_CHILD = "fork_child"


def classify_resume_start(
    *,
    lineage_path: tuple[LineageFrame, ...],
    join_group_id: str | None,
) -> ResumeStartArm:
    """Select the resume-start arm for one incomplete token.

    ARM ORDER IS LOAD-BEARING and pinned by test_resume_start_dispatch:

    1. MERGED first: join_group_id is a merge EVENT attribute; after the strict
       pop, any frames still on the merged token's path are ENCLOSING context,
       never the operation that minted it. Checking a frame arm first would
       misroute a merged token under an outer EXPAND frame into the expand arm.
    2. Innermost frame decides between EXPAND_CHILD and FORK_CHILD — the
       path-aware replacement for "expand checked before branch dispatch"
       (expanded children inside a fork branch keep their branch identity in
       outer frames but are re-driven as expand children).
    """
    if join_group_id is not None:
        return ResumeStartArm.MERGED
    if lineage_path:
        innermost = lineage_path[-1]
        if innermost.kind is FrameKind.EXPAND:
            return ResumeStartArm.EXPAND_CHILD
        return ResumeStartArm.FORK_CHILD
    raise OrchestrationInvariantError(
        "Incomplete token has an empty lineage_path and no join_group_id — no resume-start node resolvable. "
        "Linear tokens must be routed to process_existing_row by the resume filter (F1)."
    )
```

- [ ] **Step 4: Run to pass**

Run: `pytest tests/unit/engine/test_resume_start_dispatch.py -v` — Expected: PASS.

- [ ] **Step 5: Rewrite the resume decisions over the path**

1. Verify the consumed field is live (entry check): `git grep -n "lineage_path" src/elspeth/core/checkpoint/recovery.py` must show the `IncompleteTokenSpec` field and its population in `_get_incomplete_token_work` (WS1a Task 5). The three retired spec fields stay until the flip (Task 9 deletes them with the columns); `join_group_id` stays permanently (the tokens column survives).

2. `engine/orchestrator/resume.py:352-360` — the filter becomes path-based (drops three of the four field reads; equivalent under WS1a dual-write, and the correct form after the flip):

```python
        fork_expand_coalesce_specs = (
            [s for s in incomplete_by_row[row_id] if s.lineage_path or s.join_group_id is not None]
            if row_id in incomplete_by_row
            else []
        )
```

Update the comment block at `:341-348` to say "at least one lineage frame, or a merge event (`join_group_id`)" instead of naming the four fields; keep the F1 explanation.

3. `engine/processor.py:2879-2945` — `resume_incomplete_token` dispatches through the classifier (the TokenInfo reconstruction already carries `lineage_path=spec.lineage_path` from WS1a Task 5 and keeps the three legacy kwargs until the flip):

```python
        arm = classify_resume_start(lineage_path=spec.lineage_path, join_group_id=spec.join_group_id)

        if arm is ResumeStartArm.MERGED:
            # post-coalesce merged token, crashed AFTER the barrier (B1 review finding)
            ...existing :2906-2938 body unchanged, except the terminal-coalesce call gains
            ...join_group_id=spec.join_group_id (WS1a Task 9 signature)...
            return ...

        if arm is ResumeStartArm.EXPAND_CHILD:
            after = self._nav.resolve_next_node(self._resolve_step_node(spec))
            return self.process_token(token, ctx, current_node_id=after)

        # arm is ResumeStartArm.FORK_CHILD
        branch = spec.lineage_path[-1].member_key
        if BranchName(branch) in self._branch_to_sink:
            return self.process_token(token, ctx, current_node_id=None)
        if BranchName(branch) in self._branch_to_coalesce:
            coalesce_name = self._branch_to_coalesce[BranchName(branch)]
            first_node = self._nav.resolve_branch_first_node(branch)
            return self.process_token(token, ctx, current_node_id=first_node, coalesce_name=coalesce_name)

        raise OrchestrationInvariantError(
            f"Incomplete fork-child token {spec.token_id} is on branch {branch!r} which routes to neither a "
            f"sink nor a coalesce — no resume-start node resolvable. Audit/DAG inconsistency."
        )
```

Note the MERGED arm body is MOVED, not rewritten — keep `:2906-2938` verbatim (terminal-coalesce lookup guard included). Delete the old `:2940-2945` fall-through raise (the classifier raises instead). Update the docstring's "Dispatch cases" block (`:2848-2862`) to describe the classifier order and WHY merged is first.

- [ ] **Step 6: Run**

Run: `pytest tests/unit/engine/test_resume_start_dispatch.py tests/unit/engine/ tests/e2e/recovery/test_concurrent_resume.py -q`
Expected: PASS (the e2e file exercises the real resume filter and dispatch on depth-1 shapes — behaviour-neutral under dual-write).

- [ ] **Step 7: Commit**

```bash
git add src/elspeth/engine/processor.py src/elspeth/engine/orchestrator/resume.py \
        tests/unit/engine/test_resume_start_dispatch.py
git commit -m "feat(lineage): path-aware resume-start dispatch with pinned arm order"
```

---

### Task 5: MCP read surfaces + accessor-OK adjudication (exporter moves to the flip — see Task 12 Step 0)

> **Sequencing note (self-review):** the export-record reshape (decision D3) CANNOT land in
> Phase A — the corpus harness reads the exported token records' old keys until its own
> rewrite at the flip, so reshaping the exporter early would redden the corpus suite at the
> Phase A boundary. The exporter/`export_records.py` work therefore rides Phase B as
> Task 12 Step 0. This task covers the MCP surfaces (own wire, no corpus coupling).

**Files:**
- Modify: `src/elspeth/mcp/types.py:79-83` (`TokenRecord`), `src/elspeth/mcp/analyzers/queries.py:199-211` (`list_tokens`), `src/elspeth/mcp/analyzers/reports.py:706-723` (fork/join counts) — then sweep `git grep -n "fork_group_id\|expand_group_id\|branch_name" src/elspeth/mcp/` and apply the same reshaping to EVERY remaining projection (`get_token_children`'s `TokenChildRecord` included; config-vocabulary `branch_name` hits, if any, stay)
- Modify: `src/elspeth/tui/widgets/lineage_tree.py:160-164` (comment only — reword to name `lineage_path`/`token_parents`)
- Test: existing mcp suites (update assertions in the same commit)

**Interfaces:**
- Consumes: Task 1's `load_lineage_paths`, `token_lineage_frames_table` (WS1a Task 4), `FrameKind` + the path helpers `path_branch_name`/`path_fork_group_id`/`path_expand_group_id` (WS1a Task 1).
- Produces: `TokenRecord` (and `TokenChildRecord`) with `lineage_path: list[LineageFrameEntry]` — each frame a dict `{"depth": int, "kind": str, "group_id": str, "member_key": str}`, outermost first, `depth` from 0 — + `join_group_id` + the path-DERIVED `branch_name`/`fork_group_id`/`expand_group_id` wire fields — **ratified 2026-08-22 (ruling 21): the legacy names STAY on the MCP wire, derived in the projection from the path, never a stored-column read.** This is the same shape as the WS5/6 plan's final `TokenRecord` contract (its MCP task replaces any residual shim, not the shape).

- [ ] **Step 1: Write the failing test** (in the existing mcp analyzer test module — locate with `grep -rln "list_tokens" tests/unit/mcp/`):

```python
def test_list_tokens_projects_lineage_path_and_derived_names() -> None:
    # build one fork run via the module's existing run scaffolding, then:
    records = list_tokens(db, factory, run_id=run_id, row_id=None, limit=50)
    fork_children = [r for r in records if r["lineage_path"]]
    assert fork_children, "fork run must project frames"
    for record in fork_children:
        frame = record["lineage_path"][0]
        assert frame["depth"] == 0
        assert frame["kind"] == "fork"
        # ruling 21 (ratified): legacy names stay on the wire, DERIVED from the path.
        assert record["branch_name"] == frame["member_key"]
        assert record["fork_group_id"] == frame["group_id"]
        assert record["expand_group_id"] is None
        assert "join_group_id" in record
```

- [ ] **Step 2: Run it** — `pytest tests/unit/mcp/ -q -k lineage` — Expected: FAIL (KeyError `lineage_path`).

- [ ] **Step 3: Implement**

`mcp/types.py` `TokenRecord`: KEEP the three legacy keys (their values become path-derived) and add `lineage_path: list[LineageFrameEntry]` (each entry the dict `{"depth", "kind", "group_id", "member_key"}` — WS5 Task 7's final wire shape); keep `join_group_id`; same for `TokenChildRecord` and any sibling found by the sweep. `mcp/analyzers/queries.py:199-211`: batch `paths = load_lineage_paths(run_id, [row.token_id for row in rows])` (Task 1 helper via the analyzer's factory), then emit per record `lineage_path=[{"depth": depth, "kind": f.kind.value, "group_id": f.group_id, "member_key": f.member_key} for depth, f in enumerate(paths[row.token_id])]`, `branch_name=path_branch_name(paths[row.token_id])`, `fork_group_id=path_fork_group_id(paths[row.token_id])`, `expand_group_id=path_expand_group_id(paths[row.token_id])` — the WS1a Task 1 helpers, never a column read (the stored columns are deleted at Task 9). `mcp/analyzers/reports.py:707-723`:

```python
        fork_count = (
            conn.execute(
                select(func.count(func.distinct(token_lineage_frames_table.c.group_id)))
                .select_from(token_lineage_frames_table)
                .where(
                    (token_lineage_frames_table.c.run_id == run_id)
                    & (token_lineage_frames_table.c.kind == FrameKind.FORK.value)
                )
            ).scalar()
            or 0
        )

        join_count = (
            conn.execute(
                select(func.count(func.distinct(tokens_table.c.join_group_id)))
                .select_from(tokens_table)
                .where((tokens_table.c.run_id == run_id) & (tokens_table.c.join_group_id.isnot(None)))
            ).scalar()
            or 0
        )
```

(Fork count semantics are preserved: today's DISTINCT over `token_outcomes.fork_group_id` counts fork events; each fork's children carry exactly one FORK frame with that group id, so DISTINCT group_id at kind=fork counts the same events. Join count moves from the deleted outcome column to the kept tokens column — one merged token per join group, same cardinality.)

- [ ] **Step 4: Run**

Run: `pytest tests/unit/mcp/ -q` — Expected: PASS (the legacy keys survive on the wire; update only fixtures whose expected VALUES relied on the stored columns where a §4.1a delta moves them — nested shapes — and adjudicate each against the Task 2 table).

- [ ] **Step 5: Accessor-OK adjudication (no-edit record — write it into the commit message body).** The following sites keep their `token.branch_name`/`token.expand_group_id` CALL SYNTAX and need NO rewrite (the Task 8 properties preserve it); their decisions are pinned by the Task 2 accessor table plus the Task 14 oracle diff, and each is re-reviewed there because the §4.1a deltas can move their VALUES even where syntax survives (consumer-roster risk note 14): `engine/token_traversal.py:782/:849` (branch→sink routing), `engine/work_items.py:123-132` (branch-first-node resolve; message updated in Task 11 step 7), `engine/coalesce_executor.py:789-799/:877-896` (arrival/duplicate checks), `engine/row_union_executor.py:220-238/:367-376/:412-428` (union arrival reads), `core/landscape/lineage_text.py:29-30` (render).

- [ ] **Step 6: Commit**

```bash
git add src/elspeth/mcp/types.py src/elspeth/mcp/analyzers/queries.py src/elspeth/mcp/analyzers/reports.py \
        src/elspeth/tui/widgets/lineage_tree.py
git add <each pre-existing mcp test file you edited, by name>
git commit -m "feat(lineage): MCP read surfaces re-derive lineage from the frames table"
```

---

### Task 6: Web diagnostics wire shape + frontend types (decision D6)

**Files:**
- Modify: `src/elspeth/web/execution/schemas.py:900-914` (`RunDiagnosticToken`, new `RunDiagnosticLineageFrame`)
- Modify: `src/elspeth/web/execution/diagnostics.py:412-424` (token select) and the record build (~`:774-777`)
- Modify: `src/elspeth/web/execution/accounting.py:238-273` (census evidence columns)
- Modify: `src/elspeth/web/frontend/src/types/index.ts:859-871`
- Modify: `src/elspeth/web/frontend/src/stores/executionStore.test.ts` and `src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx` (fixture literals)
- Test: existing `tests/unit/web/execution/` suites (update assertions in the same commit)

**Interfaces:**
- Consumes: Task 1's `load_lineage_paths`; the D4 matrix is NOT yet flipped, so accounting keeps validating the old columns until Task 9 — this task only touches accounting's evidence-map construction if the shared validator signature already moved; otherwise leave accounting to Task 9 (check `_outcome_shape_violation`'s import — if it calls `validate_token_outcome_persisted_fields`, defer the accounting edit to Task 9 and note it in the commit message).

- [ ] **Step 1: Write the failing test** (in the existing web diagnostics test module — locate with `grep -rln "RunDiagnosticToken" tests/unit/web/`):

```python
def test_run_diagnostic_token_carries_lineage_frames() -> None:
    from elspeth.web.execution.schemas import RunDiagnosticLineageFrame, RunDiagnosticToken

    fields = set(RunDiagnosticToken.model_fields)
    assert "lineage" in fields
    assert {"branch_name", "fork_group_id", "expand_group_id"} & fields == set()
    assert "join_group_id" in fields
    assert set(RunDiagnosticLineageFrame.model_fields) == {"kind", "group_id", "member_key"}
```

- [ ] **Step 2: Run it** — `pytest tests/unit/web/execution/ -q -k lineage` — Expected: FAIL.

- [ ] **Step 3: Implement**

`schemas.py`:

```python
class RunDiagnosticLineageFrame(_StrictResponse):
    """One lineage frame (outermost first) on a diagnostics token."""

    kind: Literal["fork", "expand"]
    group_id: str
    member_key: str


class RunDiagnosticToken(_StrictResponse):
    """One token in the bounded diagnostics preview."""

    token_id: str
    row_id: str
    row_index: int | None = Field(ge=0)
    lineage: list[RunDiagnosticLineageFrame]
    join_group_id: str | None
    step_in_pipeline: int | None = Field(ge=0)
    created_at: datetime
    terminal_outcome: RunDiagnosticTerminalOutcome | None
    states: list[RunDiagnosticNodeState]
```

`diagnostics.py`: drop the three retired columns from the token select (keep `join_group_id`); after fetching the page of tokens, batch `load_lineage_paths(run_id, page_token_ids)` and build `lineage=[RunDiagnosticLineageFrame(kind=f.kind.value, group_id=f.group_id, member_key=f.member_key) for f in paths[token_id]]` in the record build.

`types/index.ts`:

```typescript
export interface RunDiagnosticLineageFrame {
  kind: 'fork' | 'expand';
  group_id: string;
  member_key: string;
}

export interface RunDiagnosticToken {
  token_id: string;
  row_id: string;
  row_index: number | null;
  lineage: RunDiagnosticLineageFrame[];
  join_group_id: string | null;
  step_in_pipeline: number | null;
  created_at: string;
  terminal_outcome: string | null;
  states: RunDiagnosticNodeState[];
}
```

Update the two frontend test fixture literals to the new shape (`lineage: []` or a one-frame array replacing the old scalar fields). Run the frontend test suite with the repo's standard command (check `src/elspeth/web/frontend/package.json` scripts — `npm test -- --run` from that directory).

- [ ] **Step 4: Run** — `pytest tests/unit/web/execution/ -q` and the frontend test command — Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add src/elspeth/web/execution/schemas.py src/elspeth/web/execution/diagnostics.py \
        src/elspeth/web/frontend/src/types/index.ts \
        src/elspeth/web/frontend/src/stores/executionStore.test.ts \
        src/elspeth/web/frontend/src/components/execution/RunsHistoryDrawer.test.tsx \
        tests/unit/web/execution/
git commit -m "feat(lineage): diagnostics wire shape carries lineage frames (pre-1.0 break)"
```

**Phase A boundary:** run the FULL suite (`pytest tests/ -n 12`), record HEAD before/after, and diff the trust-tier corpus against the pre-Phase-A baseline (add nothing). Fix any whole-tree gate the phase tripped before entering Phase B.

---

## PHASE B — THE ATOMIC FLIP (Tasks 7–12 build ONE commit; no intermediate commits; the tree is deliberately red between tasks; Task 12 ends with full green and the single flip commit)

**Entry criteria:** WS1a Task 8a's nested-fixture commit (fork-in-fork depth-2 + expand-in-fork scenario dirs wired into `EXPECTED_SCENARIOS` and `oracle_freeze.SCENARIO_CLASSIFICATION` as FROZEN) and THEN the protocols plan's Task 3 freeze commit both exist on the branch (snapshots under `tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/` — the nested scenarios included — recorded at a pre-flip HEAD, with `tests/integration/core/dag/test_oracle_freeze.py` green in compare mode; protocols §S1 executes before this flip); WS1a is fully landed and green (Tasks 1–11, incl. 8a); Phase A is fully committed. Record `git rev-parse HEAD` — this is the flip's base.

### Task 7: Flip failing tests first (RED)

**Files:**
- Create: `tests/unit/contracts/test_token_info_lineage_flip.py`
- Create: `tests/unit/engine/test_scheduler_lineage_codec_flip.py`
- Modify: `tests/unit/core/landscape/test_token_recording.py` (replay-predicate expectations — see Task 10 step 1 for the exact rewrites; write them NOW so Task 10 turns them green)

**Interfaces:** Produces the red bar the rest of Phase B turns green.

- [ ] **Step 1: TokenInfo property tests**

```python
"""tests/unit/contracts/test_token_info_lineage_flip.py"""
import dataclasses

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame, TokenInfo
from elspeth.testing import make_row

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")


def _token(path: tuple[LineageFrame, ...]) -> TokenInfo:
    return TokenInfo(row_id="r1", token_id="t1", row_data=make_row({}), lineage_path=path)


def test_stored_lineage_fields_are_retired() -> None:
    field_names = {f.name for f in dataclasses.fields(TokenInfo)}
    assert {"branch_name", "fork_group_id", "join_group_id", "expand_group_id"} & field_names == set()
    assert "lineage_path" in field_names


def test_derived_accessors_read_the_path() -> None:
    token = _token((FORK, EXPAND))
    assert token.branch_name == "path_a"
    assert token.fork_group_id == "fg-1"
    assert token.expand_group_id == "eg-1"
    assert _token(()).branch_name is None


def test_accessors_are_read_only() -> None:
    token = _token((FORK,))
    with pytest.raises(AttributeError):
        token.branch_name = "x"  # type: ignore[misc]


def test_with_updated_data_preserves_the_path() -> None:
    token = _token((FORK, EXPAND))
    assert token.with_updated_data(make_row({"a": 1})).lineage_path == (FORK, EXPAND)


def test_join_group_id_left_token_info() -> None:
    assert not hasattr(_token(()), "join_group_id")
```

- [ ] **Step 2: Journal codec round-trip + bidirectional cross-check tests**

```python
"""tests/unit/engine/test_scheduler_lineage_codec_flip.py"""
import dataclasses

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame
from elspeth.contracts.scheduler import TokenWorkItem

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")


def test_token_work_item_lineage_fields_retired_and_path_added() -> None:
    names = {f.name for f in dataclasses.fields(TokenWorkItem)}
    assert {"branch_name", "fork_group_id", "expand_group_id"} & names == set()
    assert "join_group_id" in names          # ruling 20 / decision D1
    assert "lineage_path" in names
    assert {"coalesce_node_id", "coalesce_name", "row_union_name", "barrier_key", "barrier_adopted_epoch"} <= names


def test_token_from_journal_item_reconstructs_the_path_purely() -> None:
    from elspeth.core.landscape.scheduler.payload_codec import serialize_row_payload, token_from_journal_item
    from elspeth.testing import make_row
    from datetime import UTC, datetime

    now = datetime.now(UTC)
    item = TokenWorkItem(
        work_item_id="w1", run_id="run1", token_id="t1", row_id="r1", node_id="n1",
        step_index=1, ingest_sequence=0, row_payload_json=serialize_row_payload(make_row({"a": 1})),
        status=__import__("elspeth.contracts.enums", fromlist=["TokenWorkStatus"]).TokenWorkStatus.READY,
        attempt=1, available_at=now, created_at=now, updated_at=now,
        lineage_path=(FORK,),
    )
    token = token_from_journal_item(item, attempt_offset=0, resume_checkpoint_id=None)
    assert token.lineage_path == (FORK,)
    assert token.branch_name == "path_a"
```

(Clean up the inline `__import__` to a proper top-level import when writing the file — shown compressed here only for plan brevity; the committed test uses `from elspeth.contracts.enums import TokenWorkStatus`.)

- [ ] **Step 3: Run both new modules**

Run: `pytest tests/unit/contracts/test_token_info_lineage_flip.py tests/unit/engine/test_scheduler_lineage_codec_flip.py -v`
Expected: FAIL on the retired-stored-fields assertions (the three fields still exist on both dataclasses). Two assertions already pass and stay as regression pins: `join_group_id` is gone from `TokenInfo` (WS1a Task 10) and the codec round-trip reconstructs `lineage_path` (WS1a Task 5).

- [ ] **Step 4: Rewrite the replay-predicate expectations in `tests/unit/core/landscape/test_token_recording.py`**

The four families (fork exact-replay `:413`, fork divergent-refusal `:439`, expand exact-replay `:764`, expand divergent-refusal `:793`, batch idempotency `:588/:637` — re-locate by test name, the line anchors are advisory) change as follows; edit the assertions NOW so Task 10 has a red bar:

- exact fork replay: children returned without reminting AND each child's persisted frames (query `token_lineage_frames_table`) equal `parent_frames + (LineageFrame(FORK, fork_group_id, branch),)` in ordinal order;
- divergent fork replay: calling `fork_token` again with a REORDERED branch list raises `AuditIntegrityError` (roster now derived from child frames, D2 — assert the message mentions "divergent fork replay");
- exact expand replay: children's frames equal `parent_frames + (LineageFrame(EXPAND, expand_group_id, member_key=child.token_id),)` AND exactly one `group_records` row exists for `(run_id, expand_group_id)` with `member_count == len(children)` after the re-drive (a re-drive can never mint a second group);
- add `test_expand_replay_group_record_count_mismatch_refuses`: hand-UPDATE the `group_records.member_count` to a wrong value between the two calls, assert the replay raises `AuditIntegrityError`.

- [ ] **Step 5: Confirm RED** — `pytest tests/unit/core/landscape/test_token_recording.py -x -q` — Expected: FAIL. Do NOT commit; Phase B commits once, in Task 12.

---

### Task 8: Contracts flip — `TokenInfo`, audit `Token`/`TokenOutcome`, `TokenWorkItem`, emissions, testing factory

**Files:**
- Modify: `src/elspeth/contracts/identity.py` (TokenInfo), `src/elspeth/contracts/audit.py:248-266` (Token), `:1388-1404` (TokenOutcome fields), `:1457-1540` (matrix, D4), `:1543-1590` (`validate_token_outcome_persisted_fields`)
- Modify: `src/elspeth/contracts/scheduler.py:107-152` (TokenWorkItem), `:155-203` (BarrierEmission)
- Modify: `src/elspeth/engine/scheduler_work_codec.py` (ScheduledWorkFields + both directions)
- Modify: `src/elspeth/testing/__init__.py:399-413` (`make_token_info`)
- Modify: `src/elspeth/core/landscape/model_loaders.py:228-240`, `:585-610` (Token/TokenOutcome loaders)
- Modify: `src/elspeth/core/landscape/lineage.py:55-140` (explain read side), `src/elspeth/core/landscape/execution/sink_effect_identity.py:49-56`, `:169-170`

**Interfaces:**
- Produces: the post-flip `TokenInfo` (canonical contract), audit `Token(token_id, row_id, created_at, run_id, join_group_id=None, lineage_path=(), step_in_pipeline=None, token_data_ref=None)`, `TokenWorkItem` with only `lineage_path` (WS1a Task 5's field) + `join_group_id` as group context, `ScheduledWorkFields` with the three retired fields deleted (`lineage_path` and `join_group_id` already present from WS1a Tasks 5/9).

- [ ] **Step 1: `TokenInfo` flip** (`contracts/identity.py`) — replace the four stored fields with properties:

```python
@dataclass(frozen=True, slots=True)
class TokenInfo:
    """Identity and data for a token flowing through the DAG.

    Lineage is ONE field — the lineage path (spec §4.1): a stack of typed frames,
    outermost first. branch_name / fork_group_id / expand_group_id are DERIVED
    accessors over the path (ruling 21: the only read path for the legacy names;
    any stored-field resurrection is a defect). join_group_id is a merge EVENT,
    not a membership — it left TokenInfo (ruling 20) and rides RowResult /
    PendingOutcome / TokenWorkItem carriers.
    """

    row_id: str
    token_id: str
    row_data: PipelineRow
    lineage_path: tuple[LineageFrame, ...] = ()
    resume_attempt_offset: int = 0
    resume_checkpoint_id: str | None = None

    def __post_init__(self) -> None:
        ...keep the existing row_id/token_id and resume-offset validation verbatim,
        replace the four-field loop with:
        if type(self.lineage_path) is not tuple:
            raise TypeError(f"TokenInfo.lineage_path must be a tuple, got {type(self.lineage_path).__name__}")
        for frame in self.lineage_path:
            if type(frame) is not LineageFrame:
                raise TypeError(f"TokenInfo.lineage_path entries must be LineageFrame, got {type(frame).__name__}")

    @property
    def branch_name(self) -> str | None:
        return path_branch_name(self.lineage_path)

    @property
    def fork_group_id(self) -> str | None:
        return path_fork_group_id(self.lineage_path)

    @property
    def expand_group_id(self) -> str | None:
        return path_expand_group_id(self.lineage_path)

    def with_updated_data(self, new_data: PipelineRow) -> TokenInfo:
        """dataclasses.replace preserves lineage_path and resume provenance."""
        return replace(self, row_data=new_data)
```

(The `...` lines above describe keeping EXISTING verbatim code — when editing, copy the current `__post_init__` identity/resume checks unchanged; only the four-field loop is replaced.)

- [ ] **Step 2: audit `Token` and `TokenOutcome` flip** (`contracts/audit.py`)

```python
@dataclass(frozen=True, slots=True)
class Token:
    """A row instance flowing through a specific DAG path."""

    token_id: str
    row_id: str
    created_at: datetime
    run_id: str
    join_group_id: str | None = None          # merge event — the KEPT tokens column
    lineage_path: tuple[LineageFrame, ...] = ()  # loaded from token_lineage_frames
    step_in_pipeline: int | None = None
    token_data_ref: str | None = None
```

`TokenOutcome`: delete `fork_group_id`/`join_group_id`/`expand_group_id`/`expected_branches_json` fields. Matrix (D4): `_DISCRIMINATOR_FIELDS = ("sink_name", "batch_id", "error_hash")`; `(SUCCESS, COALESCED)` → `TerminalPairFieldConstraints(forbidden=_forbid_except("sink_name"))`; `(TRANSIENT, FORK_PARENT)` and `(TRANSIENT, EXPAND_PARENT)` → `TerminalPairFieldConstraints(forbidden=_DISCRIMINATOR_FIELDS)` with a comment pointing at the frames/`group_records` evidence and the Task 10 replay predicates; delete the three kwargs from `validate_token_outcome_persisted_fields` and its `field_values` map. Amend `docs/architecture/adr/019-*.md` (locate with `ls docs/architecture/adr/ | grep 019`) with a dated note: discriminator columns for fork/expand/join retired to `token_lineage_frames`/`group_records`/`tokens.join_group_id` per the unified-lineage spec.

- [ ] **Step 3: `TokenWorkItem` + `BarrierEmission` + codec flip**

`contracts/scheduler.py` `TokenWorkItem`: delete `branch_name`/`fork_group_id`/`expand_group_id` (KEEP `join_group_id` — D1; `lineage_path` is already present from WS1a Task 5). `BarrierEmission`: same three deletions (`lineage_path` already present from WS1a Task 5).

`engine/scheduler_work_codec.py`: `ScheduledWorkFields` — delete the three retired fields (`lineage_path` and `join_group_id` stay; both landed in WS1a Tasks 5/9). `ready_fields` / `ready_emission`: delete the three retired reads — `lineage_path=token.lineage_path` and `join_group_id=item.join_group_id` (the `WorkItem.join_group_id` source is WS1a Task 9's) already carry everything. `work_item_from_scheduler`: build `TokenInfo(..., lineage_path=scheduled.lineage_path)` with the three retired kwargs deleted (`join_group_id` already gone via WS1a Task 10).

- [ ] **Step 4: testing factory + loaders + explain + sink-effect protocol**

`testing/__init__.py`:

```python
def make_token_info(
    row_id: str = "row-1",
    token_id: str | None = None,
    data: dict[str, Any] | None = None,
    lineage_path: tuple[LineageFrame, ...] = (),
) -> TokenInfo:
    """Build a TokenInfo for plugin context."""
    from elspeth.engine.tokens import TokenInfo

    return TokenInfo(
        row_id=row_id,
        token_id=token_id or f"token-{row_id}",
        row_data=make_row(data or {}),
        lineage_path=lineage_path,
    )
```

(`from elspeth.contracts.identity import LineageFrame` at module top. Every test calling `make_token_info(branch_name=...)` migrates in Task 12 to `lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=<gid>, member_key=<branch>),)`.)

`core/landscape/model_loaders.py`: token loaders (`:228-240`, `:600-610`) keep `join_group_id=row.join_group_id`, drop the other three kwargs, and accept a `lineage_path` argument supplied by their callers' batched frames fetch (add `lineage_path: tuple[LineageFrame, ...] = ()` parameter; call sites that genuinely need paths — explain, sink-effect lineage — pass them from `load_lineage_paths`; call sites that don't, pass nothing). TokenOutcome loader (`:585-590`): drop the three kwargs.

`core/landscape/lineage.py`: replace `_group_ids_for`/`_validate_group_ids` (`:76-99`) with path-based shape validation:

```python
    @staticmethod
    def _lineage_kind_for(token: Token) -> str | None:
        """The operation that minted this token: 'join' (merge event), the innermost
        frame's kind, or None for a source-row token."""
        if token.join_group_id is not None:
            return "join"
        if token.lineage_path:
            return token.lineage_path[-1].kind.value
        return None
```

and re-point `_validate_parent_link_shape` at it: `"join"` requires >= 2 parents, `"fork"`/`"expand"` exactly 1, `None` exactly 0 — preserving the existing error message style (`Audit integrity violation: token '...' ...`). The explain projection `{"fork": ..., "join": ..., "expand": ...}` (`:79-81` read side) becomes `{"join": token.join_group_id, "path": [[f.kind.value, f.group_id, f.member_key] for f in token.lineage_path]}` — update its consumer (grep `explain` in `core/landscape/lineage_text.py` and the explain MCP analyzer) in the same edit.

`core/landscape/execution/sink_effect_identity.py` `_Token` Protocol:

```python
class _Token(Protocol):
    token_id: str
    row_id: str
    run_id: str
    join_group_id: str | None
    lineage_path: tuple[LineageFrame, ...]
```

and `:169`: `if not parents and (token.join_group_id is not None or token.lineage_path):`. The identity hash itself derives from parent STRUCTURE (ordinals), never group ids — verified — so sink-effect identities are byte-stable across the flip; Task 12's oracle diff is the pin.

- [ ] **Step 5: Run the flip contract tests** — `pytest tests/unit/contracts/test_token_info_lineage_flip.py tests/unit/engine/test_scheduler_lineage_codec_flip.py -q` — TokenInfo module goes green; the codec module still RED (payload_codec not yet flipped — Task 11). Expected partial red; continue. No commit.

---

### Task 9: Schema + writer flip — column retirement, `lineage_path_json` sole-truth, outcome writer, `database.py`, recovery spec

**Files:**
- Modify: `src/elspeth/core/landscape/schema.py:611-614` (tokens), `:663-670` (token_outcomes), `:725-728` (token_work_items), `:630-636` KEPT index (verify untouched)
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` — `create_row_with_token` (`:300-325`), `create_token` (`:327-402`), fork child INSERT (`:474-544`), expand child INSERT (`:1370-1415`), coalesce merged INSERT (`:790-860` region — locate `result_join_group_id`), `_load_children_for_parent` (`:589-639`)
- Modify: `src/elspeth/core/landscape/data_flow/outcomes.py:433-540` (writer kwargs), `:237-260` (values dict), `:680-690` (reads)
- Modify: `src/elspeth/core/landscape/data_flow_repository.py` (facade pass-through of the same kwargs)
- Modify: `src/elspeth/core/landscape/database.py:288`, `:300`, `:394-397`, `:591-593`
- Modify: `src/elspeth/core/checkpoint/recovery.py` (`IncompleteTokenSpec` legacy fields + its token select)
- Modify (epoch-pin fan-out, step 1): `CHANGELOG.md`, `website/get-started.html`, `docs/guides/sharing-pipelines.md`, `docs/product/current-state.md`, `tests/unit/core/landscape/test_schema_epoch_and_required_columns.py`, `tests/unit/core/landscape/test_token_ownership_run_scope.py`, `tests/integration/web/composer/guided/test_schema9_epoch.py`, `tests/unit/docs/test_composer_capability_docs.py`, `tests/unit/docs/test_staging_session_recreation_policy.py`

**Interfaces:**
- Consumes: WS1a Task 6's frames writer `_insert_lineage_frames` — the flip makes it the SOLE lineage write path (and retypes it, step 3); WS1a Task 4's `lineage_path_json` column.
- Produces: `create_token(row_id, *, token_id=None, lineage_path: tuple[LineageFrame, ...] = (), join_group_id: str | None = None) -> Token`; `IncompleteTokenSpec(token_id, row_id, ..., join_group_id, lineage_path)` with the three retired fields deleted.

- [ ] **Step 1: Schema edits**

- `tokens` (`:611-614`): delete the `fork_group_id`, `expand_group_id`, `branch_name` columns; KEEP `join_group_id` (`:612`) and the `uq_tokens_coalesce_result_identity` index (`:630-636`) — the `coalesce_effects` composite-FK target (consumer-roster risk note 10).
- `token_outcomes` (`:663-665`, `:670`): delete `fork_group_id`, `join_group_id`, `expand_group_id`, `expected_branches_json` (D2).
- `token_work_items` (`:725-728`): delete `branch_name`, `fork_group_id`, `expand_group_id`; KEEP `join_group_id` (D1). `lineage_path_json` (added by WS1a Task 4) stays `Text, nullable=False`; verify NO `server_default` is present (decision D7 — WS1a landed the column NOT NULL with no default; if one crept in post-plan, delete it here so every writer is explicit).
- Bump the schema epoch constant that WS1a Task 4 set to 34 → **35** (locate it where WS1a Task 4 bumped it, `SQLITE_SCHEMA_EPOCH` with its history comment; dev databases are wiped pre-release — no migration, spec §4.3). Append the `35 →` line to the history comment: tri-column retirement (`tokens`/`token_outcomes`/`token_work_items`), `token_outcomes.expected_branches_json` deleted, `lineage_path_json` sole journal lineage truth.
- **Epoch-pin fan-out** (the identical sweep WS1a Task 4 step 3(f) established at 33 → 34). Run `git grep -n "epoch 34\|epoch is 34\|→ 34\|== 34"` and adjudicate every hit: statements of the CURRENT epoch move to 35 (`website/get-started.html` "29 → 34" becomes "29 → 35"; `docs/guides/sharing-pipelines.md` "below epoch 34" becomes "below epoch 35"; `docs/product/current-state.md`; the prose pinned by the two doc-prose pin tests `tests/unit/docs/test_composer_capability_docs.py` and `tests/unit/docs/test_staging_session_recreation_policy.py` — update doc AND pin together); history entries (e.g. the `34 →` epoch-history comment line, "at epoch 34 lacks it") stay. Update the epoch comments/constants in `tests/unit/core/landscape/test_schema_epoch_and_required_columns.py`, `tests/unit/core/landscape/test_token_ownership_run_scope.py`, and `tests/integration/web/composer/guided/test_schema9_epoch.py` to 35 with a one-line reason. Add a CHANGELOG entry under 0.7.2: `Landscape SQLITE_SCHEMA_EPOCH 34 → 35: unified-lineage flip — tri-field lineage columns retired onto token_lineage_frames + lineage_path_json; token_outcomes.expected_branches_json removed; existing audit stores must be recreated.`
- The `schema.py:848-855` COALESCED claim-predicate arm: UNCHANGED (D1 — it reads the kept column).

- [ ] **Step 2: `database.py` verification lists + recovery spec flip**

`:288` `("tokens", "expand_group_id")` → replace with `("token_lineage_frames", "member_key")` and add `("group_records", "member_count")` (startup now verifies the NEW tables' presence); `:300` delete the `("token_outcomes", "expected_branches_json")` line; `:394-397` delete the three retired work-item entries, keep `("token_work_items", "join_group_id")`, add `("token_work_items", "lineage_path_json")`; `:458` `coalesce_branch_losses.branch_name` STAYS (the ledger is WS3's); `:591-593` composite-FK triples STAY (both reference kept columns).

`core/checkpoint/recovery.py`: delete `branch_name`/`fork_group_id`/`expand_group_id` from `IncompleteTokenSpec` (keep `join_group_id` — the tokens column stays — and `lineage_path` from WS1a Task 5); delete the three retired column reads from `_get_incomplete_token_work`'s token select. `resume_incomplete_token`'s TokenInfo reconstruction (`processor.py:2868-2878`) drops the three retired kwargs (Task 8's `TokenInfo` no longer accepts them); its dispatch already reads only `spec.lineage_path` + `spec.join_group_id` (Task 4).

- [ ] **Step 3: `data_flow/tokens.py` writer flip**

`create_token` becomes:

```python
    def create_token(
        self,
        row_id: str,
        *,
        token_id: str | None = None,
        lineage_path: tuple[LineageFrame, ...] = (),
        join_group_id: str | None = None,
    ) -> Token:
        """Create a token (row instance in DAG path).

        lineage_path frames are written to token_lineage_frames in the SAME
        transaction as the token INSERT (the sole lineage write path).
        join_group_id is the merge-event column and is set only by coalesce
        writers (kept column, anchors the coalesce_effects composite FK).
        """
        token_id = token_id or generate_id()
        timestamp = now()
        run_id = self._ownership.resolve_run_id_for_row(row_id)

        if join_group_id is not None and not join_group_id.strip():
            raise AuditIntegrityError(f"create_token: join_group_id must be None or non-empty, got {join_group_id!r}")

        token = Token(
            token_id=token_id,
            row_id=row_id,
            join_group_id=join_group_id,
            lineage_path=lineage_path,
            created_at=timestamp,
            run_id=run_id,
        )
        with self._db.write_connection() as conn:
            result = conn.execute(
                tokens_table.insert().values(
                    token_id=token.token_id,
                    row_id=token.row_id,
                    run_id=run_id,
                    join_group_id=token.join_group_id,
                    created_at=token.created_at,
                )
            )
            if result.rowcount == 0:
                raise AuditIntegrityError(f"create_token: token INSERT affected zero rows (token_id={token.token_id})")
            self._insert_lineage_frames(conn, token_id=token.token_id, run_id=run_id, frames=lineage_path)
        return token
```

(The old mutual-exclusivity and `branch_name ⇒ fork_group_id` invariants at `:362-378` are RETIRED WITH THE COLUMNS — the shape is now unrepresentable: `LineageFrame` cannot express a branch without a group, and fork-vs-join is structural. If `create_token` previously used `self._ops.execute_insert`, keep whichever connection discipline WS1a's `_insert_lineage_frames` established — frames and token must share one transaction.)

**Seam rename (one representation post-flip):** WS1a Task 6 landed `_insert_lineage_frames(..., frames: Sequence[LineageFrame])` / `_load_lineage_frames(...) -> tuple[LineageFrame, ...]` already `LineageFrame`-typed, and the crafted-token seam as `create_token(..., lineage_frames: Sequence[LineageFrame] = ())`. The flip's residual work is the rename only: the seam kwarg becomes `lineage_path: tuple[LineageFrame, ...] = ()` (the private helpers' types are already correct) — every WS1a-era caller (including the Task 8 shared builders) migrates in this same commit; the guard in Task 13 pins the single write path, and Task 12's sweep catches stragglers.

**Release-aware parent-path threading (decision D8 prerequisite):** `fork_token` and `expand_token` gain `parent_lineage_path: tuple[LineageFrame, ...]` supplied by `TokenManager`'s in-memory (post-any-pop) path. Each verb calls `_assert_parent_lineage(conn, parent_ref=parent_ref, supplied=parent_lineage_path)` (introduced in Task 10 step 5) and stacks the child frame on the SUPPLIED path — not the durable mint frames — so a row_union-released parent's children carry the popped base path (ruling 27). `coalesce_tokens(..., parent_lineage_paths=...)` gets the same treatment for its merged-token mint.

`create_row_with_token` (`:311-321`): drop the three retired kwargs from the INSERT values (root tokens: empty path, no frames rows needed — but still call `_insert_lineage_frames(..., frames=())` for uniformity only if WS1a's writer no-ops on empty; otherwise skip). Fork child INSERT (`:482-491`): drop `fork_group_id=`/`branch_name=` values — the child's identity is its frames rows (WS1a's dual-write block becomes the only write). Expand child INSERT (`:1375-1385`): drop `expand_group_id=`. Coalesce merged-token INSERT (locate `join_group_id=join_group_id` near `:797`): keeps `join_group_id`, drops nothing else. Fork FORKED-outcome INSERT (`:526-537`): drop `fork_group_id=` and `expected_branches_json=` values. `_load_children_for_parent` (`:589-639`): drop the three retired columns from the select and the `Token(...)` construction; add a joined/ordered frames fetch so each returned `Token` carries `lineage_path` (reuse `load_lineage_paths` on the children ids inside the same connection).

- [ ] **Step 4: outcome writer flip**

`data_flow/outcomes.py`: delete the `fork_group_id`/`join_group_id`/`expand_group_id` parameters from `record_token_outcome` (`:443-445`) and every internal values-dict/validation call (`:258-260`, `:489-491`, `:535-537`); delete them from the reads at `:685-686`; `data_flow_repository.py` facade drops the same pass-through kwargs. Grep for callers passing the deleted kwargs: `git grep -n "record_token_outcome(" src/elspeth | xargs -I{} true` — fix each call site (the coalesce executor's three raw calls pass `join_group_id` — delete the kwarg; their replacement by settle-member is WS3, NOT this plan). `web/execution/accounting.py:238-273`: shrink `_evidence_presence_columns` to `batch_id`/`error_hash` and the `evidence_present` map to `sink_name`/`batch_id`/`error_hash` (the shared D4 validator drives the rest).

- [ ] **Step 5: Spot-run** — `pytest tests/unit/core/landscape/test_token_recording.py -x -q` — expect progress (fork/expand mint tests moving); full green waits for Tasks 10–12. No commit.

---

### Task 10: Replay predicates — frame equality + strict pop (§4.4, Tier-1)

**Files:**
- Modify: `src/elspeth/core/landscape/data_flow/tokens.py` — `_reconcile_fork_replay` (`:546-587`), `_reconcile_expansion_replay` (`:1493+`), expand replay entry (`:1281-1324`), coalesce replay/reconcile (locate `divergent` in the coalesce region), and the row_union release pop in `src/elspeth/engine/processor.py:3028-3105`
- Test: `tests/unit/core/landscape/test_token_recording.py` (rewritten in Task 7 step 4 — turn it green)

**Interfaces:**
- Consumes: `load_lineage_paths` (Task 1); `group_records_table` (WS1a Task 4); `_load_lineage_frames` (WS1a Task 6, already returning `tuple[LineageFrame, ...]`; its crafted-token seam kwarg is renamed in Task 9 step 3); `pop_closer_frame` (WS1a Task 1). `_assert_parent_lineage` is INTRODUCED here (step 5, decision D8) — WS1a has no supplied-path cross-check.
- Produces: the D8-weakened `_assert_parent_lineage` and the frame-equality predicate helper used by all three families:

```python
def _expected_child_frames(
    parent_path: tuple[LineageFrame, ...],
    *,
    kind: FrameKind,
    group_id: str,
    member_key: str,
) -> tuple[LineageFrame, ...]:
    return parent_path + (LineageFrame(kind=kind, group_id=group_id, member_key=member_key),)
```

- [ ] **Step 1: `_reconcile_fork_replay` rewrite**

```python
    def _reconcile_fork_replay(
        self,
        conn: Connection,
        *,
        parent_ref: TokenRef,
        row_id: str,
        branches: Sequence[str],
        step_in_pipeline: int | None,
        outcome: RowMapping,
    ) -> tuple[list[Token], str]:
        """Return a previously committed exact fork or refuse divergent replay.

        The recorded roster is DERIVED from the children's persisted FORK frames
        (written atomically with the FORKED outcome — decision D2 retired
        expected_branches_json): child i's innermost frame must be
        (FORK, fork_group_id, branches[i]) appended to the parent's own path.
        """
        children = self._load_children_for_parent(conn, parent_ref=parent_ref)
        parent_path = self.load_lineage_paths(parent_ref.run_id, [parent_ref.token_id])[parent_ref.token_id]
        child_paths = self.load_lineage_paths(parent_ref.run_id, [child.token_id for child, _ordinal in children])
        fork_group_ids = {
            child_paths[child.token_id][-1].group_id
            for child, _ordinal in children
            if child_paths[child.token_id] and child_paths[child.token_id][-1].kind is FrameKind.FORK
        }
        fork_group_id = next(iter(fork_group_ids)) if len(fork_group_ids) == 1 else None
        exact = (
            outcome["outcome"] == TerminalOutcome.TRANSIENT.value
            and outcome["path"] == TerminalPath.FORK_PARENT.value
            and fork_group_id is not None
            and len(children) == len(branches)
            and all(
                child.row_id == row_id
                and child.run_id == parent_ref.run_id
                and child.join_group_id is None
                and child.step_in_pipeline == step_in_pipeline
                and ordinal == expected_ordinal
                and child_paths[child.token_id]
                == _expected_child_frames(parent_path, kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch)
                for expected_ordinal, ((child, ordinal), branch) in enumerate(zip(children, branches, strict=True))
            )
        )
        if not exact:
            raise AuditIntegrityError(
                f"fork_token: divergent fork replay for parent token {parent_ref.token_id!r}; "
                "the requested branches or persisted lineage frames do not match the committed fork"
            )
        return [child for child, _ordinal in children], fork_group_id
```

(The old `:575` `expand_group_id is None` assertion is retired WITH the column — frame equality against the parent's full path subsumes it and is STRONGER under nesting: a child claiming extra or missing outer frames now fails the equality.)

- [ ] **Step 2: `_reconcile_expansion_replay` — same treatment plus `group_records` idempotency**

In the exact-replay branch: assert each child's persisted path `== _expected_child_frames(parent_path, kind=FrameKind.EXPAND, group_id=expand_group_id, member_key=child.token_id)` and the child ordinals/payload refs as today; then:

```python
        group_row = conn.execute(
            select(group_records_table.c.opener_token_id, group_records_table.c.member_count)
            .where(group_records_table.c.run_id == parent_ref.run_id)
            .where(group_records_table.c.group_id == expand_group_id)
        ).one_or_none()
        if (
            group_row is None
            or group_row.opener_token_id != parent_ref.token_id
            or int(group_row.member_count) != len(children)
        ):
            raise AuditIntegrityError(
                f"expand_token: divergent expansion replay for parent token {parent_ref.token_id!r}; "
                f"group_records for group {expand_group_id!r} does not match the committed expansion "
                "(a re-drive can never mint a second group)"
            )
```

The existing `batches.expansion_group_id` claim logic (`:1294-1321`, `:1347-1368`) is KEPT verbatim — the `group_records` check extends it, never replaces it (`contracts/engine.py` receipts stay bound to `batches.expansion_group_id`, consumer-roster risk note 8).

- [ ] **Step 3: coalesce replay strict pop**

Locate the coalesce divergent-replay predicate (grep `"divergent"` in the `coalesce_tokens` region of `data_flow/tokens.py`). Add to its exact-replay conditions: the merged token's persisted path equals the shared parent-path prefix — i.e. for every parent, `parent_path[:-1]` values are identical, each parent's innermost frame is a FORK frame of ONE group, and `merged_path == parent_paths[0][:-1]` (the strict pop, ruling 24/28). On the LIVE mint side, the same invariant is already enforced in BOTH layers by WS1a (Task 6's durable `AuditIntegrityError` check + Task 8's in-memory `OrchestrationInvariantError` pop); this step extends the REPLAY predicate to assert it too — replay must not trust memory:

```python
        for parent_id, parent_path in parent_paths.items():
            if not parent_path or parent_path[-1].kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"coalesce_tokens: parent {parent_id!r} arrives without an innermost FORK frame — "
                    "strict pop violated (spec §4.2; §7 rule 5 makes this unreachable from config)"
                )
        remaining = {parent_paths[parent_id][:-1] for parent_id in parent_paths}
        if len(remaining) != 1:
            raise AuditIntegrityError(
                "coalesce_tokens: parents do not share one remaining lineage path after the pop — lineage corruption"
            )
```

- [ ] **Step 4: row_union release pop (ruling 27)** — `engine/processor.py:3028-3105`

In the release path that today RETAINS branch identity (`:3042-3048` doctrine comment), derive the union's fork group ONCE from the released set, then pop every token through WS1a's strict-pop primitive:

```python
        release_group_ids: set[str] = set()
        for token in released_tokens:
            if not token.lineage_path or token.lineage_path[-1].kind is not FrameKind.FORK:
                raise OrchestrationInvariantError(
                    f"row_union release: token {token.token_id!r} has no innermost FORK frame to pop "
                    "(ruling 27 strict pop; §7 rule 5 makes this unreachable from a valid build)"
                )
            release_group_ids.add(token.lineage_path[-1].group_id)
        if len(release_group_ids) != 1:
            raise OrchestrationInvariantError(
                f"row_union release: released tokens do not share one innermost FORK group "
                f"(got {sorted(release_group_ids)!r}) — ruling 27 strict pop requires it"
            )
        (fork_group_id,) = release_group_ids
        released_tokens = [
            replace(token, lineage_path=pop_closer_frame(token.lineage_path, kind=FrameKind.FORK, group_id=fork_group_id))
            for token in released_tokens
        ]
```

(`pop_closer_frame` — WS1a Task 1 — re-verifies per token and raises `OrchestrationInvariantError` on any mismatch; `dataclasses.replace` is the `identity.py:110` pattern.) Delete the retain-identity doctrine comment; note ruling 27 and that downstream consumers needing the pre-union branch read audit rows. The `_row_union_group_released` staleness hazard note (`:3105`) gets a pointer: a popped frame can no longer authenticate a §6.2 loss (WS3 consumes this).

- [ ] **Step 5: Introduce the release-aware `_assert_parent_lineage` (decision D8 — RATIFIED 2026-08-22: the journal-row release witness stands)**

The baseline contract is EQUALITY (engine-supplied current path == durable mint frames — Task 9 step 3 threads the supplied path into the child-mint verbs). Step 4 breaks equality for exactly one shape: a row_union-released token later becomes a parent (reachable — `row-union-interleave` feeds released tokens into a downstream aggregation whose flush calls `expand_token`, `processor.py:1623`). Add the check in `data_flow/tokens.py` (mint frames come from Task 9's retyped `_load_lineage_frames`, so both sides are `tuple[LineageFrame, ...]`):

```python
    def _assert_parent_lineage(self, conn: Connection, *, parent_ref: TokenRef, supplied: tuple[LineageFrame, ...]) -> None:
        """Cross-check a parent's supplied current path against its durable mint frames.

        Exact equality, with ONE sanctioned divergence (ruling 27): a row_union
        release pops the parent's innermost FORK frame, so a released token's
        current path is its mint frames minus that frame. The release is
        journal-first, so the durable witness is the token's own row_union
        work-item row having left BLOCKED.
        """
        mint = self._load_lineage_frames(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)
        if supplied == mint:
            return
        if (
            mint
            and mint[-1].kind is FrameKind.FORK
            and supplied == mint[:-1]
            and self._row_union_release_witness(conn, token_id=parent_ref.token_id, run_id=parent_ref.run_id)
        ):
            return
        raise AuditIntegrityError(
            f"parent lineage divergence for token {parent_ref.token_id!r} (run {parent_ref.run_id!r}): "
            f"supplied={supplied!r} mint={mint!r} and no completed row_union release explains the difference"
        )

    def _row_union_release_witness(self, conn: Connection, *, token_id: str, run_id: str) -> bool:
        """Durable evidence a row_union closer released this token (ruling 27)."""
        count = conn.execute(
            select(func.count())
            .select_from(token_work_items_table)
            .where(token_work_items_table.c.run_id == run_id)
            .where(token_work_items_table.c.token_id == token_id)
            .where(token_work_items_table.c.row_union_name.is_not(None))
            .where(token_work_items_table.c.status != TokenWorkStatus.BLOCKED.value)
        ).scalar()
        return bool(count)
```

(Verify the post-release status transition for row_union BLOCKED rows in `scheduler_repository.py` — grep `row_union_name` there — and match the witness's status predicate to the actual release transition rather than `!= BLOCKED` if it is narrower.) Tests, beside the Task 7 rewrites in `test_token_recording.py`: (a) fork children minted, path popped by hand, `expand_token` with the popped path and NO union journal row → `AuditIntegrityError`; (b) same with a crafted completed row_union work-item row → accepted, child frames = popped path + EXPAND frame; (c) supplied shorter by TWO frames with the witness present → still refused (the weakening admits exactly one popped FORK frame, never a prefix walk).

- [ ] **Step 6: Run** — `pytest tests/unit/core/landscape/test_token_recording.py -q` — Expected: PASS (the Task 7 step 4 rewrites go green). `pytest tests/property/audit/test_fork_coalesce_flow.py -q` will still be RED (its SQL reads `token_outcomes.fork_group_id`) — that migration is Task 12. No commit.

---

### Task 11: Journal flip — codec, queue verbs, barrier persistence, journal restore, bidirectional cross-check

**Files:**
- Modify: `src/elspeth/core/landscape/scheduler/payload_codec.py:74-103`
- Modify: `src/elspeth/core/landscape/scheduler/work_items.py:60-140`, `:260-275`
- Modify: `src/elspeth/core/landscape/scheduler/queue.py` — every verb currently threading the three retired kwargs (`:63-66/:106-109`, `:172-175/:206-209`, `:234-237/:264-267`, `:292-295/:319-322`, `:349-352/:383-386`, `:453-456` and any further hits of `git grep -n "fork_group_id" src/elspeth/core/landscape/scheduler/queue.py`)
- Modify: `src/elspeth/core/landscape/scheduler/barrier.py:487`, `:703-706`, `:775-778`
- Modify: `src/elspeth/core/landscape/scheduler/leases.py:83` (message text)
- Modify: `src/elspeth/core/landscape/scheduler/restore_read_model.py:322-350` (outcomes-side join re-point)
- Modify: `src/elspeth/engine/journal_restore.py:55-62`, `:175-232`, `:245-258`, `:271`
- Modify: `src/elspeth/engine/scheduler_drain.py:897-905` (rebuild), `:1110-1165` (enqueue kwargs)
- Modify: `src/elspeth/engine/work_items.py:120-135`

**Interfaces:**
- Consumes: Task 8's flipped `TokenWorkItem`; WS1a Task 1's `contracts.identity.lineage_path_to_json` / `lineage_path_from_json` (raise `ValueError` on corrupt input) plus WS1a Task 5's already-landed `lineage_path_json` decode in the row→`TokenWorkItem` mapping (`item_from_mapping` wraps the `ValueError` in `AuditIntegrityError`); Task 1's `load_lineage_paths`.
- Produces: `token_from_journal_item` reconstructing ONLY `lineage_path` (legacy kwargs deleted); the bidirectional codec-vs-table cross-check `verify_lineage_journal_consistency`.

- [ ] **Step 1: `payload_codec.py`** — WS1a Task 5 already reconstructs `lineage_path` here beside the four legacy kwargs (`:96-99`); the flip DELETES the legacy reads so the function body becomes exactly:

```python
def token_from_journal_item(
    item: TokenWorkItem,
    *,
    attempt_offset: int,
    resume_checkpoint_id: str | None,
) -> TokenInfo:
    """Rebuild a TokenInfo from a journal BLOCKED row (codec-pure, no engine access)."""
    row_data = deserialize_row_payload(item.row_payload_json)
    return TokenInfo(
        row_id=item.row_id,
        token_id=item.token_id,
        row_data=row_data,
        lineage_path=item.lineage_path,
        resume_attempt_offset=attempt_offset,
        resume_checkpoint_id=resume_checkpoint_id,
    )
```

- [ ] **Step 2: row↔dataclass mapping (`work_items.py`)**

`:73-76` (row → `TokenWorkItem`): WS1a Task 5 already decodes `lineage_path` from `data["lineage_path_json"]` in `item_from_mapping`, wrapping the codec's `ValueError` in `AuditIntegrityError` (verify the wrap is present — it is WS1a Task 5 step 3(c)'s contract). The flip DELETES the three retired field reads, leaving:

```python
        join_group_id=data["join_group_id"],
        lineage_path=lineage_path_from_json(data["lineage_path_json"]),
```

`:105-134` (insert-values builder): delete the three retired parameters; the remaining group-context parameters are `join_group_id: str | None` and `lineage_path_json: str` (WS1a Task 5's threading, now the only lineage input). `:268-271` (the column list used for verbatim copies/SELECTs): same three deletions.

- [ ] **Step 3: `queue.py` verbs** — for EVERY verb hit by `git grep -n "fork_group_id" src/elspeth/core/landscape/scheduler/queue.py`: DELETE the three parameters `branch_name`/`fork_group_id`/`expand_group_id` (keep `join_group_id` and the `lineage_path: tuple[LineageFrame, ...] = ()` parameter WS1a Task 5 added beside them). `engine/scheduler_drain.py:1110-1165`: both enqueue calls delete the three retired kwargs (`lineage_path`/`join_group_id` already threaded — WS1a Tasks 5/9); the pending-sink rebuild at `:897-905` constructs `TokenInfo(..., lineage_path=scheduled.lineage_path)` (WS1a Task 9 already moved the join context onto the `RowResult`).

- [ ] **Step 4: barrier persistence + leases message**

`barrier.py:703-706` (dict form) and `:775-778` (kwargs form): DELETE the four retired entries; keep `"join_group_id": emission.join_group_id` and the `"lineage_path_json": lineage_path_to_json(emission.lineage_path)` entry WS1a Task 5 added (`lineage_path_to_json` imports from `elspeth.contracts.identity` — WS1a Task 1). `:487` (`branch_name=loss.branch_name`) is `coalesce_branch_losses` replay — UNCHANGED (WS3's ledger). `leases.py:83`: reword the COALESCED prose to "...and join_group_id for COALESCED (the kept merge-event column)".

- [ ] **Step 5: `journal_restore.py`** — branch identity from the path. `RestoredCoalesceBranch` keeps `branch_name: str` (it is the executor's roster key — config vocabulary). Derive it:

```python
            innermost = item.lineage_path[-1] if item.lineage_path else None
            if innermost is None or innermost.kind is not FrameKind.FORK:
                raise AuditIntegrityError(
                    f"BLOCKED journal row for token {item.token_id!r} at coalesce "
                    f"{item.coalesce_name!r} (run {self._run_id!r}, resume checkpoint "
                    f"{resume_checkpoint_id!r}) has no innermost FORK frame — only forked branch "
                    "tokens block at a coalesce barrier; journal corruption."
                )
            branch_name = innermost.member_key
```

then substitute `branch_name` for every subsequent `item.branch_name` read in the validation chain (`:182`, `:194-232` duplicate/allowlist checks) — the allowlist check against `self._settings[item.coalesce_name].branches` is UNCHANGED in meaning (branch vocabulary is config-level and stays).

- [ ] **Step 6: `restore_read_model.py` outcomes-side re-point** — the COALESCED parent-outcome check (`:326-349`) loses `token_outcomes.join_group_id`. The equivalent evidence: the outcome pair check stays (`SUCCESS`/`COALESCED`), and the result-binding moves entirely onto the tokens side, which this function ALREADY verifies at `:362-370` (`tokens.join_group_id == effect.result_join_group_id` — kept column). Delete `token_outcomes_table.c.join_group_id` from the select (`:330`) and the `outcome.join_group_id == effect.result_join_group_id` conjunct (`:347`); the docstring gains one line: "member-outcome join binding retired with the outcome column; the result-token binding (tokens.join_group_id + composite FK) is the durable anchor."

- [ ] **Step 7: `engine/work_items.py:120-135`** — `token.branch_name` is now the derived accessor (kept call syntax); update only the invariant MESSAGE at `:127-128` to name the frame: "Fork children must carry an innermost FORK frame."

- [ ] **Step 8: bidirectional codec-vs-table cross-check (spec §4.3 "restore integrity-checks codec-vs-table bidirectionally")**

Add to `core/landscape/scheduler/restore_read_model.py` (or the module the barrier restore composition calls — wire it where journal BLOCKED rows are listed for restore):

```python
    def verify_lineage_journal_consistency(self, run_id: str, items: Sequence[TokenWorkItem]) -> None:
        """Codec-vs-table bidirectional check: each journal row's decoded lineage_path
        must equal the token's token_lineage_frames rows exactly (both directions —
        a frames row absent from the codec path and a codec frame absent from the
        table are BOTH AuditIntegrityError)."""
        table_paths = self._data_flow.load_lineage_paths(run_id, [item.token_id for item in items])
        for item in items:
            if item.lineage_path != table_paths[item.token_id]:
                raise AuditIntegrityError(
                    f"lineage journal/table divergence for token {item.token_id!r} (run {run_id!r}): "
                    f"journal={item.lineage_path!r} table={table_paths[item.token_id]!r}"
                )
```

Call it from `BarrierJournalRestoreContext`'s restore entry (grep `BarrierJournalRestoreContext` for the composition site) on the full BLOCKED-item list before hydration, and from the resume work-set build in `core/checkpoint/recovery.py` for incomplete tokens that have journal rows. **Caveat for row_union BLOCKED rows:** the journal path at a union barrier still CARRIES the FORK frame (the pop happens at release), so the pre-hydration check runs against un-popped paths and needs no D8 relief. Unit test beside the Task 7 codec module: seed one BLOCKED row whose `lineage_path_json` disagrees with its frames rows (both directions: extra frame in JSON; extra row in table) → `AuditIntegrityError`.

- [ ] **Step 9: Run** — `pytest tests/unit/core/landscape/ tests/unit/engine/ -q` — many failures remaining are TEST-SIDE constructions (fixed next task). Production modules must import clean: `python -c "import elspeth.engine.processor, elspeth.core.landscape.schema, elspeth.core.landscape.scheduler_repository"`. No commit.

---

### Task 12: Test-builder migration, corpus harness rewrite, fixture regeneration, full green, THE flip commit

**Files:**
- Modify: `tests/unit/engine/test_processor.py` (`_make_processor`, `_persist_blocked_scheduler_work`, `_persist_token_for_scheduler`), `tests/integration/pipeline/test_barrier_intake_dispositions.py` (`_branch_token`, `_arrive_via_intake`, `_work_item_row`), `tests/integration/pipeline/test_aggregation_recovery.py` — the three cross-tier load-bearing builder modules (test-harness scout risk 1: migrate as ONE slice; the e2e death matrix, timing invariance, and both Postgres suites inherit)
- Modify: `tests/e2e/recovery/harness.py` — `_craft_crashed_lease` and every crafted work-item write gains `lineage_path_json`; add the `craft_lineage` helper (scout risk 2):

```python
def craft_lineage(db: LandscapeDB, *, run_id: str, token_id: str, path: tuple[LineageFrame, ...]) -> None:
    """Write token_lineage_frames rows for a hand-crafted token so restore's
    bidirectional codec-vs-table check passes for crafted images.

    Prefer creating crafted tokens through the production Tier-1 seam —
    create_token(..., lineage_path=...) (WS1a Task 6e's lineage_frames seam,
    renamed in Task 9 step 3) — and reserve this helper for rows
    crafted against tokens that already exist without frames.
    """
    with db.write_connection() as conn:
        for depth, frame in enumerate(path):
            conn.execute(
                token_lineage_frames_table.insert().values(
                    token_id=token_id,
                    run_id=run_id,
                    depth=depth,
                    kind=frame.kind.value,
                    group_id=frame.group_id,
                    member_key=frame.member_key,
                )
            )
```
- Modify: `tests/fixtures/dag_scenario_corpus/harness.py` — the four retired-column read sites: token sort keys (`:925-951`), `StableTokenProjection.branch_name` source (`:971`), `expand_parent` outcome gate (`:1111-1136`), expansion-children gathering (`:1146`)
- Modify: every test constructing `TokenInfo(...)`/`make_token_info(...)` with retired kwargs — enumerate with `git grep -ln "branch_name=" tests/ | xargs git grep -ln "TokenInfo\|make_token_info"` and migrate each to `lineage_path=(LineageFrame(...),)`
- Modify: `tests/property/audit/test_fork_coalesce_flow.py` — its raw SQL re-points at `token_lineage_frames` (`COUNT(DISTINCT group_id) WHERE kind='fork'`), same derivation as Task 5's reports change
- Regenerate: `row-union-interleave` fixture expectations in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` + rotation ledger comment in `tests/unit/architecture/test_dag_scenario_corpus_contract.py` — per the protocols plan's regeneration procedure

**Interfaces:**
- Consumes: everything above; the protocols plan's frozen projection files and diff tool.
- Produces (Step 0 — the wire contract the harness rewrite in Step 2 consumes):
  - `TokenExportRecord`: drop `branch_name`/`fork_group_id`/`expand_group_id`; add `lineage_path: list[list[str]]`; keep `join_group_id`.
  - `TokenOutcomeExportRecord`: drop `fork_group_id`/`join_group_id`/`expand_group_id`/`expected_branches_json`.
  - `GroupRecordExportRecord(record_type: Literal["group_record"], run_id, group_id, kind, opener_token_id, member_count, created_at)`.
  - `GroupLossExportRecord(record_type: Literal["group_loss"], loss_id, run_id, closer_name, group_id, member_key, token_id, reason, recorded_by, recorded_at, adopted_epoch)` — mirrors WS1a Task 4's `group_losses` DDL; the stream is empty until WS3 writes the ledger, but the table enters the portable export at THIS flip (ratified — D3).

- [ ] **Step 0: Export reshape (decision D3 — moved here from Phase A because the corpus harness reads these keys until Step 2 rewrites it).**

First the failing test, `tests/unit/core/landscape/test_exporter_lineage_records.py` (create):

```python
"""tests/unit/core/landscape/test_exporter_lineage_records.py"""
def test_token_export_record_carries_lineage_path_not_tri_fields() -> None:
    from elspeth.contracts.export_records import TokenExportRecord

    keys = set(TokenExportRecord.__annotations__)
    assert "lineage_path" in keys
    assert {"branch_name", "fork_group_id", "expand_group_id"} & keys == set()
    assert "join_group_id" in keys  # merge event — kept


def test_token_outcome_export_record_drops_retired_columns() -> None:
    from elspeth.contracts.export_records import TokenOutcomeExportRecord

    keys = set(TokenOutcomeExportRecord.__annotations__)
    assert {"fork_group_id", "join_group_id", "expand_group_id", "expected_branches_json"} & keys == set()


def test_group_loss_export_record_mirrors_the_ledger_ddl() -> None:
    from elspeth.contracts.export_records import GroupLossExportRecord

    assert set(GroupLossExportRecord.__annotations__) == {
        "record_type", "loss_id", "run_id", "closer_name", "group_id",
        "member_key", "token_id", "reason", "recorded_by", "recorded_at", "adopted_epoch",
    }
```

plus a behaviour test through the real exporter: run one fork→coalesce pipeline via the existing exporter-test scaffolding in `tests/unit/core/landscape/`, export, and assert every fork-child token record has `lineage_path == [["fork", <fork_group_id>, <branch>]]` matching that token's `token_lineage_frames` rows, and each `record_type == "group_record"` row round-trips the `group_records` table. Run: `pytest tests/unit/core/landscape/test_exporter_lineage_records.py -v` → FAIL.

Then implement — `contracts/export_records.py`:

```python
class TokenExportRecord(TypedDict):
    record_type: Literal["token"]
    run_id: str
    token_id: str
    row_id: str
    step_in_pipeline: int | None
    lineage_path: list[list[str]]  # [[kind, group_id, member_key], ...] outermost first
    join_group_id: str | None      # merge event (tokens.join_group_id — kept column)
    created_at: str


class GroupRecordExportRecord(TypedDict):
    record_type: Literal["group_record"]
    run_id: str
    group_id: str
    kind: str
    opener_token_id: str
    member_count: int
    created_at: str
```

and delete the four retired keys from `TokenOutcomeExportRecord`. `core/landscape/exporter.py:906-957`: preload `frames_by_token` for the run with one ordered select over `token_lineage_frames_table` (same shape as Task 1's `load_lineage_paths`, keyed by token) beside the existing `tokens_by_row` preload; emit `"lineage_path": [[f.kind.value, f.group_id, f.member_key] for f in frames_by_token.get(token.token_id, ())]`; keep `"join_group_id": token.join_group_id`; delete the three retired keys from both record builders and `"expected_branches_json"` from the outcome record; add a `group_records` export loop yielding `GroupRecordExportRecord`s ordered by `group_id`, and a `group_losses` loop yielding `GroupLossExportRecord`s ordered by `loss_id` (empty until WS3 — the loop still runs so the table is part of the export surface from this flip). Register BOTH new record types wherever `record_type` literals are enumerated (grep `"token_parent"` in `exporter.py`/`contracts/export_records.py` for the union/dispatch list). Re-run the new test file → PASS.

- [ ] **Step 1: Migrate the three shared builder modules in one pass.** `_branch_token` becomes:

```python
def _branch_token(branch: str, *, fork_group_id: str = "fg-intake", row_id: str = RUN_ROW_ID) -> TokenInfo:
    return TokenInfo(
        row_id=row_id,
        token_id=f"tok-{branch}",
        row_data=make_row({"branch": branch}),
        lineage_path=(LineageFrame(kind=FrameKind.FORK, group_id=fork_group_id, member_key=branch),),
    )
```

(Adapt names/defaults to the module's existing signature — read it first; the load-bearing change is kwargs → one FORK frame.) `_persist_blocked_scheduler_work` and `_work_item_row` write `lineage_path_json=lineage_path_to_json(path)` (import from `elspeth.contracts.identity` — WS1a Task 1) and drop the three retired column values; every crafted BLOCKED row ALSO writes matching frames rows via `craft_lineage` (or the bidirectional check will fail the HARNESS, not the code under test — scout risk 2).

- [ ] **Step 2: Corpus harness rewrite** (`tests/fixtures/dag_scenario_corpus/harness.py`) — the export records now carry `lineage_path` (Step 0 / D3):

```python
def _frame_list(record: Mapping[str, Any]) -> list[list[str]]:
    frames = record.get("lineage_path")
    return frames if isinstance(frames, list) else []


def _branch_of(record: Mapping[str, Any]) -> str | None:
    for kind, _group, member in reversed(_frame_list(record)):
        if kind == "fork":
            return member
    return None


def _expand_group_of(record: Mapping[str, Any]) -> str | None:
    for kind, group, _member in reversed(_frame_list(record)):
        if kind == "expand":
            return group
    return None
```

- `:925-951` sort keys: `record.get("branch_name")` → `_branch_of(record)`; `record.get("expand_group_id") is None` → `_expand_group_of(record) is None`.
- `:971` projection source: `branch_name=_branch_of(record)`.
- `:1111-1136` `expand_parent` outcome gate: the outcome row no longer carries `expand_group_id`/`expected_branches_json`; derive the expansion from the CHILD records — children of parent P with an innermost `expand` frame whose `member_key == child token_id`; `expected_child_count` from the exported `group_record` with that `group_id` (`member_count`).
- `:1146` children gathering: `_expand_group_of(child_record)`.

**Self-oracle guard:** before ANY assertion change, run the rewritten harness against the STORED pre-flip snapshots (protocols plan Task 3): `pytest tests/integration/core/dag/test_dag_scenario_production_path.py tests/integration/core/dag/test_oracle_freeze.py -q`. The oracle-freeze gate compares each scenario's recomputed `frozen_surface` against committed bytes — FROZEN scenarios must match byte-identically; the ONLY divergence allowed is `row-union-interleave` (classified `REGENERATED_WS1` in `oracle_freeze.SCENARIO_CLASSIFICATION`, whose `invariant_subset` — sink bytes, disposition outcomes/paths — the gate still enforces as equal; `branch_name` is a sort key, so token `#ordinal` keys may reshuffle — fixture-oracle risk 7).

- [ ] **Step 3: Regenerate `row-union-interleave`** per the protocols plan §S1: `ELSPETH_ORACLE_FREEZE=write pytest "tests/integration/core/dag/test_oracle_freeze.py" -q -k "row-union-interleave"`, then `git diff tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/row-union-interleave/` and adjudicate every changed line against ruling 27 (branch_name→None on released tokens, ordinal reshuffles enumerated; sink bytes and dispositions identical — the `invariant_subset` gate proves this mechanically). Then hand-edit the corresponding `projection_sha256`/exact blobs in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml`, append the dated A/B-verified rotation note to the ledger in `tests/unit/architecture/test_dag_scenario_corpus_contract.py`, and update `EXPECTED_CASE_REGISTRY_SHA256` if the registry moved. The rotation note MUST cite ruling 27 and the whole-projection adjudication.

- [ ] **Step 3a: Manifest rotation for the export-surface change (ratified: the three epoch-34 tables enter the portable export at this flip).** The export surface is the portable signed run bundle — `derive_run_bundle` in `src/elspeth/core/landscape/exporter.py`, whose record stream and final manifest hash chain now carry `token_lineage_frames` (embedded in every `TokenExportRecord.lineage_path`), `group_records` (`GroupRecordExportRecord`), and `group_losses` (`GroupLossExportRecord`, empty until WS3) per Step 0/D3. Because the corpus manifest pins full-projection blobs, EVERY scenario's `projection_sha256` full-projection entries in `docs/architecture/dag/scenario-corpus/v1/manifest.yaml` rotate — not just `row-union-interleave`'s. Regenerate them, then adjudicate every changed manifest line: it must be a sha/count over full-projection blobs attributable to the new/reshaped record types ONLY, never a stable-projection field (fixture-oracle risk 4 — Task 14 step 2 re-verifies this). Append one dated rotation-ledger note in `tests/unit/architecture/test_dag_scenario_corpus_contract.py` naming the export-surface change (three tables + reshaped token/outcome records) as the cause, distinct from the ruling-27 note in Step 3.

- [ ] **Step 4: Sweep the remaining test constructions.** First the NAMED re-point: `tests/unit/engine/test_token_lineage_path.py` (WS1a Task 8's mint-integration pin) — swap every stored-field comparison for the accessor read (`token.branch_name` etc., now properties); the assertions themselves must not change, and add the row-6 twin (ruling 27: released row_union token → all-None accessors) that WS1a deliberately excluded. Then run `pytest tests/ -n 12 -q`; triage every failure into (a) retired-kwarg construction → migrate to `lineage_path`, (b) assertion on a deleted column/field → rewrite against frames/carriers per the pattern of its production site, (c) genuine regression → STOP and fix production. Zero tolerance for weakening an assertion to pass. `git grep -n "fork_group_id\|expand_group_id" tests/` at the end and adjudicate every survivor (config-vocabulary `branch_name` hits are legitimate and stay).

- [ ] **Step 5: FULL GREEN + the single flip commit.**

Run: `git rev-parse HEAD` (record); `pytest tests/ -n 12`; `git rev-parse HEAD` again (must match). Expected: PASS.

Then stage BY PATHSPEC — the flip's exact file roster (every file Tasks 7–12 touched; build the list from `git status --porcelain` and verify each path is yours; the two forbidden composer files must not appear):

```bash
git add src/elspeth/contracts/identity.py src/elspeth/contracts/audit.py src/elspeth/contracts/scheduler.py \
        src/elspeth/contracts/export_records.py src/elspeth/engine/scheduler_work_codec.py \
        src/elspeth/engine/processor.py src/elspeth/engine/scheduler_drain.py src/elspeth/engine/journal_restore.py \
        src/elspeth/engine/work_items.py src/elspeth/engine/tokens.py \
        src/elspeth/core/landscape/schema.py src/elspeth/core/landscape/database.py \
        src/elspeth/core/landscape/data_flow/tokens.py src/elspeth/core/landscape/data_flow/outcomes.py \
        src/elspeth/core/landscape/data_flow_repository.py src/elspeth/core/landscape/model_loaders.py \
        src/elspeth/core/landscape/lineage.py src/elspeth/core/landscape/exporter.py \
        src/elspeth/core/landscape/execution/sink_effect_identity.py \
        src/elspeth/core/landscape/scheduler/payload_codec.py src/elspeth/core/landscape/scheduler/work_items.py \
        src/elspeth/core/landscape/scheduler/queue.py src/elspeth/core/landscape/scheduler/barrier.py \
        src/elspeth/core/landscape/scheduler/leases.py src/elspeth/core/landscape/scheduler/restore_read_model.py \
        src/elspeth/core/checkpoint/recovery.py src/elspeth/testing/__init__.py \
        src/elspeth/web/execution/accounting.py \
        docs/architecture/adr/  \
        tests/unit/contracts/test_token_info_lineage_flip.py tests/unit/engine/test_scheduler_lineage_codec_flip.py \
        tests/unit/core/landscape/test_token_recording.py tests/unit/core/landscape/test_exporter_lineage_records.py \
        tests/unit/engine/test_processor.py tests/unit/engine/test_token_lineage_path.py \
        tests/integration/pipeline/test_barrier_intake_dispositions.py tests/integration/pipeline/test_aggregation_recovery.py \
        tests/e2e/recovery/harness.py tests/fixtures/dag_scenario_corpus/harness.py \
        tests/fixtures/dag_scenario_corpus/oracle_freeze/v1/row-union-interleave/ \
        tests/property/audit/test_fork_coalesce_flow.py \
        tests/unit/architecture/test_dag_scenario_corpus_contract.py \
        docs/architecture/dag/scenario-corpus/v1/manifest.yaml \
        CHANGELOG.md website/get-started.html docs/guides/sharing-pipelines.md docs/product/current-state.md \
        tests/unit/core/landscape/test_schema_epoch_and_required_columns.py \
        tests/unit/core/landscape/test_token_ownership_run_scope.py \
        tests/integration/web/composer/guided/test_schema9_epoch.py \
        tests/unit/docs/test_composer_capability_docs.py tests/unit/docs/test_staging_session_recreation_policy.py
git add <every additional test file migrated in step 4, by name>
git commit -m "feat(lineage)!: atomic flip — retire tri-field lineage onto lineage_path + token_lineage_frames

Four-table column retirement per spec 4.3; replay predicates rewritten to
frame equality + strict pop (4.4); journal lineage_path_json is sole lineage
truth with a bidirectional codec-vs-table restore check; row_union pops on
release (ruling 27, row-union-interleave regenerated and adjudicated); ADR-019
discriminator matrix re-specified (fork/expand evidence -> frames+group_records)."
```

(Replace the `docs/architecture/adr/` directory add with the specific ADR file you amended, by name.)

---

## PHASE C — guard and checkpoint

### Task 13: Lint/AST guard — retired names cannot come back

**Files:**
- Create: `tests/unit/architecture/test_lineage_retirement_guard.py`

**Interfaces:**
- Consumes: the flipped tree.
- Produces: the standing whole-tree gate (spec §11: "no stored field with a retired name on TokenInfo/TokenWorkItem, no new columns with retired names outside the allowlist, token_lineage_frames as the sole lineage write path").

- [ ] **Step 1: Write the guard (it must PASS against the flipped tree — its red condition is a future regression; mutation-check it in step 2)**

```python
"""tests/unit/architecture/test_lineage_retirement_guard.py

Whole-tree gate for the unified-lineage retirement (spec §11). A green scoped
run elsewhere proves nothing about this file's subject — it asserts over the
ENTIRE src tree.
"""
from __future__ import annotations

import ast
import dataclasses
from pathlib import Path

from elspeth.contracts.identity import TokenInfo
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.core.landscape import schema

SRC_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth"

RETIRED_FIELD_NAMES = {"branch_name", "fork_group_id", "expand_group_id"}

# Columns with retired names that deliberately SURVIVE, with their reasons:
COLUMN_ALLOWLIST = {
    ("tokens", "join_group_id"),            # merge-event anchor; coalesce_effects composite FK target (spec §4.1)
    ("token_work_items", "join_group_id"),  # merged-token work-item carrier (ruling 20, plan decision D1)
    ("coalesce_branch_losses", "branch_name"),  # WS3 replaces this table with group_losses; until then it stands
}


def test_no_stored_retired_lineage_fields_on_token_contracts() -> None:
    for cls in (TokenInfo, TokenWorkItem):
        stored = {f.name for f in dataclasses.fields(cls)}
        illegal = stored & RETIRED_FIELD_NAMES
        assert not illegal, f"{cls.__name__} regrew stored lineage fields {sorted(illegal)} — ruling 21 forbids it"
    assert "join_group_id" not in {f.name for f in dataclasses.fields(TokenInfo)}, "join_group_id left TokenInfo (ruling 20)"
    assert "lineage_path" in {f.name for f in dataclasses.fields(TokenInfo)}


def test_no_retired_name_columns_outside_the_allowlist() -> None:
    violations: list[str] = []
    for table in schema.metadata.tables.values():
        for column in table.columns:
            if column.name in (RETIRED_FIELD_NAMES | {"join_group_id"}) and (table.name, column.name) not in COLUMN_ALLOWLIST:
                violations.append(f"{table.name}.{column.name}")
    assert not violations, (
        f"retired lineage column names reappeared outside the allowlist: {violations} "
        "(spec §11 — extend COLUMN_ALLOWLIST only with an adjudicated reason)"
    )


def test_expected_branches_json_stays_deleted() -> None:
    assert "expected_branches_json" not in schema.token_outcomes_table.c, "decision D2 — roster derives from child frames"


def _modules_inserting_into(table_attr: str) -> set[str]:
    hits: set[str] = []
    for path in SRC_ROOT.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            # matches <table_attr>.insert() — attribute chain ending in the table name then .insert
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "insert"
                and isinstance(node.func.value, (ast.Name, ast.Attribute))
                and (getattr(node.func.value, "id", None) == table_attr or getattr(node.func.value, "attr", None) == table_attr)
            ):
                hits.append(str(path.relative_to(SRC_ROOT)))
    return set(hits)


def test_token_lineage_frames_has_one_write_path() -> None:
    writers = _modules_inserting_into("token_lineage_frames_table")
    assert writers == {"core/landscape/data_flow/tokens.py"}, (
        f"token_lineage_frames must have exactly one writer module (spec §11 sole-write-path rule); found {sorted(writers)}"
    )
```

(Fix `hits` to a `set` from the start when writing the real file — shown as list/set mix here; the committed version uses a set throughout.)

- [ ] **Step 2: Run and mutation-check the guard** — `pytest tests/unit/architecture/test_lineage_retirement_guard.py -v` → PASS. Then mutation-check (the guard must be able to go red): temporarily add `branch_name: str | None = None` back to `TokenInfo` in the working tree → the first test must FAIL; temporarily add a `Column("fork_group_id", String(64))` to any table → the second must FAIL; temporarily add a `token_lineage_frames_table.insert()` call in another module → the fourth must FAIL. Revert all three probes (verify `git diff --stat` shows only the new test file).

- [ ] **Step 3: Commit**

```bash
git add tests/unit/architecture/test_lineage_retirement_guard.py
git commit -m "test(lineage): whole-tree retirement guard — retired names, column allowlist, sole write path"
```

---

### Task 14: WS1 CHECKPOINT — frozen-oracle diff, gates, and the STOP rule

**Files:**
- Create: `docs/superpowers/plans/2026-08-21-unified-lineage-ws1-checkpoint-record.md` (the checkpoint evidence record — a process document under the normal delivery posture, no sign-off ceremony)

**Interfaces:**
- Consumes: the protocols plan's frozen projection files + diff tool; the flip commit.

- [ ] **Step 1: Full suite, HEAD-stable.**

```bash
git rev-parse HEAD | tee /tmp/claude-ws1-head-before
pytest tests/ -n 12
git rev-parse HEAD | diff - /tmp/claude-ws1-head-before   # MUST be identical or the run is uninterpretable — re-run
```

Expected: PASS. Any failure: classify before touching anything — if the failing surface is NOT in the §4.1a enumerated deltas or the new-audit-rows class, this is a checkpoint failure (see Step 5).

- [ ] **Step 2: Frozen-oracle diff.** Run the protocols plan's compare gate over every corpus scenario against the stored pre-flip snapshots: `pytest tests/integration/core/dag/test_oracle_freeze.py -q` (compare mode — `ELSPETH_ORACLE_FREEZE` unset). Acceptance, verbatim from spec §11:
  - FROZEN fixtures: all four stable projection classes byte-identical. This includes the WS1a Task 8a nested scenarios (fork-in-fork depth-2, expand-in-fork), the r23 casualty `parallel-coalesces` (stays frozen THROUGH WS1, leaves the set only at WS2), and `fork-multiple-terminals-partial-failure` — pure fan-out, LEGAL under §7 rule 2 (2026-08-22 spec correction), permanently FROZEN, not a casualty.
  - REGENERATED: `row-union-interleave` only — whole-projection delta adjudicated in Task 12 step 3 (sink bytes and disposition outcomes/paths identical; branch_name None on released tokens; ordinal reshuffles enumerated in the rotation ledger).
  - The 56 golden JSONs (`tests/golden/`): `git diff --stat tests/golden/` must be EMPTY (no plugin schema changed in WS1).
  - Manifest sha churn attributable ONLY to the new record types / reshaped token records (D3) — spot-verify one FROZEN scenario's manifest diff and confirm every changed line is a sha/count over full-projection blobs, never a stable-projection field.

- [ ] **Step 3: Whole-tree gates.**

```bash
ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE=shape-only-when-key-missing \
  elspeth-lints check --rules all --root src/elspeth   # diff finding corpus vs the pre-Phase-A baseline: ADDED NOTHING
wardline scan . --fail-on ERROR --fail-on-inert --trust-pack scripts.wardline_pack --allow-custom-packs --local-only  # exit 0
```

Count findings, never tail them. The lints gate exits 1 with its standing corpus — the acceptance is corpus-delta zero (or negative), not exit 0.

- [ ] **Step 4: Write the checkpoint record** (`2026-08-21-unified-lineage-ws1-checkpoint-record.md`): flip commit sha, full-suite result + HEAD stability proof, the frozen-oracle diff summary (per-scenario verdict table), golden-JSON diff (empty), trust-tier corpus counts before/after, wardline exit code, and the §4.1a delta enumeration with pointers to `tests/unit/contracts/test_lineage_path_delta_table.py` (pure-path table) and `tests/unit/engine/test_token_lineage_path.py` (mint-integration, re-pointed at accessors). Commit it:

```bash
git add docs/superpowers/plans/2026-08-21-unified-lineage-ws1-checkpoint-record.md
git commit -m "docs(lineage): WS1 checkpoint record — frozen-oracle diff and gate evidence"
```

- [ ] **Step 5: THE STOP RULE (spec §11, verbatim obligation).** If the checkpoint cannot reach green-with-only-enumerated-deltas — any frozen projection moved, any routing/dispatch/disposition/sink-effect-identity decision changed outside the §4.1a table, any golden JSON churned, or the trust-tier corpus grew — **STOP. Do not press into WS3 on a red foundation. Surface to the maintainer** with: the exact diff, the fixture/site it appeared on, and the candidate §4.1a row it would need (there is none — the table is exhaustive; an unlisted delta is a defect or a spec gap, and only the maintainer rules which). Abandonment AFTER the flip is accepted residue per §11 (the flipped model is strictly more consistent than the two-truth model it replaced); reverting the flip commit is NOT the default remedy and is a maintainer decision.

---

## Self-review record

- **Spec coverage:** §4.1 field/accessor contract → Tasks 7/8; §4.1a delta table + decision pins → Tasks 2/4/12 (corpus differential via the oracle-freeze gate); §4.2 row_union pop → Task 10; §4.3 retirement matrix + codec purity + bidirectional check → Tasks 9/11; §4.4 replay predicates → Task 10; §11 landing shape (prep slices → one flip → cleanup), oracle diff, STOP rule → phase structure + Task 14; ruling 20/21 → Tasks 3/8/13; D1–D8 recorded above. Out of scope by assignment: settle-member/`GroupLossSpec`/`group_losses` writes (WS3), builder rules (WS2), collector executor (WS4), satisfiability gate (WS5), model core + tables + carriers + strict pop (WS1a Tasks 1–11 incl. 8a — consumed, never re-landed), nested-fixture authoring (WS1a Task 8a) and fixture freezing (protocols plan Tasks 1–3).
- **Cross-plan consistency (re-verified against the landed sibling drafts):** helpers are WS1a Task 1's `path_branch_name`/`path_fork_group_id`/`path_expand_group_id`/`pop_closer_frame` (this plan defines no siblings); the JSON codec is WS1a Task 1's `contracts.identity.lineage_path_to_json`/`lineage_path_from_json` (`ValueError` on corrupt input, wrapped to `AuditIntegrityError` at WS1a Task 5's journal mapping); the journal column is WS1a Task 4's `lineage_path_json` (NOT NULL, no server default — nothing to drop at epoch 35); the join carriers are WS1a Task 9's (this plan's Task 3 only verifies + shrinks the finalization member); the nested fixtures are WS1a Task 8a's; the oracle artifacts are protocols Tasks 1–3's `oracle_freeze.py` + `test_oracle_freeze.py` + `oracle_freeze/v1/` snapshots. Task 0's mechanical pre-flight re-runs this citation-vs-Produces diff before any implementation.
- **Type consistency:** `lineage_path: tuple[LineageFrame, ...]` everywhere in memory; `lineage_path_json: str` on the journal column/params; `lineage_path: list[list[str]]` on EXPORT records; `lineage_path: list[LineageFrameEntry]` (`{depth, kind, group_id, member_key}` dicts) on the MCP wire; `join_group_id: str | None` on `RowResult`/`PendingOutcome`/`TokenWorkItem`/audit `Token`/work-item column; `load_lineage_paths(run_id, token_ids) -> dict[str, tuple[LineageFrame, ...]]` used identically in Tasks 1, 4, 5, 6, 10, 11, 12.
- **Contested items — all RATIFIED by the 2026-08-22 synthesis, none remain open:** D1 (work-item join column KEPT — ruling 20; this plan's column-deletion slice allowlists `token_work_items.join_group_id`), D2 (`expected_branches_json` deleted, roster derives from child frames), and D8 (the ruling-27 journal-row release witness stands as planned).

## Formerly Open Questions — resolved by the 2026-08-22 synthesis (recorded, not re-decidable here)

1. **D1 ratification — RESOLVED (keep the column):** `token_work_items.join_group_id` is KEPT (ruling 20; codec purity + COALESCED redrive require durable join context). This plan's Task 9 deletes only `branch_name`/`fork_group_id`/`expand_group_id` from `token_work_items`, and the Task 13 guard allowlists the kept column.
2. **D8 witness ratification — RESOLVED (as planned):** the row_union release witness is the token's own row_union journal row having left BLOCKED (journal-first release). No dedicated durable release record; Task 10 step 5 implements exactly this.
3. **`fork-multiple-terminals-partial-failure` classification — RESOLVED (spec corrected):** its topology is pure fan-out, LEGAL under §7 rule 2; it stays permanently FROZEN. `parallel-coalesces` is the r23 casualty (frozen through WS1, leaves the set at WS2). Task 14 step 2's acceptance list reflects this.

The one campaign-level question still open (maintainer pedagogy call, owned outside this plan): the `examples/row_union_ab_experiment/settings_screened.yaml` replacement story.
