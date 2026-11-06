# Composer Continuation Storage Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Retain one Composer request through segment handoffs and process loss with exact private state, fenced mutations and cumulative physical accounting.

**Architecture:** Extend the existing session SQL parent with a versioned lifecycle, immutable complete snapshots and a durable segment queue. SQL transactions retain events, owner receipts, checkpoint pointers and generation changes together; private state is never exposed in observation DTOs. Existing quota/audit owners remain authoritative for every physical provider attempt.

**Tech Stack:** Python 3.12+, SQLAlchemy Core, SQLite/PostgreSQL, frozen dataclasses/Pydantic 2, asyncio, pytest.

---

**Current status:** the immutable source below is a historical extraction
anchor. The main plan's current combined source and qualification are
authoritative for implementation. The coordinator has qualified exact
`2e869240a388d1ede0454f1fbb9b019d22a9a9da` locally and created the isolated
integration target; that qualification does not implement this storage packet.
The source and epoch proposal below remain subject to P0 reconciliation.
Companion assembly and the requested final Astra GO are still distinct from
the already completed donor qualification.

This companion supplies C2, C3, C1b, C4a, C5 and C4b of [the main plan](2026-10-20-composer-continuation.md). The client companion owns C7/C8/S7. These are authoring instructions; no code, test, import, migration, service or provider call has been executed by writing this plan.

Implementation reconciliation, 22 October 2026: the pure frame/frontier and C2
prerequisites have independent specification and Astra source GO in the current
integration branch. Later C3 runtime owners remain incomplete. Apply these
measured corrections when executing the literal packet below:

- The new V2 parent and segment-fence CHECK expressions require explicit
  `IS NOT NULL` for required phase, ingress, committed user-message and epoch fields;
  SQLite/PostgreSQL CHECK UNKNOWN does not reject a missing value. Keep the
  existing V1 status bundles and transition protections intact.
- Use SQL numeric defaults `text("0")` for the two new bigint counters. A
  quoted string default reflects differently on PostgreSQL and fails the
  existing schema validator; do not weaken that validator.
- Preserve the original V1 running-parent attribution trigger on terminal
  settlement for every protected field except the claim token. Only the
  claim-token equality rule is limited to running-to-running transitions,
  permitting legitimate terminal clearing. The other six original fields
  remain protected whenever the old row is running. Test all six attempted
  terminal substitutions and legitimate settlement on both databases.
- C3.0/C3.8 must move the **current** complete state decoder, including the
  carried `read_content_publication` validation. The older extraction body
  alone would lose that guard. Keep the service method as a delegating wrapper.

These are source corrections backed by focused native tests and independent
review, not evidence that V2 admission, workers, recovery, current-use authority,
PostgreSQL contention or the combined release has been qualified. No live store
migration, reset or downgrade is authorized by the schema epoch proposal.

Historical immutable extraction: `b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree `6a34e912debc469e9ce7e2048d433c08d5e606a9`, parent `ce4103539fe97f07fc60fb0cd096256c207c8bc5`, version 0.8.9. Preserve its Advisor rejection error-class retention, exact boolean Azure character-limit policy/fingerprint (disabled only for explicitly unconfigured debug mode), and actual safe request-rejection tool feedback. Later test-only `.9` checkpoint `2e869240a388d1ede0454f1fbb9b019d22a9a9da`, tree `a44b6334aa674c5868ef79b4c2035b5563e47efe`, changes only `tests/unit/web/sessions/test_prompt_guard_capacity_errors.py` and has since qualified locally. The main plan records the current combined target and qualification limits. Preserve `SessionServiceImpl._assert_graph_review_receipt`'s `AdvisorCheckpointPassRecord.from_dict` decoder, guidance `c299f2e7d852f897d0e34a1178aed4aa732cbd41`, and the included `.7` `0d216f` behavior. Main S7.2 governs the separate actual net `2d5c` to `e6b2477` seven-path carry. Preserve `_bind_attempt_call`'s database admission/monotonic elapsed binding and current storage/quota custody. Both epoch constants are 78 in this extraction; this packet proposes both become 79, with P0 refreshing both exact values if the qualified target has advanced. This document grants no test-capacity or runtime ownership.

All commands use ordinary repository workflows after P0. There is no bespoke controller, new wrapper, profile, marker or proof-cycle tool in this plan. Each commit step includes normal status/staged-path/preflight discipline; no commit or hook is run during planning. A native exit 0 is GREEN. Entirely new modules receive the complete importable code packet before their behavioral RED mutation; import/collection errors never count as RED.

## Core file map

| Path | Responsibility |
| --- | --- |
| `src/elspeth/web/sessions/models.py` | Versioned parent shape and seven private continuation child tables; symmetric SQLite/PostgreSQL checks, indexes, FKs and transition triggers. |
| `src/elspeth/web/sessions/schema.py` | Current epoch and exact metadata admission, preserving incompatible stores unchanged. |
| `src/elspeth/web/sessions/continuation.py` | Private exact chunks, complete snapshots, cursor CAS, segment/effect/provider joins on a caller-owned SQL transaction. |
| `src/elspeth/web/coordination/composer_operation_authority.py` | Parent admission, segment claims, yield/takeover transactions, atomic authorized public projections. |
| `src/elspeth/web/coordination/repository.py` | Live session fence acquisition and mutation-facade integration. |
| `src/elspeth/web/sessions/composer_async_worker.py` | Scan/claim/resume and verified durable-yield release. |
| `src/elspeth/web/composer/continuation_clock.py` | Independent bounded action stages and pure segment-fit decisions. |
| `src/elspeth/web/composer/provider_quota.py` | Durable ordinal and actual provider admission, pre-SDK marker and actual settlement custody. |
| `src/elspeth/web/sessions/service.py` | Existing ingress/SQL/provider/archive owners gain same-transaction continuation hooks. |

New continuation SQL helpers never own an engine, open a transaction, call a provider, acquire a live lease or serialize SDK objects. Their caller already holds `locked_session_transaction` or the existing session mutation transaction. This prevents separate event and mutation commits.

### C1a.0: Define the complete retained policy before any frame codec

**Create:** `src/elspeth/web/composer/continuation_policy.py`, `tests/helpers/continuation_policy.py`, `tests/unit/web/composer/test_continuation_policy.py`.

This pure prerequisite executes before the main frame models/codec and before C2. C5's runtime clock imports these nominal models; it does not redefine them. Current security/capability validation remains fresh at every actual action, while clock allowances are the request's retained admitted policy.

- [ ] Create this complete pure policy module. It imports no session storage, runtime provider, app or settings object. `from_settings` is added in C5 only after the settings fields exist.

```python
from __future__ import annotations

import math
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json, stable_hash
from elspeth.contracts.json_parser import parse_json_strict

PositiveSeconds = Annotated[float, Field(strict=True, gt=0, allow_inf_nan=False)]


class ContinuationActionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    preparation: PositiveSeconds
    guard: PositiveSeconds | None
    quota: PositiveSeconds | None
    physical: PositiveSeconds
    retention: PositiveSeconds

    @field_validator("preparation", "guard", "quota", "physical", "retention", mode="before")
    @classmethod
    def _numeric(cls, value: object) -> object:
        if type(value) is bool:
            raise ValueError("stage bound cannot be boolean")
        return value

    @property
    def total(self) -> float:
        return self.preparation + (self.guard or 0.0) + (self.quota or 0.0) + self.physical + self.retention

    @model_validator(mode="after")
    def _finite_total(self) -> Self:
        if not math.isfinite(self.total):
            raise ValueError("total action allowance must be finite")
        return self


class ContinuationSegmentPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    nominal: PositiveSeconds
    watermark: PositiveSeconds
    quantum: PositiveSeconds
    reserve: PositiveSeconds

    @model_validator(mode="after")
    def _watermark(self) -> Self:
        if self.watermark >= self.nominal:
            raise ValueError("watermark must be smaller than nominal segment")
        return self


class ContinuationActionBounds(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    primary: ContinuationActionPolicy
    advisor: ContinuationActionPolicy
    title: ContinuationActionPolicy
    read: ContinuationActionPolicy
    session_sql: ContinuationActionPolicy
    blob: ContinuationActionPolicy
    external: ContinuationActionPolicy


class ContinuationAdmissionPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    segment: ContinuationSegmentPolicy
    actions: ContinuationActionBounds

    @model_validator(mode="after")
    def _finite_segment_sizing(self) -> Self:
        for action in (
            self.actions.primary, self.actions.advisor, self.actions.title, self.actions.read,
            self.actions.session_sql, self.actions.blob, self.actions.external,
        ):
            if not math.isfinite(action.total + self.segment.reserve + self.segment.watermark):
                raise ValueError("complete segment sizing allowance must be finite")
        return self

    @property
    def policy_hash(self) -> str:
        return stable_hash(self.model_dump(mode="json"))


def decode_continuation_policy(encoded: str, expected_hash: str) -> ContinuationAdmissionPolicy:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("continuation policy is not strict JSON")
    try:
        policy = ContinuationAdmissionPolicy.model_validate_json(canonical_json(value))
    except ValueError as exc:
        raise AuditIntegrityError("continuation policy violates its complete owned contract") from exc
    if policy.policy_hash != expected_hash:
        raise AuditIntegrityError("continuation policy hash mismatch")
    return policy
```

- [ ] Add this complete ordinary test fixture helper. Use it in all frame, seed, cold-restore and client-fixture tests needing an enabled valid policy; calculate its actual hash rather than passing an opaque `"p"` or a fake digest.

```python
from elspeth.web.composer.continuation_policy import (
    ContinuationActionBounds, ContinuationActionPolicy,
    ContinuationAdmissionPolicy, ContinuationSegmentPolicy,
)


def minimal_continuation_policy() -> ContinuationAdmissionPolicy:
    provider = ContinuationActionPolicy(preparation=1.0, guard=5.0, quota=5.0, physical=120.0, retention=5.0)
    local = ContinuationActionPolicy(preparation=1.0, guard=None, quota=None, physical=5.0, retention=1.0)
    return ContinuationAdmissionPolicy(
        segment=ContinuationSegmentPolicy(nominal=300.0, watermark=120.0, quantum=180.0, reserve=25.0),
        actions=ContinuationActionBounds(primary=provider, advisor=provider, title=provider,
                                         read=local, session_sql=local, blob=local, external=local),
    )
```

- [ ] Add these actual behavioral assertions. On new-module setup first make the complete module importable; then establish RED by temporarily removing its hash comparison, run only `test_changed_bound_is_refused_under_original_policy_hash`, and require its `pytest.raises` assertion to fail. Restore the comparison and run all policy tests with native exit 0.

```python
import json

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from tests.helpers.continuation_policy import minimal_continuation_policy


def test_complete_policy_roundtrips_for_flag_off_draining():
    policy = minimal_continuation_policy()
    assert decode_continuation_policy(policy.model_dump_json(), policy.policy_hash) == policy


def test_changed_bound_is_refused_under_original_policy_hash():
    policy = minimal_continuation_policy()
    changed = json.loads(policy.model_dump_json())
    changed["actions"]["primary"]["physical"] = 500.0
    with pytest.raises(AuditIntegrityError, match="hash mismatch"):
        decode_continuation_policy(json.dumps(changed), policy.policy_hash)


def test_missing_role_or_boolean_stage_is_refused():
    policy = minimal_continuation_policy()
    missing = json.loads(policy.model_dump_json())
    del missing["actions"]["external"]
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(json.dumps(missing), policy.policy_hash)
    boolean = json.loads(policy.model_dump_json())
    boolean["actions"]["primary"]["physical"] = True
    with pytest.raises(AuditIntegrityError, match="complete owned contract"):
        decode_continuation_policy(json.dumps(boolean), policy.policy_hash)
```

Ordinary command after execution admission:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_policy.py >"$COMPOSER_LOG_DIR/c1a-policy.log" 2>&1
COMPOSER_CHECK_EXIT=$?
printf 'exit=%s\n' "$COMPOSER_CHECK_EXIT"
cat "$COMPOSER_LOG_DIR/c1a-policy.log"
test "$COMPOSER_CHECK_EXIT" -eq 0
```

- [ ] Commit these three explicit paths as `feat(composer): define complete retained continuation policy` using normal repository hooks after GREEN. The main frame codec then validates `admission_policy_json` and `policy_hash` through `decode_continuation_policy` on every decode, including cold restart and flag-off draining.

### C2.0: Share the exact closed event vocabulary

**Create:** `src/elspeth/web/sessions/continuation_events.py`. This pure prerequisite precedes C2 metadata and C3's nominal event. It imports neither models, codecs, service nor authority. The actual `checkpoint` event emitted by the phase/batch/worker owners is part of this same vocabulary.

- [ ] Add this complete pure module. SQL metadata and the nominal event consume the same tuple; do not copy a second handwritten SQL list.

```python
from typing import Final, Literal, get_args


ContinuationEventTag = Literal[
    "ingress", "checkpoint", "input_prepared", "provider_admitted", "may_dispatch", "response_retained",
    "tool_prepared", "effect_committed", "effect_refused", "effect_unknown", "review_retained",
    "yielded", "recovered", "settled",
]
CONTINUATION_EVENT_TAGS: Final[tuple[ContinuationEventTag, ...]] = get_args(ContinuationEventTag)
```

The exact models import is:

```python
from elspeth.web.sessions.continuation_events import CONTINUATION_EVENT_TAGS
```

The exact C3 `sessions/continuation.py` import is:

```python
from elspeth.web.sessions.continuation_events import CONTINUATION_EVENT_TAGS, ContinuationEventTag
```

- [ ] After C3's real store fixture and complete snapshot owner exist, add this full test to `tests/unit/web/sessions/test_continuation_cold_restore.py` and import it into the existing real PostgreSQL continuation module with its PostgreSQL `operation_store`. It proves actual SQL acceptance for every nominal tag and raw-SQL rejection for an unfamiliar tag. A green metadata-only reflection test is not this transaction proof.

```python
def test_nominal_event_vocabulary_and_actual_sql_check_are_identical(operation_store):
    from typing import cast
    from sqlalchemy import insert, select
    from sqlalchemy.exc import IntegrityError
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import ContinuationEvent, append_complete_snapshot, load_snapshot
    from elspeth.web.sessions.continuation_events import CONTINUATION_EVENT_TAGS, ContinuationEventTag
    from elspeth.web.sessions.models import composer_continuation_events_table
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    try:
        assert "checkpoint" in CONTINUATION_EVENT_TAGS and len(set(CONTINUATION_EVENT_TAGS)) == len(CONTINUATION_EVENT_TAGS)
        with engine.begin() as conn:
            stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
            initial_cursor = stored.cursor
            for tag in CONTINUATION_EVENT_TAGS:
                stored = append_complete_snapshot(conn, session_id=str(sid), operation_id=record.operation_id,
                    expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
                    snapshot_json=stored.payload_json, event_batch=(ContinuationEvent(tag, '{}'),), handoff=False)
            rows = conn.execute(select(composer_continuation_events_table).where(
                composer_continuation_events_table.c.session_id == str(sid),
                composer_continuation_events_table.c.operation_id == record.operation_id,
                composer_continuation_events_table.c.sequence > initial_cursor,
            ).order_by(composer_continuation_events_table.c.sequence)).all()
            assert tuple(row.tag for row in rows) == CONTINUATION_EVENT_TAGS
        with pytest.raises(AuditIntegrityError, match="unfamiliar owned tag"):
            ContinuationEvent(cast(ContinuationEventTag, "future_unowned_event"), '{}')
        forged = dict(rows[-1]._mapping)
        forged.update(sequence=stored.cursor + 1, tag="future_unowned_event")
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(insert(composer_continuation_events_table).values(**forged))
        with engine.connect() as conn:
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == stored
            actual = conn.execute(select(composer_continuation_events_table).where(
                composer_continuation_events_table.c.session_id == str(sid),
                composer_continuation_events_table.c.operation_id == record.operation_id,
                composer_continuation_events_table.c.sequence > initial_cursor,
            ).order_by(composer_continuation_events_table.c.sequence)).all()
            assert actual == rows
    finally:
        repository.release(running.session_operation_context)
```

The intentionally invalid constructor is a boundary negative. The test's explicit `cast` supplies only the deliberate wrong input to the runtime guard; it grants no production authority and adds no lint suppression. Parse foreign event tags through `TypeAdapter(ContinuationEventTag)` at their boundary rather than widening this owned field to `str`. Behavioral RED: temporarily omit only `checkpoint` from the SQL CHECK construction while leaving the nominal tuple intact; the actual `append_complete_snapshot` loop must fail. Restore the single shared CHECK and require the cold-restore/native PostgreSQL commands to finish with exit 0.

### C2.1: Add private child-table metadata

**Files:** Modify `src/elspeth/web/sessions/models.py:3564`, `src/elspeth/web/sessions/schema.py:541`; create `tests/unit/web/sessions/test_continuation_schema.py`.

- [ ] **Step 1: Add the exact metadata test.**

```python
from sqlalchemy import inspect

from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema


def test_continuation_children_bind_every_parent_key(tmp_path):
    engine = create_session_engine(f"sqlite:///{tmp_path / 'continuation.db'}")
    try:
        initialize_session_schema(engine)
        inspector = inspect(engine)
        expected = {
            "composer_operation_segments",
            "composer_segment_attempts",
            "composer_continuation_chunks",
            "composer_continuation_events",
            "composer_continuation_checkpoints",
            "composer_continuation_effects",
            "composer_continuation_provider_ordinals",
        }
        assert expected <= set(inspector.get_table_names())
        for name in expected:
            parent_fks = [
                fk for fk in inspector.get_foreign_keys(name)
                if fk["referred_table"] == "composer_async_operations"
            ]
            assert len(parent_fks) == 1
            assert parent_fks[0]["constrained_columns"] == ["session_id", "operation_id"]
            assert parent_fks[0]["referred_columns"] == ["session_id", "operation_id"]
    finally:
        engine.dispose()
```

- [ ] **Step 2: Run the focused RED.**

Run the following against the baseline test before adding tables. Expected native exit 1 from its missing-table assertion; a collection error is setup failure.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_schema.py >"$COMPOSER_LOG_DIR/c2-child-schema-red.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c2-child-schema-red.log"
test "$composer_exit" -eq 1
```

- [ ] **Step 3: Add child tables directly after parent table/index definition and before ingress-receipt FKs.** All bytes below are new; these tables use only existing imported SQLAlchemy symbols. Add `sha256`-column constraints using `_lower_sha256_constraints` as shown. `checkpoint_id` in the parent and segment is an owner-checked pointer, deliberately without a cyclic FK; every snapshot insert and pointer change share one transaction. Events refer to chunks through composite keys.

```python
def _continuation_parent_fk(name: str) -> ForeignKeyConstraint:
    return ForeignKeyConstraint(
        ["session_id", "operation_id"],
        ["composer_async_operations.session_id", "composer_async_operations.operation_id"],
        name=name,
        ondelete="CASCADE",
    )


composer_operation_segments_table = Table(
    "composer_operation_segments", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("predecessor_sequence", Integer),
    Column("state", String(16), nullable=False),
    Column("generation", Integer, nullable=False),
    Column("claim_token", String(256)),
    Column("claim_owner_instance_id", String(128)),
    Column("claim_expires_at", DateTime(timezone=True)),
    Column("session_operation_id", String(128)),
    Column("session_operation_lease_token", String(256)),
    Column("session_operation_epoch", Integer),
    Column("enqueued_at", DateTime(timezone=True), nullable=False),
    Column("next_eligible_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True)),
    Column("cooperative_at", DateTime(timezone=True)),
    Column("deadline_at", DateTime(timezone=True)),
    Column("checkpoint_id", String(36), nullable=False),
    Column("next_action_json", Text, nullable=False),
    Column("policy_sha256", String(64), nullable=False),
    Column("rollover_reason", String(32)),
    _continuation_parent_fk("fk_composer_segments_parent"),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "predecessor_sequence"],
        ["composer_operation_segments.session_id", "composer_operation_segments.operation_id", "composer_operation_segments.sequence"],
        name="fk_composer_segments_predecessor", ondelete="CASCADE",
    ),
    UniqueConstraint("session_id", "operation_id", "predecessor_sequence", name="uq_composer_segments_successor"),
    CheckConstraint("sequence > 0 AND generation > 0", name="ck_composer_segments_identity"),
    CheckConstraint("predecessor_sequence IS NULL OR predecessor_sequence = sequence - 1", name="ck_composer_segments_predecessor"),
    CheckConstraint("state IN ('queued', 'running', 'yielded', 'interrupted', 'settled')", name="ck_composer_segments_state"),
    CheckConstraint(
        "(state = 'queued' AND started_at IS NULL AND deadline_at IS NULL AND cooperative_at IS NULL) OR "
        "(state <> 'queued' AND started_at IS NOT NULL AND deadline_at IS NOT NULL AND cooperative_at IS NOT NULL AND deadline_at > started_at AND cooperative_at >= started_at AND cooperative_at < deadline_at)",
        name="ck_composer_segments_times",
    ),
    CheckConstraint(
        "(claim_token IS NULL AND claim_owner_instance_id IS NULL AND claim_expires_at IS NULL) OR "
        "(claim_token IS NOT NULL AND claim_owner_instance_id IS NOT NULL AND claim_expires_at IS NOT NULL)",
        name="ck_composer_segments_claim",
    ),
    CheckConstraint(
        "(session_operation_id IS NULL AND session_operation_lease_token IS NULL AND session_operation_epoch IS NULL) OR "
        "(session_operation_id IS NOT NULL AND session_operation_lease_token IS NOT NULL AND session_operation_epoch IS NOT NULL AND session_operation_epoch > 0)",
        name="ck_composer_segments_fence",
    ),
    *_lower_sha256_constraints("policy_sha256", name="ck_composer_segments_policy_sha256"),
)
Index(
    "ix_composer_segments_eligible",
    composer_operation_segments_table.c.next_eligible_at,
    composer_operation_segments_table.c.enqueued_at,
    composer_operation_segments_table.c.session_id,
    composer_operation_segments_table.c.operation_id,
    composer_operation_segments_table.c.sequence,
    sqlite_where=composer_operation_segments_table.c.state.in_(("queued", "interrupted")),
    postgresql_where=composer_operation_segments_table.c.state.in_(("queued", "interrupted")),
)

composer_segment_attempts_table = Table(
    "composer_segment_attempts", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("sequence", Integer, primary_key=True),
    Column("generation", Integer, primary_key=True),
    Column("claim_attempt", Integer, nullable=False),
    Column("session_operation_id", String(128), nullable=False),
    Column("session_operation_epoch", Integer, nullable=False),
    Column("session_operation_lease_token", String(256), nullable=False),
    Column("owner_instance_id", String(128), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=False),
    Column("claim_expires_at", DateTime(timezone=True), nullable=False),
    Column("revoked_at", DateTime(timezone=True)),
    Column("outcome", String(16)),
    _continuation_parent_fk("fk_composer_attempts_parent"),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "sequence"],
        ["composer_operation_segments.session_id", "composer_operation_segments.operation_id", "composer_operation_segments.sequence"],
        name="fk_composer_attempts_segment", ondelete="CASCADE",
    ),
    UniqueConstraint("session_id", "session_operation_epoch", name="uq_composer_attempts_epoch"),
    UniqueConstraint("session_id", "session_operation_id", name="uq_composer_attempts_fence"),
    CheckConstraint("sequence > 0 AND generation > 0 AND session_operation_epoch > 0 AND claim_attempt > 0", name="ck_composer_attempts_identity"),
    CheckConstraint("outcome IS NULL OR outcome IN ('yielded', 'interrupted', 'settled')", name="ck_composer_attempts_outcome"),
    CheckConstraint("(revoked_at IS NULL) = (outcome IS NULL)", name="ck_composer_attempts_revocation"),
)

composer_continuation_chunks_table = Table(
    "composer_continuation_chunks", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("chunk_key", String(128), primary_key=True),
    Column("codec", String(64), nullable=False),
    Column("payload", LargeBinary, nullable=False),
    Column("sha256", String(64), nullable=False),
    Column("byte_length", BigInteger, nullable=False),
    _continuation_parent_fk("fk_composer_chunks_parent"),
    CheckConstraint("byte_length >= 0", name="ck_composer_chunks_byte_length"),
    CheckConstraint("length(payload) = byte_length", name="ck_composer_chunks_exact_bytes").ddl_if(dialect="sqlite"),
    CheckConstraint("octet_length(payload) = byte_length", name="ck_composer_chunks_exact_bytes").ddl_if(dialect="postgresql"),
    *_lower_sha256_constraints("sha256", name="ck_composer_chunks_sha256"),
)

composer_continuation_checkpoints_table = Table(
    "composer_continuation_checkpoints", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("checkpoint_id", String(36), primary_key=True),
    Column("kind", String(16), nullable=False),
    Column("cursor", BigInteger, nullable=False),
    Column("snapshot_chunk_key", String(128), nullable=False),
    Column("frame_stack_version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    _continuation_parent_fk("fk_composer_checkpoints_parent"),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "snapshot_chunk_key"],
        ["composer_continuation_chunks.session_id", "composer_continuation_chunks.operation_id", "composer_continuation_chunks.chunk_key"],
        name="fk_composer_checkpoints_chunk", ondelete="CASCADE",
    ),
    CheckConstraint("kind IN ('transition', 'handoff')", name="ck_composer_checkpoints_kind"),
    CheckConstraint("cursor >= 0 AND frame_stack_version = 1", name="ck_composer_checkpoints_version"),
    UniqueConstraint("session_id", "operation_id", "cursor", name="uq_composer_checkpoints_cursor"),
)

composer_continuation_events_table = Table(
    "composer_continuation_events", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("sequence", BigInteger, primary_key=True),
    Column("tag", String(32), nullable=False),
    Column("payload_chunk_key", String(128), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    _continuation_parent_fk("fk_composer_events_parent"),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "payload_chunk_key"],
        ["composer_continuation_chunks.session_id", "composer_continuation_chunks.operation_id", "composer_continuation_chunks.chunk_key"],
        name="fk_composer_events_chunk", ondelete="CASCADE",
    ),
    CheckConstraint("sequence > 0", name="ck_composer_events_sequence"),
    CheckConstraint(
        "tag IN (" + ", ".join(repr(tag) for tag in CONTINUATION_EVENT_TAGS) + ")",
        name="ck_composer_events_tag",
    ),
)

composer_continuation_effects_table = Table(
    "composer_continuation_effects", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("effect_key", String(128), primary_key=True),
    Column("input_sha256", String(64), nullable=False),
    Column("owner", String(32), nullable=False),
    Column("action_json", Text),
    Column("disposition", String(16), nullable=False),
    Column("receipt_key", String(256)),
    Column("result_chunk_key", String(128)),
    Column("provider_attempt_id", String),
    Column("physical_sequence", Integer),
    Column("physical_generation", Integer),
    Column("requested_model", String(1024)),
    Column("response_finished_at", DateTime(timezone=True)),
    Column("creator_sequence", Integer, nullable=False),
    Column("creator_generation", Integer, nullable=False),
    Column("committed_state_ids_json", Text),
    Column("graph_sha256", String(64)),
    _continuation_parent_fk("fk_composer_effects_parent"),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "creator_sequence", "creator_generation"],
        ["composer_segment_attempts.session_id", "composer_segment_attempts.operation_id", "composer_segment_attempts.sequence", "composer_segment_attempts.generation"],
        name="fk_composer_effects_creator", ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "physical_sequence", "physical_generation"],
        ["composer_segment_attempts.session_id", "composer_segment_attempts.operation_id", "composer_segment_attempts.sequence", "composer_segment_attempts.generation"],
        name="fk_composer_effects_physical_creator", ondelete="CASCADE",
    ),
    CheckConstraint(
        "(provider_attempt_id IS NULL AND physical_sequence IS NULL AND physical_generation IS NULL AND requested_model IS NULL) OR "
        "(provider_attempt_id IS NOT NULL AND physical_sequence IS NOT NULL AND physical_generation IS NOT NULL AND physical_sequence > 0 AND physical_generation > 0 AND requested_model IS NOT NULL)",
        name="ck_composer_effects_physical_bundle",
    ),
    ForeignKeyConstraint(
        ["session_id", "operation_id", "result_chunk_key"],
        ["composer_continuation_chunks.session_id", "composer_continuation_chunks.operation_id", "composer_continuation_chunks.chunk_key"],
        name="fk_composer_effects_result", ondelete="CASCADE",
    ),
    ForeignKeyConstraint(["provider_attempt_id"], ["quota_provider_attempts.attempt_id"], name="fk_composer_effects_attempt", ondelete="RESTRICT"),
    CheckConstraint("disposition IN ('prepared', 'dispatched', 'committed', 'refused', 'unknown')", name="ck_composer_effects_disposition"),
    CheckConstraint("disposition NOT IN ('committed', 'refused') OR (receipt_key IS NOT NULL AND result_chunk_key IS NOT NULL)", name="ck_composer_effects_receipt"),
    *_lower_sha256_constraints("input_sha256", name="ck_composer_effects_input_sha256"),
    *_lower_sha256_constraints("graph_sha256", name="ck_composer_effects_graph_sha256", nullable=True),
)

composer_continuation_provider_ordinals_table = Table(
    "composer_continuation_provider_ordinals", metadata,
    Column("session_id", String(128), primary_key=True),
    Column("operation_id", String(36), primary_key=True),
    Column("family", Integer, primary_key=True),
    Column("next_ordinal", BigInteger, nullable=False),
    _continuation_parent_fk("fk_composer_ordinals_parent"),
    CheckConstraint("family IN (0, 1, 2, 3) AND next_ordinal >= 0", name="ck_composer_ordinals_range"),
)
```

- [ ] **Step 4: Run GREEN:** Require native exit 0. Every metadata inventory and exact-schema test must include these actual tables, not a filtered source count.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_schema.py tests/unit/web/sessions/test_schema.py >"$COMPOSER_LOG_DIR/c2-child-schema-green.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c2-child-schema-green.log"
test "$composer_exit" -eq 0
```
- [ ] **Step 5: Commit the schema unit only after C2.2 closes the parent CHECK/trigger contract; it is one schema epoch change.**

### C2.2: Make parent lifecycle checks and trigger ownership explicit

**Files:** Modify `src/elspeth/web/sessions/models.py:3564-3743`; modify `src/elspeth/web/sessions/schema.py:541-549`; extend `tests/unit/web/sessions/test_continuation_schema.py` and existing preserve/refuse tests in `tests/unit/web/sessions/test_schema.py`.

- [ ] **Step 1: Add this metadata assertion to the schema test file.**

```python
def test_v2_parent_owns_no_total_deadline_and_has_resume_pointer():
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    assert jobs.c.deadline_at.nullable
    assert {
        "lifecycle_version", "phase", "status_revision", "current_segment_sequence",
        "ownership_generation", "checkpoint_id", "journal_cursor", "ingress_phase",
        "reserved_user_message_id",
    } <= set(jobs.c.keys())
    assert jobs.c.lifecycle_version.server_default.arg == "1"
```

- [ ] **Step 2: RED:** Run the command below on the baseline parent metadata. Require native exit 1 from the assertion about the non-null parent deadline.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_schema.py >"$COMPOSER_LOG_DIR/c2-parent-red.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c2-parent-red.log"
test "$composer_exit" -eq 1
```
- [ ] **Step 3: Add these columns immediately after parent `status`; change the existing `deadline_at` column to `nullable=True`.**

```python
Column("lifecycle_version", Integer, nullable=False, server_default="1"),
Column("phase", String(24)),
Column("status_revision", BigInteger, nullable=False, server_default=text("0")),
Column("current_segment_sequence", Integer),
Column("ownership_generation", Integer),
Column("checkpoint_id", String(36)),
Column("journal_cursor", BigInteger, nullable=False, server_default=text("0")),
Column("ingress_phase", String(16)),
Column("reserved_user_message_id", String(128)),
```

Replace only `deadline_at > created_at` inside `ck_composer_operations_times` with `(deadline_at IS NULL OR deadline_at > created_at)`. Move the exact existing `ck_composer_operations_status_bundle` SQL string unchanged into `_COMPOSER_V1_STATUS_BUNDLE`; use it in this replacement. This is a source relocation: preserve the existing literal byte for byte, including every terminal integrity constraint.

```python
_COMPOSER_V2_STATUS_BUNDLE = (
    "lifecycle_version = 2 AND deadline_at IS NULL AND current_segment_sequence IS NOT NULL AND current_segment_sequence > 0 AND "
    "ownership_generation IS NOT NULL AND ownership_generation > 0 AND "
    "checkpoint_id IS NOT NULL AND reserved_user_message_id IS NOT NULL AND journal_cursor >= 0 AND status_revision >= 0 AND "
    "phase IS NOT NULL AND ingress_phase IS NOT NULL AND ingress_phase IN ('pending', 'committed') AND "
    "((ingress_phase = 'pending' AND user_message_id IS NULL) OR "
    "(ingress_phase = 'committed' AND user_message_id IS NOT NULL AND user_message_id = reserved_user_message_id)) AND "
    "((status IN ('queued', 'running') AND phase IN ('queued', 'executing', 'continuation_queued', 'reconciling', 'retry_waiting') AND "
    "request_json IS NOT NULL AND result_schema IS NULL AND result_json IS NULL AND result_sha256 IS NULL AND "
    "failure_code IS NULL AND settled_by IS NULL AND settled_at IS NULL AND claim_token IS NULL AND claim_expires_at IS NULL) OR "
    "(status = 'completed' AND phase = 'settled' AND request_json IS NULL AND claim_token IS NULL AND claim_expires_at IS NULL AND "
    "cancel_requested_at IS NULL AND failure_code IS NULL AND result_schema = 'message_with_state.v1' AND result_json IS NOT NULL AND "
    "result_sha256 IS NOT NULL AND settled_at IS NOT NULL AND settled_by IS NOT NULL AND user_message_id IS NOT NULL) OR "
    "(status = 'failed' AND phase = 'settled' AND request_json IS NULL AND claim_token IS NULL AND claim_expires_at IS NULL AND "
    "failure_code IS NOT NULL AND result_schema = 'composer_operation_error.v1' AND result_json IS NOT NULL AND "
    "result_sha256 IS NOT NULL AND settled_at IS NOT NULL AND settled_by IS NOT NULL))"
)
composer_async_operations_table.append_constraint(CheckConstraint(
    "(lifecycle_version = 1 AND deadline_at IS NOT NULL AND phase IS NULL AND current_segment_sequence IS NULL AND "
    "ownership_generation IS NULL AND checkpoint_id IS NULL AND reserved_user_message_id IS NULL AND ingress_phase IS NULL AND ("
    + _COMPOSER_V1_STATUS_BUNDLE + ")) OR (" + _COMPOSER_V2_STATUS_BUNDLE + ")",
    name="ck_composer_operations_status_bundle",
))
```

Keep terminal rows immutable and keep the existing queued-to-completed refusal. Replace the complete SQLite transition-guard statement and PostgreSQL transition-guard function with generated strings below. Preserve the separate existing no-delete-live triggers unchanged. This function replaces duplicated long transition condition literals; it does not alter unrelated audit DDL.

```python
def _composer_operation_transition_condition(*, dialect: Literal["sqlite", "postgresql"]) -> str:
    different = "IS NOT" if dialect == "sqlite" else "IS DISTINCT FROM"
    immutable = (
        "session_id", "operation_id", "kind", "request_hash", "actor_user_id", "request_id",
        "base_state_id", "created_at", "deadline_at", "lifecycle_version", "reserved_user_message_id",
    )
    clauses = [f"NEW.{name} {different} OLD.{name}" for name in immutable]
    clauses.extend((
        "OLD.status IN ('completed', 'failed')",
        "(OLD.status = 'running' AND NEW.status = 'queued')",
        "(OLD.status = 'queued' AND NEW.status = 'completed')",
        f"(NEW.status IN ('queued', 'running') AND NEW.request_json {different} OLD.request_json)",
        f"(OLD.cancel_requested_at IS NOT NULL AND NEW.cancel_requested_at {different} OLD.cancel_requested_at)",
        f"(OLD.user_message_id IS NOT NULL AND NEW.user_message_id {different} OLD.user_message_id)",
        "(NEW.lifecycle_version = 2 AND NEW.status_revision < OLD.status_revision)",
        "(NEW.lifecycle_version = 2 AND NEW.journal_cursor < OLD.journal_cursor)",
    ))
    legacy_owner = (
        "claim_owner_instance_id", "session_operation_id", "session_operation_lease_token",
        "session_operation_epoch", "started_at", "attempt",
    )
    clauses.append(
        f"(OLD.lifecycle_version = 1 AND OLD.status = 'running' AND NEW.status = 'running' AND NEW.claim_token {different} OLD.claim_token)"
    )
    clauses.append(
        "(OLD.lifecycle_version = 1 AND OLD.status = 'running' AND ("
        + " OR ".join(f"NEW.{name} {different} OLD.{name}" for name in legacy_owner) + "))"
    )
    return " OR ".join(clauses)


_SQLITE_COMPOSER_TRANSITION_DDL = (
    "CREATE TRIGGER trg_composer_async_operations_transition_guard BEFORE UPDATE ON composer_async_operations "
    "FOR EACH ROW WHEN " + _composer_operation_transition_condition(dialect="sqlite")
    + " BEGIN SELECT RAISE(ABORT, 'composer operation immutable transition'); END"
)
_POSTGRESQL_COMPOSER_TRANSITION_FUNCTION = (
    "CREATE FUNCTION elspeth_composer_operations_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN IF "
    + _composer_operation_transition_condition(dialect="postgresql")
    + " THEN RAISE EXCEPTION 'composer operation immutable transition' USING ERRCODE = '23514'; END IF; RETURN NEW; END $$"
)
```

Set `SESSION_SCHEMA_EPOCH` and `_COORDINATION_HARD_CUT_EPOCH` from the reviewed b003 value 78 to 79. P0 must re-read both constants after the actual `.9` GREEN checkpoint; if that checkpoint has advanced the epoch, update both literals and their refusal fixtures together before implementing this schema packet. Existing initializer still compares exact metadata/epoch and refuses predecessors/future stores unchanged; no migration function is added.

- [ ] **Step 4: GREEN:** Require native exit 0 after the record model branch in C3.1. The complete refusal tests below assert raw file bytes unchanged for previous/future/incomplete schema.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_schema.py tests/unit/web/sessions/test_schema.py tests/unit/web/sessions/test_composer_operation_codec.py >"$COMPOSER_LOG_DIR/c2-parent-green.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c2-parent-green.log"
test "$composer_exit" -eq 0
```
- [ ] **Step 5: Commit all C2 schema paths together:** stage `src/elspeth/web/sessions/models.py`, `src/elspeth/web/sessions/schema.py`, `tests/unit/web/sessions/test_continuation_schema.py`, `tests/unit/web/sessions/test_schema.py`; run required preflight, then `git commit -m "feat: persist versioned composer continuation state"`.

- [ ] Add these complete preserve/refuse tests to the new schema test module; add `pytest`, `SESSION_SCHEMA_EPOCH` and `SessionSchemaError` imports. The positive control is the fresh initializer already used by its first test. These assertions measure the actual database bytes after closing the connection and explicitly exercise both epoch directions and current-epoch shape corruption.

```python
@pytest.mark.parametrize("delta", [-1, 1])
def test_wrong_epoch_is_preserved_without_startup_upgrade(tmp_path, delta):
    from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH
    from elspeth.web.sessions.schema import SessionSchemaError
    path = tmp_path / "preserve.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql(f"PRAGMA user_version = {SESSION_SCHEMA_EPOCH + delta}")
    engine.dispose()
    before = path.read_bytes()
    refused = create_session_engine(f"sqlite:///{path}")
    try:
        with pytest.raises(SessionSchemaError):
            initialize_session_schema(refused)
    finally:
        refused.dispose()
    assert path.read_bytes() == before


def test_current_epoch_with_missing_continuation_table_is_not_repaired(tmp_path):
    from elspeth.web.sessions.schema import SessionSchemaError
    path = tmp_path / "missing-table.db"
    engine = create_session_engine(f"sqlite:///{path}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("DROP TABLE composer_continuation_events")
    engine.dispose()
    before = path.read_bytes()
    refused = create_session_engine(f"sqlite:///{path}")
    try:
        with pytest.raises(SessionSchemaError):
            initialize_session_schema(refused)
    finally:
        refused.dispose()
    assert path.read_bytes() == before
```

### C2.3: Complete snapshot and immutable-byte repository

**Files:** Create `src/elspeth/web/sessions/continuation.py`; extend `tests/unit/web/coordination/test_composer_operation_authority.py` using its existing `operation_store`, `admit`, and `start` fixtures.

- [ ] **Step 1: Add exact-byte and collision tests.** These tests use a v1 parent only as a real composite-FK fixture; worker admission remains disabled.

```python
def test_continuation_chunk_collision_preserves_original_bytes(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import put_chunk, read_chunk
    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response", payload=b'{"value":"exact"}', codec="json.v2")
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response") == b'{"value":"exact"}'
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response", payload=b'{"value":"exact"}', codec="json.v2")
        with pytest.raises(AuditIntegrityError, match="immutable chunk collision"):
            put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response", payload=b'{"value":"other"}', codec="json.v2")
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response") == b'{"value":"exact"}'


def test_continuation_chunk_digest_corruption_is_refused(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import put_chunk, read_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    engine, _repository, authority, _service, sid = operation_store
    record, _ = admit(authority, sid)
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response", payload=b"one", codec="json.v2")
        conn.execute(update(chunks).where(chunks.c.operation_id == record.operation_id).values(payload=b"two"))
        with pytest.raises(AuditIntegrityError, match="chunk digest"):
            read_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="native-response")
```

- [ ] **Step 2: Bootstrap the complete importable packet in Step 3, then prove behavioral RED.** Temporarily remove only `put_chunk`'s existing-byte collision comparison. Run the Step 4 command expecting native exit 1 from the collision test's `pytest.raises(AuditIntegrityError)` assertion. Restore the comparison before GREEN; an import/collection error is a setup failure.
- [ ] **Step 3: Create the complete transaction-local byte/snapshot module.** C1a owns the typed codec; this SQL layer retains the codec's exact output, not a lossy re-encoding. Credential admission occurs before caller supplies bytes, and C1a's closed codec refuses unsupported fields. Repository read is private and never reachable through a public DTO.

```python
"""Private transaction-local bytes and complete continuation snapshots."""
from __future__ import annotations

from dataclasses import dataclass
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import Connection, func, insert, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.models import (
    composer_async_operations_table as jobs,
    composer_continuation_checkpoints_table as checkpoints,
    composer_continuation_chunks_table as chunks,
    composer_continuation_events_table as events,
)


@dataclass(frozen=True, slots=True)
class StoredContinuationSnapshot:
    payload_json: str
    cursor: int
    checkpoint_id: str


@dataclass(frozen=True, slots=True)
class ContinuationEvent:
    tag: ContinuationEventTag
    payload_json: str

    def __post_init__(self) -> None:
        if type(self.tag) is not str or self.tag not in CONTINUATION_EVENT_TAGS:
            raise AuditIntegrityError("continuation event has an unfamiliar owned tag")
        if type(self.payload_json) is not str:
            raise AuditIntegrityError("continuation event payload is not owned JSON text")


class ContinuationCASConflict(AuditIntegrityError):
    """The complete selected snapshot is no longer the expected head."""


def put_chunk(
    conn: Connection, *, session_id: str, operation_id: str,
    key: str, payload: bytes, codec: str,
) -> None:
    if type(payload) is not bytes or not key or not codec:
        raise ValueError("chunk requires exact bytes and owned codec/key")
    digest = sha256(payload).hexdigest()
    old = conn.execute(select(chunks).where(
        chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key,
    )).one_or_none()
    if old is not None:
        if old.payload != payload or old.sha256 != digest or old.byte_length != len(payload) or old.codec != codec:
            raise AuditIntegrityError("immutable chunk collision")
        return
    conn.execute(insert(chunks).values(
        session_id=session_id, operation_id=operation_id, chunk_key=key,
        payload=payload, sha256=digest, byte_length=len(payload), codec=codec,
    ))


def read_chunk(conn: Connection, *, session_id: str, operation_id: str, key: str) -> bytes:
    row = conn.execute(select(chunks).where(
        chunks.c.session_id == session_id, chunks.c.operation_id == operation_id, chunks.c.chunk_key == key,
    )).one_or_none()
    if row is None or type(row.payload) is not bytes or len(row.payload) != row.byte_length or sha256(row.payload).hexdigest() != row.sha256:
        raise AuditIntegrityError("continuation chunk digest or binding mismatch")
    return row.payload


def load_snapshot(conn: Connection, *, session_id: str, operation_id: str) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    parent = conn.execute(select(jobs.c.checkpoint_id, jobs.c.journal_cursor).where(
        jobs.c.session_id == session_id, jobs.c.operation_id == operation_id, jobs.c.lifecycle_version == 2,
    )).one_or_none()
    if parent is None:
        raise AuditIntegrityError("continuation parent missing")
    snapshot = conn.execute(select(checkpoints).where(
        checkpoints.c.session_id == session_id, checkpoints.c.operation_id == operation_id,
        checkpoints.c.checkpoint_id == parent.checkpoint_id,
    )).one_or_none()
    last = conn.execute(select(func.coalesce(func.max(events.c.sequence), 0)).where(
        events.c.session_id == session_id, events.c.operation_id == operation_id,
    )).scalar_one()
    if snapshot is None or snapshot.cursor != parent.journal_cursor or last != snapshot.cursor:
        raise AuditIntegrityError("event/snapshot cursor integrity mismatch")
    payload = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=snapshot.snapshot_chunk_key)
    encoded = payload.decode("utf-8", errors="strict")
    stack = decode_continuation_snapshot(encoded)
    if (stack.logical.session_id, stack.logical.operation_id, stack.cursor) != (session_id, operation_id, snapshot.cursor):
        raise AuditIntegrityError("complete continuation snapshot differs from its SQL identity/cursor")
    return StoredContinuationSnapshot(encoded, snapshot.cursor, snapshot.checkpoint_id)


def append_complete_snapshot(
    conn: Connection, *, session_id: str, operation_id: str,
    expected_checkpoint_id: str, expected_cursor: int, snapshot_json: str,
    event_batch: tuple[ContinuationEvent, ...], handoff: bool,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    current = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if current.checkpoint_id != expected_checkpoint_id or current.cursor != expected_cursor:
        raise ContinuationCASConflict("continuation snapshot compare-and-swap failed")
    if not event_batch:
        raise ValueError("a durable transition requires its attribution event")
    previous_stack = decode_continuation_snapshot(current.payload_json)
    frame_stack = decode_continuation_snapshot(snapshot_json)
    if (frame_stack.logical != previous_stack.logical
        or frame_stack.policy_hash != previous_stack.policy_hash
        or frame_stack.admission_policy_json != previous_stack.admission_policy_json
        or frame_stack.cursor not in (expected_cursor, expected_cursor + len(event_batch))):
        raise ContinuationCASConflict("continuation transition changed immutable request/policy or cursor")
    now = database_now(conn)
    checkpoint_id = str(uuid4())
    cursor = expected_cursor
    for item in event_batch:
        cursor += 1
        event_key = f"event:{cursor}"
        put_chunk(conn, session_id=session_id, operation_id=operation_id, key=event_key, payload=item.payload_json.encode("utf-8"), codec="event.v2")
        conn.execute(insert(events).values(
            session_id=session_id, operation_id=operation_id, sequence=cursor,
            tag=item.tag, payload_chunk_key=event_key, created_at=now,
        ))
    snapshot_json = encode_continuation_snapshot(frame_stack.model_copy(update={"cursor": cursor}))
    snapshot_key = f"snapshot:{checkpoint_id}"
    put_chunk(conn, session_id=session_id, operation_id=operation_id, key=snapshot_key, payload=snapshot_json.encode("utf-8"), codec="snapshot.v2")
    conn.execute(insert(checkpoints).values(
        session_id=session_id, operation_id=operation_id, checkpoint_id=checkpoint_id,
        kind="handoff" if handoff else "transition", cursor=cursor,
        snapshot_chunk_key=snapshot_key, frame_stack_version=1, created_at=now,
    ))
    changed = conn.execute(update(jobs).where(
        jobs.c.session_id == session_id, jobs.c.operation_id == operation_id,
        jobs.c.lifecycle_version == 2, jobs.c.checkpoint_id == expected_checkpoint_id,
        jobs.c.journal_cursor == expected_cursor,
    ).values(checkpoint_id=checkpoint_id, journal_cursor=cursor, status_revision=jobs.c.status_revision + 1, updated_at=now))
    if changed.rowcount != 1:
        raise ContinuationCASConflict("continuation snapshot compare-and-swap failed")
    return StoredContinuationSnapshot(snapshot_json, cursor, checkpoint_id)
```

- [ ] **Step 4: GREEN:** Require native exit 0. Run `tests/unit/web/sessions/test_schema.py` after source contract inventories include the real metadata.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/coordination/test_composer_operation_authority.py -k continuation_chunk >"$COMPOSER_LOG_DIR/c2-bytes.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c2-bytes.log"
test "$composer_exit" -eq 0
```
- [ ] **Step 5: Commit:** stage this module and the modified authority test file; preflight then `git commit -m "feat: retain exact continuation chunks and complete snapshots"`.

### C3.0: Make the existing complete owned state decoder available before admission

- [ ] Perform C3.8's exact unchanged `SessionServiceImpl._row_to_state_record` to `sessions.converters.state_record_from_row` body relocation first, preserving every existing field/envelope/compatibility check and the one-line delegating service method. This prerequisite exists before C3.3 imports the decoder for its actual v2 base-state admission check. No new state format, recovery fallback or migration is added.

The current decoder constructs the full `CompositionStateRecord`, calls
`read_content_publication(record)`, then returns it. Move that actual body,
including the publication check and its `AuditIntegrityError` classification.
Replace only `self._unwrap_envelope` with existing `unwrap_state_column` in the
relocated function; retain `_unwrap_envelope` for its other service callers.
The historical decoder excerpt is not permission to drop the carried check.

Add the required `Any`, `UUID`, `read_content_publication`,
`decode_stored_composition_validation_errors`, `unwrap_state_column` and
`restore_utc` imports from their existing owners. The current complete body is:

```python
def state_record_from_row(row: Any) -> CompositionStateRecord:
    """Convert a SQLAlchemy row to a CompositionStateRecord.

    Seam contract A: metadata_ maps DB column metadata_ back to the
    dataclass field. JSON columns are unwrapped from their _version envelope.
    """
    row_mapping = row._mapping
    record = CompositionStateRecord(
        id=UUID(row.id),
        session_id=UUID(row.session_id),
        version=row.version,
        source=unwrap_state_column(row.source),
        sources=unwrap_state_column(row_mapping["sources"] if "sources" in row_mapping else None),
        nodes=unwrap_state_column(row.nodes),
        edges=unwrap_state_column(row.edges),
        outputs=unwrap_state_column(row.outputs),
        metadata_=unwrap_state_column(row.metadata_),
        is_valid=row.is_valid,
        validation_errors=decode_stored_composition_validation_errors(row.validation_errors),
        created_at=restore_utc(row.created_at),
        derived_from_state_id=(UUID(row.derived_from_state_id) if row.derived_from_state_id is not None else None),
        composer_meta=unwrap_state_column(row.composer_meta),
    )
    read_content_publication(record)
    return record
```

- [ ] Install the complete shared effect/snapshot utility definitions before the C3.8 owner tests: C5.3's `bound_action_json`, `retain_action_execution_receipt_on_connection`, `_effect_snapshot`, and C5.5's `private_chunk_key`. Their complete literal bodies appear once in those sections; move their implementation order into this prerequisite without adding alternate definitions. They depend on the already-created C2 schema, main pure C6 frontier/action codec and C1a private chunk codec, not provider execution. Import `PrivateChunk` and `verify_private_chunk` from `composer.continuation_codec` and `PrivateChunkRef` from `composer.continuation` in `sessions/continuation.py` now. Initially install C3.8b/d/e's complete pure value classes and proposal/interpretation codecs with F1.2's `owned_json`, plus the current-claim owner verifier described below. After the actual B1 nominal ToolResult codec exists, C3.8c and C3.8e may supply the complete passive read-result prerequisite described below. The selected-read helper remains deferred until main step 9's actual B4/current-read owners exist. Defer the entire result-retention/pending-reader and service/protocol writer cluster too: its unconditional read-decoder imports are not satisfied by a DTO alone. No stub, partial decoder or separately phased writer substitutes for those owners. Require static checks at each increment; lazy imports do not excuse missing definitions. C5 later integrates provider admission/SDK/settlement around the shared utilities rather than redefining them.

### C3.1: Keep v1 records strict and add a distinct v2 record-validation branch

**Files:** Modify `src/elspeth/web/sessions/composer_operations.py:242-410`; modify `_record_from_row` in `src/elspeth/web/coordination/composer_operation_authority.py:83-120`; extend `tests/unit/web/sessions/test_composer_operation_codec.py`.

- [ ] **Step 1: Extend existing `ComposerOperationClaim` with defaulted fields `segment_sequence: int | None = None` and `ownership_generation: int | None = None`.** Add this code at the end of its existing `__post_init__`, preserving its canonical identity/token/attempt validations:

```python
segment = (self.segment_sequence, self.ownership_generation)
if any(value is not None for value in segment):
    if any(type(value) is not int or value < 1 for value in segment):
        raise ValueError("partial or invalid continuation claim")
```

In `ComposerOperationRecord`, change `deadline_at` annotation to `datetime | None` and append these defaulted fields after `request_json`:

```python
lifecycle_version: Literal[1, 2] = 1
phase: Literal["queued", "executing", "continuation_queued", "reconciling", "retry_waiting", "settled"] | None = None
status_revision: int = 0
current_segment_sequence: int | None = None
ownership_generation: int | None = None
checkpoint_id: str | None = None
journal_cursor: int = 0
ingress_phase: Literal["pending", "committed"] | None = None
reserved_user_message_id: UUID | None = None
```

Add this concrete property to `ComposerOperationRecord` and use it at every legacy-only `record.deadline_at`/`current.deadline_at` read in `sessions/composer_async_worker.py` (the existing `_queued_outcome`, `_run_started_under_lease`, anchor-bearing `_watch`, failure projection and v1 `reap_once` paths). The v2 worker branch and v2 watch's `anchor is None` branch never call it. Replace the two v1 status projection reads and v1 SSE deadline read in `sessions/routes/composer/operations.py` with `row.legacy_deadline_at`/`snapshot.legacy_deadline_at`. This preserves concrete v1 DTO bytes while making the shared record's optional annotation type-correct. SQL-row `.deadline_at` reads remain unchanged.

Also update the seven existing nominal-record deadline reads in the current
legacy owners: two in `repository.start_composer_async_operation`, one in
`SessionServiceImpl._run_composer_terminal_sql`'s retry failure selection, two
in completed-terminal retry/publication, and two in failed-terminal
retry/publication. Use only `.legacy_deadline_at`; preserve each surrounding
method body and every SQL-row read. These are required by the shared record's
optional type. Later C3.10 supplies the actual V2 terminal path; do not invent
a V2 total deadline or silently treat a missing deadline as unexpired.

```python
@property
def legacy_deadline_at(self) -> datetime:
    if self.lifecycle_version != 1 or self.deadline_at is None:
        raise AuditIntegrityError("legacy deadline requested from a continuation record")
    return self.deadline_at
```

Rename the existing `_validate` body to `_validate_v1`. Add only the explicit narrowing below after its existing local schema import; preserve the remaining v1 body. This retains the existing refusal of a missing v1 deadline while making its now-optional shared annotation honest to type checking. Add this dispatcher and v2 branch. Import `Literal` if absent. V2 parent fields are projection/identity facts; current execution authority always comes from segment+attempt+live fence SQL.

```python
if self.deadline_at is None:
    raise ValueError("Legacy operation requires its original absolute deadline")
```

```python
def _validate(self) -> None:
    lifecycle_version: object = self.lifecycle_version
    if type(lifecycle_version) is not int or lifecycle_version not in (1, 2):
        raise ValueError("unknown composer lifecycle version")
    if self.lifecycle_version == 1:
        if self.deadline_at is None or any(value is not None for value in (
            self.phase, self.current_segment_sequence, self.ownership_generation,
            self.checkpoint_id, self.ingress_phase, self.reserved_user_message_id,
        )):
            raise ValueError("legacy parent contains continuation state")
        self._validate_v1()
        return
    self._validate_v2()


def _validate_v2(self) -> None:
    from elspeth.web.sessions.schemas import MessageWithStateResponse, RecomposeRequest, SendMessageRequest
    if type(self.session_id) is not UUID or self.deadline_at is not None:
        raise ValueError("continuation parent must have UUID custody and no total deadline")
    require_canonical_operation_id(self.operation_id)
    if self.kind not in COMPOSER_OPERATION_KINDS or self.status not in COMPOSER_OPERATION_STATUSES:
        raise ValueError("unknown operation discriminator")
    if not is_lower_sha256_hex(self.request_hash):
        raise ValueError("invalid request digest")
    if type(self.actor_user_id) is not str or not 1 <= len(self.actor_user_id) <= 128:
        raise ValueError("invalid actor")
    if self.request_id is not None and (type(self.request_id) is not str or not 1 <= len(self.request_id) <= 128):
        raise ValueError("invalid correlation")
    if self.claim_owner_instance_id is not None and (
        type(self.claim_owner_instance_id) is not str or not 1 <= len(self.claim_owner_instance_id) <= 128
    ):
        raise ValueError("invalid owner projection")
    if self.session_operation_id is not None and (type(self.session_operation_id) is not str or not self.session_operation_id):
        raise ValueError("invalid session operation projection")
    if self.session_operation_epoch is not None and (type(self.session_operation_epoch) is not int or self.session_operation_epoch < 1):
        raise ValueError("invalid session operation epoch projection")
    for value in (self.base_state_id, self.user_message_id):
        if value is not None and type(value) is not UUID:
            raise ValueError("invalid UUID locator")
    if type(self.status_revision) is not int or self.status_revision < 0 or type(self.journal_cursor) is not int or self.journal_cursor < 0:
        raise ValueError("invalid continuation counters")
    if type(self.current_segment_sequence) is not int or self.current_segment_sequence < 1:
        raise ValueError("invalid segment sequence")
    if type(self.ownership_generation) is not int or self.ownership_generation < 1:
        raise ValueError("invalid ownership generation")
    if self.checkpoint_id is None:
        raise ValueError("missing complete snapshot pointer")
    require_canonical_operation_id(self.checkpoint_id)
    if type(self.reserved_user_message_id) is not UUID or self.ingress_phase not in ("pending", "committed"):
        raise ValueError("missing ingress reservation")
    if self.ingress_phase == "pending" and self.user_message_id is not None:
        raise ValueError("pending ingress already has a user row")
    if self.ingress_phase == "committed" and self.user_message_id != self.reserved_user_message_id:
        raise ValueError("committed ingress changed reserved identity")
    if self.kind == "compose_recompose" and self.ingress_phase != "committed":
        raise ValueError("recompose ingress must be committed")
    if (type(self.attempt) is not int or self.attempt < 0
        or type(self.claim_token_present) is not bool or self.claim_token_present
        or self.claim_expires_at is not None):
        raise ValueError("v2 parent does not own a claim")
    if type(self.created_at) is not datetime or type(self.updated_at) is not datetime or restore_utc(self.updated_at) < restore_utc(self.created_at):
        raise ValueError("invalid parent timestamps")
    for timestamp in (self.started_at, self.settled_at, self.cancel_requested_at):
        if timestamp is not None and (type(timestamp) is not datetime or restore_utc(timestamp) < restore_utc(self.created_at)):
            raise ValueError("invalid optional timestamp")
    if self.status in ("queued", "running"):
        if self.phase not in ("queued", "executing", "continuation_queued", "reconciling", "retry_waiting"):
            raise ValueError("invalid active phase")
        if any(value is not None for value in (self.settled_at, self.result_schema, self.result_json, self.result_sha256, self.failure_code, self.settled_by)):
            raise ValueError("active parent contains terminal result")
        if self.request_json is not None:
            validate_composer_operation_request_json(self.request_json)
            model = SendMessageRequest if self.kind == "compose_message" else RecomposeRequest
            request = model.model_validate_json(self.request_json, strict=True)
            if request.operation_id != self.operation_id or request.state_id != self.base_state_id:
                raise ValueError("request identity/base mismatch")
            if isinstance(request, RecomposeRequest) and request.expected_user_message_id != self.reserved_user_message_id:
                raise ValueError("recompose request changed reserved original message")
            if composer_operation_v2_request_hash(session_id=self.session_id, kind=self.kind, request=request) != self.request_hash:
                raise ValueError("v2 request digest mismatch")
        return
    if self.phase != "settled" or self.request_json is not None or self.settled_at is None:
        raise ValueError("invalid terminal continuation bundle")
    if self.settled_by not in COMPOSER_OPERATION_SETTLED_BY or self.result_json is None or not is_lower_sha256_hex(self.result_sha256):
        raise ValueError("missing terminal receipt")
    if self.status == "completed":
        if self.cancel_requested_at is not None or self.failure_code is not None or self.user_message_id is None or self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_SUCCESS:
            raise ValueError("invalid completed continuation")
        response: BaseModel = MessageWithStateResponse.model_validate_json(self.result_json, strict=True)
    else:
        if self.result_schema != COMPOSER_OPERATION_RESULT_SCHEMA_ERROR or self.failure_code not in COMPOSER_OPERATION_FAILURE_CODES:
            raise ValueError("invalid failed continuation")
        response = ComposerOperationError.model_validate_json(self.result_json, strict=True)
        if response.failure_code != self.failure_code:
            raise ValueError("failed continuation code mismatch")
    if strict_response_hash(response) != self.result_sha256:
        raise ValueError("terminal continuation digest mismatch")
```

In `_record_from_row`, change its deadline argument to `restore_utc(row.deadline_at) if row.deadline_at is not None else None`; append every new field from the row, converting the reserved UUID. These are the exact added arguments:

```python
lifecycle_version=row.lifecycle_version,
phase=row.phase,
status_revision=row.status_revision,
current_segment_sequence=row.current_segment_sequence,
ownership_generation=row.ownership_generation,
checkpoint_id=row.checkpoint_id,
journal_cursor=row.journal_cursor,
ingress_phase=row.ingress_phase,
reserved_user_message_id=UUID(row.reserved_user_message_id) if row.reserved_user_message_id is not None else None,
```

- [ ] **Step 2: Add a complete nominal claim regression in the existing codec test file.**

```python
def test_segment_claim_requires_both_positive_sequence_and_generation():
    from uuid import uuid4
    from elspeth.web.sessions.composer_operations import ComposerOperationClaim
    sid = uuid4()
    operation_id = str(uuid4())
    assert ComposerOperationClaim(session_id=sid, operation_id=operation_id, claim_token="x" * 32, attempt=1).segment_sequence is None
    claim = ComposerOperationClaim(session_id=sid, operation_id=operation_id, claim_token="x" * 32, attempt=1, segment_sequence=2, ownership_generation=3)
    assert (claim.segment_sequence, claim.ownership_generation) == (2, 3)
    with pytest.raises(ValueError, match="continuation claim"):
        ComposerOperationClaim(session_id=sid, operation_id=operation_id, claim_token="x" * 32, attempt=1, segment_sequence=2)
```

- [ ] **Step 3: GREEN:** Require native exit 0, with v1 constructors and bytes unchanged.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_composer_operation_codec.py tests/unit/web/sessions/test_composer_operation_dtos.py >"$COMPOSER_LOG_DIR/c3-records.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-records.log"
test "$composer_exit" -eq 0
```

### C3.2: Add attempt-history enforcement before legacy mutation lookup

**Files:** Extend `src/elspeth/web/sessions/continuation.py`; modify `require_composer_operation_mutation_on_connection` in `src/elspeth/web/coordination/composer_operation_authority.py:929`; extend `tests/unit/web/coordination/test_composer_operation_authority.py`.

- [ ] **Step 1: Add this complete historical-fence guard to the private module.** Add `field` to the dataclass import, `RootTransaction` from `sqlalchemy.engine`, `and_`, the existing context/kind imports, `ComposerOperationFenceLost`, `ComposerOperationCancelledDuringTurn`, `ComposerOperationClaim`, segment/attempt/fence-table aliases, and `restore_utc` imports. It returns `False` only for a fence genuinely absent from all continuation history; a known old fence always refuses.

```python
_OWED_P4_SCOPE_KEY = "elspeth.continuation.owed-p4"
_OWED_P4_SEAL = object()


@dataclass(frozen=True, slots=True)
class _OwedP4Scope:
    connection: Connection
    context: SessionOperationContext
    seal: object
    transaction: RootTransaction = field(init=False, repr=False)

    def __post_init__(self) -> None:
        transaction = self.connection.get_transaction()
        if transaction is None or not transaction.is_active:
            raise AuditIntegrityError("owed P4 scope requires an active SQL owner transaction")
        object.__setattr__(self, "transaction", transaction)


def _owed_continuation_p4_active(conn: Connection, context: SessionOperationContext) -> bool:
    selected = conn.info.get(_OWED_P4_SCOPE_KEY)
    if selected is None:
        return False
    if (
        type(selected) is not _OwedP4Scope
        or selected.seal is not _OWED_P4_SEAL
        or selected.connection is not conn
        or selected.context != context
        or not conn.in_transaction()
        or conn.get_transaction() is not selected.transaction
        or not selected.transaction.is_active
    ):
        raise AuditIntegrityError("owed P4 scope escaped its exact current SQL owner transaction")
    return True


def require_continuation_mutation(
    conn: Connection, context: SessionOperationContext, *, allow_cancel: bool = False,
    allow_expired_segment: bool = False,
) -> bool:
    if context.operation_kind is not SessionOperationKind.COMPOSE:
        return False
    fence = context.fence
    owed_p4 = _owed_continuation_p4_active(conn, context)
    allow_cancel = allow_cancel or owed_p4
    allow_expired_segment = allow_expired_segment or owed_p4
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == fence.session_id,
        attempts.c.session_operation_epoch == fence.operation_epoch,
    )).one_or_none()
    if history is None:
        return False
    parent = conn.execute(select(jobs).where(
        jobs.c.session_id == history.session_id, jobs.c.operation_id == history.operation_id,
    )).one_or_none()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == history.session_id, segments.c.operation_id == history.operation_id,
        segments.c.sequence == history.sequence,
    )).one_or_none()
    now = database_now(conn)
    live_fence = conn.execute(select(fences.c.session_id).where(
        fences.c.session_id == fence.session_id, fences.c.operation_id == fence.operation_id,
        fences.c.operation_epoch == fence.operation_epoch, fences.c.lease_token == fence.lease_token,
        fences.c.operation_kind == SessionOperationKind.COMPOSE.value,
        fences.c.released_at.is_(None), fences.c.lease_expires_at > now,
    )).one_or_none()
    principal = conn.execute(select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
        sessions_table.c.id == fence.session_id,
    )).one_or_none()
    live = (
        parent is not None and segment is not None and live_fence is not None
        and parent.lifecycle_version == 2 and parent.status == "running"
        and principal is not None and principal.archived_at is None and principal.user_id == parent.actor_user_id
        and parent.current_segment_sequence == history.sequence
        and parent.ownership_generation == history.generation
        and parent.attempt == history.claim_attempt
        and segment.generation == history.generation and segment.state == "running"
        and history.revoked_at is None
        and history.session_operation_id == fence.operation_id
        and history.session_operation_lease_token == fence.lease_token
        and segment.session_operation_id == fence.operation_id
        and segment.session_operation_epoch == fence.operation_epoch
        and segment.session_operation_lease_token == fence.lease_token
        and segment.deadline_at is not None and (allow_expired_segment or restore_utc(segment.deadline_at) > now)
    )
    if parent is None or segment is None or not live:
        raise ComposerOperationFenceLost(session_id=UUID(history.session_id), operation_id=history.operation_id)
    if parent.cancel_requested_at is not None and not allow_cancel:
        raise ComposerOperationCancelledDuringTurn(ComposerOperationClaim(
            session_id=UUID(history.session_id), operation_id=history.operation_id,
            claim_token=segment.claim_token, attempt=parent.attempt,
            segment_sequence=history.sequence, ownership_generation=history.generation,
        ))
    return True
```

The exact new imports in `sessions/continuation.py` are:

```python
from uuid import UUID
from elspeth.contracts.session_operation import SessionOperationContext, SessionOperationKind
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn, ComposerOperationClaim, ComposerOperationFenceLost,
)
from elspeth.web.sessions.models import (
    composer_operation_segments_table as segments,
    composer_segment_attempts_table as attempts,
    session_operation_fences_table as fences,
    sessions_table,
)
from elspeth.web.sessions.time_normalization import restore_utc
```

Immediately after the existing `if context.operation_kind is not COMPOSE: return` branch, before legacy `row = ...`, insert:

```python
from elspeth.web.sessions.continuation import require_continuation_mutation
if require_continuation_mutation(conn, context):
    return
```

- [ ] **Step 2: Add a known-old-history negative control using the existing real v1 start fixture.** This intentionally binds an old real COMPOSE fence to continuation history without a live segment, proving absence of the current segment cannot fall through to synchronous authority.

```python
def test_known_historical_fence_cannot_fall_back_to_sync_compose(operation_store):
    from sqlalchemy import insert
    from elspeth.web.coordination.composer_operation_authority import require_composer_operation_mutation_on_connection
    from elspeth.web.sessions.models import composer_operation_segments_table as segments
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    engine, repository, _authority, _service, sid = operation_store
    record, running = start(operation_store)
    fence = running.session_operation_context.fence
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(insert(segments).values(
            session_id=str(sid), operation_id=record.operation_id, sequence=1,
            predecessor_sequence=None, state="queued", generation=1,
            enqueued_at=now, next_eligible_at=now, checkpoint_id=str(uuid4()),
            next_action_json="{}", policy_sha256="a" * 64,
        ))
        conn.execute(insert(attempts).values(
            session_id=str(sid), operation_id=record.operation_id, sequence=1, generation=1,
            claim_attempt=running.claim.attempt,
            session_operation_id=fence.operation_id, session_operation_epoch=fence.operation_epoch,
            session_operation_lease_token=fence.lease_token, owner_instance_id="test-owner",
            started_at=now, claim_expires_at=now,
        ))
        with pytest.raises(ComposerOperationFenceLost):
            require_composer_operation_mutation_on_connection(conn, running.session_operation_context)
    repository.release(running.session_operation_context)
```

- [ ] **Step 3: GREEN:** Require native exit 0. Temporarily deleting the newly inserted history guard must make `test_known_historical_fence_cannot_fall_back_to_sync_compose` fail its old-fence refusal assertion; restore that intentional local mutation before proceeding.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/coordination/test_composer_operation_authority.py -k 'historical_fence or mutation' >"$COMPOSER_LOG_DIR/c3-fence-history.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-fence-history.log"
test "$composer_exit" -eq 0
```

### C3.3: Atomically admit one parent, reserved ingress and first queued segment

**Files:** Modify `ComposerAsyncOperationAuthority.admit` in `src/elspeth/web/coordination/composer_operation_authority.py:300-408`; extend `src/elspeth/web/sessions/continuation.py`; extend admission tests in `tests/unit/web/coordination/test_composer_operation_authority.py`.

The master C1a section defines nominal `ContinuationAdmissionSeed` and its `validate_binding` method. The transport task delegates its lifecycle hash to `composer_operation_v2_request_hash` in `sessions/composer_operations.py`; never replace the v1 hash function or re-encode its input.

- [ ] **Step 1: Add this complete initializer to the SQL module.** It is called after parent INSERT in the same savepoint/outer quota transaction; no initial user row is inserted.

```python
def initialize_continuation(
    conn: Connection, *, session_id: str, operation_id: str,
    checkpoint_id: str, snapshot_json: str, action_json: str, policy_sha256: str,
) -> None:
    now = database_now(conn)
    snapshot_key = f"snapshot:{checkpoint_id}"
    put_chunk(conn, session_id=session_id, operation_id=operation_id, key=snapshot_key, payload=snapshot_json.encode("utf-8"), codec="snapshot.v2")
    conn.execute(insert(checkpoints).values(
        session_id=session_id, operation_id=operation_id, checkpoint_id=checkpoint_id,
        kind="transition", cursor=0, snapshot_chunk_key=snapshot_key,
        frame_stack_version=1, created_at=now,
    ))
    conn.execute(insert(segments).values(
        session_id=session_id, operation_id=operation_id, sequence=1, predecessor_sequence=None,
        state="queued", generation=1, enqueued_at=now, next_eligible_at=now,
        checkpoint_id=checkpoint_id, next_action_json=action_json, policy_sha256=policy_sha256,
    ))
```

- [ ] **Step 2: Add keyword arguments `lifecycle_version: Literal[1, 2] = 1`, `continuation_enabled: bool = False` and `seed: ContinuationAdmissionSeed | None = None` to `admit`.** Import the C1a seed nominal type and `composer_operation_v2_request_hash`. Immediately after `_bounded(max_nonterminal)`, insert:

```python
if type(lifecycle_version) is not int or lifecycle_version not in (1, 2):
    raise ValueError("unknown composer lifecycle version")
if type(continuation_enabled) is not bool or (lifecycle_version == 1 and seed is not None):
    raise ValueError("invalid continuation admission configuration")
```

Replace only the request-digest expression in admission's existing request-binding `if` with:

```python
(composer_operation_request_hash if lifecycle_version == 1 else composer_operation_v2_request_hash)(
    session_id=session_id, kind=kind, request=request,
) != request_hash
```

Retain the existing exact request parser, current actor/session ownership, same-session base-state precondition, capacity and savepoint conflict handling. In both replay arms, before `_binding`, add:

```python
if old.lifecycle_version != lifecycle_version:
    raise ComposerOperationConflictError(session_id=session_id, operation_id=operation_id)
```

Immediately after the first existing-parent replay branch returns, before capacity/quota/insert work, insert this gate. An exact v2 replay survives disabling new continuation admission; changed hash/lifecycle still conflicts through the preceding binding checks. Existing v2 readers/cancellation do not consult this flag.

```python
if lifecycle_version == 2 and not continuation_enabled:
    raise ComposerOperationPreconditionRefused(error=ComposerOperationError(
        http_status=409, failure_code="http_error", error_type="composer_continuation_disabled",
        body={"detail": {"error_type": "composer_continuation_disabled", "detail": "New continuation requests are disabled."}},
        diagnostic_id=None,
    ))
if lifecycle_version == 2:
    original_message_id = check_composer_request_preconditions_on_connection(
        conn, ComposerOperationPreconditions(session_id, actor_user_id, request_id, base_state_id, kind, request_json),
        auth_provider_type=auth_provider_type,
    )
    if base_state_id is not None:
        base_row = conn.execute(select(composition_states_table).where(
            composition_states_table.c.id == str(base_state_id), composition_states_table.c.session_id == str(session_id),
        )).one()
        # Decode the complete actual owned base before any quota/parent/ingress
        # write. V2 does not create a recovery initializer for malformed state.
        try:
            state_from_record(state_record_from_row(base_row))
        except (TypeError, ValueError, KeyError) as exc:
            raise AuditIntegrityError("continuation base state cannot be hydrated") from exc
    initial_trust_mode = conn.execute(
        select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(session_id))
    ).scalar_one()
    if type(initial_trust_mode) is not str or initial_trust_mode not in ("auto_commit", "explicit_approve"):
        raise AuditIntegrityError("continuation admission requires the current owned session trust mode")
    if seed is None:
        if self._continuation_policy is None:
            raise ValueError("new continuation admission has no validated server policy")
        seed = prepare_composer_continuation_admission(
            session_id=session_id, operation_id=operation_id, kind=kind, request_json=request_json,
            request_hash=request_hash, base_state_id=base_state_id, policy_hash=self._continuation_policy.policy_hash,
            admission_policy_json=self._continuation_policy.model_dump_json(),
            initial_trust_mode=initial_trust_mode,
            initial_action_json=self._continuation_policy.actions.session_sql.model_dump_json(),
            original_user_message_id=str(original_message_id) if original_message_id is not None else None,
        )
    if type(seed) is not ContinuationAdmissionSeed:
        raise ValueError("fresh continuation admission requires its owned server seed")
```

Immediately before admission's `try: with conn.begin_nested()`, insert this complete binding preparation. `UUID5` is a stable identity convention, not a source of authorization; current request/auth SQL remains authoritative.

```python
initial_checkpoint_id: str | None = None
reserved_message_id: str | None = None
if lifecycle_version == 2:
    if seed is None:
        raise AuditIntegrityError("continuation seed disappeared")
    reserved_message_id = (
        str(request.expected_user_message_id) if isinstance(request, RecomposeRequest)
        else reserved_user_message_id(session_id=session_id, operation_id=operation_id)
    )
    seed.validate_binding(
        session_id=session_id, operation_id=operation_id, kind=kind,
        request_json=request_json, request_hash=request_hash, base_state_id=base_state_id,
        reserved_user_message_id=reserved_message_id,
    )
    initial_stack = decode_continuation_snapshot(seed.snapshot_json)
    if initial_stack.mutation_ceiling.initial_trust_mode != initial_trust_mode:
        raise AuditIntegrityError("fresh continuation seed substituted another initial session trust mode")
    retained_policy = decode_continuation_policy(initial_stack.admission_policy_json, initial_stack.policy_hash)
    initial_action = ContinuationActionPolicy.model_validate_json(seed.action_json, strict=True)
    if initial_action != retained_policy.actions.session_sql:
        raise AuditIntegrityError("initial action differs from the retained complete admission policy")
    if self._continuation_policy is not None and retained_policy != self._continuation_policy:
        raise AuditIntegrityError("fresh server seed substituted another admission policy")
    initial_checkpoint_id = str(uuid4())
```

For recompose, retain the existing recompose start precondition against its original message; mark admission ingress committed only after an admission-time lookup verifies that same-session original message exists and is the requested latest eligible user message. Use the existing `check_composer_operation_preconditions_on_connection` parser/selector extracted as a pure-on-connection function for admission/start, preserving its existing body; do not invent a second transcript selector. The parent C1a initializer calls that same selector before constructing a recompose seed.

The actual session trust mode is read only for a fresh enabled V2 request on
this locked connection. Exact replay returns before the flag, current policy,
seed construction or current-mode read. An internal supplied nominal seed must
also match that actual initial mode. The pure ceiling contract stays unchanged.
The existing `quota.prepare(self._engine)` remains before the session lock and
can commit expiry cleanup independently. Fresh admission rows and charges are
atomic; refusal/replay does not promise that pre-existing expired quota rows
survive that maintenance. Test those two effects separately.

In the existing INSERT values, replace `deadline_at=now+...` with the following values and append the new fields. The old argument remains validated even when unused in v2 so callers cannot sneak an invalid worker configuration into admission.

```python
deadline_at=now + timedelta(seconds=deadline_seconds) if lifecycle_version == 1 else None,
lifecycle_version=lifecycle_version,
phase="queued" if lifecycle_version == 2 else None,
status_revision=0,
current_segment_sequence=1 if lifecycle_version == 2 else None,
ownership_generation=1 if lifecycle_version == 2 else None,
checkpoint_id=initial_checkpoint_id,
journal_cursor=0,
ingress_phase=("pending" if kind == "compose_message" else "committed") if lifecycle_version == 2 else None,
reserved_user_message_id=reserved_message_id,
user_message_id=reserved_message_id if lifecycle_version == 2 and kind == "compose_recompose" else None,
```

Immediately after the parent `conn.execute(insert(jobs)...` inside that same savepoint, add:

```python
if lifecycle_version == 2:
    if seed is None or initial_checkpoint_id is None:
        raise AuditIntegrityError("initial continuation binding disappeared")
    initialize_continuation(
        conn, session_id=str(session_id), operation_id=operation_id,
        checkpoint_id=initial_checkpoint_id, snapshot_json=seed.snapshot_json,
        action_json=seed.action_json, policy_sha256=seed.policy_hash,
    )
```

Preserve the existing `quota.check_on_connection` call outside the insert conflict handler, still inside the outer session transaction. A quota refusal rolls back parent, chunks, checkpoint and first segment together; replay runs no quota check. Add `uuid4` to the UUID import, and import `reserved_user_message_id` from the master C1a `composer.continuation` module. That single function owns the product namespace.

- [ ] **Step 3: Extend the existing test `admit` helper with `lifecycle_version=1, seed=None`, selecting the versioned hash and passing both new arguments.** Its complete changed call arguments are:

```python
request_hash=(composer_operation_request_hash if lifecycle_version == 1 else composer_operation_v2_request_hash)(session_id=sid, kind=kind, request=request),
lifecycle_version=lifecycle_version,
seed=seed,
```

The master initializer tests construct real seeds and exercise this helper; a request-supplied dictionary cannot pass the nominal seed check.

- [ ] **Step 4: GREEN:** Require native exit 0. Inspect actual SQL for the replay test: one parent, one cursor-zero checkpoint, one sequence-1 segment and no extra rate-limit event; quota-refusal control must find none of the new rows.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/coordination/test_composer_operation_authority.py tests/unit/web/sessions/test_composer_operation_lifecycle.py >"$COMPOSER_LOG_DIR/c3-admission.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-admission.log"
test "$composer_exit" -eq 0
```

### C3.4: Claim and start the exact admitted first segment under one SQL fence

This initial increment is restricted to the exact fresh initializer. C3.9 is a preactivation dependency. C3.4b below extends this same API only after its existing F28.3/private-chunk producers and real resume tests; no later cold restore, recovery start or worker activation is qualified by first-start GREEN.

#### Task 1 — First-segment RED and exact source boundary

Modify only `coordination/composer_operation_authority.py`, `coordination/repository.py`, `sessions/continuation.py`, `sessions/protocol.py`; create `tests/unit/web/coordination/test_composer_continuation_claim_start.py` and `tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py`. Paths are under `src/elspeth/web/` unless prefixed `tests/`. No worker/flag/settings/API routing activation, recovery/yield implementation or shared-fixture rewrite belongs to this increment.

- [ ] Add the complete standalone unit module in Task 3, then run it serially. Before implementation its new API calls must fail because the actual methods are absent; preserve the original semantic controls for GREEN and omission mutants.
- [ ] Apply Task 2 and the narrow legacy patch below. Match actual signatures exactly. The public repository method retains the approved optional retained-segment-policy comparison. Add its same signature to the actual `SessionOperationAuthority` protocol, with `ContinuationSegmentPolicy` imported under the existing TYPE_CHECKING block:

```python
def start_composer_segment(
    self, claim: ComposerOperationClaim, *, owner_instance_id: str,
    lease_seconds: int, auth_provider_type: str,
    segment_policy: ContinuationSegmentPolicy | None = None,
) -> SessionOperationContext:
    """Start one current admitted continuation claim under its actual SQL fence."""
    ...
```

This is a Protocol declaration, not an implementation placeholder. The concrete implementation follows.

#### Task 2 — Claim, complete selection, and start in the same locked transaction

In `composer_operation_authority.py`, import `composer_operation_segments_table as segments`, `web_instances_table`, `false` from `sqlalchemy`, and `ColumnElement` from `sqlalchemy.sql.elements`. `datetime`, `timedelta`, `and_`, `or_`, secrets and the nominal claim/error classes already exist. Add the following module helpers and concrete class method. The first helper is deliberately the only initial-versus-later queue-scope switch; Task 5 replaces it before later starts are permitted.

```python
def _continuation_claim_scope() -> tuple[ColumnElement[bool], ...]:
    return (segments.c.sequence == 1, segments.c.generation == 1, segments.c.state == "queued")


def _continuation_claim_predicate(conn: Connection, now: datetime) -> ColumnElement[bool]:
    takeover = and_(
        fences.c.lease_expires_at <= now,
        select(web_instances_table.c.instance_id).where(
            web_instances_table.c.instance_id == fences.c.owner_instance_id,
            web_instances_table.c.lease_expires_at <= now,
        ).correlate(fences).exists(),
    ) if conn.dialect.name == "postgresql" else false()
    available_fence = select(fences.c.session_id).where(
        fences.c.session_id == segments.c.session_id,
        or_(fences.c.released_at.is_not(None), takeover),
    ).correlate(segments).exists()
    parent = select(jobs.c.operation_id).where(
        jobs.c.session_id == segments.c.session_id,
        jobs.c.operation_id == segments.c.operation_id,
        jobs.c.lifecycle_version == 2,
        jobs.c.status.in_(("queued", "running")),
        jobs.c.current_segment_sequence == segments.c.sequence,
        jobs.c.ownership_generation == segments.c.generation,
        jobs.c.checkpoint_id == segments.c.checkpoint_id,
        select(sessions_table.c.id).where(
            sessions_table.c.id == jobs.c.session_id,
            sessions_table.c.user_id == jobs.c.actor_user_id,
            sessions_table.c.archived_at.is_(None),
        ).correlate(jobs).exists(),
    ).correlate(segments).exists()
    return and_(
        *_continuation_claim_scope(), parent, available_fence,
        segments.c.next_eligible_at <= now,
        or_(segments.c.claim_token.is_(None), segments.c.claim_expires_at <= now),
    )


def claim_segment(self, *, limit: int) -> tuple[ComposerOperationClaim, ...]:
    _bounded(limit)
    with self._engine.connect() as conn:
        now = database_now(conn)
        candidates = conn.execute(select(
            segments.c.session_id, segments.c.operation_id, segments.c.sequence,
        ).where(_continuation_claim_predicate(conn, now)).order_by(
            segments.c.next_eligible_at, segments.c.enqueued_at, segments.c.session_id,
            segments.c.operation_id, segments.c.sequence,
        ).limit(limit)).all()
    claimed: list[ComposerOperationClaim] = []
    for hint in candidates:
        with locked_session_transaction(self._engine, hint.session_id) as conn:
            now = database_now(conn)
            row = conn.execute(select(segments, jobs.c.attempt.label("parent_attempt")).join(
                jobs, and_(jobs.c.session_id == segments.c.session_id,
                           jobs.c.operation_id == segments.c.operation_id),
            ).where(
                segments.c.session_id == hint.session_id,
                segments.c.operation_id == hint.operation_id,
                segments.c.sequence == hint.sequence,
                _continuation_claim_predicate(conn, now),
            )).one_or_none()
            if row is None:
                continue
            token = secrets.token_urlsafe(32)
            changed = conn.execute(update(segments).where(
                segments.c.session_id == hint.session_id,
                segments.c.operation_id == hint.operation_id,
                segments.c.sequence == hint.sequence,
                segments.c.generation == row.generation,
                _continuation_claim_predicate(conn, now),
            ).values(claim_token=token, claim_owner_instance_id=self._owner_instance_id,
                     claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds)))
            if changed.rowcount != 1:
                continue
            parent = conn.execute(update(jobs).where(
                jobs.c.session_id == hint.session_id, jobs.c.operation_id == hint.operation_id,
                jobs.c.lifecycle_version == 2, jobs.c.status.in_(("queued", "running")),
                jobs.c.current_segment_sequence == hint.sequence,
                jobs.c.ownership_generation == row.generation,
                jobs.c.checkpoint_id == row.checkpoint_id, jobs.c.attempt == row.parent_attempt,
            ).values(attempt=row.parent_attempt + 1, updated_at=now,
                     status_revision=jobs.c.status_revision + 1))
            if parent.rowcount != 1:
                raise ComposerOperationFenceLost(session_id=UUID(hint.session_id),
                    operation_id=hint.operation_id, attempt=row.parent_attempt + 1)
            claimed.append(ComposerOperationClaim(session_id=UUID(hint.session_id),
                operation_id=hint.operation_id, claim_token=token, attempt=row.parent_attempt + 1,
                segment_sequence=hint.sequence, ownership_generation=row.generation))
    return tuple(claimed)
```

Place `claim_segment` inside `ComposerAsyncOperationAuthority`; the other two definitions are module functions. There is intentionally no parent-cancel predicate. The PostgreSQL web-instance predicate predicts the existing takeover rule without weakening the authoritative repository recheck. SQLite never takes over an unreleased expired owner. A finite queue-order witness is not a general starvation proof; claim renewal/release and failed-adoption scheduling remain their separately planned C4 owners.

In `sessions/continuation.py`, add imports `Any`, `timedelta`, `Row`, the existing pure `ContinuationFrameStack`, `prepare_composer_continuation_admission`, `reserved_user_message_id`, codec decode, all three owned policy classes plus decoder, `size_segment`, both request schemas, `composer_operation_v2_request_hash`, and `ComposerOperationRunning`. Preserve initializer, C2 snapshot/chunk bodies and the entire C3.2 history/RootTransaction guard. Add these complete private helpers after `StoredContinuationSnapshot`/the existing snapshot functions:

```python
@dataclass(frozen=True, slots=True)
class _SegmentStartSelection:
    snapshot: StoredContinuationSnapshot
    policy: ContinuationAdmissionPolicy
    action: ContinuationActionPolicy
    action_json: str


def _selected_segment_action(
    conn: Connection, *, parent: Row[Any], segment: Row[Any],
    snapshot: StoredContinuationSnapshot, stack: ContinuationFrameStack,
    policy: ContinuationAdmissionPolicy,
) -> ContinuationActionPolicy:
    del conn
    if (segment.sequence != 1 or segment.generation != 1 or segment.state != "queued"
            or parent.status != "queued" or parent.started_at is not None or snapshot.cursor != 0):
        raise AuditIntegrityError("later continuation starts require the reviewed C3.4b producer binding")
    expected = prepare_composer_continuation_admission(
        session_id=UUID(parent.session_id), operation_id=parent.operation_id, kind=stack.logical.kind,
        request_json=parent.request_json, request_hash=parent.request_hash,
        base_state_id=UUID(parent.base_state_id) if parent.base_state_id is not None else None,
        policy_hash=stack.policy_hash, admission_policy_json=stack.admission_policy_json,
        initial_trust_mode=stack.mutation_ceiling.initial_trust_mode,
        initial_action_json=policy.actions.session_sql.model_dump_json(),
        original_user_message_id=parent.reserved_user_message_id if parent.kind == "compose_recompose" else None,
    )
    if decode_continuation_snapshot(expected.snapshot_json) != stack:
        raise AuditIntegrityError("first segment is not its exact admitted complete initializer")
    return policy.actions.session_sql


def _select_segment_start_on_connection(
    conn: Connection, claim: ComposerOperationClaim, *, owner_instance_id: str,
    segment_policy: ContinuationSegmentPolicy | None = None,
) -> _SegmentStartSelection:
    if type(claim) is not ComposerOperationClaim or claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("continuation start requires its nominal segment claim")
    if segment_policy is not None and type(segment_policy) is not ContinuationSegmentPolicy:
        raise TypeError("segment policy must be its actual owned type")
    now = database_now(conn)
    parent = conn.execute(select(jobs).where(
        jobs.c.session_id == str(claim.session_id), jobs.c.operation_id == claim.operation_id,
    )).one_or_none()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence,
    )).one_or_none()
    if (parent is None or segment is None or parent.lifecycle_version != 2
            or parent.status not in ("queued", "running") or parent.attempt != claim.attempt
            or parent.current_segment_sequence != claim.segment_sequence
            or parent.ownership_generation != claim.ownership_generation
            or segment.generation != claim.ownership_generation
            or segment.state not in ("queued", "interrupted")
            or segment.claim_token != claim.claim_token or segment.claim_owner_instance_id != owner_instance_id
            or segment.claim_expires_at is None or restore_utc(segment.claim_expires_at) <= now
            or restore_utc(segment.next_eligible_at) > now):
        raise ComposerOperationFenceLost(session_id=claim.session_id,
            operation_id=claim.operation_id, attempt=claim.attempt)
    principal = conn.execute(select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
        sessions_table.c.id == str(claim.session_id),
    )).one_or_none()
    if principal is None or principal.archived_at is not None or principal.user_id != parent.actor_user_id:
        raise ComposerOperationFenceLost(session_id=claim.session_id,
            operation_id=claim.operation_id, attempt=claim.attempt)
    snapshot = load_snapshot(conn, session_id=str(claim.session_id), operation_id=claim.operation_id)
    stack = decode_continuation_snapshot(snapshot.payload_json)
    if snapshot.checkpoint_id != parent.checkpoint_id or segment.checkpoint_id != snapshot.checkpoint_id:
        raise AuditIntegrityError("segment and parent selected different complete checkpoints")
    request: SendMessageRequest | RecomposeRequest
    try:
        if parent.kind == "compose_message":
            request = SendMessageRequest.model_validate_json(parent.request_json)
            original = reserved_user_message_id(claim.session_id, claim.operation_id)
        elif parent.kind == "compose_recompose":
            request = RecomposeRequest.model_validate_json(parent.request_json)
            original = str(request.expected_user_message_id)
        else:
            raise AuditIntegrityError("continuation parent kind is invalid")
    except ValueError as exc:
        raise AuditIntegrityError("SQL continuation request violates its admitted owned shape") from exc
    logical = stack.logical
    if (request.operation_id != claim.operation_id
            or (str(request.state_id) if request.state_id is not None else None) != parent.base_state_id
            or composer_operation_v2_request_hash(session_id=claim.session_id, kind=parent.kind, request=request) != parent.request_hash
            or (logical.session_id, logical.operation_id, logical.kind, logical.request_hash,
                logical.base_state_id, logical.original_user_message_id)
               != (str(claim.session_id), claim.operation_id, parent.kind, parent.request_hash,
                   parent.base_state_id, parent.reserved_user_message_id)
            or original != parent.reserved_user_message_id
            or (parent.ingress_phase == "committed" and parent.user_message_id != original)
            or (parent.ingress_phase == "pending" and parent.user_message_id is not None)):
        raise AuditIntegrityError("complete snapshot differs from its actual SQL logical request")
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    if segment_policy is not None and segment_policy != policy.segment:
        raise AuditIntegrityError("start substituted current defaults for retained segment policy")
    if segment.policy_sha256 != stack.policy_hash:
        raise AuditIntegrityError("segment policy pointer differs from the retained complete policy")
    expected = _selected_segment_action(conn, parent=parent, segment=segment,
        snapshot=snapshot, stack=stack, policy=policy)
    try:
        action = ContinuationActionPolicy.model_validate_json(segment.next_action_json, strict=True)
    except ValueError as exc:
        raise AuditIntegrityError("segment action violates its retained owned contract") from exc
    if action != expected:
        raise AuditIntegrityError("segment allowance differs from its selected retained action")
    return _SegmentStartSelection(snapshot, policy, expected, segment.next_action_json)


def start_segment_on_connection(
    conn: Connection, claim: ComposerOperationClaim, *, context: SessionOperationContext,
    owner_instance_id: str, segment_policy: ContinuationSegmentPolicy | None = None,
) -> ComposerOperationRunning:
    if type(claim) is not ComposerOperationClaim:
        raise TypeError("continuation start requires its nominal claim")
    if (type(context) is not SessionOperationContext or context.operation_kind is not SessionOperationKind.COMPOSE
            or context.fence.session_id != str(claim.session_id)):
        raise AuditIntegrityError("continuation start requires its exact same-session COMPOSE context")
    selected = _select_segment_start_on_connection(conn, claim, owner_instance_id=owner_instance_id,
        segment_policy=segment_policy)
    now = database_now(conn)
    f = context.fence
    owner = conn.execute(select(fences.c.lease_expires_at).where(
        fences.c.session_id == f.session_id, fences.c.operation_id == f.operation_id,
        fences.c.operation_epoch == f.operation_epoch, fences.c.lease_token == f.lease_token,
        fences.c.operation_kind == SessionOperationKind.COMPOSE.value,
        fences.c.owner_instance_id == owner_instance_id,
        fences.c.released_at.is_(None), fences.c.lease_expires_at > now,
    )).one_or_none()
    if owner is None:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    duration = size_segment(selected.action, selected.policy.segment)
    deadline = now + timedelta(seconds=duration)
    cooperative = now + timedelta(seconds=min(selected.policy.segment.quantum, duration - selected.policy.segment.watermark))
    changed = conn.execute(update(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
        segments.c.state.in_(("queued", "interrupted")), segments.c.claim_token == claim.claim_token,
        segments.c.claim_owner_instance_id == owner_instance_id, segments.c.claim_expires_at > now,
        segments.c.next_eligible_at <= now, segments.c.checkpoint_id == selected.snapshot.checkpoint_id,
        segments.c.policy_sha256 == selected.policy.policy_hash, segments.c.next_action_json == selected.action_json,
    ).values(state="running", started_at=now, cooperative_at=cooperative, deadline_at=deadline,
        session_operation_id=f.operation_id, session_operation_lease_token=f.lease_token,
        session_operation_epoch=f.operation_epoch))
    if changed.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    conn.execute(insert(attempts).values(
        session_id=str(claim.session_id), operation_id=claim.operation_id,
        sequence=claim.segment_sequence, generation=claim.ownership_generation, claim_attempt=claim.attempt,
        session_operation_id=f.operation_id, session_operation_epoch=f.operation_epoch,
        session_operation_lease_token=f.lease_token, owner_instance_id=owner_instance_id,
        started_at=now, claim_expires_at=restore_utc(owner.lease_expires_at),
    ))
    parent = conn.execute(update(jobs).where(
        jobs.c.session_id == str(claim.session_id), jobs.c.operation_id == claim.operation_id,
        jobs.c.lifecycle_version == 2, jobs.c.status.in_(("queued", "running")), jobs.c.attempt == claim.attempt,
        jobs.c.current_segment_sequence == claim.segment_sequence, jobs.c.ownership_generation == claim.ownership_generation,
        jobs.c.checkpoint_id == selected.snapshot.checkpoint_id, jobs.c.journal_cursor == selected.snapshot.cursor,
    ).values(status="running", phase="executing", started_at=func.coalesce(jobs.c.started_at, now),
        session_operation_id=f.operation_id, session_operation_lease_token=f.lease_token,
        session_operation_epoch=f.operation_epoch, claim_owner_instance_id=owner_instance_id,
        status_revision=jobs.c.status_revision + 1, updated_at=now))
    if parent.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    return ComposerOperationRunning(claim=claim, session_operation_context=context)
```

In `_SessionOperationAuthorityRepository`, add the concrete method below. Add these exact runtime imports (merge into existing import groups). All other runtime globals in this method already have actual repository bindings. The second selection after minting is deliberate: the lower boundary rereads actual custody and repeats final claim/checkpoint/action CAS before writes.

```python
from elspeth.web.composer.continuation_policy import ContinuationSegmentPolicy
from elspeth.web.coordination.composer_operation_authority import _require_owner
from elspeth.web.sessions.continuation import _select_segment_start_on_connection, start_segment_on_connection
from elspeth.web.sessions.models import composer_async_operations_table
```

```python
def start_composer_segment(
    self, claim: ComposerOperationClaim, *, owner_instance_id: str,
    lease_seconds: int, auth_provider_type: str,
    segment_policy: ContinuationSegmentPolicy | None = None,
) -> SessionOperationContext:
    if type(claim) is not ComposerOperationClaim or claim.segment_sequence is None or claim.ownership_generation is None:
        raise TypeError("continuation start requires its nominal segment claim")
    if type(auth_provider_type) is not str or auth_provider_type not in get_args(AuthProviderType):
        raise ValueError("auth_provider_type must name a browser authentication provider")
    _validate_owner(owner_instance_id)
    _validate_lease_seconds(lease_seconds)
    with self._locked_transaction(str(claim.session_id)) as conn:
        parent = conn.execute(select(composer_async_operations_table.c.actor_user_id).where(
            composer_async_operations_table.c.session_id == str(claim.session_id),
            composer_async_operations_table.c.operation_id == claim.operation_id,
            composer_async_operations_table.c.lifecycle_version == 2,
            composer_async_operations_table.c.status.in_(("queued", "running")),
        )).one_or_none()
        if parent is None:
            raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
        _require_owner(conn, session_id=claim.session_id, actor_user_id=parent.actor_user_id,
            auth_provider_type=auth_provider_type)
        _select_segment_start_on_connection(conn, claim, owner_instance_id=owner_instance_id, segment_policy=segment_policy)
        context = self._advance_exclusive_fence_on_connection(
            conn, session_id_text=str(claim.session_id), operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id=owner_instance_id, lease_seconds=lease_seconds,
        )
        start_segment_on_connection(conn, claim, context=context, owner_instance_id=owner_instance_id,
            segment_policy=segment_policy)
        return context
```

No elapsed parent total-deadline, current trust-mode substitution or business-cancellation guard is used to authorize cleanup start. The current actor/browser-provider checks and distributed old-owner fence checks still apply. Ordinary work after a cancelled start must be refused by the unchanged C3.2 business guard. This private increment does not call the provider or install an owed-P4 scope.

##### Exact legacy isolation patch

Apply this unified patch to the current source; format afterward. No other legacy algorithm changes are required. In particular the v1 cancellation/deadline/failure-priority behavior, terminal early return for actual v1 and every original claim predicate stay intact.

```diff
--- a/src/elspeth/web/coordination/composer_operation_authority.py
+++ b/src/elspeth/web/coordination/composer_operation_authority.py
@@ -213,7 +213,11 @@
 def get_composer_operation_for_start_on_connection(conn: Connection, claim: ComposerOperationClaim) -> ComposerOperationRecord:
+    if type(claim) is not ComposerOperationClaim:
+        raise TypeError("claim must be ComposerOperationClaim")
     row = _read(conn, claim.session_id, claim.operation_id, body=True)
     if (
         row is None
+        or row.lifecycle_version != 1
+        or claim.segment_sequence is not None
         or not _claim_matches(row, claim)
         or row.claim_expires_at is None
         or restore_utc(row.claim_expires_at) <= database_now(conn)
@@ -640,6 +644,7 @@
             candidates = conn.execute(
                 select(jobs.c.session_id, jobs.c.operation_id)
                 .where(
+                    jobs.c.lifecycle_version == 1,
                     jobs.c.status == "queued",
                     jobs.c.cancel_requested_at.is_(None),
                     jobs.c.deadline_at > now,
@@ -659,7 +664,7 @@
             sid = UUID(candidate.session_id)
             with locked_session_transaction(self._engine, candidate.session_id) as conn:
                 now = database_now(conn)
-                query = select(jobs).where(*_key(sid, candidate.operation_id), jobs.c.status == "queued").limit(1)
+                query = select(jobs).where(*_key(sid, candidate.operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued").limit(1)
                 if conn.dialect.name == "postgresql":
                     query = query.with_for_update(skip_locked=True)
                 row = conn.execute(query).one_or_none()
@@ -670,6 +675,7 @@
                     update(jobs)
                     .where(
                         *_key(sid, candidate.operation_id),
+                        jobs.c.lifecycle_version == 1,
                         jobs.c.status == "queued",
                         jobs.c.attempt == row.attempt,
                         jobs.c.cancel_requested_at.is_(None),
@@ -771,12 +777,14 @@
             raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
         with locked_session_transaction(self._engine, str(session_id)) as conn:
             row = _read(conn, session_id, operation_id)
+            if row is not None and row.lifecycle_version != 1:
+                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
             if row is not None and row.status in ("completed", "failed"):
                 return _record_from_row(row)
             now = database_now(conn)
             if row is None or row.status != "queued":
                 raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
-            predicates = [*_key(session_id, operation_id), jobs.c.status == "queued"]
+            predicates = [*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "queued"]
             if claim_or_none is None:
                 if row.claim_token is not None and row.claim_expires_at is not None and restore_utc(row.claim_expires_at) > now:
                     raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
@@ -815,6 +823,7 @@
             rows = conn.execute(
                 select(*(c for c in jobs.c if c.name != "request_json"))
                 .where(
+                    jobs.c.lifecycle_version == 1,
                     jobs.c.status == "queued",
                     or_(jobs.c.deadline_at <= now, jobs.c.cancel_requested_at.is_not(None)),
                     or_(jobs.c.claim_token.is_(None), jobs.c.claim_expires_at <= now),
@@ -831,6 +840,7 @@
             rows = conn.execute(
                 select(*(c for c in jobs.c if c.name != "request_json"))
                 .where(
+                    jobs.c.lifecycle_version == 1,
                     jobs.c.status == "running",
                     ~select(fences.c.session_id)
                     .where(
@@ -931,6 +941,8 @@
             raise TypeError("recovery settlement requires an exact SessionOperationContext")
         with locked_session_transaction(self._engine, str(session_id)) as conn:
             row = _read(conn, session_id, operation_id)
+            if row is not None and row.lifecycle_version != 1:
+                raise ComposerOperationFenceLost(session_id=session_id, operation_id=operation_id)
             if row is not None and row.status in ("completed", "failed"):
                 return _record_from_row(row)
             now = database_now(conn)
@@ -974,7 +986,7 @@
             failure = _select_failure_on_connection(row, failure, now=now, authoritative_failure=authoritative_failure)
             conn.execute(
                 update(jobs)
-                .where(*_key(session_id, operation_id), jobs.c.status == "running")
+                .where(*_key(session_id, operation_id), jobs.c.lifecycle_version == 1, jobs.c.status == "running")
                 .values(**_terminal_values(failure, now=now, settled_by=settled_by))
             )
             settled = _read(conn, session_id, operation_id)
@@ -1045,6 +1057,7 @@
         update(jobs)
         .where(
             *_key(claim.session_id, claim.operation_id),
+            jobs.c.lifecycle_version == 1,
             jobs.c.status == "queued",
             jobs.c.claim_token == claim.claim_token,
             jobs.c.attempt == claim.attempt,
```

#### Task 3 — Complete standalone SQLite and PostgreSQL tests

Create `tests/unit/web/coordination/test_composer_continuation_claim_start.py` with this module. It imports the actual existing SQLite fixture and real C3.3 admission helper; it never hand-builds a parent or fakes operation authority. SQL corruption/fault seams below are test controls, not new production writers. No yield/release-segment/worker API is referenced.

```python
from dataclasses import replace
from datetime import timedelta
from hashlib import sha256
from uuid import uuid4

import pytest
from sqlalchemy import event, select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_clock import size_segment
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.database_clock import database_now
from elspeth.web.sessions.composer_operations import (
    ComposerOperationCancelledDuringTurn, ComposerOperationError,
    ComposerOperationFenceLost, ComposerOperationPreconditionRefused,
)
from elspeth.web.sessions.continuation import load_snapshot, require_continuation_mutation
from elspeth.web.sessions.models import composer_async_operations_table as jobs
from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import session_operation_fences_table as fences
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.time_normalization import restore_utc
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination.test_composer_operation_authority import (
    _admission_rows, _admission_v2_authority, admit,
    operation_store as _operation_store,
)

operation_store = _operation_store


def queued(store, *, policy=None):
    authority = _admission_v2_authority(store, policy=policy)
    record, fresh = admit(authority, store[4], lifecycle_version=2, continuation_enabled=True)
    assert fresh
    return authority, record


def start_segment(repository, claim, **changes):
    args = dict(owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    args.update(changes)
    return repository.start_composer_segment(claim, **args)


def test_fresh_start_has_exact_history_snapshot_and_independent_clocks(operation_store, monkeypatch):
    from elspeth.web.coordination import composer_operation_authority as admission_module
    engine, repository, _, _, sid = operation_store
    policy = minimal_continuation_policy()
    real_clock = admission_module.database_now
    with monkeypatch.context() as old_admission_clock:
        old_admission_clock.setattr(admission_module, "database_now", lambda conn: real_clock(conn) - timedelta(days=2))
        authority, record = queued(operation_store, policy=policy)
    with engine.connect() as conn:
        snapshot = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        oldest = database_now(conn) - timedelta(days=2)
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        history = conn.execute(select(attempts)).one()
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == snapshot
        assert require_continuation_mutation(conn, context) is True
    assert parent.deadline_at is None and parent.attempt == history.claim_attempt == claim.attempt
    assert parent.current_segment_sequence == segment.sequence == history.sequence == 1
    assert parent.ownership_generation == segment.generation == history.generation == 1
    assert parent.session_operation_id == segment.session_operation_id == history.session_operation_id == context.fence.operation_id
    assert history.session_operation_lease_token == context.fence.lease_token
    assert history.session_operation_epoch == context.fence.operation_epoch
    assert history.owner_instance_id == segment.claim_owner_instance_id == "test-owner"
    assert (restore_utc(segment.deadline_at) - restore_utc(segment.started_at)).total_seconds() == size_segment(policy.actions.session_sql, policy.segment)
    assert (restore_utc(segment.cooperative_at) - restore_utc(segment.started_at)).total_seconds() == min(policy.segment.quantum, size_segment(policy.actions.session_sql, policy.segment) - policy.segment.watermark)
    assert restore_utc(segment.started_at) > oldest + timedelta(days=1)


@pytest.mark.parametrize("field", ["attempt", "claim_token", "segment_sequence", "ownership_generation", "session_id"])
def test_stale_claim_refuses_without_any_row_or_fence_change(operation_store, field):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    changes = dict(attempt=claim.attempt + 1, claim_token="other-token",
        segment_sequence=claim.segment_sequence + 1, ownership_generation=claim.ownership_generation + 1,
        session_id=uuid4())
    bad = replace(claim, **{field: changes[field]})
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        start_segment(repository, bad)
    assert _admission_rows(engine) == before


@pytest.mark.parametrize("change", ["owner", "provider", "archived", "transferred", "expired", "policy"])
def test_start_refuses_current_custody_or_policy_substitution(operation_store, change):
    engine, repository, _, _, sid = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    args = {}
    if change in ("archived", "transferred", "expired"):
        with engine.begin() as conn:
            now = database_now(conn)
            if change == "archived":
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=now))
            elif change == "transferred":
                from tests.fixtures.identities import ensure_test_identity
                ensure_test_identity(conn, identity_id="bob")
                conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(user_id="bob"))
            else:
                conn.execute(update(segments).values(claim_expires_at=now - timedelta(seconds=1)))
    elif change == "owner":
        args["owner_instance_id"] = "other-owner"
    elif change == "provider":
        args["auth_provider_type"] = "entra"
    else:
        args["segment_policy"] = minimal_continuation_policy().segment.model_copy(update={"nominal": 301.0})
    before = _admission_rows(engine)
    expected = AuditIntegrityError if change == "policy" else ComposerOperationPreconditionRefused if change in ("provider", "archived", "transferred") else ComposerOperationFenceLost
    with pytest.raises(expected):
        start_segment(repository, claim, **args)
    assert _admission_rows(engine) == before


def test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start(operation_store):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (old,) = authority.claim_segment(limit=1)
    assert authority.claim_segment(limit=1) == ()
    with engine.begin() as conn:
        conn.execute(update(segments).values(claim_expires_at=database_now(conn) - timedelta(seconds=1)))
    (new,) = authority.claim_segment(limit=1)
    assert new.attempt == old.attempt + 1 and new.claim_token != old.claim_token
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        start_segment(repository, old)
    assert _admission_rows(engine) == before
    assert start_segment(repository, new).fence.session_id == str(new.session_id)


@pytest.mark.parametrize("changed", ["logical_hash", "logical_kind", "logical_base", "logical_original", "action", "policy_pointer", "checkpoint"])
def test_valid_shaped_snapshot_and_segment_substitutions_are_refused(operation_store, changed):
    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    with engine.begin() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        if changed.startswith("logical_"):
            root = decode_continuation_snapshot(stored.payload_json)
            replacement_id = str(uuid4())
            values = {"logical_hash": {"request_hash": "b" * 64}, "logical_kind": {"kind": "compose_recompose"},
                "logical_base": {"base_state_id": replacement_id}, "logical_original": {"original_user_message_id": replacement_id}}
            bad = root.model_copy(update={"logical": root.logical.model_copy(update=values[changed]),
                "mutation_ceiling": root.mutation_ceiling.model_copy(update={"request_id": replacement_id}) if changed == "logical_original" else root.mutation_ceiling})
            encoded = encode_continuation_snapshot(bad).encode("utf-8")
            assert decode_continuation_snapshot(encoded.decode("utf-8")) == bad
            conn.execute(update(chunks).where(chunks.c.chunk_key == f"snapshot:{stored.checkpoint_id}").values(
                payload=encoded, byte_length=len(encoded), sha256=sha256(encoded).hexdigest()))
        elif changed == "action":
            conn.execute(update(segments).values(next_action_json=minimal_continuation_policy().actions.primary.model_dump_json()))
        elif changed == "policy_pointer":
            conn.execute(update(segments).values(policy_sha256="b" * 64))
        else:
            conn.execute(update(segments).values(checkpoint_id=str(uuid4())))
    before = _admission_rows(engine)
    with pytest.raises(AuditIntegrityError):
        start_segment(repository, claim)
    assert _admission_rows(engine) == before


def test_cancelled_v2_can_start_cleanup_but_business_guard_still_refuses(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, record = queued(operation_store)
    with engine.begin() as conn:
        conn.execute(update(jobs).values(cancel_requested_at=database_now(conn)))
    assert authority.claim_next(limit=1) == () and authority.list_expired_queued(limit=1) == ()
    failure = ComposerOperationError(http_status=500, failure_code="worker_lost", error_type=None,
        body={"detail": "controlled legacy settlement"}, diagnostic_id=None)
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority.settle_unstarted(None, session_id=sid, operation_id=record.operation_id, failure=failure)
    assert _admission_rows(engine) == before
    (claim,) = authority.claim_segment(limit=1)
    context = start_segment(repository, claim)
    with engine.begin() as conn:
        with pytest.raises(ComposerOperationCancelledDuringTurn):
            require_continuation_mutation(conn, context)
        assert require_continuation_mutation(conn, context, allow_cancel=True) is True
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority.settle_unstarted(None, session_id=sid, operation_id=record.operation_id, failure=failure)
    with pytest.raises(ComposerOperationFenceLost):
        repository.start_composer_async_operation(claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local")
    assert authority.list_expired_running(limit=1) == ()
    assert _admission_rows(engine) == before
    repository.release(context)
    assert authority.list_expired_running(limit=1) == ()
    with engine.begin() as conn:
        conn.execute(update(sessions_table).where(sessions_table.c.id == str(sid)).values(archived_at=database_now(conn)))
    before = _admission_rows(engine)
    with pytest.raises(ComposerOperationFenceLost):
        authority._settle_recovery(session_id=sid, operation_id=record.operation_id, failure=failure,
            settled_by="settle_lost_inactive_session", recovery_context=None, authoritative_failure=False)
    assert _admission_rows(engine) == before


def test_history_insert_failure_rolls_back_actual_fence_and_segment_start(operation_store):
    engine, repository, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    (claim,) = authority.claim_segment(limit=1)
    before = _admission_rows(engine)
    injected = RuntimeError("controlled post-history insert failure")
    def fail_after_insert(conn, cursor, statement, parameters, context, executemany):
        if context.compiled is not None and context.compiled.isinsert and context.compiled.statement.table.name == attempts.name:
            raise injected
    event.listen(engine, "after_cursor_execute", fail_after_insert)
    try:
        with pytest.raises(RuntimeError) as caught:
            start_segment(repository, claim)
        assert caught.value is injected
    finally:
        event.remove(engine, "after_cursor_execute", fail_after_insert)
    assert _admission_rows(engine) == before
    assert start_segment(repository, claim).fence.session_id == str(claim.session_id)
```

`entra` was checked against the actual `AuthProviderType` literal and is a valid browser provider distinct from the fixture's `local`, so this test reaches the existing ownership refusal. The old-admission-clock seam affects admission's clock binding only; actual start uses unmodified SQL database time. It avoids an illegal UPDATE of the trigger-protected immutable `created_at`. The full code intentionally has no fixture-wide identity normalizer.

Add this PostgreSQL collector using explicit aliases for every shared test. Importing the actual `operation_store` and target under same-name aliases gives real isolated databases, independent SQL connections and the existing bounded cached target. No shared helper is copied or weakened.

```python
import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_fresh_start_has_exact_history_snapshot_and_independent_clocks as test_fresh_start_has_exact_history_snapshot_and_independent_clocks,
    test_stale_claim_refuses_without_any_row_or_fence_change as test_stale_claim_refuses_without_any_row_or_fence_change,
    test_start_refuses_current_custody_or_policy_substitution as test_start_refuses_current_custody_or_policy_substitution,
    test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start as test_expired_unstarted_claim_is_replaced_but_old_claim_cannot_start,
    test_valid_shaped_snapshot_and_segment_substitutions_are_refused as test_valid_shaped_snapshot_and_segment_substitutions_are_refused,
    test_cancelled_v2_can_start_cleanup_but_business_guard_still_refuses as test_cancelled_v2_can_start_cleanup_but_business_guard_still_refuses,
    test_history_insert_failure_rolls_back_actual_fence_and_segment_start as test_history_insert_failure_rolls_back_actual_fence_and_segment_start,
)
pytestmark = pytest.mark.testcontainer
```

- [ ] Extend this module with the actual independent-process claimant below, then add its explicit same-name collector alias. It supplies concurrency evidence from real independent engines/processes, not a fake repository or SQLite-only lock proof:

```python
def _claim_from_process(url: str, owner: str, rendezvous):
    from elspeth.web.sessions.engine import create_session_engine
    engine = create_session_engine(url)
    def after_hint(conn, cursor, statement, parameters, context, executemany):
        compiled = context.compiled
        if compiled is not None and compiled.statement.is_select:
            selected = compiled.statement
            if tuple(column.name for column in selected.selected_columns) == ("session_id", "operation_id", "sequence") and selected.get_final_froms() == [segments]:
                rendezvous.wait(timeout=10)
    event.listen(engine, "after_cursor_execute", after_hint)
    try:
        return ComposerAsyncOperationAuthority(engine, owner_instance_id=owner, claim_lease_seconds=30).claim_segment(limit=1)
    finally:
        event.remove(engine, "after_cursor_execute", after_hint)
        engine.dispose()


def test_independent_processes_cannot_both_claim_one_segment(operation_store):
    import multiprocessing
    from concurrent.futures import ProcessPoolExecutor
    engine, _, _, _, _ = operation_store
    queued(operation_store)
    url = engine.url.render_as_string(hide_password=False)
    context = multiprocessing.get_context("spawn")
    with context.Manager() as manager:
        rendezvous = manager.Barrier(2)
        with ProcessPoolExecutor(max_workers=2, mp_context=context) as pool:
            results = list(pool.map(_claim_from_process, [url, url], ["test-owner", "peer-owner"], [rendezvous, rendezvous]))
    assert sorted(len(result) for result in results) == [0, 1]
    with engine.connect() as conn:
        parent = conn.execute(select(jobs)).one()
        segment = conn.execute(select(segments)).one()
        assert parent.attempt == 1
        assert segment.claim_token == next(result[0].claim_token for result in results if result)
        assert conn.execute(select(attempts)).all() == []
```

The caller that wins only CLAIMS; it does not start under an unregistered PostgreSQL owner. The exact outer SQL hint shape is positively observed at the process rendezvous; both contenders must arrive or the test fails. Add the following complete stale-hint/order/retained-sizing tests to the same module and their explicit same-name PostgreSQL collector aliases. The pause is AFTER the hint connection has closed, before the actual session lock, so SQLite readers cannot deadlock the controlled second writer.

```python
@pytest.mark.parametrize("changed", ["claimed", "delayed"])
def test_stale_hint_is_rechecked_under_the_actual_session_lock(operation_store, monkeypatch, changed):
    from contextlib import contextmanager
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event
    from elspeth.web.coordination import composer_operation_authority as module
    engine, _, _, _, _ = operation_store
    authority, _ = queued(operation_store)
    reached, resume = Event(), Event()
    actual_lock = module.locked_session_transaction
    @contextmanager
    def pause_after_hint(selected_engine, session_id):
        if not reached.is_set():
            reached.set()
            assert resume.wait(10), "controlled hint was not released"
        with actual_lock(selected_engine, session_id) as conn:
            yield conn
    monkeypatch.setattr(module, "locked_session_transaction", pause_after_hint)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(authority.claim_segment, limit=1)
        try:
            assert reached.wait(10), "actual outer hint never reached the lock seam"
            if changed == "claimed":
                peer = ComposerAsyncOperationAuthority(engine, owner_instance_id="peer-owner", claim_lease_seconds=30)
                assert len(peer.claim_segment(limit=1)) == 1
            else:
                with engine.begin() as conn:
                    conn.execute(update(segments).values(next_eligible_at=database_now(conn) + timedelta(minutes=1)))
            before = _admission_rows(engine)
        finally:
            resume.set()
        assert pending.result(timeout=10) == ()
    assert _admission_rows(engine) == before


def test_unexpired_oldest_claim_does_not_consume_limit_one(operation_store):
    engine, repository, _, _, sid = operation_store
    authority, first = queued(operation_store)
    (old_claim,) = authority.claim_segment(limit=1)
    other = repository.create_session_with_initial_fence(user_id="alice", title="Younger",
        auth_provider_type="local", owner_instance_id="test-owner", lease_seconds=30)
    second, fresh = admit(authority, other.id, lifecycle_version=2, continuation_enabled=True)
    assert fresh
    (new_claim,) = authority.claim_segment(limit=1)
    assert old_claim.operation_id == first.operation_id and new_claim.operation_id == second.operation_id
    assert new_claim.session_id == other.id and old_claim.session_id == sid
    with engine.connect() as conn:
        rows = conn.execute(select(segments).order_by(segments.c.operation_id)).all()
    assert {row.operation_id: row.claim_token for row in rows} == {
        first.operation_id: old_claim.claim_token, second.operation_id: new_claim.claim_token}


@pytest.mark.parametrize("physical", [500.0, 1e308])
def test_retained_complete_action_sizes_or_atomically_refuses_overflow(operation_store, physical):
    engine, repository, _, _, _ = operation_store
    original = minimal_continuation_policy()
    action = original.actions.session_sql.model_copy(update={"physical": physical})
    policy = original.model_copy(update={"actions": original.actions.model_copy(update={"session_sql": action})})
    authority, _ = queued(operation_store, policy=policy)
    (claim,) = authority.claim_segment(limit=1)
    before = _admission_rows(engine)
    if physical == 1e308:
        with pytest.raises(OverflowError):
            start_segment(repository, claim)
        assert _admission_rows(engine) == before
    else:
        start_segment(repository, claim)
        with engine.connect() as conn:
            row = conn.execute(select(segments)).one()
        assert (restore_utc(row.deadline_at) - restore_utc(row.started_at)).total_seconds() == size_segment(action, policy.segment)
        assert size_segment(action, policy.segment) > policy.segment.nominal


def test_expired_physical_lease_preserves_distributed_old_owner_liveness(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.sessions.models import web_instances_table
    engine, repository, _, _, sid = operation_store
    old = repository.acquire(session_id=sid, operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="test-owner", lease_seconds=30)
    queued(operation_store)
    claimant = ComposerAsyncOperationAuthority(engine, owner_instance_id="peer-owner", claim_lease_seconds=30)
    if engine.dialect.name == "postgresql":
        from tests.testcontainer.web.test_session_operation_fence_postgres import _register_instance
        _register_instance(engine, instance_id="peer-owner", lease_delta=timedelta(minutes=10))
    with engine.begin() as conn:
        now = database_now(conn)
        conn.execute(update(fences).where(fences.c.session_id == str(sid)).values(lease_expires_at=now - timedelta(seconds=1)))
    before = _admission_rows(engine)
    assert claimant.claim_segment(limit=1) == ()
    assert _admission_rows(engine) == before
    if engine.dialect.name == "postgresql":
        with engine.begin() as conn:
            changed = conn.execute(update(web_instances_table).where(web_instances_table.c.instance_id == "test-owner").values(
                lease_expires_at=database_now(conn) - timedelta(seconds=1)))
            assert changed.rowcount == 1
        (claim,) = claimant.claim_segment(limit=1)
        current = start_segment(repository, claim, owner_instance_id="peer-owner")
        assert current.fence.operation_epoch == old.fence.operation_epoch + 1
```

SQLite intentionally has no automatic expired-owner takeover positive; the fresh released-fence test supplies its positive. PostgreSQL explicitly requires an actual registered old instance to expire before its real repository start takes over. These bounded witnesses do not assert general starvation freedom. The delayed-row test is an explicit scheduling-fixture SQL mutation; the claimed-row variant uses a second actual claim authority. Omission controls remove the single shared expiry/next-eligible/current-parent relation, not an isolated redundant check while leaving the composed invariant intact.

The second request kind also needs this standalone actual-ingress positive in the unit module and its collector alias below:

```python
@pytest.mark.asyncio
async def test_recompose_start_retains_actual_original_ingress_without_inserting_again(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.sessions.models import chat_messages_table
    from elspeth.web.sessions.schemas import RecomposeRequest
    engine, repository, _, service, sid = operation_store
    context = repository.acquire(session_id=sid, operation_kind=SessionOperationKind.COMPOSE,
        owner_instance_id="test-owner", lease_seconds=30)
    original = await service.add_message(sid, "user", "Build", writer_principal="route_user_message", session_operation_context=context)
    repository.release(context)
    authority = _admission_v2_authority(operation_store)
    request = RecomposeRequest(operation_id=str(uuid4()), expected_user_message_id=original.id)
    record, fresh = admit(authority, sid, request, lifecycle_version=2, continuation_enabled=True)
    assert fresh and record.reserved_user_message_id == record.user_message_id == original.id
    with engine.connect() as conn:
        before = conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all()
    (claim,) = authority.claim_segment(limit=1)
    started = start_segment(repository, claim)
    with engine.connect() as conn:
        selected = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        assert decode_continuation_snapshot(selected.payload_json).logical.original_user_message_id == str(original.id)
        assert conn.execute(select(chat_messages_table).order_by(chat_messages_table.c.sequence_no)).all() == before
        assert require_continuation_mutation(conn, started) is True
```

#### Task 4 — C3.9 is an actual preactivation dependency

Keep C3.9 as a separately reviewable repository change, or explicitly release it with this owner. In either case it must land before exposure. Add exactly `composer_operation_id: str | None = None` to `_advance_exclusive_fence_on_connection` and insert this guard before its existing current-fence selection. Append exactly `composer_operation_id=claim.operation_id` in the new segment-start call. No unrelated acquisition receives the exception; preserve the entire old owner-liveness/takeover/CAS body.

```python
parent = conn.execute(select(composer_async_operations_table.c.operation_id).where(
    composer_async_operations_table.c.session_id == session_id_text,
    composer_async_operations_table.c.lifecycle_version == 2,
    composer_async_operations_table.c.status.in_(("queued", "running")),
).limit(1)).scalar_one_or_none()
if parent is not None and (operation_kind is not SessionOperationKind.COMPOSE or composer_operation_id != parent):
    raise SessionOperationConflictError
```

Replace C3.9's dependency on the unavailable two-fence test with this standalone test in Task 3's module, plus its same-name PostgreSQL collector alias:

```python
def test_logical_reservation_blocks_unrelated_writer_before_first_physical_start(operation_store):
    from elspeth.contracts.session_operation import SessionOperationKind
    from elspeth.web.coordination.repository import SessionOperationConflictError
    engine, repository, _, _, sid = operation_store
    authority, _ = queued(operation_store)
    before = _admission_rows(engine)
    with pytest.raises(SessionOperationConflictError):
        repository.acquire(session_id=sid, operation_kind=SessionOperationKind.ARCHIVE,
            owner_instance_id="test-owner", lease_seconds=30)
    assert _admission_rows(engine) == before
    (claim,) = authority.claim_segment(limit=1)
    assert start_segment(repository, claim).fence.session_id == str(sid)
```

Removing only that logical-parent guard must make this real ARCHIVE refusal fail. No public enablement may occur with the matching-parent start exception added but the reservation guard omitted. Ordinary first-start owner checks do not protect the interval between admission/physical leases.

The additional exact same-name imports in the initial PostgreSQL collector are:

```python
from tests.unit.web.coordination.test_composer_continuation_claim_start import (
    test_independent_processes_cannot_both_claim_one_segment as test_independent_processes_cannot_both_claim_one_segment,
    test_stale_hint_is_rechecked_under_the_actual_session_lock as test_stale_hint_is_rechecked_under_the_actual_session_lock,
    test_unexpired_oldest_claim_does_not_consume_limit_one as test_unexpired_oldest_claim_does_not_consume_limit_one,
    test_retained_complete_action_sizes_or_atomically_refuses_overflow as test_retained_complete_action_sizes_or_atomically_refuses_overflow,
    test_expired_physical_lease_preserves_distributed_old_owner_liveness as test_expired_physical_lease_preserves_distributed_old_owner_liveness,
    test_recompose_start_retains_actual_original_ingress_without_inserting_again as test_recompose_start_retains_actual_original_ingress_without_inserting_again,
    test_logical_reservation_blocks_unrelated_writer_before_first_physical_start as test_logical_reservation_blocks_unrelated_writer_before_first_physical_start,
)
```

### C3.4b: Extend reviewed later producer/start binding after F28.3

**Mandatory predecessors:** C2/C3.1–C3.4, C3.9; actual private-chunk storage `load_private_chunks`; the nominal `SelectedContinuationState`; and **the EXISTING F28.3 `next_continuation_action_policy`** in `docs/superpowers/plans/2026-10-21-composer-continuation-frames.md:9632–9694`. Implement the approved C3.5/C3.6 bodies together with the producer corrections below in this later increment; retain their complete recovery invariants. Those frame/title/end/tool-batch/registry producers must exist before importing that selector into storage. This ordering does not pull runtime producers into the first increment. C3.5/C3.6 methods remain private/unexposed until the producer checks below pass. Their first implementation GREEN uses the actual resume-start tests below, not the initial claim-only module or a promise that future C1b already covers them.

**Break the existing helper-import cycle explicitly:** immediately after initial C3.4, create ONLY the existing C1b module prefix (imports, actual `admit_continuation`, and `start_continuation`) from storage C1b.1's literal through the end of `start_continuation`. Defer its two-segment test body. That prefix uses only already implemented C1/C2/C3.3/C3.4 and supplies the existing F10/F28 test imports. Replace just `start_continuation` with the following complete retained-policy version. The prefix is a fixture-adapter prerequisite, not a claim of completed cold restoration.

```python
def start_continuation(repository, authority):
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local",
    )
    return ComposerOperationRunning(claim=claim, session_operation_context=context)
```

- [ ] Replace only the initial scope helper with this exact general queue scope; all current-parent, claim-expiry, SQL principal, available-fence, locked recheck and final CAS predicates remain unchanged:

```python
def _continuation_claim_scope() -> tuple[ColumnElement[bool], ...]:
    return (segments.c.state.in_(("queued", "interrupted")),)
```

- [ ] Rename ONLY Task 2's existing `_selected_segment_action` definition to `_initial_segment_action`, preserving its signature/body byte for byte; add the complete dispatcher below under the original `_selected_segment_action` name. First sequence/generation still traverses exact initializer reconstruction, including the queued/cursor-zero restrictions. Keep the full `_select_segment_start_on_connection` logical/request/checkpoint/clock policy checks unchanged. Later generations use the reviewed finite structural selector, actual complete SQL snapshot and its verified private chunks; this adds no semantic selector:

```python
def _selected_segment_action(
    conn: Connection, *, parent: Row[Any], segment: Row[Any],
    snapshot: StoredContinuationSnapshot, stack: ContinuationFrameStack,
    policy: ContinuationAdmissionPolicy,
) -> ContinuationActionPolicy:
    if segment.sequence == 1 and segment.generation == 1:
        return _initial_segment_action(
            conn, parent=parent, segment=segment, snapshot=snapshot, stack=stack, policy=policy,
        )
    from elspeth.web.composer.continuation_execution import SelectedContinuationState, next_continuation_action_policy
    if (parent.current_segment_sequence != segment.sequence or parent.ownership_generation != segment.generation
            or parent.checkpoint_id != snapshot.checkpoint_id or segment.checkpoint_id != snapshot.checkpoint_id):
        raise AuditIntegrityError("selected segment differs from its current SQL snapshot")
    private = load_private_chunks(conn, session_id=parent.session_id, operation_id=parent.operation_id, stack=stack)
    return next_continuation_action_policy(
        selected=SelectedContinuationState(snapshot.checkpoint_id, stack, private), policy=policy,
    )
```

- [ ] Add this COMPLETE helper in `sessions/continuation.py`. It runs after `append_complete_snapshot` in the SAME transaction, before any successor/recovery generation is exposed. It binds the actual retained root and private chunks, and checks the caller's purported allowance/policy before deriving the authoritative stored values:

```python
def _retained_successor_action(
    conn: Connection, *, session_id: str, operation_id: str,
    snapshot: StoredContinuationSnapshot, next_action_json: str | None,
    policy_sha256: str | None,
) -> tuple[str, str]:
    from elspeth.web.composer.continuation_execution import SelectedContinuationState, next_continuation_action_policy
    actual = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if actual != snapshot:
        raise ContinuationCASConflict("producer selected another retained continuation head")
    stack = decode_continuation_snapshot(actual.payload_json)
    policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    private = load_private_chunks(conn, session_id=session_id, operation_id=operation_id, stack=stack)
    expected = next_continuation_action_policy(
        selected=SelectedContinuationState(actual.checkpoint_id, stack, private), policy=policy,
    )
    if policy_sha256 is not None and policy_sha256 != stack.policy_hash:
        raise AuditIntegrityError("successor substituted another retained complete policy")
    if next_action_json is not None:
        try:
            supplied = ContinuationActionPolicy.model_validate_json(next_action_json, strict=True)
        except ValueError as exc:
            raise AuditIntegrityError("successor allowance is not its owned policy") from exc
        if supplied != expected:
            raise AuditIntegrityError("successor allowance differs from the actual selected retained action")
    return expected.model_dump_json(), stack.policy_hash
```

In `yield_segment_on_connection`, immediately after its `retained = append_complete_snapshot(...)`, insert:

```python
bound_action_json, bound_policy_hash = _retained_successor_action(
    conn, session_id=str(claim.session_id), operation_id=claim.operation_id, snapshot=retained,
    next_action_json=next_action_json, policy_sha256=policy_sha256,
)
```

Its successor INSERT must use exactly `next_action_json=bound_action_json, policy_sha256=bound_policy_hash`, replacing the two direct caller-string assignments. Keep every original pending-effect/head/handoff/history-revocation/parent CAS check. The prior transaction writes roll back on a rejected supplied allowance.

In repository `recover_composer_segment`, import `_retained_successor_action` from `sessions.continuation`. Immediately after its actual `retained = append_complete_snapshot(...)`, insert:

```python
bound_action_json, bound_policy_hash = _retained_successor_action(
    conn, session_id=str(session_id), operation_id=operation_id, snapshot=retained,
    next_action_json=None, policy_sha256=None,
)
```

In that method's EXISTING `update(composer_operation_segments_table)...values(...)`, add exactly these two keywords, retaining every existing predicate/assignment:

```python
next_action_json=bound_action_json,
policy_sha256=bound_policy_hash,
```

In `suspend_segment_on_connection`, immediately after its actual `retained = append_complete_snapshot(...)`, insert:

```python
bound_action_json, bound_policy_hash = _retained_successor_action(
    conn, session_id=str(claim.session_id), operation_id=claim.operation_id, snapshot=retained,
    next_action_json=None, policy_sha256=None,
)
```

Add those same two keywords to its EXISTING `update(segments)...values(...)`, retaining the actual retry clock, `next_eligible_at` and all other assignments. A retained snapshot can advance while the original segment action stays old. Preserve actual current/history generation checks, old-attempt revocation, original snapshot/logical request, parent generation update, released-owner versus live-owner distinction and no redispatch of unknown effects. Missing/invalid chunks or an unsupported closed phase fail the transaction; no current defaults are substituted.

- [ ] Add this real SQL two-fence test to NEW `tests/unit/web/coordination/test_composer_continuation_resume_start.py` and explicitly re-export it through the PostgreSQL collector. Its existing future F10 helpers have real ingress/title/accounting/SQL retention with only the SDK function controlled. They are **prerequisites**, not current APIs or a fake graph. `apply_title` produces the actual ready bootstrap; do not use C1b's current invalid `model_copy(ingress_phase="ready")` shortcut without its required bootstrap.

```python
import pytest
from datetime import timedelta
from sqlalchemy import select, update
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.continuation_execution import SelectedContinuationState, next_continuation_action_policy
from elspeth.web.composer.continuation_policy import decode_continuation_policy
from elspeth.web.sessions.composer_operations import ComposerOperationFenceLost
from elspeth.web.sessions.continuation import load_private_chunks, load_snapshot, require_continuation_mutation
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions import continuation as continuation_module
from elspeth.web.coordination import composer_operation_authority as operation_module
from tests.unit.web.composer.test_continuation_title import retained_title, apply_title
from tests.unit.web.coordination.test_composer_continuation_claim_start import start_segment
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as _operation_store
from tests.unit.web.coordination.test_composer_operation_authority import _admission_rows

operation_store = _operation_store


@pytest.mark.asyncio
@pytest.mark.parametrize("corrupt", ["none", "yield_action", "yield_policy", "start_action"])
async def test_actual_retained_title_handoff_restarts_under_bound_action_and_new_fence(operation_store, monkeypatch, corrupt):
    engine, repository, authority, _, sid = operation_store
    first, custody, binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None
    await apply_title(operation_store, first, custody, binding, observed, candidate)
    with engine.connect() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=first.claim.operation_id)
        root = decode_continuation_snapshot(stored.payload_json)
        private = load_private_chunks(conn, session_id=str(sid), operation_id=first.claim.operation_id, stack=root)
    assert root.is_handoff_safe
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    action = next_continuation_action_policy(selected=SelectedContinuationState(stored.checkpoint_id, root, private), policy=policy)
    if corrupt in ("yield_action", "yield_policy"):
        before = _admission_rows(engine)
        with pytest.raises(AuditIntegrityError):
            authority.commit_checkpoint_and_successor(first, expected_head=None,
                expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
                snapshot_json=stored.payload_json,
                next_action_json=policy.actions.primary.model_dump_json() if corrupt == "yield_action" else action.model_dump_json(),
                policy_sha256="b" * 64 if corrupt == "yield_policy" else root.policy_hash)
        assert _admission_rows(engine) == before
    receipt = authority.commit_checkpoint_and_successor(first, expected_head=None,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=stored.payload_json, next_action_json=action.model_dump_json(), policy_sha256=root.policy_hash)
    authority.verify_yield(receipt)
    repository.release(first.session_operation_context)
    (claim,) = authority.claim_segment(limit=1)
    if corrupt == "start_action":
        with engine.begin() as conn:
            conn.execute(update(segments).where(segments.c.sequence == 2).values(next_action_json=policy.actions.primary.model_dump_json()))
        before = _admission_rows(engine)
        with pytest.raises(AuditIntegrityError):
            start_segment(repository, claim)
        assert _admission_rows(engine) == before
        with engine.begin() as conn:
            conn.execute(update(segments).where(segments.c.sequence == 2).values(next_action_json=action.model_dump_json()))
    second = start_segment(repository, claim)
    assert claim.segment_sequence == 2 and claim.ownership_generation == 2
    assert second.fence != first.session_operation_context.fence and len(calls) == 1
    with engine.connect() as conn:
        actual = load_snapshot(conn, session_id=str(sid), operation_id=claim.operation_id)
        current = conn.execute(select(segments).where(segments.c.sequence == 2)).one()
        history = conn.execute(select(attempts).order_by(attempts.c.sequence)).all()
        assert require_continuation_mutation(conn, second) is True
        with pytest.raises(ComposerOperationFenceLost):
            require_continuation_mutation(conn, first.session_operation_context)
    assert actual.checkpoint_id == receipt.checkpoint_id
    assert current.next_action_json == action.model_dump_json() and current.policy_sha256 == root.policy_hash
    assert history[0].revoked_at is not None and history[0].outcome == "yielded"
    assert history[1].claim_attempt == claim.attempt and history[1].session_operation_epoch == second.fence.operation_epoch


def pending_title_selection(store, first, *, stale_policy):
    engine, _repository, _authority, _service, sid = store
    with engine.connect() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=first.claim.operation_id)
        root = decode_continuation_snapshot(stored.payload_json)
        private = load_private_chunks(conn, session_id=str(sid), operation_id=first.claim.operation_id, stack=root)
        old = conn.execute(select(segments).where(
            segments.c.session_id == str(sid), segments.c.operation_id == first.claim.operation_id,
            segments.c.sequence == first.claim.segment_sequence)).one()
    assert type(root.frames[-1]) is InitializeFrame
    assert root.frames[-1].ingress_phase == "title_pending" and root.frames[-1].bootstrap is not None
    policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
    selected = next_continuation_action_policy(
        selected=SelectedContinuationState(stored.checkpoint_id, root, private), policy=policy)
    assert old.next_action_json == policy.actions.session_sql.model_dump_json()
    assert old.policy_sha256 == root.policy_hash
    assert selected == policy.actions.title and selected != policy.actions.session_sql
    if stale_policy:
        # A separate valid-shaped SQL pointer corruption, never a new retained policy.
        wrong_hash = "a" * 64 if root.policy_hash != "a" * 64 else "b" * 64
        with engine.begin() as conn:
            changed = conn.execute(update(segments).where(
                segments.c.session_id == str(sid), segments.c.operation_id == first.claim.operation_id,
                segments.c.sequence == first.claim.segment_sequence).values(policy_sha256=wrong_hash))
            assert changed.rowcount == 1
    return root, private, selected


def assert_pending_title_restart(store, first, claim, second, root, private, action, calls):
    engine, _repository, _authority, _service, sid = store
    assert claim.segment_sequence == first.claim.segment_sequence
    assert claim.ownership_generation == first.claim.ownership_generation + 1
    assert second.fence != first.session_operation_context.fence and len(calls) == 1
    with engine.connect() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=claim.operation_id)
        current_root = decode_continuation_snapshot(stored.payload_json)
        current_private = load_private_chunks(conn, session_id=str(sid), operation_id=claim.operation_id, stack=current_root)
        current = conn.execute(select(segments).where(
            segments.c.session_id == str(sid), segments.c.operation_id == claim.operation_id)).one()
        history = conn.execute(select(attempts).where(
            attempts.c.session_id == str(sid), attempts.c.operation_id == claim.operation_id,
        ).order_by(attempts.c.generation)).all()
        assert require_continuation_mutation(conn, second) is True
        with pytest.raises(ComposerOperationFenceLost):
            require_continuation_mutation(conn, first.session_operation_context)
    assert current_root.frames == root.frames and current_private == private
    assert current_root.effect_frontier == root.effect_frontier
    assert current.next_action_json == action.model_dump_json() and current.policy_sha256 == root.policy_hash
    assert len(history) == 2 and history[0].outcome == "interrupted" and history[0].revoked_at is not None
    assert history[1].generation == claim.ownership_generation and history[1].claim_attempt == claim.attempt


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_policy", [False, True])
async def test_actual_recovery_restarts_same_sequence_with_new_generation_and_bound_action(operation_store, monkeypatch, stale_policy):
    engine, repository, authority, _, sid = operation_store
    first, _custody, _binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None and observed.response is not None
    root, private, action = pending_title_selection(operation_store, first, stale_policy=stale_policy)
    kwargs = dict(session_id=sid, operation_id=first.claim.operation_id,
        expected_generation=first.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
    before = _admission_rows(engine)
    assert repository.recover_composer_segment(**kwargs) is False
    assert _admission_rows(engine) == before
    repository.release(first.session_operation_context)
    assert repository.recover_composer_segment(**kwargs) is True
    assert repository.recover_composer_segment(**kwargs) is False
    with engine.connect() as conn:
        produced = conn.execute(select(segments).where(segments.c.operation_id == first.claim.operation_id)).one()
    assert produced.next_action_json == action.model_dump_json() and produced.policy_sha256 == root.policy_hash
    (claim,) = authority.claim_segment(limit=1)
    second = start_segment(repository, claim)
    try:
        assert_pending_title_restart(operation_store, first, claim, second, root, private, action, calls)
    finally:
        repository.release(second)


@pytest.mark.asyncio
@pytest.mark.parametrize("stale_policy", [False, True])
async def test_live_owner_suspension_restarts_at_retry_clock_with_bound_title_action(operation_store, monkeypatch, stale_policy):
    engine, repository, authority, _, _sid = operation_store
    first, _custody, _binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None and observed.response is not None
    root, private, action = pending_title_selection(operation_store, first, stale_policy=stale_policy)
    with engine.connect() as conn:
        clock = [continuation_module.database_now(conn)]

    def observed_database_now(conn):
        assert conn.engine is engine
        return clock[0]

    monkeypatch.setattr(continuation_module, "database_now", observed_database_now)
    monkeypatch.setattr(operation_module, "database_now", observed_database_now)
    monkeypatch.setattr(type(repository), "_database_now", staticmethod(observed_database_now))
    receipt = authority.suspend_segment(first, reason="owner_unavailable", retry_delay_seconds=1)
    authority.verify_recovery(receipt)
    assert receipt.next_eligible_at == clock[0] + timedelta(seconds=1)
    with engine.connect() as conn:
        produced = conn.execute(select(segments).where(segments.c.operation_id == first.claim.operation_id)).one()
        with pytest.raises(ComposerOperationFenceLost):
            require_continuation_mutation(conn, first.session_operation_context)
    assert produced.next_action_json == action.model_dump_json() and produced.policy_sha256 == root.policy_hash
    repository.release(first.session_operation_context)
    clock[0] = receipt.next_eligible_at - timedelta(microseconds=1)
    assert authority.claim_segment(limit=1) == ()
    clock[0] = receipt.next_eligible_at
    (claim,) = authority.claim_segment(limit=1)
    second = start_segment(repository, claim)
    try:
        assert_pending_title_restart(operation_store, first, claim, second, root, private, action, calls)
    finally:
        repository.release(second)
```

The yield variants reject a different valid retained allowance or hash with all registered rows unchanged, then execute the valid positive. The start variant changes only the real successor allowance and proves no partial fence/start before restoring that mutation. The two title-pending tests do **not** call `apply_title`: their old segment action is actual admission SQL, their current complete retained frame selects actual title, and the retained physical response/audit remains in the verified unchanged private chunks. Recovery uses the real released-owner producer; suspension uses its real live-owner public facade and the existing three-owner controlled database-clock seam. Neither invokes a second SDK call or dispatches an unknown effect.

Run semantic omissions independently, restoring each before the next: (a) delete only recovery's `next_action_json=bound_action_json` → recovery's normal case must fail the produced pointer assertion; (b) delete only its `policy_sha256=bound_policy_hash` → its `stale_policy=True` case must fail the policy assertion; (c/d) delete those respective assignments only in suspension → the corresponding suspension case must fail. The controlled stale hash is necessary to distinguish a missing policy assignment because an immutable retained admission policy normally keeps that hash equal. It changes only the old SQL pointer, never retained policy/snapshot/history. Delete only yield's bound-action comparison or bound-policy comparison → its existing no-write refusal must fail; delete only start's stored-action equality → `start_action` must fail. All unchanged positives must pass natively. Collection/import/clock setup failure is not a semantic omission witness.

Create `tests/testcontainer/web/test_composer_continuation_resume_start_postgres.py` as the exact collector:

```python
import pytest
from tests.testcontainer.web.test_composer_operations_postgres import operation_postgres_target as operation_postgres_target
from tests.testcontainer.web.test_composer_operations_postgres import operation_store as operation_store
from tests.unit.web.coordination.test_composer_continuation_resume_start import (
    test_actual_retained_title_handoff_restarts_under_bound_action_and_new_fence as test_actual_retained_title_handoff_restarts_under_bound_action_and_new_fence,
    test_actual_recovery_restarts_same_sequence_with_new_generation_and_bound_action as test_actual_recovery_restarts_same_sequence_with_new_generation_and_bound_action,
    test_live_owner_suspension_restarts_at_retry_clock_with_bound_title_action as test_live_owner_suspension_restarts_at_retry_clock_with_bound_title_action,
)
pytestmark = pytest.mark.testcontainer
```

#### Task 6 — Focused commands and release limits

- [ ] Use the existing venv, BOTH source roots and project-private TMPDIR/log/cache/basetemp. First run the standalone unit module plus affected existing authority/facet tests with `-n 0`; after capacity/cached-target checks run the new PG collector with `-m testcontainer -n 0`. Record actual child exits and read complete logs. Do not use a whole default suite as PostgreSQL evidence.
- [ ] For the later extension, run `test_composer_continuation_resume_start.py` and its PG collector AND the unchanged initial claim/start module/collector before C1b/C4. The general extension must preserve the original first-start initializer-corruption refusals. Keep failure/positive controls source-bound. Run Ruff/format on the actual touched paths, focused mypy on the four production modules, and coordinated actual contracts/whole-tree gates whose inputs moved. Root alone stages/commits after fresh SPEC and separate Astra review. No settings/provider/worker activation follows merely from these storage tests.

```bash
cd "$COMPOSER_WORKTREE" && PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src" "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/coordination/test_composer_continuation_claim_start.py tests/unit/web/coordination/test_composer_operation_authority.py tests/unit/web/coordination/test_session_mutation_facets.py > "$COMPOSER_LOG_DIR/c3-claim-start.log" 2>&1
composer_exit=$?
printf 'exit=%s\n' "$composer_exit"
cat "$COMPOSER_LOG_DIR/c3-claim-start.log"
test "$composer_exit" -eq 0
```

Expected GREEN is native exit 0 after the semantic RED/control cycle. The PG command uses the exact new collector path and `-m testcontainer -n 0`, not the entire PG corpus. Environment paths must be supplied from the actual checkout/venv and preflight, not committed home paths.

### C3.5: Commit handoff, predecessor revocation and one successor atomically

**Files:** Extend `src/elspeth/web/sessions/continuation.py`; expose one transaction-owning authority method in `src/elspeth/web/coordination/composer_operation_authority.py`; extend `tests/unit/web/coordination/test_composer_operation_authority.py`.

- [ ] **Step 1: Add the complete yield receipt and transaction body.** Add `composition_states_table as states` and `composer_continuation_effects_table as effects` imports. The C1a codec's `is_handoff_safe` is required; event attribution never substitutes for complete frame validation.

```python
@dataclass(frozen=True, slots=True)
class ComposerSegmentYieldReceipt:
    session_id: UUID
    operation_id: str
    predecessor_sequence: int
    predecessor_generation: int
    successor_sequence: int
    successor_generation: int
    checkpoint_id: str
    cursor: int


def yield_segment_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, expected_head: UUID | None,
    expected_checkpoint_id: str, expected_cursor: int, snapshot_json: str,
    next_action_json: str, policy_sha256: str,
) -> ComposerSegmentYieldReceipt:
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
    claim = running.claim
    if claim.segment_sequence is None or claim.ownership_generation is None:
        raise ValueError("yield requires a continuation segment")
    require_continuation_mutation(conn, running.session_operation_context)
    frame = decode_continuation_snapshot(snapshot_json)
    if not frame.is_handoff_safe:
        raise AuditIntegrityError("handoff contains live or unsettled work")
    actual_head = conn.execute(select(states.c.id).where(states.c.session_id == str(claim.session_id)).order_by(states.c.version.desc()).limit(1)).scalar_one_or_none()
    if actual_head != (None if expected_head is None else str(expected_head)):
        raise ContinuationCASConflict("composition head changed before handoff")
    pending = conn.execute(select(effects.c.effect_key).where(
        effects.c.session_id == str(claim.session_id), effects.c.operation_id == claim.operation_id,
        effects.c.disposition.in_(("dispatched", "unknown")),
    ).limit(1)).one_or_none()
    if pending is not None:
        raise AuditIntegrityError("handoff contains an unresolved dispatched effect")
    retained = append_complete_snapshot(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
        snapshot_json=snapshot_json,
        event_batch=(ContinuationEvent("yielded", '{"version":2}'),), handoff=True,
    )
    now = database_now(conn)
    old = conn.execute(update(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
        segments.c.claim_token == claim.claim_token, segments.c.state == "running",
    ).values(state="yielded", checkpoint_id=retained.checkpoint_id,
             claim_token=None, claim_owner_instance_id=None, claim_expires_at=None))
    if old.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    revoked = conn.execute(update(attempts).where(
        attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
        attempts.c.sequence == claim.segment_sequence, attempts.c.generation == claim.ownership_generation,
        attempts.c.revoked_at.is_(None),
    ).values(revoked_at=now, outcome="yielded"))
    if revoked.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    successor_sequence = claim.segment_sequence + 1
    successor_generation = claim.ownership_generation + 1
    conn.execute(insert(segments).values(
        session_id=str(claim.session_id), operation_id=claim.operation_id,
        sequence=successor_sequence, predecessor_sequence=claim.segment_sequence,
        state="queued", generation=successor_generation, enqueued_at=now, next_eligible_at=now,
        checkpoint_id=retained.checkpoint_id, next_action_json=next_action_json, policy_sha256=policy_sha256,
    ))
    parent = conn.execute(update(jobs).where(
        jobs.c.session_id == str(claim.session_id), jobs.c.operation_id == claim.operation_id,
        jobs.c.lifecycle_version == 2, jobs.c.status == "running",
        jobs.c.current_segment_sequence == claim.segment_sequence,
        jobs.c.ownership_generation == claim.ownership_generation,
        jobs.c.cancel_requested_at.is_(None),
    ).values(
        current_segment_sequence=successor_sequence, ownership_generation=successor_generation,
        phase="continuation_queued", status_revision=jobs.c.status_revision + 1, updated_at=now,
    ))
    if parent.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    return ComposerSegmentYieldReceipt(
        claim.session_id, claim.operation_id, claim.segment_sequence, claim.ownership_generation,
        successor_sequence, successor_generation, retained.checkpoint_id, retained.cursor,
    )


def verify_yield_on_connection(conn: Connection, receipt: ComposerSegmentYieldReceipt) -> None:
    parent = conn.execute(select(jobs).where(jobs.c.session_id == str(receipt.session_id), jobs.c.operation_id == receipt.operation_id)).one()
    previous = conn.execute(select(segments).where(
        segments.c.session_id == str(receipt.session_id), segments.c.operation_id == receipt.operation_id,
        segments.c.sequence == receipt.predecessor_sequence,
    )).one()
    successor = conn.execute(select(segments).where(
        segments.c.session_id == str(receipt.session_id), segments.c.operation_id == receipt.operation_id,
        segments.c.sequence == receipt.successor_sequence,
    )).one()
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == str(receipt.session_id), attempts.c.operation_id == receipt.operation_id,
        attempts.c.sequence == receipt.predecessor_sequence, attempts.c.generation == receipt.predecessor_generation,
    )).one()
    snapshot = load_snapshot(conn, session_id=str(receipt.session_id), operation_id=receipt.operation_id)
    if (
        parent.status != "running" or parent.phase != "continuation_queued"
        or parent.current_segment_sequence != receipt.successor_sequence
        or parent.ownership_generation != receipt.successor_generation
        or previous.state != "yielded" or history.revoked_at is None or history.outcome != "yielded"
        or successor.state != "queued" or successor.predecessor_sequence != receipt.predecessor_sequence
        or successor.generation != receipt.successor_generation
        or snapshot.checkpoint_id != receipt.checkpoint_id or snapshot.cursor != receipt.cursor
    ):
        raise AuditIntegrityError("durable yield readback mismatch")
```

- [ ] **Step 2: Add transaction-owning authority wrappers.** They retain all SQL/private payloads inside the session authority; returned receipt carries no graph, request, secret or tool result bytes.

```python
def commit_checkpoint_and_successor(
    self, running: ComposerOperationRunning, *, expected_head: UUID | None,
    expected_checkpoint_id: str, expected_cursor: int, snapshot_json: str,
    next_action_json: str, policy_sha256: str,
) -> ComposerSegmentYieldReceipt:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return yield_segment_on_connection(
            conn, running, expected_head=expected_head, expected_checkpoint_id=expected_checkpoint_id,
            expected_cursor=expected_cursor, snapshot_json=snapshot_json,
            next_action_json=next_action_json, policy_sha256=policy_sha256,
        )


def verify_yield(self, receipt: ComposerSegmentYieldReceipt) -> None:
    if type(receipt) is not ComposerSegmentYieldReceipt:
        raise TypeError("yield verifier requires its nominal durable receipt")
    with locked_session_transaction(self._engine, str(receipt.session_id)) as conn:
        verify_yield_on_connection(conn, receipt)
```

- [ ] **Step 3: GREEN:** Run C1b's real two-segment tests after they are added, then the command below, requiring native exit 0. C4b repeats the actual yield transaction on serial PostgreSQL, checking successor rows and revocation of the old attempt.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/coordination/test_composer_operation_authority.py tests/unit/web/coordination/test_session_mutation_facets.py >"$COMPOSER_LOG_DIR/c3-yield.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-yield.log"
test "$composer_exit" -eq 0
```

### C3.6: Recover an interrupted sequence with a new generation

**Files:** Add this method to `_SessionOperationAuthorityRepository` in `src/elspeth/web/coordination/repository.py`; use its current process-owner liveness policy for PostgreSQL. No predecessor marked yielded can enter this function successfully.

- [ ] **Step 1: Add the complete repository recovery method.** A new generation reuses the same sequence and complete state. It does not create a successor or an artificial model response. Import `load_snapshot`, `append_complete_snapshot`, `ContinuationEvent` and the existing segment/attempt tables.

```python
def recover_composer_segment(
    self, *, session_id: UUID, operation_id: str, expected_generation: int,
    actor_user_id: str, auth_provider_type: str,
) -> bool:
    with self._locked_transaction(str(session_id)) as conn:
        _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
        parent = conn.execute(select(composer_async_operations_table).where(
            composer_async_operations_table.c.session_id == str(session_id),
            composer_async_operations_table.c.operation_id == operation_id,
            composer_async_operations_table.c.lifecycle_version == 2,
        )).one_or_none()
        if parent is None or parent.status not in ("queued", "running") or parent.ownership_generation != expected_generation:
            return False
        segment = conn.execute(select(composer_operation_segments_table).where(
            composer_operation_segments_table.c.session_id == str(session_id),
            composer_operation_segments_table.c.operation_id == operation_id,
            composer_operation_segments_table.c.sequence == parent.current_segment_sequence,
        )).one()
        if segment.state != "running" or segment.generation != expected_generation:
            return False
        now = self._database_now(conn)
        fence = self._select_fence(conn, session_id=str(session_id))
        if fence is None:
            raise AuditIntegrityError("continuation session fence missing")
        if fence.released_at is None:
            if _ensure_utc(fence.lease_expires_at) > now:
                return False
            if not self._expired_owner_allows_takeover(conn, owner_instance_id=fence.owner_instance_id, database_now=now):
                return False
        stored = load_snapshot(conn, session_id=str(session_id), operation_id=operation_id)
        retained = append_complete_snapshot(
            conn, session_id=str(session_id), operation_id=operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
            snapshot_json=stored.payload_json,
            event_batch=(ContinuationEvent("recovered", '{"version":2}'),), handoff=False,
        )
        changed = conn.execute(update(composer_segment_attempts_table).where(
            composer_segment_attempts_table.c.session_id == str(session_id),
            composer_segment_attempts_table.c.operation_id == operation_id,
            composer_segment_attempts_table.c.sequence == segment.sequence,
            composer_segment_attempts_table.c.generation == expected_generation,
            composer_segment_attempts_table.c.revoked_at.is_(None),
        ).values(revoked_at=now, outcome="interrupted"))
        if changed.rowcount != 1:
            raise AuditIntegrityError("continuation recovery lost its immutable attempt")
        conn.execute(update(composer_operation_segments_table).where(
            composer_operation_segments_table.c.session_id == str(session_id),
            composer_operation_segments_table.c.operation_id == operation_id,
            composer_operation_segments_table.c.sequence == segment.sequence,
            composer_operation_segments_table.c.generation == expected_generation,
        ).values(
            state="interrupted", generation=expected_generation + 1, checkpoint_id=retained.checkpoint_id,
            claim_token=None, claim_owner_instance_id=None, claim_expires_at=None,
            enqueued_at=now, next_eligible_at=now,
        ))
        conn.execute(update(composer_async_operations_table).where(
            composer_async_operations_table.c.session_id == str(session_id),
            composer_async_operations_table.c.operation_id == operation_id,
            composer_async_operations_table.c.ownership_generation == expected_generation,
        ).values(ownership_generation=expected_generation + 1, phase="reconciling", updated_at=now,
                 status_revision=composer_async_operations_table.c.status_revision + 1))
        return True
```

Expose this exact method on `SessionOperationAuthority`. Resumption first reconciles all prepared/dispatched/unknown effects; merely obtaining a new fence never makes an unknown effect safe to retry. An unavailable owner persists `retry_waiting` with a future `next_eligible_at`; queue scheduling does not retain original parent age.

- [ ] Add the nominal recovery receipt and live-owner suspension transaction below to `sessions/continuation.py`. This is distinct from a safe successor: suspension preserves the same sequence, advances ownership generation, retains unknown/pending facts, and schedules a bounded retry. The caller must join all actual required work before this transaction and before releasing its existing lease. It never declares unresolved work safe to redispatch.

```python
@dataclass(frozen=True, slots=True)
class ComposerSegmentRecoveryReceipt:
    session_id: UUID
    operation_id: str
    sequence: int
    previous_generation: int
    generation: int
    checkpoint_id: str
    cursor: int
    next_eligible_at: datetime


def suspend_segment_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, reason: str, retry_delay_seconds: float | None = None,
    released_adoption_owner: bool = False,
) -> ComposerSegmentRecoveryReceipt:
    from sqlalchemy import func

    if retry_delay_seconds is not None and (type(retry_delay_seconds) not in (int, float)
            or not math.isfinite(retry_delay_seconds) or not 1 <= retry_delay_seconds <= 60):
        raise ValueError("recovery retry delay must be finite between one and sixty seconds")
    if reason not in ("adoption_failed", "required_work_failed", "owner_unavailable", "unknown_effect"):
        raise ValueError("unsupported continuation recovery reason")
    if released_adoption_owner:
        if reason != "adoption_failed":
            raise ValueError("released-owner recovery is restricted to failed adoption")
        require_failed_adoption_recovery(conn, running)
    else:
        require_continuation_mutation(conn, running.session_operation_context,
                                      allow_cancel=True, allow_expired_segment=True)
    claim = running.claim
    if claim.segment_sequence is None or claim.ownership_generation is None:
        raise ValueError("suspension requires a continuation segment")
    if retry_delay_seconds is None:
        prior = conn.execute(select(func.count()).select_from(attempts).where(
            attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
            attempts.c.sequence == claim.segment_sequence, attempts.c.outcome == "interrupted",
            attempts.c.revoked_at.is_not(None),
        )).scalar_one()
        if type(prior) is not int or prior < 0:
            raise AuditIntegrityError("recovery retry history returned a noncanonical count")
        retry_delay_seconds = (1.0, 2.0, 4.0, 8.0, 16.0, 30.0)[min(prior, 5)]
    now = database_now(conn)
    next_eligible_at = now + timedelta(seconds=retry_delay_seconds)
    stored = load_snapshot(conn, session_id=str(claim.session_id), operation_id=claim.operation_id)
    retained = append_complete_snapshot(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=stored.payload_json,
        event_batch=(ContinuationEvent("recovered", canonical_json({"reason": reason, "same_sequence": claim.segment_sequence})),),
        handoff=False,
    )
    previous = conn.execute(update(attempts).where(
        attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
        attempts.c.sequence == claim.segment_sequence, attempts.c.generation == claim.ownership_generation,
        attempts.c.session_operation_id == running.session_operation_context.fence.operation_id,
        attempts.c.revoked_at.is_(None),
    ).values(revoked_at=now, outcome="interrupted"))
    segment = conn.execute(update(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
        segments.c.state == "running", segments.c.claim_token == claim.claim_token,
    ).values(state="interrupted", generation=claim.ownership_generation + 1,
             checkpoint_id=retained.checkpoint_id, enqueued_at=now, next_eligible_at=next_eligible_at,
             claim_token=None, claim_owner_instance_id=None, claim_expires_at=None))
    parent = conn.execute(update(jobs).where(
        jobs.c.session_id == str(claim.session_id), jobs.c.operation_id == claim.operation_id,
        jobs.c.lifecycle_version == 2, jobs.c.status == "running",
        jobs.c.current_segment_sequence == claim.segment_sequence,
        jobs.c.ownership_generation == claim.ownership_generation,
    ).values(ownership_generation=claim.ownership_generation + 1, phase="retry_waiting",
             updated_at=now, status_revision=jobs.c.status_revision + 1))
    if previous.rowcount != 1 or segment.rowcount != 1 or parent.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    return ComposerSegmentRecoveryReceipt(claim.session_id, claim.operation_id, claim.segment_sequence,
                                          claim.ownership_generation, claim.ownership_generation + 1,
                                          retained.checkpoint_id, retained.cursor, next_eligible_at)


def verify_recovery_on_connection(conn: Connection, receipt: ComposerSegmentRecoveryReceipt) -> None:
    if type(receipt) is not ComposerSegmentRecoveryReceipt:
        raise TypeError("recovery verification requires its nominal SQL receipt")
    parent = conn.execute(select(jobs).where(jobs.c.session_id == str(receipt.session_id), jobs.c.operation_id == receipt.operation_id)).one()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == str(receipt.session_id), segments.c.operation_id == receipt.operation_id,
        segments.c.sequence == receipt.sequence,
    )).one()
    previous = conn.execute(select(attempts).where(
        attempts.c.session_id == str(receipt.session_id), attempts.c.operation_id == receipt.operation_id,
        attempts.c.sequence == receipt.sequence, attempts.c.generation == receipt.previous_generation,
    )).one()
    stored = load_snapshot(conn, session_id=str(receipt.session_id), operation_id=receipt.operation_id)
    if (parent.status != "running" or parent.phase != "retry_waiting"
        or parent.current_segment_sequence != receipt.sequence or parent.ownership_generation != receipt.generation
        or segment.state != "interrupted" or segment.generation != receipt.generation
        or restore_utc(segment.next_eligible_at) != receipt.next_eligible_at
        or segment.claim_token is not None or previous.outcome != "interrupted" or previous.revoked_at is None
        or stored.checkpoint_id != receipt.checkpoint_id or stored.cursor != receipt.cursor):
        raise AuditIntegrityError("continuation recovery receipt failed actual SQL readback")
```

- [ ] Add these exact synchronous authority facades; they expose no raw connection. Import `math` and the nominal recovery receipt/helpers in their owning modules.

```python
def suspend_segment(self, running: ComposerOperationRunning, *, reason: str, retry_delay_seconds: float | None = None) -> ComposerSegmentRecoveryReceipt:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return suspend_segment_on_connection(conn, running, retry_delay_seconds=retry_delay_seconds, reason=reason)


def verify_recovery(self, receipt: ComposerSegmentRecoveryReceipt) -> None:
    with locked_session_transaction(self._engine, str(receipt.session_id)) as conn:
        verify_recovery_on_connection(conn, receipt)
```

- [ ] Add the failed-adoption guard and dedicated authority facade below. `SessionOperationLease.adopt` already joins its actual adoption CAS and releases the original context before propagating a failure/cancellation. This one port accepts that exact released fence and its immutable current attempt; it refuses any replacement fence, changed parent generation, revoked history, changed session principal or different claim. Normal execution never receives a relaxed live-owner mutation guard.

```python
def require_failed_adoption_recovery(conn: Connection, running: ComposerOperationRunning) -> None:
    claim, context = running.claim, running.session_operation_context
    fence = context.fence
    parent = conn.execute(select(jobs).where(
        jobs.c.session_id == str(claim.session_id), jobs.c.operation_id == claim.operation_id,
    )).one()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence,
    )).one()
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
        attempts.c.sequence == claim.segment_sequence, attempts.c.generation == claim.ownership_generation,
    )).one()
    actual = conn.execute(select(fences).where(fences.c.session_id == str(claim.session_id))).one()
    principal = conn.execute(select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
        sessions_table.c.id == str(claim.session_id),
    )).one()
    if (parent.lifecycle_version != 2 or parent.status != "running"
        or parent.current_segment_sequence != claim.segment_sequence
        or parent.ownership_generation != claim.ownership_generation or parent.attempt != claim.attempt
        or segment.state != "running" or segment.generation != claim.ownership_generation
        or segment.claim_token != claim.claim_token
        or history.revoked_at is not None or history.claim_attempt != claim.attempt
        or (history.session_operation_id, history.session_operation_epoch, history.session_operation_lease_token)
           != (fence.operation_id, fence.operation_epoch, fence.lease_token)
        or (actual.operation_id, actual.operation_epoch, actual.lease_token)
           != (fence.operation_id, fence.operation_epoch, fence.lease_token)
        or actual.operation_kind != SessionOperationKind.COMPOSE.value or actual.released_at is None
        or principal.archived_at is not None or principal.user_id != parent.actor_user_id):
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)


def suspend_failed_adoption(
    self, running: ComposerOperationRunning, *, retry_delay_seconds: float | None = None,
) -> ComposerSegmentRecoveryReceipt:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return suspend_segment_on_connection(
            conn, running, retry_delay_seconds=retry_delay_seconds, reason="adoption_failed",
            released_adoption_owner=True,
        )
```

### C3.7: Insert the authority-reserved user row and complete ingress snapshot together

**Files:** Modify `bind_composer_operation_user_message_on_connection` in `src/elspeth/web/coordination/composer_operation_authority.py:1105`; modify existing `SessionServiceImpl.add_message_with_transcript` in `src/elspeth/web/sessions/service.py:5885`; add its optional argument to `SessionServiceProtocol`.

- [ ] **Step 1: Add a v2 branch at the beginning of `bind_composer_operation_user_message_on_connection`, before its existing v1 SQL.** Keep the complete legacy body unchanged below this branch.

```python
if running.claim.segment_sequence is not None:
    from elspeth.web.sessions.continuation import require_continuation_mutation
    require_continuation_mutation(conn, running.session_operation_context)
    changed = conn.execute(update(jobs).where(
        *_key(running.claim.session_id, running.claim.operation_id), jobs.c.lifecycle_version == 2,
        jobs.c.current_segment_sequence == running.claim.segment_sequence,
        jobs.c.ownership_generation == running.claim.ownership_generation,
        jobs.c.ingress_phase == "pending", jobs.c.user_message_id.is_(None),
        jobs.c.reserved_user_message_id == str(user_message_id),
    ).values(user_message_id=str(user_message_id), ingress_phase="committed", updated_at=database_now(conn)))
    if changed.rowcount != 1:
        raise AuditIntegrityError("continuation ingress differs from reserved user identity")
    return
```

- [ ] **Step 2: Add `reserved_user_message_id: UUID | None = None` to the existing ingress service/protocol signature.** Before `_sync`, reject an optional reservation on v1 or a missing reservation on v2. This comparison uses the existing nominal running claim, not a user field.

```python
is_continuation = running.claim.segment_sequence is not None
if is_continuation != (reserved_user_message_id is not None):
    _refuse_required_sql(required_work, ValueError("ingress reservation must match lifecycle"))
```

In `_sync`'s existing `_insert_chat_message` call, add `message_id=str(reserved_user_message_id) if reserved_user_message_id is not None else None`. The existing writer already accepts this parameter; do not create a second message writer.

Immediately after the existing same-connection transcript SELECT, before `_sync` returns `message_rows`, insert the following. Parent C1a defines the exact `snapshot_after_composer_ingress` builder. The builder retains the actual `MessageIngressFresh` and complete ordered transcript, not a reconstructed chat prefix.

```python
if is_continuation:
    from elspeth.web.composer.continuation_codec import snapshot_after_composer_ingress
    from elspeth.web.sessions.continuation import (
        ContinuationEvent, append_complete_snapshot, load_snapshot,
    )
    stored = load_snapshot(conn, session_id=sid, operation_id=request_id)
    transcript_records = [self._row_to_chat_message_record(row) for row in message_rows]
    if not transcript_records or str(transcript_records[-1].id) != msg_id_holder["id"]:
        raise AuditIntegrityError("continuation ingress transcript is missing its reserved row")
    actual_message = replace(transcript_records[-1], operation_id=operation_id)
    transcript_records[-1] = actual_message
    actual_receipt = MessageIngressFresh(operation_id=operation_id, message=actual_message, transcript=tuple(transcript_records))
    snapshot_json = snapshot_after_composer_ingress(
        prior_snapshot_json=stored.payload_json, receipt=actual_receipt, next_cursor=stored.cursor + 1,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=request_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=snapshot_json,
        event_batch=(ContinuationEvent("ingress", canonical_json({"user_message_id": msg_id_holder["id"]})),),
        handoff=False,
    )
```

A crash after SQL commit before the outer receipt projection restores `InitializeFrame.ingress_receipt_json`; it never calls this writer again. A crash before commit leaves no user row or ingress receipt, so the exact same reserved UUID can be inserted. The existing same-session user-message FK remains intact. No reservation FK points to a nonexistent row.

### C1b.1: Cold SQL restore through two genuinely different segment fences

**Files:** Create `tests/unit/web/sessions/test_continuation_cold_restore.py`; reuse the real SQLite fixture exported by `tests/unit/web/coordination/test_composer_operation_authority.py`.

- [ ] **Step 1: Split the fixture prefix from late cold-restoration qualification.** Immediately after initial C3.4, create only this module's imports and `admit_continuation`/`start_continuation` adapters. They depend only on C1/C2/C3.3/initial C3.4 and supply F7/F10/F28 fixture imports without importing F28. Export the exact existing decorated SQL fixture through its source module reference and assignment shown below. The direct same-name imported fixture causes genuine F811 when ordinary test parameters are appended; do not replace its real factory or suppress that error. C5.2a supplies the complete current initial module and its five first-fence tests. Add the two-fence test definition below and execute Steps 2–4 only at main step 9, after F10's real retained-title/apply-title owners, F28.3, C3.4b and corrected C3.5/C3.6 pass.

```python
from uuid import UUID, uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation import prepare_composer_continuation_admission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationSegmentPolicy
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.sessions.composer_operations import ComposerOperationRunning, ComposerOperationFenceLost, composer_operation_v2_request_hash
from elspeth.web.sessions.continuation import load_snapshot
from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
from elspeth.web.sessions.models import composer_operation_segments_table as segments
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.unit.web.coordination import test_composer_operation_authority
from tests.helpers.continuation_policy import minimal_continuation_policy

operation_store = test_composer_operation_authority.operation_store


def admit_continuation(store):
    engine, _repository, authority, _service, sid = store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Preserve café and exact context")
    request_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request)
    policy = minimal_continuation_policy()
    action = policy.actions.session_sql
    with engine.connect() as conn:
        initial_trust_mode = conn.execute(
            select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(sid))
        ).scalar_one()
    seed = prepare_composer_continuation_admission(
        session_id=sid, operation_id=request.operation_id, kind="compose_message",
        request_json=request.model_dump_json(), request_hash=request_hash, base_state_id=None,
        policy_hash=policy.policy_hash, admission_policy_json=policy.model_dump_json(), initial_action_json=action.model_dump_json(),
        initial_trust_mode=initial_trust_mode,
    )
    record, fresh = authority.admit(
        quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        session_id=sid, operation_id=request.operation_id, kind="compose_message", request_hash=request_hash,
        actor_user_id="alice", request_id="continuation", base_state_id=None,
        request_json=request.model_dump_json(), deadline_seconds=180, max_nonterminal=64,
        auth_provider_type="local", lifecycle_version=2, continuation_enabled=True, seed=seed,
    )
    assert fresh
    return record, seed, action


def start_continuation(repository, authority):
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim, owner_instance_id="test-owner", lease_seconds=30, auth_provider_type="local",
    )
    return ComposerOperationRunning(claim=claim, session_operation_context=context)


@pytest.mark.asyncio
async def test_cold_restore_preserves_real_ingress_and_revokes_old_fence(operation_store, monkeypatch):
    from tests.unit.web.composer.test_continuation_title import retained_title, apply_title
    from elspeth.web.composer.continuation_policy import decode_continuation_policy

    engine, repository, authority, service, sid = operation_store
    running, custody, binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None
    await apply_title(operation_store, running, custody, binding, observed, candidate)
    record = authority.get_for_turn(session_id=sid, operation_id=running.claim.operation_id)
    assert record is not None and record.user_message_id == record.reserved_user_message_id
    with engine.connect() as conn:
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
    stack = decode_continuation_snapshot(stored.payload_json)
    assert stack.frames[0].ingress_receipt_json is not None
    assert stack.frames[0].ingress_phase == "ready" and stack.frames[0].bootstrap is not None
    policy_hash = stack.policy_hash
    action = decode_continuation_policy(stack.admission_policy_json, policy_hash).actions.session_sql
    ready = stack
    receipt = authority.commit_checkpoint_and_successor(
        running, expected_head=None, expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor, snapshot_json=encode_continuation_snapshot(ready),
        next_action_json=action.model_dump_json(), policy_sha256=policy_hash,
    )
    authority.verify_yield(receipt)
    repository.release(running.session_operation_context)
    del stack, ready, stored
    # Recreate the SQL reader/claim owner instead of retaining its Python frame.
    restarted = ComposerAsyncOperationAuthority(engine, owner_instance_id="test-owner", claim_lease_seconds=30)
    second = start_continuation(repository, restarted)
    assert second.claim.segment_sequence == 2
    assert second.session_operation_context.fence != running.session_operation_context.fence
    with engine.connect() as conn:
        restored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id).order_by(attempts.c.sequence)).all()
        current = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id, segments.c.sequence == 2)).one()
    assert restored.checkpoint_id == receipt.checkpoint_id
    assert decode_continuation_snapshot(restored.payload_json).frames[0].ingress_receipt_json is not None
    assert history[0].outcome == "yielded" and history[0].revoked_at is not None
    assert history[1].session_operation_epoch == second.session_operation_context.fence.operation_epoch
    assert current.state == "running"
    assert [message.id for message in await service.get_messages(sid, limit=None) if message.role == "user"] == [record.user_message_id]
    assert len(calls) == 1
    with pytest.raises(ComposerOperationFenceLost):
        with engine.begin() as conn:
            from elspeth.web.sessions.continuation import require_continuation_mutation
            require_continuation_mutation(conn, running.session_operation_context)
    repository.release(second.session_operation_context)
```

- [ ] **Step 2: Prove both a positive and an adversarial control.** Run the command below, requiring native exit 0. Temporarily remove only the history guard call inserted by C3.2; the old-fence refusal assertion must fail before restoring it. Temporarily suppress only the `append_complete_snapshot` pointer update in its owned transaction body; the next load must refuse an event/snapshot cursor mismatch. Restore both lines and rerun the affected tests.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py >"$COMPOSER_LOG_DIR/c1b-cold.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c1b-cold.log"
test "$composer_exit" -eq 0
```
- [ ] **Step 3: Extend the master C1a evidence-owner cold tests to feed this same two-fence SQL path.** Exact captured inspection/attachment/buffer/advisor fixtures remain C1a-owned; their restore methods must accept current live context and historical-reference authority. The test above proves storage/fence/ingress custody and does not by itself claim every mutable Composer owner is covered.
- [ ] **Step 4: Commit:** stage this new test plus C3 service/authority/protocol changes; preflight then `git commit -m "feat: commit continuation ingress and fenced handoffs atomically"`.

### C5.1: Define independent action policies and pure clock admission

**Files:** Create `src/elspeth/web/composer/continuation_clock.py`; create `tests/unit/web/composer/test_continuation_action_admission.py`.

- [ ] **Step 1: Write complete pure-clock tests.**

```python
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import ValidationError

from elspeth.web.composer.continuation_clock import SegmentFit, admit_action, size_segment
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationSegmentPolicy


def test_long_action_gets_its_own_full_allowance():
    action = ContinuationActionPolicy(preparation=10, guard=30, quota=10, physical=400, retention=10)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)
    assert action.total == 460
    assert size_segment(action, policy) == 605
    assert admit_action(action, policy, hard_remaining=605, cooperative_remaining=180, fresh=True) is SegmentFit.ADMIT
    assert admit_action(action, policy, hard_remaining=460, cooperative_remaining=180, fresh=False) is SegmentFit.YIELD
    with pytest.raises(ValueError, match="fresh segment cannot fit"):
        admit_action(action, policy, hard_remaining=460, cooperative_remaining=180, fresh=True)


def test_yield_watermark_never_shortens_an_admitted_action():
    action = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=5, retention=1)
    policy = ContinuationSegmentPolicy(nominal=300, watermark=120, quantum=180, reserve=25)
    assert admit_action(action, policy, hard_remaining=200, cooperative_remaining=0, fresh=False) is SegmentFit.YIELD
    assert admit_action(action, policy, hard_remaining=119, cooperative_remaining=40, fresh=False) is SegmentFit.YIELD
    assert admit_action(action, policy, hard_remaining=200, cooperative_remaining=40, fresh=False) is SegmentFit.ADMIT
    assert action.total == 7


@pytest.mark.parametrize("bad", [0, -1, float("inf"), float("nan"), True])
def test_executed_stage_must_have_positive_finite_bound(bad):
    with pytest.raises(ValidationError):
        ContinuationActionPolicy(preparation=bad, guard=None, quota=None, physical=5, retention=1)


def test_frozen_observation_uses_db_timestamp_and_is_immutable():
    from dataclasses import FrozenInstanceError
    from elspeth.web.composer.continuation_clock import freeze_observation
    now = datetime(2026, 10, 20, tzinfo=UTC)
    observation = freeze_observation(
        sequence=2, generation=3, now=now,
        deadline=now + timedelta(seconds=500), cooperative=now + timedelta(seconds=50),
        watermark=120,
    )
    assert observation.hard_remaining_seconds == 500
    assert observation.cooperative_remaining_seconds == 50
    assert not observation.yield_pending
    with pytest.raises(FrozenInstanceError):
        observation.generation = 4
```

- [ ] **Step 2: Bootstrap the complete importable Step 3 packet, then prove behavioral RED.** Temporarily replace `size_segment`'s computed return with `return policy.nominal`; Step 4 must exit 1 because the long-action assertion expects 605. Restore the real calculation before GREEN. A collection error is setup failure.
- [ ] **Step 3: Add this complete pure module.**

```python
"""Independent action allowances and immutable pre-input segment observations."""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum
from elspeth.web.composer.continuation_policy import ContinuationActionPolicy, ContinuationSegmentPolicy


class SegmentFit(Enum):
    ADMIT = "admit"
    YIELD = "yield"


def size_segment(action: ContinuationActionPolicy, policy: ContinuationSegmentPolicy) -> float:
    size = max(policy.nominal, action.total + policy.reserve + policy.watermark)
    if not math.isfinite(size):
        raise ValueError("complete segment sizing allowance must be finite")
    return size


def admit_action(
    action: ContinuationActionPolicy,
    policy: ContinuationSegmentPolicy,
    *,
    hard_remaining: float,
    cooperative_remaining: float,
    fresh: bool,
) -> SegmentFit:
    if not math.isfinite(hard_remaining) or not math.isfinite(cooperative_remaining):
        raise ValueError("segment observations must be finite")
    if hard_remaining < action.total + policy.reserve:
        if fresh:
            raise ValueError("fresh segment cannot fit its retained next action")
        return SegmentFit.YIELD
    if cooperative_remaining <= 0 or hard_remaining <= policy.watermark:
        return SegmentFit.YIELD
    return SegmentFit.ADMIT


@dataclass(frozen=True, slots=True)
class ContinuationTimingObservation:
    sequence: int
    generation: int
    observed_at: datetime
    hard_remaining_seconds: float
    cooperative_remaining_seconds: float
    watermark_seconds: float
    yield_pending: bool


def freeze_observation(
    *, sequence: int, generation: int, now: datetime,
    deadline: datetime, cooperative: datetime, watermark: float,
) -> ContinuationTimingObservation:
    if sequence < 1 or generation < 1 or now.utcoffset() is None or deadline.utcoffset() is None or cooperative.utcoffset() is None:
        raise ValueError("timing requires positive identities and aware database times")
    hard = max(0.0, (deadline - now).total_seconds())
    soft = max(0.0, (cooperative - now).total_seconds())
    return ContinuationTimingObservation(sequence, generation, now, hard, soft, watermark, soft <= 0 or hard <= watermark)
```

- [ ] **Step 4: GREEN:** Require native exit 0. This proves arithmetic only; C5.4 covers actual timeout enforcement.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_action_admission.py >"$COMPOSER_LOG_DIR/c5-clock.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-clock.log"
test "$composer_exit" -eq 0
```
- [ ] **Step 5: Commit:** `git add src/elspeth/web/composer/continuation_clock.py tests/unit/web/composer/test_continuation_action_admission.py` then run the required branch preflight and `git commit -m "feat: define independent continuation action clocks"`.

### C5.2: Close settings and the server-only admission seed producer

**Files:** Extend `src/elspeth/web/composer/continuation_policy.py`; modify `src/elspeth/web/config.py`, `src/elspeth/web/app.py:2317`, `src/elspeth/web/sessions/composer_operation_preconditions.py`, and `ComposerAsyncOperationAuthority.admit`/`admit_composer_operation` in `src/elspeth/web/coordination/composer_operation_authority.py`.

- [ ] Add only this constructor adapter as a method on C1a.0's existing `ContinuationAdmissionPolicy`; do not redefine the policy classes. Import `TYPE_CHECKING` and put `WebSettings` behind `TYPE_CHECKING` in the pure policy module to avoid a runtime config cycle. `physical` denotes the independently enforced execution stage of non-provider actions; their absent guard/quota stages contribute zero exactly once.

```python
@classmethod
def from_settings(cls, settings: WebSettings) -> ContinuationAdmissionPolicy:
    if settings.composer_continuation_settlement_reserve_seconds is None or settings.composer_continuation_action_bounds is None:
        raise ValueError("enabled continuation requires explicit reserve and action policies")
    return cls(
        segment=ContinuationSegmentPolicy(
            nominal=settings.composer_continuation_segment_seconds,
            watermark=settings.composer_continuation_watermark_seconds,
            quantum=settings.composer_continuation_quantum_seconds,
            reserve=settings.composer_continuation_settlement_reserve_seconds,
        ),
        actions=settings.composer_continuation_action_bounds,
    )
```

- [ ] C7 owns the single `composer_continuation_enabled: bool = False` setting. Add these other exact fields to `WebSettings` beside existing async worker settings; import the two policy classes at runtime because Pydantic must resolve their field types.

```python
composer_continuation_segment_seconds: float = Field(default=300.0, gt=0, allow_inf_nan=False)
composer_continuation_watermark_seconds: float = Field(default=120.0, gt=0, allow_inf_nan=False)
composer_continuation_quantum_seconds: float = Field(default=180.0, gt=0, allow_inf_nan=False)
composer_continuation_settlement_reserve_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
composer_continuation_action_bounds: ContinuationActionBounds | None = None

@model_validator(mode="after")
def _continuation_policy_complete(self) -> Self:
    if self.composer_continuation_enabled:
        ContinuationAdmissionPolicy.from_settings(self)
    return self
```

- [ ] Extend `ComposerAsyncOperationAuthority.__init__` with `continuation_policy: ContinuationAdmissionPolicy | None = None`, validate its nominal type when non-null, and assign `self._continuation_policy = continuation_policy`. The existing constructor arguments/default behavior remain unchanged. In `web/app.py`'s existing `ComposerAsyncOperationAuthority(...)` call, append exactly:

```python
continuation_policy=ContinuationAdmissionPolicy.from_settings(settings) if settings.composer_continuation_enabled else None,
```

- [ ] Extract the existing precondition body without changing its selector. Add this frozen input in `composer_operation_preconditions.py`, rename the original function body to `check_composer_request_preconditions_on_connection`, update only its `job` parameter/type check from `ComposerOperationRecord` to this exact class, and change `_refuse`'s annotation to the same input. All ownership/base/latest-user/proposal checks and error bodies in the existing body stay byte-for-byte unchanged.

```python
@dataclass(frozen=True, slots=True)
class ComposerOperationPreconditions:
    session_id: UUID
    actor_user_id: str
    request_id: str | None
    base_state_id: UUID | None
    kind: ComposerOperationKind
    request_json: str | None


def check_composer_operation_preconditions_on_connection(
    conn: Connection, job: ComposerOperationRecord, *, auth_provider_type: str,
) -> UUID | None:
    if type(job) is not ComposerOperationRecord:
        raise TypeError("job must be ComposerOperationRecord")
    return check_composer_request_preconditions_on_connection(
        conn, ComposerOperationPreconditions(job.session_id, job.actor_user_id, job.request_id,
                                            job.base_state_id, job.kind, job.request_json),
        auth_provider_type=auth_provider_type,
    )
```

The relocated function has the exact new signature; its complete unchanged source body is identified above:

```python-signature
def check_composer_request_preconditions_on_connection(
    conn: Connection, job: ComposerOperationPreconditions, *, auth_provider_type: str,
) -> UUID | None:
```

- [ ] C3.3 contains the complete fresh server seed producer and invokes the nominal seed's full credential-safe binding validation before SQL retention. Import its concrete `ComposerOperationPreconditions`, `check_composer_request_preconditions_on_connection`, `prepare_composer_continuation_admission`, `ContinuationActionPolicy`, `decode_continuation_policy` and snapshot codec from the pure prerequisites. That code runs inside the authenticated admission transaction after existing-key replay and the enabled flag gate; no HTTP-supplied seed is accepted. The typed initial action must equal the retained policy's session-SQL action. Exact replay while disabled consults neither current policy nor a new seed.

The disabled error must preserve the existing route's nested `detail` shape:

```python
body={"detail": {"error_type": "composer_continuation_disabled", "detail": "New continuation requests are disabled."}},
```

- [ ] Add `lifecycle_version: Literal[1, 2] = 1` and `continuation_enabled: bool = False` to the existing async `admit_composer_operation` signature and forward these exact arguments to `authority.admit`. The route sends no `seed`; the synchronous authority constructs it from its server configuration/current transaction. No replacement admission route is added.
- [ ] Add complete constructor/disabled replay tests to `test_continuation_cold_restore.py` using its `admit_continuation` fixture adapter:

```python
def test_exact_replay_survives_admission_flag_off(operation_store):
    _engine, _repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    request = SendMessageRequest.model_validate_json(authority.get_for_turn(session_id=sid, operation_id=record.operation_id).request_json)
    quota = ComposerQuotaAdmission(RepositoryRateLimitAuthority(authority._engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000)
    replay, fresh = authority.admit(
        quota=quota, session_id=sid, operation_id=record.operation_id, kind=record.kind,
        request_hash=record.request_hash, actor_user_id="alice", request_id="replayed",
        base_state_id=None, request_json=request.model_dump_json(), deadline_seconds=180,
        max_nonterminal=64, auth_provider_type="local", lifecycle_version=2, continuation_enabled=False,
    )
    assert not fresh and replay.operation_id == record.operation_id
    changed = request.model_copy(update={"content": "different request"})
    with pytest.raises(ComposerOperationConflictError):
        authority.admit(
            quota=quota, session_id=sid, operation_id=record.operation_id, kind=record.kind,
            request_hash=composer_operation_v2_request_hash(session_id=sid, kind=record.kind, request=changed),
            actor_user_id="alice", request_id=None, base_state_id=None, request_json=changed.model_dump_json(),
            deadline_seconds=180, max_nonterminal=64, auth_provider_type="local",
            lifecycle_version=2, continuation_enabled=False,
        )


def test_disabled_fresh_admission_retains_no_parent(operation_store):
    engine, _repository, authority, _service, sid = operation_store
    request = SendMessageRequest(operation_id=str(uuid4()), content="new")
    quota = ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000)
    with pytest.raises(ComposerOperationPreconditionRefused) as caught:
        authority.admit(
            quota=quota, session_id=sid, operation_id=request.operation_id, kind="compose_message",
            request_hash=composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request),
            actor_user_id="alice", request_id=None, base_state_id=None, request_json=request.model_dump_json(),
            deadline_seconds=180, max_nonterminal=64, auth_provider_type="local",
            lifecycle_version=2, continuation_enabled=False,
        )
    assert caught.value.error.http_status == 409
    assert caught.value.error.body["detail"]["error_type"] == "composer_continuation_disabled"
    assert authority.get(session_id=sid, operation_id=request.operation_id) is None
```

Add `ComposerOperationConflictError` and `ComposerOperationPreconditionRefused` to the test file's existing imports. Behavioral RED: temporarily move the enabled gate before existing-key lookup; `test_exact_replay_survives_admission_flag_off` must fail with a 409 refusal. Restore its position, then run the two tests plus existing v1 admission tests with ordinary pytest/native exit logging. All must exit 0.

### C7 prerequisite: Atomic authorized observation and Stop facets

**Files:** Modify `src/elspeth/web/coordination/composer_operation_authority.py`, `src/elspeth/web/sessions/service.py`, `src/elspeth/web/sessions/protocol.py`. C7 owns the exact DTO definitions in `sessions/composer_continuation_dtos.py`.

- [ ] Add this complete private projection function to the SQL authority module. It calls `_record_from_row` first, retaining all owned row/result-digest checks. The DB timestamp is observational; decreasing remaining time does not change the semantic revision.

```python
def _v2_snapshot_on_connection(conn: Connection, row: Row[Any], *, now: datetime) -> ComposerOperationV2Snapshot:
    from elspeth.web.sessions.composer_continuation_dtos import ComposerOperationSegmentV2, ComposerOperationV2Snapshot
    record = _record_from_row(row)
    if record.lifecycle_version != 2:
        raise ValueError("v2 observation requested for a legacy operation")
    if record.status in ("completed", "failed") and record.result_json is None:
        raise AuditIntegrityError("terminal continuation lacks its actual sealed result")
    result = MessageWithStateResponse.model_validate_json(record.result_json, strict=True) if record.status == "completed" else None
    error = ComposerOperationError.model_validate_json(record.result_json, strict=True) if record.status == "failed" else None
    observed_segment = None
    if record.status in ("queued", "running"):
        segment = conn.execute(select(segments).where(
            segments.c.session_id == str(record.session_id), segments.c.operation_id == record.operation_id,
            segments.c.sequence == record.current_segment_sequence,
        )).one()
        if segment.generation != record.ownership_generation or segment.state not in ("queued", "running", "interrupted"):
            raise AuditIntegrityError("public continuation pointer selects an invalid segment")
        if segment.state == "running" and segment.deadline_at is None:
            raise AuditIntegrityError("running continuation observation has no actual deadline")
        deadline = restore_utc(segment.deadline_at) if segment.state == "running" else None
        remaining = math.ceil(max(0.0, (deadline - now).total_seconds()) * 1000) if deadline is not None else None
        rollover = deadline is not None and (
            deadline <= now or restore_utc(segment.cooperative_at) <= now
        )
        observed_segment = ComposerOperationSegmentV2(
            sequence=segment.sequence, generation=segment.generation, state=segment.state,
            observed_at=now, deadline_at=deadline, remaining_ms=remaining, rollover_pending=rollover,
        )
    return ComposerOperationV2Snapshot(
        operation_id=record.operation_id, kind=record.kind, logical_status=record.status,
        phase=record.phase, status_revision=record.status_revision, poll_after_ms=1000,
        cancel_requested=record.cancel_requested_at is not None, segment=observed_segment, result=result, error=error,
    )
```

- [ ] Add the exact transaction-owning read/cancel facets to `ComposerAsyncOperationAuthority`. Stop writes one durable marker; it never fabricates a final provider result. Cancelled queued work is still claimed by the v2 worker so that its actual effect/audit owners can reconcile and settle it.

```python
def get_v2_snapshot(
    self, *, session_id: UUID, operation_id: str, actor_user_id: str, auth_provider_type: str,
) -> ComposerOperationV2Snapshot | None:
    _identity(session_id, operation_id)
    with locked_session_transaction(self._engine, str(session_id)) as conn:
        _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
        row = _read(conn, session_id, operation_id)
        return None if row is None else _v2_snapshot_on_connection(conn, row, now=database_now(conn))


def request_v2_cancel(
    self, *, session_id: UUID, operation_id: str, actor_user_id: str, auth_provider_type: str,
) -> tuple[ComposerOperationV2Snapshot, datetime] | None:
    _identity(session_id, operation_id)
    with locked_session_transaction(self._engine, str(session_id)) as conn:
        _require_owner(conn, session_id=session_id, actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
        row = _read(conn, session_id, operation_id)
        if row is None:
            return None
        if row.lifecycle_version != 2:
            raise ValueError("v2 cancellation requested for a legacy operation")
        now = database_now(conn)
        if row.status in ("queued", "running") and row.cancel_requested_at is None:
            conn.execute(update(jobs).where(*_key(session_id, operation_id), jobs.c.cancel_requested_at.is_(None)).values(
                cancel_requested_at=now, updated_at=now, status_revision=jobs.c.status_revision + 1,
            ))
            row = _read(conn, session_id, operation_id)
            if row is None:
                raise AuditIntegrityError("cancelled continuation disappeared")
        return _v2_snapshot_on_connection(conn, row, now=now), now
```

- [ ] Add these two synchronous service facades verbatim to `SessionServiceImpl`. Use `TYPE_CHECKING` imports of the DTO in `sessions/protocol.py`, because DTO imports schemas and schemas imports protocol. The implementation's return annotation is deferred by its existing `from __future__ import annotations`; imports can be inside the method. The read/cancel authority instance opens the transaction on the same engine and has no provider/runtime behavior.

```python
def get_composer_operation_v2_snapshot(
    self, *, session_id: UUID, operation_id: str, actor_user_id: str, auth_provider_type: str,
) -> ComposerOperationV2Snapshot | None:
    authority = ComposerAsyncOperationAuthority(self._engine, owner_instance_id=self._owner_instance_id, claim_lease_seconds=30)
    return authority.get_v2_snapshot(session_id=session_id, operation_id=operation_id,
                                   actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)


def request_composer_operation_v2_cancel(
    self, *, session_id: UUID, operation_id: str, actor_user_id: str, auth_provider_type: str,
) -> tuple[ComposerOperationV2Snapshot, datetime] | None:
    authority = ComposerAsyncOperationAuthority(self._engine, owner_instance_id=self._owner_instance_id, claim_lease_seconds=30)
    return authority.request_v2_cancel(session_id=session_id, operation_id=operation_id,
                                     actor_user_id=actor_user_id, auth_provider_type=auth_provider_type)
```

Declare exactly these two signatures with `...` protocol bodies on `SessionServiceProtocol`; no connection escapes this facade. Import DTO only under `TYPE_CHECKING` in both authority/service modules if needed for typechecking; private projection imports nominal DTOs locally at runtime.

- [ ] Add these meaningful SQL assertions to the cold-restore test module:

```python
def test_v2_observation_and_stop_are_same_authorized_parent_snapshot(operation_store):
    _engine, _repository, authority, service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    original = service.get_composer_operation_v2_snapshot(session_id=sid, operation_id=record.operation_id, actor_user_id="alice", auth_provider_type="local")
    assert original.logical_status == "queued" and original.segment.state == "queued"
    assert original.segment.deadline_at is None and original.segment.remaining_ms is None
    stopped, at = service.request_composer_operation_v2_cancel(session_id=sid, operation_id=record.operation_id, actor_user_id="alice", auth_provider_type="local")
    repeated, later = service.request_composer_operation_v2_cancel(session_id=sid, operation_id=record.operation_id, actor_user_id="alice", auth_provider_type="local")
    assert at.utcoffset() is not None and later >= at
    assert stopped.cancel_requested and repeated.cancel_requested
    assert stopped.status_revision == repeated.status_revision == original.status_revision + 1
    with pytest.raises(ComposerOperationPreconditionRefused):
        authority.get_v2_snapshot(session_id=sid, operation_id=record.operation_id, actor_user_id="mallory", auth_provider_type="local")
    assert len(authority.claim_segment(limit=1)) == 1
```

Change C3.4's v2 claim query by removing its `jobs.c.cancel_requested_at.is_(None)` predicate only; keep the predicate in legacy `claim_next`. Remove the parent-cancel refusal from v2 `start_segment_on_connection` and `start_composer_segment`, retaining all generation/claim/actor checks. The resumed driver receives the durable cancel and is permitted only reconciliation/terminal settlement, never another provider/tool action. Normal business mutation still checks cancellation in `require_continuation_mutation`.

### C3.8: Validate retained graph effects against their immutable SQL creator

**Files:** Extend `src/elspeth/web/sessions/continuation.py`; extract the existing state row decoder into `src/elspeth/web/sessions/converters.py`; keep the service's decoder method as a delegating compatibility seam. Extend `tests/unit/web/sessions/test_continuation_cold_restore.py`.

- [ ] Add the complete graph-state decoder extraction: move the body of `SessionServiceImpl._row_to_state_record` to module-level `state_record_from_row(row: Any) -> CompositionStateRecord` in `sessions/converters.py`, replacing only `self._unwrap_envelope(...)` with the already-owned `unwrap_state_column(...)` function. Import its existing authority and error decoder from the same modules used by `service.py`; preserve every field and the `_mapping["sources"]` compatibility branch. Replace the old service body with exactly:

```python
return state_record_from_row(row)
```

This unchanged decoder relocation must land as the C3.0 prerequisite **before C3.3 admission**, which imports `state_record_from_row`, `state_from_record` and `composition_states_table` to strictly hydrate an actual non-null v2 base before quota/parent writes. It is not a new row-schema/parser policy. The current graph-review receipt decoder remains `AdvisorCheckpointPassRecord.from_dict` in the service. V1's existing `GraphRecoveryAdmission` and `compose_recovery` paths remain unchanged; no new recovery initializer is introduced.

- [ ] Add the nominal historical reference and complete validator below to `sessions/continuation.py`. Import `json`, `stable_hash`, `canonical_json`, `state_record_from_row`, `state_from_record` and the effect table alias. `expected_creator_operation_id` is the historical creator retained in the snapshot; it never replaces the current live context.

```python
@dataclass(frozen=True, slots=True)
class ValidatedHistoricalEffectReference:
    session_id: UUID
    operation_id: str
    effect_key: str
    input_sha256: str
    creator_operation_id: str
    creator_operation_epoch: int
    receipt_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str
    result_bytes: bytes


@dataclass(frozen=True, slots=True)
class HistoricalEffectLocator:
    effect_key: str
    input_sha256: str
    creator_operation_id: str
    receipt_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str


def _authored_row_hash(row: object) -> str:
    from elspeth.web.composer.turn_effects import authored_state_hash
    return authored_state_hash(state_from_record(state_record_from_row(row)))


def validate_historical_effect_reference(
    conn: Connection, *, session_id: UUID, operation_id: str, effect_key: str,
    expected_input_sha256: str, expected_creator_operation_id: str,
    expected_receipt_id: str, expected_inserted_state_ids: tuple[UUID, ...],
    expected_authored_state_hash: str,
) -> ValidatedHistoricalEffectReference:
    effect = conn.execute(select(effects).where(
        effects.c.session_id == str(session_id), effects.c.operation_id == operation_id,
        effects.c.effect_key == effect_key,
    )).one_or_none()
    if effect is None or effect.owner != "session_sql" or effect.disposition != "committed":
        raise AuditIntegrityError("historical graph effect lacks a committed SQL owner")
    creator = conn.execute(select(attempts).where(
        attempts.c.session_id == str(session_id), attempts.c.operation_id == operation_id,
        attempts.c.sequence == effect.creator_sequence, attempts.c.generation == effect.creator_generation,
    )).one_or_none()
    if creator is None or creator.session_operation_id != expected_creator_operation_id:
        raise AuditIntegrityError("historical graph effect creator mismatch")
    if effect.input_sha256 != expected_input_sha256 or effect.receipt_key != expected_receipt_id:
        raise AuditIntegrityError("historical graph effect input or receipt mismatch")
    if effect.committed_state_ids_json is None or effect.graph_sha256 != expected_authored_state_hash:
        raise AuditIntegrityError("historical graph effect lost its state frontier")
    ids, error = parse_json_strict(effect.committed_state_ids_json)
    if error is not None:
        raise AuditIntegrityError("historical graph effect state identities are not strict JSON")
    expected_ids = [str(state_id) for state_id in expected_inserted_state_ids]
    if type(ids) is not list or not ids or ids != expected_ids or len(set(ids)) != len(ids):
        raise AuditIntegrityError("historical graph effect state identities differ")
    last = None
    for state_id in ids:
        row = conn.execute(select(states).where(states.c.session_id == str(session_id), states.c.id == state_id)).one_or_none()
        if row is None or row.creator_operation_id != creator.session_operation_id:
            raise AuditIntegrityError("historical graph effect substituted another creator's state")
        last = row
    if last is None or _authored_row_hash(last) != expected_authored_state_hash:
        raise AuditIntegrityError("historical graph effect authored content mismatch")
    result = read_chunk(conn, session_id=str(session_id), operation_id=operation_id, key=effect.result_chunk_key)
    return ValidatedHistoricalEffectReference(
        session_id, operation_id, effect_key, effect.input_sha256,
        creator.session_operation_id, creator.session_operation_epoch, effect.receipt_key,
        expected_inserted_state_ids, expected_authored_state_hash, result,
    )
```

- [ ] Add this exact synchronous current-owner facet on `ComposerAsyncOperationAuthority`. The current SQL fence/session principal, selected snapshot and every retained locator are verified in one transaction. Only this authority issues validated historical references; the snapshot decoder returns locators. Call it through the existing required SQL/read tickets from the cold owner factory, with the checkpoint selected with the supplied root. No restored lease is minted and no historical reference becomes fresh mutation authority.

```python
def validate_historical_effect_references(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    locators: tuple[HistoricalEffectLocator, ...],
) -> tuple[ValidatedHistoricalEffectReference, ...]:
    if any(type(locator) is not HistoricalEffectLocator for locator in locators):
        raise TypeError("historical validation requires nominal retained locators")
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context,
                                      allow_cancel=True, allow_expired_segment=True)
        require_snapshot_cas(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
                             expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        parent = conn.execute(select(jobs.c.actor_user_id).where(*_key(running.claim.session_id, running.claim.operation_id))).one()
        principal = conn.execute(select(sessions_table.c.user_id, sessions_table.c.archived_at).where(
            sessions_table.c.id == str(running.claim.session_id),
        )).one()
        if principal.archived_at is not None or principal.user_id != parent.actor_user_id:
            raise AuditIntegrityError("historical reference restore changed its current authenticated principal")
        return tuple(validate_historical_effect_reference(
            conn, session_id=running.claim.session_id, operation_id=running.claim.operation_id,
            effect_key=locator.effect_key, expected_input_sha256=locator.input_sha256,
            expected_creator_operation_id=locator.creator_operation_id, expected_receipt_id=locator.receipt_id,
            expected_inserted_state_ids=locator.inserted_state_ids,
            expected_authored_state_hash=locator.authored_state_hash,
        ) for locator in locators)
```

- [ ] Add this exact CAS helper before the effect helpers. All callers with a precomputed next snapshot must provide the checkpoint and cursor selected with that snapshot. The check happens before ordinal allocation, effect insertion or owner mutation. The transaction's current row is never substituted for the caller's expectation.

```python
def require_snapshot_cas(
    conn: Connection, *, session_id: str, operation_id: str,
    expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    stored = load_snapshot(conn, session_id=session_id, operation_id=operation_id)
    if stored.checkpoint_id != expected_checkpoint_id or stored.cursor != expected_cursor:
        raise ContinuationCASConflict("continuation snapshot changed before effect admission")
    return stored
```

- [ ] **DEFERRED complete generic SQL/read and finish variants:** preserve the full bodies below, but do not install them in the initial storage slice. Install only C5.2a's complete provider-only `prepare_effect_on_connection` now; it explicitly refuses SQL/read/action-owner arguments. The full SQL/read preparation/finish/result-writer cluster requires main step 9's actual B1/C3.8c/B4/current-read and writer owners together. At that integration, retain the exactTrue/actual SQL six-tuple and fresh-work owed-P4 checks from C5.2a; the preserved raw bodies are not permission to restore their weaker unchecked guard. Their callers hold the actual owner mutation transaction and call `require_snapshot_cas` before mutation SQL. A session SQL effect's mutation, real state rows, exact result, effect receipt and next complete snapshot must then commit or roll back together. A known committed result is replayed; the helper never calls the original tool.

```python
def prepare_effect_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, effect_key: str,
    input_sha256: str, owner: str, next_snapshot_json: str,
    expected_checkpoint_id: str, expected_cursor: int,
    action_owner: Literal["primary_turn", "review", "read_tool"] | None = None,
    read_tool_name: str | None = None, read_tool_arguments_json: str | None = None,
) -> StoredContinuationSnapshot:
    require_continuation_mutation(conn, running.session_operation_context)
    claim = running.claim
    stored = require_snapshot_cas(conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
                                  expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
    stack = decode_continuation_snapshot(next_snapshot_json)
    if stack.cursor != expected_cursor or (stack.logical.session_id, stack.logical.operation_id) != (str(claim.session_id), claim.operation_id):
        raise ContinuationCASConflict("prepared effect carries an obsolete or foreign frame stack")
    old = conn.execute(select(effects).where(
        effects.c.session_id == str(claim.session_id), effects.c.operation_id == claim.operation_id,
        effects.c.effect_key == effect_key,
    )).one_or_none()
    if old is not None:
        if old.input_sha256 != input_sha256 or old.owner != owner:
            raise AuditIntegrityError("prepared effect identity collision")
        raise ContinuationCASConflict("effect already prepared; restore its actual disposition")
    conn.execute(insert(effects).values(
        session_id=str(claim.session_id), operation_id=claim.operation_id, effect_key=effect_key,
        input_sha256=input_sha256, owner=owner, disposition="prepared",
        creator_sequence=claim.segment_sequence, creator_generation=claim.ownership_generation,
        action_json=bound_action_json(stack, action_owner=action_owner,
                                      read_tool_name=read_tool_name, read_tool_arguments_json=read_tool_arguments_json),
    ))
    next_snapshot_json = _effect_snapshot(next_snapshot_json, effect_key=effect_key, disposition="prepared", receipt_id=None)
    retained_policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    next_action = retained_policy.actions.read if action_owner == "read_tool" else retained_policy.actions.session_sql
    conn.execute(update(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
    ).values(next_action_json=next_action.model_dump_json(), policy_sha256=stack.policy_hash))
    next_snapshot_json = retain_action_execution_receipt_on_connection(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id, effect_key=effect_key,
        disposition="prepared", snapshot_json=next_snapshot_json,
    )
    return append_complete_snapshot(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=next_snapshot_json,
        event_batch=(ContinuationEvent("tool_prepared", canonical_json({"effect_key": effect_key, "input_sha256": input_sha256})),),
        handoff=False,
    )


def finish_sql_effect_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, effect_key: str,
    input_sha256: str, receipt_id: str, result_json: str,
    inserted_state_ids: tuple[UUID, ...], next_snapshot_json: str,
    expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    require_continuation_mutation(conn, running.session_operation_context)
    claim = running.claim
    stored = require_snapshot_cas(conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
                                  expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
    if not inserted_state_ids or len(set(inserted_state_ids)) != len(inserted_state_ids):
        raise AuditIntegrityError("SQL graph effect requires actual distinct inserted states")
    final = None
    for state_id in inserted_state_ids:
        final = conn.execute(select(states).where(states.c.session_id == str(claim.session_id), states.c.id == str(state_id))).one()
        if final.creator_operation_id != running.session_operation_context.fence.operation_id:
            raise AuditIntegrityError("SQL effect claims a foreign creator")
    digest = _authored_row_hash(final)
    result_key = f"effect-result:{effect_key}"
    put_chunk(conn, session_id=str(claim.session_id), operation_id=claim.operation_id, key=result_key, payload=result_json.encode("utf-8"), codec="tool-result.v1")
    changed = conn.execute(update(effects).where(
        effects.c.session_id == str(claim.session_id), effects.c.operation_id == claim.operation_id,
        effects.c.effect_key == effect_key, effects.c.input_sha256 == input_sha256,
        effects.c.owner == "session_sql", effects.c.disposition == "prepared",
        effects.c.creator_sequence == claim.segment_sequence, effects.c.creator_generation == claim.ownership_generation,
    ).values(disposition="committed", receipt_key=receipt_id, result_chunk_key=result_key,
             committed_state_ids_json=canonical_json([str(state_id) for state_id in inserted_state_ids]), graph_sha256=digest))
    if changed.rowcount != 1:
        raise AuditIntegrityError("SQL effect receipt lost its prepared creator binding")
    next_snapshot_json = _effect_snapshot(next_snapshot_json, effect_key=effect_key, disposition="committed", receipt_id=receipt_id)
    next_snapshot_json = retain_action_execution_receipt_on_connection(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id, effect_key=effect_key,
        disposition="committed", snapshot_json=next_snapshot_json,
    )
    return append_complete_snapshot(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=next_snapshot_json,
        event_batch=(ContinuationEvent("effect_committed", canonical_json({"effect_key": effect_key, "receipt_id": receipt_id})),), handoff=False,
    )
```

`finish_sql_effect_on_connection` is the primitive for a graph-only SQL owner whose actual receipt is its inserted state ID; the standalone historical-reference test below uses that owner shape. P4 uses C3.8b's literal `persist_compose_turn` hook instead: its receipt ID is the actual assistant message ID, with all inserted state IDs and authored hash in `PersistedComposerTurnReceipt`. Do not invoke both hooks for one P4 effect. C1a adopts validated historical references into its retained committed frontier without calling the fresh `record_checkpoint` increment path.

- [ ] Add a forged-parent/creator test around a real inserted state. This is an assertion test; temporarily skipping the creator comparison must make it fail.

```python
@pytest.mark.asyncio
async def test_historical_effect_keeps_creator_and_refuses_forged_locator(operation_store):
    from sqlalchemy import insert
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.continuation import put_chunk, validate_historical_effect_reference
    from elspeth.web.composer.turn_effects import authored_state_hash
    from elspeth.web.sessions.converters import state_from_record
    engine, repository, authority, service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    saved = await service.save_composition_state(sid, CompositionStateData(), provenance="session_seed", session_operation_context=running.session_operation_context)
    graph_hash = authored_state_hash(state_from_record(saved))
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id, key="result:one", payload=b'{"success":true}', codec="tool-result.v1")
        conn.execute(insert(effects).values(
            session_id=str(sid), operation_id=record.operation_id, effect_key="response:0:tool:0", input_sha256="b" * 64,
            owner="session_sql", disposition="committed", receipt_key=str(saved.id), result_chunk_key="result:one",
            creator_sequence=running.claim.segment_sequence, creator_generation=running.claim.ownership_generation,
            committed_state_ids_json='["' + str(saved.id) + '"]', graph_sha256=graph_hash,
        ))
        args = dict(session_id=sid, operation_id=record.operation_id, effect_key="response:0:tool:0",
                    expected_input_sha256="b" * 64, expected_creator_operation_id=running.session_operation_context.fence.operation_id,
                    expected_receipt_id=str(saved.id), expected_inserted_state_ids=(saved.id,), expected_authored_state_hash=graph_hash)
        first = validate_historical_effect_reference(conn, **args)
        second = validate_historical_effect_reference(conn, **args)
        assert first == second and first.inserted_state_ids == (saved.id,)
        assert first.creator_operation_epoch == running.session_operation_context.fence.operation_epoch
        with pytest.raises(AuditIntegrityError, match="creator"):
            validate_historical_effect_reference(conn, **(args | {"expected_creator_operation_id": str(uuid4())}))
        with pytest.raises(AuditIntegrityError, match="committed SQL owner"):
            validate_historical_effect_reference(conn, **(args | {"operation_id": str(uuid4())}))
    repository.release(running.session_operation_context)
```

This test proves nominal validated references are repeatable reads, not additional graph revisions. Main C1a's retained `committed_revision_ids` assertion verifies no duplicate accounting when these references are adopted under a new live fence.

### C3.8b: Retain actual proposal and P4 owner results inside their existing mutation transactions

**Files:** Extend `sessions/continuation.py`; add `sessions/continuation_owner_results.py`; modify the actual `SessionServiceImpl.persist_compose_turn`, `persist_compose_turn_async`, `create_composition_proposal`, `create_pipeline_composition_proposal`, `create_pipeline_composition_proposal_finish_once`, `_create_pipeline_composition_proposal` and matching protocol signatures. The batch driver owns the stable prepared effect key/input hash and selected checkpoint; these hooks do not infer commit from `ToolResult`.

- [ ] Add the complete owned result models/codecs below. A P4 result stores state IDs and original creator as historical locator data, never a serialized `CommittedCheckpointWitness`. A proposal result retains the actual full nominal `CompositionProposalRecord`; its original event is independently verified by existing proposal authority. Use `TypeAdapter` over the actual owned dataclass and reject a noncanonical round-trip rather than dropping fields. Its argument mappings and nested metadata base are frozen by the existing DTO: serialize all ordinary fields strictly, then restore only those frozen JSON fields through F1.2's `owned_json`. Install that complete helper first. Keep distinct original/redacted values, every actual DTO field, metadata `None`, strict serialization warnings and closed-JSON refusal in the focused codec tests.

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Literal, Self
from uuid import UUID

from pydantic import BaseModel, ConfigDict, TypeAdapter, ValidationError, model_validator

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.json_parser import parse_json_strict
from elspeth.web.composer.continuation_codec import owned_json
from elspeth.web.sessions.protocol import CompositionProposalRecord, PipelineProposalPublicMetadata


class PersistedComposerTurnReceipt(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["persisted_turn"] = "persisted_turn"
    effect_key: str
    input_sha256: str
    assistant_id: UUID
    current_state_id: UUID | None
    checkpoint_disposition: Literal["committed", "no_new_checkpoint", "empty"]
    creator_operation_id: str
    inserted_state_ids: tuple[UUID, ...]
    authored_state_hash: str | None
    chat_message_ids: tuple[UUID, ...]

    @model_validator(mode="after")
    def actual_checkpoint_bundle(self) -> Self:
        if self.checkpoint_disposition == "committed":
            if not self.inserted_state_ids or self.current_state_id != self.inserted_state_ids[-1] or self.authored_state_hash is None:
                raise ValueError("P4 receipt lost its actual inserted checkpoint bundle")
        elif self.inserted_state_ids or self.authored_state_hash is not None:
            raise ValueError("P4 receipt invented a newly inserted checkpoint")
        if self.assistant_id not in self.chat_message_ids or len(set(self.chat_message_ids)) != len(self.chat_message_ids):
            raise ValueError("P4 receipt lost its actual distinct transcript rows")
        return self


_PROPOSAL = TypeAdapter(CompositionProposalRecord)
_PIPELINE_METADATA = TypeAdapter(PipelineProposalPublicMetadata)


def encode_proposal_owner_result(record: CompositionProposalRecord) -> str:
    if type(record) is not CompositionProposalRecord:
        raise TypeError("proposal retention requires its actual nominal writer result")
    if not isinstance(record.arguments_json, Mapping) or not isinstance(record.arguments_redacted_json, Mapping):
        raise AuditIntegrityError("proposal retention requires argument objects")
    payload = _PROPOSAL.dump_python(
        record,
        mode="json",
        warnings="error",
        exclude={"arguments_json", "arguments_redacted_json", "pipeline_metadata"},
    )
    payload["arguments_json"] = owned_json(record.arguments_json)
    payload["arguments_redacted_json"] = owned_json(record.arguments_redacted_json)
    metadata = record.pipeline_metadata
    if metadata is None:
        payload["pipeline_metadata"] = None
    else:
        if type(metadata) is not PipelineProposalPublicMetadata or not isinstance(metadata.base, Mapping):
            raise AuditIntegrityError("proposal retention requires actual pipeline metadata with a base object")
        metadata_payload = _PIPELINE_METADATA.dump_python(metadata, mode="json", warnings="error", exclude={"base"})
        metadata_payload["base"] = owned_json(metadata.base)
        payload["pipeline_metadata"] = metadata_payload
    return canonical_json(payload)


def decode_proposal_owner_result(encoded: str) -> CompositionProposalRecord:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained proposal result is not strict JSON")
    try:
        result = _PROPOSAL.validate_json(canonical_json(value), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("retained proposal result violates its owned codec") from exc
    if encode_proposal_owner_result(result) != canonical_json(value):
        raise AuditIntegrityError("retained proposal result lost fields during decode")
    return result
```

- [ ] Add the following immutable dispatch binding and same-connection helper to `sessions/continuation.py`. Initially install only `ContinuationOwnerCommit` and `require_owner_commit`, replacing the entire different-creator branch with `raise ContinuationCASConflict("business SQL effect changed its immutable preparing owner")`; omit the complete selected-read helper until main step 9 supplies its real dependencies and B4's refined carrier. Keep the exact-True guard and actual context-history/segment claim tuple in both phases. The initial boundary refuses every different creator, including reads. Add the rest of this complete writer/pending-reader cluster only with the real read-result functions, whose unconditional imports must resolve. `RetainedOwnerResultRef` and root `pending_owner_results` carry `(effect_key, kind, result:PrivateChunkRef)` with kind `proposal|persisted_turn|read|interpretation`. Cold application consumes the ref and persists the advanced frame once; it never reruns the mutation or remints its original witness. Import `CompositionState` from `composer.state` for the optional actual credential-state argument only when installing that later writer.

At main step 9, B4's final verifier refinement adds actual preparing-attempt existence and unset-committer checks while retaining this creator policy. Only the selected immutable read branch below may cross preparing sequence/generation, with actual current authorization and C3.8e's version/response admission before retention. A still-prepared business SQL effect, including proposal, interpretation and P4, refuses a different creator even when its input is unchanged and no result exists. An already committed result follows the separate current-authorized pending-result application path without another business write. The four complete passive read-result functions may precede activation under C3.8e's prerequisite boundary. Keep selected-read dependencies, complete result writer/pending reader, and service/protocol hooks together; the initial verifier remains unchanged until that real cluster exists. Cross-creator uncommitted business SQL recovery is an unresolved policy requirement, not an implicit grant from B4 committer provenance.

```python
@dataclass(frozen=True, slots=True)
class ContinuationOwnerCommit:
    running: ComposerOperationRunning
    effect_key: str
    input_sha256: str
    expected_checkpoint_id: str
    expected_cursor: int


def require_owner_commit(
    conn: Connection, commit: ContinuationOwnerCommit, context: SessionOperationContext, *, allow_completed_read: bool = False,
) -> StoredContinuationSnapshot:
    if (
        type(commit) is not ContinuationOwnerCommit
        or type(commit.running) is not ComposerOperationRunning
        or type(context) is not SessionOperationContext
        or commit.running.session_operation_context != context
    ):
        raise AuditIntegrityError("SQL owner commit changed its actual execution context")
    if require_continuation_mutation(
        conn, context, allow_cancel=allow_completed_read, allow_expired_segment=allow_completed_read,
    ) is not True:
        raise AuditIntegrityError("SQL owner commit requires current continuation authority")
    claim = commit.running.claim
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == context.fence.session_id,
        attempts.c.session_operation_epoch == context.fence.operation_epoch,
    )).one()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == history.session_id,
        segments.c.operation_id == history.operation_id,
        segments.c.sequence == history.sequence,
        segments.c.generation == history.generation,
    )).one()
    if (
        history.session_id, history.operation_id, history.sequence, history.generation,
        history.claim_attempt, segment.claim_token,
    ) != (
        str(claim.session_id), claim.operation_id, claim.segment_sequence, claim.ownership_generation,
        claim.attempt, claim.claim_token,
    ):
        raise AuditIntegrityError("SQL owner commit changed its current claim/history binding")
    stored = require_snapshot_cas(conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
                                  expected_checkpoint_id=commit.expected_checkpoint_id, expected_cursor=commit.expected_cursor)
    effect = conn.execute(select(effects).where(
        effects.c.session_id == str(claim.session_id), effects.c.operation_id == claim.operation_id,
        effects.c.effect_key == commit.effect_key,
    )).one()
    if allow_completed_read and effect.owner != "read":
        raise AuditIntegrityError("owed read-result application cannot authorize a business SQL effect")
    if (effect.owner not in ("session_sql", "read") or effect.disposition != "prepared" or effect.input_sha256 != commit.input_sha256):
        raise ContinuationCASConflict("SQL owner result is already retained or its prepared authority changed")
    same_creator = (effect.creator_sequence, effect.creator_generation) == (claim.segment_sequence, claim.ownership_generation)
    if not same_creator:
        if not allow_completed_read or effect.owner != "read":
            raise ContinuationCASConflict("business SQL effect changed its immutable preparing owner")
        require_selected_read_input_on_connection(conn, commit, stored)
    return stored


def require_selected_read_input_on_connection(
    conn: Connection, commit: ContinuationOwnerCommit, stored: StoredContinuationSnapshot,
) -> None:
    from elspeth.contracts.hashing import stable_hash
    from elspeth.web.composer.advisor_output import ReadContinuationAction
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_codec import decode_frontier_chunk
    from elspeth.web.composer.continuation_evidence import state_from_evidence_json
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_progress import continuation_action_key, decode_prescribed_continuation_action
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
    root = decode_continuation_snapshot(stored.payload_json)
    values = load_private_chunks(conn, session_id=str(commit.running.claim.session_id),
        operation_id=commit.running.claim.operation_id, stack=root)
    top = root.frames[-1]
    if type(top) is ToolBatchFrame:
        require_selected_tool_completion(conn, commit, stored)
        batch = decode_batch_chunk(continuation_chunk(values, top.accumulator))
        call = batch.current_call
        if call is None or call.audit is None or call.completion_json is None or commit.tool_completion is None:
            raise AuditIntegrityError("repeated immutable read lost its selected actual call/input")
        expected_key = f"batch:{top.response.digest}:call:{call.index}"
        expected_input = stable_hash({"schema": "composer.tool-input.v1", "response": top.response.digest,
            "index": call.index, "call_id": call.call_id,
            "arguments": call.audit.authority_arguments_hash or call.audit.arguments_hash,
            "completion": commit.tool_completion.value.model_dump(mode="json")})
    elif type(top) is ComposeFrame and root.active_action_json is not None and root.semantic_frontier is not None:
        action = decode_prescribed_continuation_action(root.active_action_json)
        if type(action) is not ReadContinuationAction or commit.tool_completion is not None:
            raise AuditIntegrityError("repeated direct read lost its exact prescribed variant")
        require_continuation_action_source_on_connection(conn, commit.running, stored=stored)
        frontier = decode_frontier_chunk(continuation_chunk(values, root.semantic_frontier))
        action_key = continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence})
        expected_key = f"action:read:{action_key}"
        state = state_from_evidence_json(continuation_chunk(values, top.state).canonical)
        expected_input = stable_hash({"schema": "composer.prescribed-read-input.v1",
            "logical": root.logical.model_dump(mode="json"), "action": action.model_dump(mode="json"),
            "authored_state": state.to_dict()})
    else:
        raise AuditIntegrityError("old prepared read has no exact currently selected immutable read owner")
    if (commit.effect_key, commit.input_sha256) != (expected_key, expected_input):
        raise AuditIntegrityError("old prepared read changed its actual immutable input")


def retain_owner_result_on_connection(
    conn: Connection, commit: ContinuationOwnerCommit, *, kind: Literal["proposal", "persisted_turn", "read", "interpretation"],
    receipt_id: str, canonical_result: str, inserted_state_ids: tuple[UUID, ...] = (), authored_hash: str | None = None,
    credential_state: CompositionState | None = None,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import RetainedOwnerResultRef
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.credential_guard import require_no_credential_material, require_no_credential_material_for_tool
    from elspeth.web.sessions.continuation_owner_results import (
        PersistedComposerTurnReceipt, decode_interpretation_owner_result, decode_proposal_owner_result,
        decode_read_owner_result, require_read_versions_on_connection,
    )
    stored = require_owner_commit(conn, commit, commit.running.session_operation_context, allow_completed_read=kind == "read")
    parsed, error = parse_json_strict(canonical_result)
    if error is not None:
        raise AuditIntegrityError("SQL owner result is not strict JSON")
    effect = conn.execute(select(effects).where(
        effects.c.session_id == str(commit.running.claim.session_id),
        effects.c.operation_id == commit.running.claim.operation_id, effects.c.effect_key == commit.effect_key,
    )).one()
    if (kind == "read") != (effect.owner == "read"):
        raise AuditIntegrityError("retained result kind differs from its prepared owner")
    if kind == "proposal":
        proposal = decode_proposal_owner_result(canonical_result)
        if proposal.session_id != commit.running.claim.session_id:
            raise AuditIntegrityError("proposal result belongs to another session")
        require_no_credential_material_for_tool(proposal.tool_name, proposal.arguments_json,
            surface="continuation proposal arguments", credential_state=credential_state)
        require_no_credential_material_for_tool(proposal.tool_name, proposal.arguments_redacted_json,
            surface="continuation proposal audit arguments", credential_state=credential_state)
    elif kind == "persisted_turn":
        receipt = PersistedComposerTurnReceipt.model_validate_json(canonical_result)
        if (receipt.effect_key, receipt.input_sha256) != (commit.effect_key, commit.input_sha256):
            raise AuditIntegrityError("P4 result differs from its prepared owner")
        require_no_credential_material(parsed, surface="continuation P4 metadata")
    elif kind == "interpretation":
        decode_interpretation_owner_result(canonical_result)
        require_no_credential_material(parsed, surface="continuation interpretation metadata")
    else:
        read = decode_read_owner_result(canonical_result)
        require_read_versions_on_connection(conn, session_id=str(commit.running.claim.session_id), result=read)
    # Scan the complete serialized record for credential values, without treating
    # graph-owned field-mapper labels as newly introduced credential slots.
    require_no_credential_material(canonical_result, surface="continuation SQL owner wire",
                                   max_string_bytes=len(canonical_result.encode("utf-8")))
    chunk = encode_private_chunk(codec="composer.owner-result.v1", canonical=canonical_json(parsed))
    sid, operation_id = str(commit.running.claim.session_id), commit.running.claim.operation_id
    key = private_chunk_key(chunk)
    put_chunk(conn, session_id=sid, operation_id=operation_id, key=key,
              payload=chunk.canonical.encode("utf-8"), codec=chunk.reference.codec)
    conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id,
        effects.c.effect_key == commit.effect_key,
    ).values(disposition="committed", receipt_key=receipt_id, result_chunk_key=key,
             committed_state_ids_json=canonical_json([str(item) for item in inserted_state_ids]) if inserted_state_ids else None,
             graph_sha256=authored_hash))
    stack = decode_continuation_snapshot(stored.payload_json)
    if any(item.effect_key == commit.effect_key for item in stack.pending_owner_results):
        raise AuditIntegrityError("SQL owner result pending application is duplicated")
    from elspeth.web.composer.continuation import EffectFrontierEntry
    frontier = tuple(item for item in stack.effect_frontier if item.effect_id != commit.effect_key)
    stack = stack.model_copy(update={
        "pending_owner_results": (*stack.pending_owner_results,
            RetainedOwnerResultRef(effect_key=commit.effect_key, kind=kind, result=chunk.reference)),
        "effect_frontier": (*frontier, EffectFrontierEntry(effect_id=commit.effect_key,
                                                          disposition="committed", receipt_id=receipt_id)),
    })
    encoded = encode_continuation_snapshot(stack)
    encoded = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=commit.effect_key,
        disposition="committed", snapshot_json=encoded,
    )
    return append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=encoded,
        event_batch=(ContinuationEvent("effect_committed", canonical_json({"effect_key": commit.effect_key,
                     "kind": kind, "receipt_id": receipt_id, "phase": "owner_result_retained"})),), handoff=False,
    )
```

- [ ] Add optional `continuation: ContinuationOwnerCommit | None = None` to the six named service methods and protocol declarations; forward it in `persist_compose_turn_async`'s existing call to `persist_compose_turn`, both public pipeline proposal methods' calls to `_create_pipeline_composition_proposal`, and nowhere on v1 routes. In each `_sync`/sync transaction, place this exact block immediately after `_session_composer_mutation_transaction` has entered and before any sequence reservation or actual proposal write:

```python
if continuation is not None:
    require_owner_commit(conn, continuation, session_operation_context)
```

- [ ] In `create_composition_proposal._sync`, change its existing `return transaction.composer.create_composition_proposal(...)` into `record = transaction.composer.create_composition_proposal(...)`, keeping every argument unchanged. Immediately after that actual writer returns, inside the same transaction, add:

```python
if continuation is not None:
    credential_row = conn.execute(select(composition_states_table).where(
        composition_states_table.c.session_id == str(record.session_id),
        composition_states_table.c.id == str(record.base_state_id),
    )).one_or_none() if record.base_state_id is not None else None
    if record.base_state_id is not None and credential_row is None:
        raise AuditIntegrityError("proposal retention lost its actual same-session base state")
    retain_owner_result_on_connection(conn, continuation, kind="proposal", receipt_id=str(record.id),
        canonical_result=encode_proposal_owner_result(record),
        credential_state=state_from_record(self._row_to_state_record(credential_row)) if credential_row is not None else None)
return record
```

- [ ] Apply the exact same assignment/hook block to `_create_pipeline_composition_proposal._sync`'s actual `transaction.composer.create_pipeline_composition_proposal(...)` call. Preserve its existing `derive_proposal_composer_binding_on_connection`, current required-work authority check, full creation payload and finish-once handoff. No new UUID is minted when applying a retained result; all existing proposal/event UUID allocation remains in the actual fresh writer path.

- [ ] In `persist_compose_turn`, immediately after the current `checkpoint = CommittedCheckpointWitness(...)` block and still inside the inner mutation transaction, add this full hook. It queries actual inserted rows under the held write lock. Existing v1 `AuditOutcome` return and every failure/unwind disposition remain unchanged. V2 never serializes `checkpoint` itself; it retains the actual owner receipt before a caller cancellation can hide the return value.

```python
if continuation is not None:
    from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt
    from elspeth.web.composer.turn_effects import authored_state_hash
    written_ids = tuple(UUID(value) for value in conn.execute(select(chat_messages_table.c.id).where(
        chat_messages_table.c.session_id == session_id,
        chat_messages_table.c.sequence_no >= base_seq,
        chat_messages_table.c.sequence_no < base_seq + 1 + 2 * len(redacted_tool_rows) + len(private_tool_failures),
    ).order_by(chat_messages_table.c.sequence_no)).scalars())
    receipt = PersistedComposerTurnReceipt(
        effect_key=continuation.effect_key, input_sha256=continuation.input_sha256,
        assistant_id=UUID(assistant_id), current_state_id=UUID(current_state_id) if current_state_id is not None else None,
        checkpoint_disposition="committed" if checkpoint is not None else "no_new_checkpoint" if current_state_id is not None else "empty",
        creator_operation_id=session_operation_context.fence.operation_id,
        inserted_state_ids=tuple(inserted_state_ids),
        authored_state_hash=authored_state_hash(state_from_record(checkpoint.state)) if checkpoint is not None else None,
        chat_message_ids=written_ids,
    )
    retain_owner_result_on_connection(
        conn, continuation, kind="persisted_turn", receipt_id=assistant_id,
        canonical_result=canonical_json(receipt.model_dump(mode="json")),
        inserted_state_ids=receipt.inserted_state_ids, authored_hash=receipt.authored_state_hash,
    )
```

- [ ] Add the complete pending-result read facet below to `ComposerAsyncOperationAuthority`. This current-owner read verifies the selected root, SQL effect, exact result chunk and immutable original creator. It returns bytes for closed nominal decode/application; a caller cannot submit an arbitrary locator or replace fresh execution authority with a stored lease.

```python
def load_pending_owner_result(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int, effect_key: str,
) -> tuple[str, str]:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        stored = require_snapshot_cas(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
                                     expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        stack = decode_continuation_snapshot(stored.payload_json)
        pending = tuple(item for item in stack.pending_owner_results if item.effect_key == effect_key)
        if len(pending) != 1:
            raise AuditIntegrityError("selected snapshot has no exact pending SQL owner result")
        effect = conn.execute(select(effects).where(
            effects.c.session_id == str(running.claim.session_id), effects.c.operation_id == running.claim.operation_id,
            effects.c.effect_key == effect_key,
        )).one()
        if effect.disposition != "committed" or effect.result_chunk_key != f"value:{pending[0].result.codec}:{pending[0].result.digest}":
            raise AuditIntegrityError("pending result differs from the actual committed effect")
        conn.execute(select(attempts).where(
            attempts.c.session_id == str(running.claim.session_id), attempts.c.operation_id == running.claim.operation_id,
            attempts.c.sequence == effect.creator_sequence, attempts.c.generation == effect.creator_generation,
        )).one()
        canonical = read_chunk(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
                               key=effect.result_chunk_key).decode("utf-8", errors="strict")
        verify_private_chunk(pending[0].result, canonical)
        if pending[0].kind == "read":
            from elspeth.web.sessions.continuation_owner_results import decode_read_owner_result, require_read_versions_on_connection
            require_read_versions_on_connection(conn, session_id=str(running.claim.session_id),
                                                result=decode_read_owner_result(canonical))
        return pending[0].kind, canonical
```

- [ ] Add this complete actual-writer test to the cold-restore module. It injects a failure after retention SQL has run but before the transaction exits, proves both owner rows and snapshot roll back, then checks the real returned writer result matches the retained owner receipt. It covers direct proposal, pipeline proposal and P4 audit persistence.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("owner_kind", ["direct_proposal", "pipeline_proposal", "persisted_turn"])
async def test_actual_owner_result_and_mutation_share_one_transaction(operation_store, monkeypatch, owner_kind):
    import elspeth.web.sessions.service as service_module
    from elspeth.core.canonical import stable_hash
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationOwnerCommit, prepare_effect_on_connection
    from elspeth.web.sessions.continuation_owner_results import PersistedComposerTurnReceipt, decode_proposal_owner_result
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import chat_messages_table, composition_proposals_table, proposal_events_table
    engine, repository, authority, service, sid = operation_store
    record, seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    ingress = await service.add_message_with_transcript(
        sid, "user", "Preserve café and exact context", operation_id=UUID(record.operation_id),
        requested_state_id=None, writer_principal="route_user_message", running=running,
        reserved_user_message_id=UUID(seed.reserved_user_message_id), session_operation_context=running.session_operation_context,
    )
    selected = authority.load_continuation_snapshot(running)
    with locked_session_transaction(engine, str(sid)) as conn:
        prepared = prepare_effect_on_connection(
            conn, running, effect_key="response:1:tool:0", input_sha256="b" * 64, owner="session_sql",
            next_snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
    commit = ContinuationOwnerCommit(running, "response:1:tool:0", "b" * 64, prepared.checkpoint_id, prepared.cursor)
    pipeline = {"sources": {}, "nodes": [], "edges": [], "outputs": []}
    planned = PipelineProposal.create(pipeline=pipeline, base=AbsentBase(), repair_count=0, skill_hash=stable_hash("planner-skill"))
    plan = PipelinePlanResult(proposal=planned, tool_call_id="actual_owner_call", custody_result="not_required",
                             model_identifier="planner", model_version="planner-v1", provider="test")
    async def write():
        if owner_kind == "direct_proposal":
            return await service.create_composition_proposal(
                session_id=sid, tool_call_id="actual_owner_call", tool_name="set_pipeline", summary="Requested graph",
                rationale="User request", affects=("graph",), arguments_json=pipeline, arguments_redacted_json=pipeline,
                base_state_id=None, actor="composer-web:user:alice", user_message_id=ingress.message.id,
                session_operation_context=running.session_operation_context, continuation=commit,
            )
        if owner_kind == "pipeline_proposal":
            return await service.create_pipeline_composition_proposal(
                session_id=sid, plan=plan, summary="Requested graph", rationale="User request", affects=("graph",),
                arguments_redacted_json=pipeline, actor="composer-web:user:alice", user_message_id=ingress.message.id,
                composer_model_identifier="planner", composer_model_version="planner-v1", composer_provider="test",
                session_operation_context=running.session_operation_context, continuation=commit,
            )
        return await service.persist_compose_turn_async(
            session_id=str(sid), assistant_content="Exact retained P4 audit", raw_content=None,
            redacted_assistant_tool_calls=(), redacted_tool_rows=(), private_tool_evidence=(),
            parent_composition_state_id=None, expected_current_state_id=None, writer_principal="compose_loop",
            plugin_crash_pending=False, session_operation_context=running.session_operation_context, continuation=commit,
        )
    def rows():
        with engine.connect() as conn:
            return (conn.execute(select(chat_messages_table).where(chat_messages_table.c.session_id == str(sid))).all(),
                    conn.execute(select(composition_proposals_table).where(composition_proposals_table.c.session_id == str(sid))).all(),
                    conn.execute(select(proposal_events_table).where(proposal_events_table.c.session_id == str(sid))).all())
    before = rows()
    actual_retain = service_module.retain_owner_result_on_connection
    injected = AuditIntegrityError("controlled owner retention failure")
    def fail_after_retention(*args, **kwargs):
        actual_retain(*args, **kwargs)
        raise injected
    try:
        with monkeypatch.context() as mutation:
            mutation.setattr(service_module, "retain_owner_result_on_connection", fail_after_retention)
            with pytest.raises(AuditIntegrityError) as caught:
                await write()
            assert caught.value is injected
        assert rows() == before
        assert authority.load_continuation_snapshot(running) == prepared
        result = await write()
        selected = authority.load_continuation_snapshot(running)
        kind, encoded = authority.load_pending_owner_result(
            running, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor, effect_key=commit.effect_key,
        )
        if owner_kind == "persisted_turn":
            receipt = PersistedComposerTurnReceipt.model_validate_json(encoded, strict=True)
            assert kind == "persisted_turn" and str(receipt.assistant_id) == result.assistant_id
            assert receipt.checkpoint_disposition == "empty" and receipt.inserted_state_ids == ()
        else:
            retained = decode_proposal_owner_result(encoded)
            assert kind == "proposal" and retained == result
        after = rows()
        with pytest.raises(ContinuationCASConflict):
            await write()
        assert rows() == after
        assert not decode_continuation_snapshot(selected.payload_json).is_handoff_safe
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Bootstrap the importable owner-hook packet, then prove behavioral RED by temporarily moving only the retention hook after the existing transaction exits. The injected failure test must fail `rows() == before`, exposing committed owner rows without matching retained snapshot. Restore the in-transaction hook. Run the command below, requiring native exit 0, before committing the exact new owner model/service/protocol/test paths as `feat(composer): retain actual SQL owner results atomically`.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_actual_owner_result_and_mutation_share_one_transaction >"$COMPOSER_LOG_DIR/c3-owner-results.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-owner-results.log"
test "$composer_exit" -eq 0
```

### C3.9: Retain the logical writer reservation between physical leases

**Files:** Modify `_SessionOperationAuthorityRepository._advance_exclusive_fence_on_connection` in `coordination/repository.py`; use C3.4 Task 4's standalone `test_composer_continuation_claim_start.py` and its PostgreSQL collector. This is the same preactivation guard specified there: install it once, preserving the actual old takeover/CAS body.

- [ ] Add optional keyword-only `composer_operation_id: str | None = None` to this private helper. Before its existing current-fence selection, add the complete guard below. Import `composer_async_operations_table` into this repository module. Its default applies to every existing exclusive acquisition; only the v2 segment-start method passes the public parent ID.

```python
parent = conn.execute(select(composer_async_operations_table.c.operation_id).where(
    composer_async_operations_table.c.session_id == session_id_text,
    composer_async_operations_table.c.lifecycle_version == 2,
    composer_async_operations_table.c.status.in_(("queued", "running")),
).limit(1)).scalar_one_or_none()
if parent is not None and (operation_kind is not SessionOperationKind.COMPOSE or composer_operation_id != parent):
    raise SessionOperationConflictError
```

In `start_composer_segment`'s call to this helper, append `composer_operation_id=claim.operation_id`. No other caller receives an exception. Shareable `BLOB_READ` continues through the existing separate `_admit_blob_read` authority and never enters this exclusive helper. Archive, Revert, Execute, proposal mutations and ordinary unrelated COMPOSE writers stay busy while this logical request owns its reservation.

- [ ] Run the complete standalone reservation test and exact PostgreSQL alias in C3.4 Task 4 before exposure. It imports the nominal `SessionOperationConflictError` from `elspeth.web.coordination.repository`, attempts actual ARCHIVE before first physical start, requires complete registered-row equality on refusal, then positively starts the matching parent. No future yield API or two-fence fixture is required.

Temporarily removing only the logical-parent guard must fail that actual ARCHIVE refusal; restore it before GREEN. This also protects all private chunks/checkpoints from between-segment whole-session deletion; no new export, retention duration, cache reset or migration is introduced.

### C5.2a: Initial prepared-only storage preparation and observation

**Files:** Extend `src/elspeth/web/sessions/continuation.py` and `src/elspeth/web/coordination/composer_operation_authority.py`; extend the C1b prefix in `tests/unit/web/sessions/test_continuation_cold_restore.py`; export the five ordinary tests through `tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py`. Do not install service/protocol writers, provider custody, quota/SDK hooks, settings/worker activation or the late C1b cold test in this increment.

This is the finite first-fence storage variant used at main step 7. It requires the actual C2 schema/chunk repository, strict frame/policy/private-chunk codecs, initial C3 claim/start/history, current owner verifier and C3.8's four already-installed shared utilities. Reuse `bound_action_json`, `retain_action_execution_receipt_on_connection`, `_effect_snapshot` and `private_chunk_key` without redefining or weakening them. The original primitive retains legacy False behavior. New boundaries require exact nominal Running/claim/context, strict True, and the unconditional actual SQL history/segment six-tuple. Fresh preparation also refuses an authentic active owed-P4 scope even when that primitive deliberately returns True for draining owed work.

**Phase limit:** family0 with a reserved initializer and arbitrary `"a" * 64` is a storage key/input association, not actual selected physical provider eligibility. Family-specific `next_action_json`, a nominal binding or the returned complete checkpoint supplies no actual source/action/input/clock/SDK admission. The later raw `mint_continuation` nominality/context check is a concrete HOLD until the actual selected owners and fresh eligibility checks are identified and qualified. No stand-in DTO, seal, owner, synthetic source or alternate provider path fills that gap.

- [ ] **Step 1: Add these complete ordinary first-fence tests, then establish their genuine RED.** Retain the existing C1b helper bodies; the complete initial module below supplies their exact current imports/binding and appends all five tests. If the prefix already exists, merge this complete module without adding a second helper or duplicate test. The module-owner assignment exports the exact existing decorated fixture object and avoids the measured imported-name F811; it constructs no alternate authority. Do not append C1b's two-fence body until main step 9.

```python
from dataclasses import replace
from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select, update

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.session_operation import SessionOperationKind
from elspeth.web.composer.continuation import prepare_composer_continuation_admission
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
from elspeth.web.coordination.rate_limit_authority import ComposerQuotaAdmission, RepositoryRateLimitAuthority
from elspeth.web.sessions.composer_operations import (
    ComposerOperationFenceLost,
    ComposerOperationRunning,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation import ContinuationCASConflict, load_snapshot
from elspeth.web.sessions.models import sessions_table
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination import test_composer_operation_authority

operation_store = test_composer_operation_authority.operation_store


def admit_continuation(store):
    engine, _repository, authority, _service, sid = store
    request = SendMessageRequest(operation_id=str(uuid4()), content="Preserve café and exact context")
    request_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request)
    policy = minimal_continuation_policy()
    action = policy.actions.session_sql
    with engine.connect() as conn:
        initial_trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(sid))).scalar_one()
    seed = prepare_composer_continuation_admission(
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_json=request.model_dump_json(),
        request_hash=request_hash,
        base_state_id=None,
        policy_hash=policy.policy_hash,
        admission_policy_json=policy.model_dump_json(),
        initial_action_json=action.model_dump_json(),
        initial_trust_mode=initial_trust_mode,
    )
    record, fresh = authority.admit(
        quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
        session_id=sid,
        operation_id=request.operation_id,
        kind="compose_message",
        request_hash=request_hash,
        actor_user_id="alice",
        request_id="continuation",
        base_state_id=None,
        request_json=request.model_dump_json(),
        deadline_seconds=180,
        max_nonterminal=64,
        auth_provider_type="local",
        lifecycle_version=2,
        continuation_enabled=True,
        seed=seed,
    )
    assert fresh
    return record, seed, action


def start_continuation(repository, authority):
    (claim,) = authority.claim_segment(limit=1)
    context = repository.start_composer_segment(
        claim,
        owner_instance_id="test-owner",
        lease_seconds=30,
        auth_provider_type="local",
    )
    return ComposerOperationRunning(claim=claim, session_operation_context=context)


def test_provider_action_facades_return_the_actual_complete_checkpoint(operation_store):
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    selected = authority.load_continuation_snapshot(running)
    prepared = authority.prepare_provider_action(
        running,
        family=0,
        input_sha256="a" * 64,
        snapshot_json=selected.payload_json,
        expected_checkpoint_id=selected.checkpoint_id,
        expected_cursor=selected.cursor,
    )
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
    assert prepared.snapshot.checkpoint_id != selected.checkpoint_id
    assert prepared.snapshot.cursor > selected.cursor
    observed = authority.observe_provider_action(prepared.binding)
    assert observed.snapshot == prepared.snapshot
    assert observed.effect_key == prepared.binding.effect_key and observed.disposition == "prepared"
    assert observed.response is None and observed.provider_attempt_id is None and observed.ledger_entry_id is None
    repository.release(running.session_operation_context)


@pytest.mark.parametrize(
    "entrypoint", ("prepare_provider_effect", "prepare_provider_action", "load_continuation_snapshot", "observe_provider_action")
)
@pytest.mark.parametrize(
    "change", ("claim_token", "claim_attempt", "no_history_context", "session", "expired_lease", "released_lease", "nominal_running")
)
def test_initial_provider_boundaries_require_actual_claim_without_writes(operation_store, entrypoint, change):
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.continuation import prepare_provider_effect_on_connection, require_continuation_mutation
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_segment_attempts_table as attempts
    from elspeth.web.sessions.models import metadata
    from elspeth.web.sessions.models import session_operation_fences_table as fences

    engine, repository, authority, service, sid = operation_store
    ordinary = None
    if change == "no_history_context":
        ordinary = repository.acquire(
            session_id=sid,
            operation_kind=SessionOperationKind.COMPOSE,
            owner_instance_id="test-owner",
            lease_seconds=30,
        )
        with engine.connect() as conn:
            assert (
                conn.execute(
                    select(attempts).where(
                        attempts.c.session_id == str(sid),
                        attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                    )
                ).one_or_none()
                is None
            )
            assert require_continuation_mutation(conn, ordinary) is False
        repository.release(ordinary)
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    other = None
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        # Exercise the actual SQL preparation positively, then roll back only this savepoint.
        # No retained receipt from this rollback is treated as committed authority.
        with locked_session_transaction(engine, str(sid)) as conn:
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            assert require_continuation_mutation(conn, running.session_operation_context) is True
            positive = conn.begin_nested()
            try:
                first = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                prepared = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
                assert first.effect_key == "provider:0:0" and first.input_sha256 == "a" * 64
                assert prepared.checkpoint_id != selected.checkpoint_id and prepared.cursor > selected.cursor
            finally:
                positive.rollback()
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
        binding = None
        if entrypoint == "observe_provider_action":
            committed = authority.prepare_provider_action(
                running,
                family=0,
                input_sha256="a" * 64,
                snapshot_json=selected.payload_json,
                expected_checkpoint_id=selected.checkpoint_id,
                expected_cursor=selected.cursor,
            )
            binding = committed.binding
            selected = committed.snapshot
            assert authority.observe_provider_action(binding).snapshot == selected
        refused = running
        expected_error = AuditIntegrityError
        expected_message = "continuation boundary changed its current claim/history binding"
        if change in ("claim_token", "claim_attempt"):
            changed_claim = (
                replace(running.claim, claim_token="wrong-current-token")
                if change == "claim_token"
                else replace(
                    running.claim,
                    attempt=running.claim.attempt + 1,
                )
            )
            refused = ComposerOperationRunning(claim=changed_claim, session_operation_context=running.session_operation_context)
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, refused.session_operation_context) is True
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        elif change == "no_history_context":
            assert ordinary is not None
            refused = ComposerOperationRunning(claim=running.claim, session_operation_context=ordinary)
            expected_message = "continuation boundary requires current continuation authority"
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, ordinary) is False
                assert (
                    conn.execute(
                        select(attempts).where(
                            attempts.c.session_id == str(sid),
                            attempts.c.session_operation_epoch == ordinary.fence.operation_epoch,
                        )
                    ).one_or_none()
                    is None
                )
        elif change == "session":
            second_session = repository.create_session_with_initial_fence(
                user_id="alice",
                title="Another actual session",
                auth_provider_type="local",
                owner_instance_id="test-owner",
                lease_seconds=30,
            )
            admit_continuation((engine, repository, authority, service, second_session.id))
            other = start_continuation(repository, authority)
            refused = ComposerOperationRunning(
                claim=replace(running.claim, session_id=second_session.id),
                session_operation_context=other.session_operation_context,
            )
            with engine.connect() as conn:
                assert require_continuation_mutation(conn, other.session_operation_context) is True
                assert other.claim.operation_id != running.claim.operation_id
        elif change == "expired_lease":
            with engine.begin() as conn:
                conn.execute(
                    update(fences)
                    .where(
                        fences.c.session_id == str(sid),
                        fences.c.operation_id == running.session_operation_context.fence.operation_id,
                        fences.c.operation_epoch == running.session_operation_context.fence.operation_epoch,
                    )
                    .values(lease_expires_at=database_now(conn) - timedelta(seconds=1))
                )
            expected_error = ComposerOperationFenceLost
            expected_message = None
        elif change == "released_lease":
            repository.release(running.session_operation_context)
            expected_error = ComposerOperationFenceLost
            expected_message = None
        else:
            refused = object()
            expected_error = TypeError
            expected_message = "continuation boundary requires nominal running custody"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        with pytest.raises(expected_error, match=expected_message):
            if entrypoint == "prepare_provider_effect":
                authority.prepare_provider_effect(
                    refused,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
            elif entrypoint == "prepare_provider_action":
                authority.prepare_provider_action(
                    refused,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
            elif entrypoint == "load_continuation_snapshot":
                authority.load_continuation_snapshot(refused)
            else:
                assert binding is not None
                authority.observe_provider_action(replace(binding, running=refused))
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        if change not in ("expired_lease", "released_lease"):
            assert authority.load_continuation_snapshot(running) == selected
            if entrypoint == "prepare_provider_effect":
                restored = authority.prepare_provider_effect(
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert restored.effect_key == "provider:0:0"
            elif entrypoint == "prepare_provider_action":
                restored = authority.prepare_provider_action(
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert restored.snapshot.checkpoint_id != selected.checkpoint_id
            elif binding is not None:
                assert authority.observe_provider_action(binding).snapshot == selected
    finally:
        if other is not None:
            repository.release(other.session_operation_context)
        if change not in ("expired_lease", "released_lease"):
            repository.release(running.session_operation_context)


@pytest.mark.parametrize("entrypoint", ("prepare_provider_effect", "prepare_provider_action"))
@pytest.mark.parametrize("change", ("checkpoint", "cursor", "selected_stack", "digest", "family"))
def test_initial_provider_preparation_preserves_head_on_invalid_input(operation_store, entrypoint, change):
    from elspeth.web.sessions.continuation import prepare_provider_effect_on_connection
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        with locked_session_transaction(engine, str(sid)) as conn:
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            positive = conn.begin_nested()
            try:
                first = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                assert first.effect_key == "provider:0:0"
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id).checkpoint_id != selected.checkpoint_id
            finally:
                positive.rollback()
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
        args = {
            "family": 0,
            "input_sha256": "a" * 64,
            "snapshot_json": selected.payload_json,
            "expected_checkpoint_id": selected.checkpoint_id,
            "expected_cursor": selected.cursor,
        }
        expected_error = ContinuationCASConflict
        expected_message = "continuation snapshot changed before effect admission"
        if change == "checkpoint":
            args["expected_checkpoint_id"] = str(uuid4())
        elif change == "cursor":
            args["expected_cursor"] = selected.cursor + 1
        elif change == "selected_stack":
            stack = decode_continuation_snapshot(selected.payload_json)
            frame = stack.frames[-1].model_copy(update={"request_json": stack.frames[-1].request_json + " "})
            args["snapshot_json"] = encode_continuation_snapshot(stack.model_copy(update={"frames": (frame,)}))
            expected_message = "provider preparation replaced the actual selected complete head"
        elif change == "digest":
            args["input_sha256"] = "invalid-digest"
            expected_error = ValueError
            expected_message = "provider preparation requires its actual input digest"
        else:
            args["family"] = True
            expected_error = ValueError
            expected_message = "unknown physical provider family"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
        with pytest.raises(expected_error, match=expected_message):
            if entrypoint == "prepare_provider_effect":
                authority.prepare_provider_effect(running, **args)
            else:
                authority.prepare_provider_action(running, **args)
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256="a" * 64,
            snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
        assert prepared.snapshot.checkpoint_id != selected.checkpoint_id
        assert authority.observe_provider_action(prepared.binding).snapshot == prepared.snapshot
    finally:
        repository.release(running.session_operation_context)


def test_initial_provider_observation_binds_actual_selected_input_without_writes(operation_store, monkeypatch):
    from contextlib import contextmanager

    from sqlalchemy import delete

    import elspeth.web.coordination.composer_operation_authority as authority_module
    from elspeth.web.sessions.continuation import put_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        prepared = authority.prepare_provider_action(
            running,
            family=0,
            input_sha256="a" * 64,
            snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id,
            expected_cursor=selected.cursor,
        )
        first = authority.observe_provider_action(prepared.binding)
        assert first.snapshot == prepared.snapshot and first.disposition == "prepared"
        with engine.connect() as conn:
            before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
        with pytest.raises(AuditIntegrityError, match="actual selected provider locator"):
            authority.observe_provider_action(replace(prepared.binding, input_sha256="b" * 64))
        with engine.connect() as conn:
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
            assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
        assert authority.observe_provider_action(prepared.binding) == first
        actual_transaction = authority_module.locked_session_transaction
        checked_owners = []

        @contextmanager
        def observe_actual_transaction(actual_engine, actual_session_id):
            assert actual_engine is engine and actual_session_id == str(sid)
            with actual_transaction(actual_engine, actual_session_id) as owner_conn:
                owner_rows = {table.name: sorted((tuple(row) for row in owner_conn.execute(select(table))), key=repr) for table in tables}
                try:
                    yield owner_conn
                finally:
                    # Before the original SQL owner's exit/rollback; this remains a net-row oracle.
                    assert {
                        table.name: sorted((tuple(row) for row in owner_conn.execute(select(table))), key=repr) for table in tables
                    } == owner_rows
                    assert load_snapshot(owner_conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
                    checked_owners.append(True)

        with monkeypatch.context() as observation:
            observation.setattr(authority_module, "locked_session_transaction", observe_actual_transaction)
            assert authority.observe_provider_action(prepared.binding) == first
            assert checked_owners == [True]
            for unsupported in ("wrong_codec", "dispatched", "unknown"):
                with engine.begin() as fixture_conn:
                    if unsupported == "wrong_codec":
                        put_chunk(
                            fixture_conn,
                            session_id=str(sid),
                            operation_id=record.operation_id,
                            key="fixture:unexpected-provider-response",
                            payload=b'{"owned":"wrong provider codec"}',
                            codec="composer.state.v1",
                        )
                        fixture_conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == str(sid),
                                effects.c.operation_id == record.operation_id,
                                effects.c.effect_key == prepared.binding.effect_key,
                            )
                            .values(result_chunk_key="fixture:unexpected-provider-response")
                        )
                    else:
                        fixture_conn.execute(
                            update(effects)
                            .where(
                                effects.c.session_id == str(sid),
                                effects.c.operation_id == record.operation_id,
                                effects.c.effect_key == prepared.binding.effect_key,
                            )
                            .values(disposition=unsupported)
                        )
                prior_checks = len(checked_owners)
                with pytest.raises(AuditIntegrityError, match="storage-only provider observation requires an unadvanced prepared effect"):
                    authority.observe_provider_action(prepared.binding)
                assert len(checked_owners) == prior_checks + 1
                with engine.begin() as fixture_conn:
                    fixture_conn.execute(
                        update(effects)
                        .where(
                            effects.c.session_id == str(sid),
                            effects.c.operation_id == record.operation_id,
                            effects.c.effect_key == prepared.binding.effect_key,
                        )
                        .values(disposition="prepared", result_chunk_key=None)
                    )
                    if unsupported == "wrong_codec":
                        fixture_conn.execute(
                            delete(chunks).where(
                                chunks.c.session_id == str(sid),
                                chunks.c.operation_id == record.operation_id,
                                chunks.c.chunk_key == "fixture:unexpected-provider-response",
                            )
                        )
                with engine.connect() as conn:
                    assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
                    assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
                assert authority.observe_provider_action(prepared.binding) == first
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.parametrize("entrypoint", ("generic", "provider"))
@pytest.mark.parametrize("cancelled", (False, True))
def test_new_provider_preparation_refuses_owed_p4_scope_without_writes(operation_store, entrypoint, cancelled):
    from elspeth.web.coordination.database_clock import database_now
    from elspeth.web.sessions.composer_operations import ComposerOperationCancelledDuringTurn
    from elspeth.web.sessions.continuation import (
        _OWED_P4_SCOPE_KEY,
        _OWED_P4_SEAL,
        _owed_continuation_p4_active,
        _OwedP4Scope,
        prepare_effect_on_connection,
        prepare_provider_effect_on_connection,
        require_continuation_mutation,
    )
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_async_operations_table as jobs
    from elspeth.web.sessions.models import metadata

    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    context = running.session_operation_context
    tables = tuple(sorted(metadata.tables.values(), key=lambda table: table.name))
    try:
        selected = authority.load_continuation_snapshot(running)
        with locked_session_transaction(engine, str(sid)) as conn:
            assert require_continuation_mutation(conn, context) is True
            original_rows = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
            positive = conn.begin_nested()
            try:
                binding = prepare_provider_effect_on_connection(
                    conn,
                    running,
                    family=0,
                    input_sha256="a" * 64,
                    snapshot_json=selected.payload_json,
                    expected_checkpoint_id=selected.checkpoint_id,
                    expected_cursor=selected.cursor,
                )
                produced = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
                assert produced.checkpoint_id != selected.checkpoint_id
            finally:
                positive.rollback()
            assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == original_rows
            # Reuse the actual producer's complete result, restoring only the pre-append input cursor.
            # This is a proposed input, never a committed checkpoint receipt or fabricated authority.
            next_stack = decode_continuation_snapshot(produced.payload_json).model_copy(update={"cursor": selected.cursor})
            next_snapshot_json = encode_continuation_snapshot(next_stack)
            if entrypoint == "generic":
                direct_positive = conn.begin_nested()
                try:
                    direct = prepare_effect_on_connection(
                        conn,
                        running,
                        effect_key=binding.effect_key,
                        input_sha256=binding.input_sha256,
                        owner="provider",
                        next_snapshot_json=next_snapshot_json,
                        expected_checkpoint_id=selected.checkpoint_id,
                        expected_cursor=selected.cursor,
                    )
                    assert direct.checkpoint_id != selected.checkpoint_id
                finally:
                    direct_positive.rollback()
                assert {
                    table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables
                } == original_rows
            if cancelled:
                conn.execute(
                    update(jobs)
                    .where(
                        jobs.c.session_id == str(sid),
                        jobs.c.operation_id == record.operation_id,
                    )
                    .values(cancel_requested_at=database_now(conn))
                )
                with pytest.raises(ComposerOperationCancelledDuringTurn):
                    require_continuation_mutation(conn, context)
            # Fixture-only construction of the real nominal active-transaction scope.
            # This does not claim a real P4 was issued or grant fresh provider eligibility.
            conn.info[_OWED_P4_SCOPE_KEY] = _OwedP4Scope(conn, context, _OWED_P4_SEAL)
            try:
                assert _owed_continuation_p4_active(conn, context) is True
                assert require_continuation_mutation(conn, context) is True
                before = {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables}
                with pytest.raises(AuditIntegrityError, match="new continuation preparation cannot use owed P4 authority"):
                    if entrypoint == "generic":
                        prepare_effect_on_connection(
                            conn,
                            running,
                            effect_key=binding.effect_key,
                            input_sha256=binding.input_sha256,
                            owner="provider",
                            next_snapshot_json=next_snapshot_json,
                            expected_checkpoint_id=selected.checkpoint_id,
                            expected_cursor=selected.cursor,
                        )
                    else:
                        prepare_provider_effect_on_connection(
                            conn,
                            running,
                            family=0,
                            input_sha256="a" * 64,
                            snapshot_json=selected.payload_json,
                            expected_checkpoint_id=selected.checkpoint_id,
                            expected_cursor=selected.cursor,
                        )
                # Inside the live owner transaction: outer rollback cannot hide an early mutation.
                assert {table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr) for table in tables} == before
                assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == selected
            finally:
                del conn.info[_OWED_P4_SCOPE_KEY]
    finally:
        repository.release(context)
```

The tests use the real admitted queued row, actual claim/start, current schema and original SQLite fixture. Their savepoint positive is rolled back before the refusal input is interpreted. A rolled-back binding is not committed custody. The observation test first accepts a real committed prepared row through the exact original locking/transaction owner. Its passive test-local delegator checks complete registered rows and the selected head before that owner exits; the oracle is net-row equality, not absence of write-and-undo or external effects. FK-valid wrong-codec/dispatched/unknown fixture changes happen outside `pytest.raises`; a setup/constraint error is a prerequisite failure, never the intended refusal.

Add only these complete reexports to the existing PostgreSQL collector, retaining its actual PostgreSQL `operation_store`, `operation_postgres_target`, old imports and `pytestmark = pytest.mark.testcontainer`. The imported tests use that collector's existing PG fixture, not the SQLite fixture exported by the source module.

```python
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_boundaries_require_actual_claim_without_writes as test_initial_provider_boundaries_require_actual_claim_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_observation_binds_actual_selected_input_without_writes as test_initial_provider_observation_binds_actual_selected_input_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_initial_provider_preparation_preserves_head_on_invalid_input as test_initial_provider_preparation_preserves_head_on_invalid_input,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_new_provider_preparation_refuses_owed_p4_scope_without_writes as test_new_provider_preparation_refuses_owed_p4_scope_without_writes,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_provider_action_facades_return_the_actual_complete_checkpoint as test_provider_action_facades_return_the_actual_complete_checkpoint,
)
```

Use P0's already-established `COMPOSER_WORKTREE`, `COMPOSER_PYTHON`, `COMPOSER_LOG_DIR` and both worktree source roots on exported `PYTHONPATH`. Root owns capacity and each frozen execution window. First run the retained named positive; its genuine initial RED must identify the missing preparation/read boundary, not an unrelated import, fixture, policy or schema error.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_provider_action_facades_return_the_actual_complete_checkpoint >"$COMPOSER_LOG_DIR/c5-storage-positive.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-storage-positive.log"
test "$composer_exit" -eq 0
```

Before implementation the expected status assertion is RED; preserve its actual exit and full log. After implementation the same command must exit 0. Do not rewrite a setup RED as a semantic witness.

- [ ] **Step 2: Install the exact initial storage helpers and nominal receipts below, preserving every old body.** Add `InitializeFrame` to the existing `composer.continuation` import group and the real provider ordinal table import shown here; all other used imports/owners already belong to the current continuation module.

```python
from elspeth.web.composer.continuation import InitializeFrame
from elspeth.web.sessions.models import (
    composer_continuation_provider_ordinals_table as ordinals,
)
```

The following is the complete initial helper/type/preparation block. Place it before the existing `ContinuationOwnerCommit` definition. Do not duplicate the old owner verifier or substitute the effect-dependent verifier before a new effect exists.

```python
def _require_current_continuation_running(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    allow_cancel: bool = False,
    allow_expired_segment: bool = False,
) -> None:
    if (
        type(running) is not ComposerOperationRunning
        or type(running.claim) is not ComposerOperationClaim
        or type(running.session_operation_context) is not SessionOperationContext
        or running.session_operation_context.fence.session_id != str(running.claim.session_id)
        or running.session_operation_context.operation_kind is not SessionOperationKind.COMPOSE
    ):
        raise AuditIntegrityError("continuation boundary changed its actual running context")
    context = running.session_operation_context
    if (
        require_continuation_mutation(
            conn,
            context,
            allow_cancel=allow_cancel,
            allow_expired_segment=allow_expired_segment,
        )
        is not True
    ):
        raise AuditIntegrityError("continuation boundary requires current continuation authority")
    claim = running.claim
    history = conn.execute(
        select(attempts).where(
            attempts.c.session_id == context.fence.session_id,
            attempts.c.session_operation_epoch == context.fence.operation_epoch,
        )
    ).one()
    segment = conn.execute(
        select(segments).where(
            segments.c.session_id == history.session_id,
            segments.c.operation_id == history.operation_id,
            segments.c.sequence == history.sequence,
            segments.c.generation == history.generation,
        )
    ).one()
    if (
        history.session_id,
        history.operation_id,
        history.sequence,
        history.generation,
        history.claim_attempt,
        segment.claim_token,
    ) != (
        str(claim.session_id),
        claim.operation_id,
        claim.segment_sequence,
        claim.ownership_generation,
        claim.attempt,
        claim.claim_token,
    ):
        raise AuditIntegrityError("continuation boundary changed its current claim/history binding")


def _require_reserved_provider_frame(stack: ContinuationFrameStack) -> InitializeFrame:
    selected = stack.frames[-1]
    if (
        len(stack.frames) != 1
        or type(selected) is not InitializeFrame
        or selected.ingress_phase != "reserved"
        or selected.title_attempt_id is not None
        or selected.ingress_receipt_json is not None
        or selected.bootstrap is not None
        or selected.title_response is not None
        or stack.semantic_frontier is not None
        or stack.active_action_json is not None
        or stack.active_action_source is not None
        or stack.pending_owner_results
    ):
        raise AuditIntegrityError("initial provider preparation requires its reserved initializer without later action owners")
    return selected


@dataclass(frozen=True, slots=True)
class ContinuationProviderBinding:
    running: ComposerOperationRunning
    effect_key: str
    family: int
    ordinal: int
    input_sha256: str


@dataclass(frozen=True, slots=True)
class PreparedContinuationProvider:
    binding: ContinuationProviderBinding
    snapshot: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class ProviderActionObservation:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    response: PrivateChunk | None
    provider_attempt_id: str | None
    ledger_entry_id: str | None


def prepare_effect_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    effect_key: str,
    input_sha256: str,
    owner: str,
    next_snapshot_json: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
    action_owner: Literal["primary_turn", "review", "read_tool"] | None = None,
    read_tool_name: str | None = None,
    read_tool_arguments_json: str | None = None,
) -> StoredContinuationSnapshot:
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("new continuation preparation cannot use owed P4 authority")
    claim = running.claim
    stored = require_snapshot_cas(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    current = decode_continuation_snapshot(stored.payload_json)
    current_frame = _require_reserved_provider_frame(current)
    if current_frame.provider_effect_key is not None or current_frame.provider_input_sha256 is not None:
        raise ContinuationCASConflict("new provider preparation would replace an unapplied selected effect")
    if owner != "provider" or action_owner is not None or read_tool_name is not None or read_tool_arguments_json is not None:
        raise AuditIntegrityError("initial effect preparation cannot authorize later SQL, read or prescribed-action owners")
    stack = decode_continuation_snapshot(next_snapshot_json)
    if stack.cursor != expected_cursor or (stack.logical.session_id, stack.logical.operation_id) != (
        str(claim.session_id),
        claim.operation_id,
    ):
        raise ContinuationCASConflict("prepared effect carries an obsolete or foreign frame stack")
    expected_frame = current_frame.model_copy(update={"provider_effect_key": effect_key, "provider_input_sha256": input_sha256})
    expected_stack = current.model_copy(update={"frames": (expected_frame,)})
    expected_json = _effect_snapshot(
        encode_continuation_snapshot(expected_stack), effect_key=effect_key, disposition="prepared", receipt_id=None
    )
    if stack != decode_continuation_snapshot(expected_json):
        raise ContinuationCASConflict("prepared effect replaced its actual selected initializer or input")
    old = conn.execute(
        select(effects).where(
            effects.c.session_id == str(claim.session_id),
            effects.c.operation_id == claim.operation_id,
            effects.c.effect_key == effect_key,
        )
    ).one_or_none()
    if old is not None:
        if old.input_sha256 != input_sha256 or old.owner != owner:
            raise AuditIntegrityError("prepared effect identity collision")
        raise ContinuationCASConflict("effect already prepared; restore its actual disposition")
    conn.execute(
        insert(effects).values(
            session_id=str(claim.session_id),
            operation_id=claim.operation_id,
            effect_key=effect_key,
            input_sha256=input_sha256,
            owner=owner,
            disposition="prepared",
            creator_sequence=claim.segment_sequence,
            creator_generation=claim.ownership_generation,
            action_json=bound_action_json(
                stack, action_owner=action_owner, read_tool_name=read_tool_name, read_tool_arguments_json=read_tool_arguments_json
            ),
        )
    )
    next_snapshot_json = _effect_snapshot(next_snapshot_json, effect_key=effect_key, disposition="prepared", receipt_id=None)
    retained_policy = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash)
    next_action = retained_policy.actions.session_sql
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == str(claim.session_id),
            segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
            segments.c.generation == claim.ownership_generation,
        )
        .values(next_action_json=next_action.model_dump_json(), policy_sha256=stack.policy_hash)
    )
    next_snapshot_json = retain_action_execution_receipt_on_connection(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        effect_key=effect_key,
        disposition="prepared",
        snapshot_json=next_snapshot_json,
    )
    return append_complete_snapshot(
        conn,
        session_id=str(claim.session_id),
        operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor,
        snapshot_json=next_snapshot_json,
        event_batch=(ContinuationEvent("tool_prepared", canonical_json({"effect_key": effect_key, "input_sha256": input_sha256})),),
        handoff=False,
    )


def prepare_provider_effect_on_connection(
    conn: Connection,
    running: ComposerOperationRunning,
    *,
    family: int,
    input_sha256: str,
    snapshot_json: str,
    expected_checkpoint_id: str,
    expected_cursor: int,
) -> ContinuationProviderBinding:
    _require_current_continuation_running(conn, running)
    if _owed_continuation_p4_active(conn, running.session_operation_context):
        raise AuditIntegrityError("new continuation preparation cannot use owed P4 authority")
    stored = require_snapshot_cas(
        conn,
        session_id=str(running.claim.session_id),
        operation_id=running.claim.operation_id,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    if snapshot_json != stored.payload_json:
        raise ContinuationCASConflict("provider preparation replaced the actual selected complete head")
    if type(family) is not int or family not in (0, 1, 2, 3):
        raise ValueError("unknown physical provider family")
    if type(input_sha256) is not str or len(input_sha256) != 64 or any(char not in "0123456789abcdef" for char in input_sha256):
        raise ValueError("provider preparation requires its actual input digest")
    prepared_stack = decode_continuation_snapshot(snapshot_json)
    selected = _require_reserved_provider_frame(prepared_stack)
    if selected.provider_effect_key is not None or selected.provider_input_sha256 is not None:
        raise ContinuationCASConflict("new provider preparation would replace an unapplied selected effect")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    old = conn.execute(
        select(ordinals.c.next_ordinal).where(
            ordinals.c.session_id == sid,
            ordinals.c.operation_id == operation_id,
            ordinals.c.family == family,
        )
    ).scalar_one_or_none()
    ordinal = 0 if old is None else old
    key = f"provider:{family}:{ordinal}"
    selected = selected.model_copy(update={"provider_effect_key": key, "provider_input_sha256": input_sha256})
    prepared_stack = prepared_stack.model_copy(update={"frames": (*prepared_stack.frames[:-1], selected)})
    next_snapshot = _effect_snapshot(encode_continuation_snapshot(prepared_stack), effect_key=key, disposition="prepared", receipt_id=None)
    prepare_effect_on_connection(
        conn,
        running,
        effect_key=key,
        input_sha256=input_sha256,
        owner="provider",
        next_snapshot_json=next_snapshot,
        expected_checkpoint_id=expected_checkpoint_id,
        expected_cursor=expected_cursor,
    )
    if old is None:
        conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation_id, family=family, next_ordinal=1))
    else:
        conn.execute(
            update(ordinals)
            .where(
                ordinals.c.session_id == sid,
                ordinals.c.operation_id == operation_id,
                ordinals.c.family == family,
            )
            .values(next_ordinal=ordinal + 1)
        )
    retained_policy = decode_continuation_policy(prepared_stack.admission_policy_json, prepared_stack.policy_hash)
    action = (
        retained_policy.actions.primary,
        retained_policy.actions.advisor,
        retained_policy.actions.primary,
        retained_policy.actions.title,
    )[family]
    conn.execute(
        update(segments)
        .where(
            segments.c.session_id == sid,
            segments.c.operation_id == operation_id,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation,
        )
        .values(
            next_action_json=action.model_dump_json(),
            policy_sha256=prepared_stack.policy_hash,
        )
    )
    return ContinuationProviderBinding(running, key, family, ordinal, input_sha256)
```

The actual complete selected head is compared before preparation. Generic provider preparation independently permits only the paired locator/frontier changes to that root; strict decode validates copied models before effect insertion. Ordinal mutation follows successful generic preparation in the same real owner transaction. Generic SQL/read/action owners and later selected frames remain explicitly refused.

- [ ] **Step 3: Install these complete initial authority facets.** Merge this exact current continuation import group with the existing authority imports and add the real effects-table alias. Keep existing other imports and every previous method; do not add future response/attempt/ledger imports.

```python
from elspeth.web.sessions.continuation import (
    ContinuationProviderBinding,
    HistoricalEffectLocator,
    PreparedContinuationProvider,
    ProviderActionObservation,
    StoredContinuationSnapshot,
    ValidatedHistoricalEffectReference,
    _require_current_continuation_running,
    _require_reserved_provider_frame,
    initialize_continuation,
    load_snapshot,
    prepare_provider_effect_on_connection,
    require_continuation_mutation,
    require_snapshot_cas,
    validate_historical_effect_reference,
)
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
```

Insert the following methods inside the existing `ComposerAsyncOperationAuthority` class. Load/observation allow committed cancellation or spent segment clocks for read-only observation but still demand actual current fence/principal/history/claim binding. Observation is same-creator, prepared-only, with all unsupported action/result/receipt/physical/graph fields absent; it does not silently return None for a present unsupported result.

```python
    def load_continuation_snapshot(self, running: ComposerOperationRunning) -> StoredContinuationSnapshot:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            return load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)

    def prepare_provider_effect(
        self,
        running: ComposerOperationRunning,
        *,
        family: int,
        input_sha256: str,
        snapshot_json: str,
        expected_checkpoint_id: str,
        expected_cursor: int,
    ) -> ContinuationProviderBinding:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            return prepare_provider_effect_on_connection(
                conn,
                running,
                family=family,
                input_sha256=input_sha256,
                snapshot_json=snapshot_json,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
            )

    def prepare_provider_action(
        self,
        running: ComposerOperationRunning,
        *,
        family: int,
        input_sha256: str,
        snapshot_json: str,
        expected_checkpoint_id: str,
        expected_cursor: int,
    ) -> PreparedContinuationProvider:
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
            binding = prepare_provider_effect_on_connection(
                conn,
                running,
                family=family,
                input_sha256=input_sha256,
                snapshot_json=snapshot_json,
                expected_checkpoint_id=expected_checkpoint_id,
                expected_cursor=expected_cursor,
            )
            snapshot = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
            return PreparedContinuationProvider(binding, snapshot)

    def observe_provider_action(self, binding: ContinuationProviderBinding) -> ProviderActionObservation:
        if type(binding) is not ContinuationProviderBinding:
            raise TypeError("provider observation requires its actual nominal binding")
        running = binding.running
        if type(running) is not ComposerOperationRunning:
            raise TypeError("continuation boundary requires nominal running custody")
        if binding.effect_key != f"provider:{binding.family}:{binding.ordinal}":
            raise AuditIntegrityError("provider observation differs from its actual prepared input")
        sid, operation_id = str(running.claim.session_id), running.claim.operation_id
        with locked_session_transaction(self._engine, sid) as conn:
            _require_current_continuation_running(conn, running, allow_cancel=True, allow_expired_segment=True)
            snapshot = load_snapshot(conn, session_id=sid, operation_id=operation_id)
            selected = _require_reserved_provider_frame(decode_continuation_snapshot(snapshot.payload_json))
            if (selected.provider_effect_key, selected.provider_input_sha256) != (binding.effect_key, binding.input_sha256):
                raise AuditIntegrityError("provider observation differs from its actual selected provider locator")
            effect = conn.execute(
                select(effects).where(
                    effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key
                )
            ).one()
            if effect.owner != "provider" or effect.input_sha256 != binding.input_sha256:
                raise AuditIntegrityError("provider observation differs from its actual prepared input")
            if (effect.creator_sequence, effect.creator_generation) != (
                running.claim.segment_sequence,
                running.claim.ownership_generation,
            ):
                raise AuditIntegrityError("initial provider observation changed its immutable preparing owner")
            if (
                effect.disposition != "prepared"
                or effect.action_json is not None
                or effect.receipt_key is not None
                or effect.result_chunk_key is not None
                or effect.provider_attempt_id is not None
                or effect.physical_sequence is not None
                or effect.physical_generation is not None
                or effect.requested_model is not None
                or effect.response_finished_at is not None
                or effect.committed_state_ids_json is not None
                or effect.graph_sha256 is not None
            ):
                raise AuditIntegrityError("storage-only provider observation requires an unadvanced prepared effect")
            return ProviderActionObservation(snapshot, binding.effect_key, "prepared", None, None, None)
```

- [ ] **Step 4: Require actual ordinary SQLite and serial PostgreSQL GREEN on the normal tree.** Preserve full logs, explicit exits and frozen input captures. No native C5 result is established by this plan text, source GO, target-aware Ruff, shadow mypy or collector/fixture import identity.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py >"$COMPOSER_LOG_DIR/c5-storage-sqlite.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-storage-sqlite.log"
test "$composer_exit" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 -m testcontainer tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py::test_provider_action_facades_return_the_actual_complete_checkpoint tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py::test_initial_provider_boundaries_require_actual_claim_without_writes tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py::test_initial_provider_preparation_preserves_head_on_invalid_input tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py::test_initial_provider_observation_binds_actual_selected_input_without_writes tests/testcontainer/web/test_composer_continuation_claim_start_postgres.py::test_new_provider_preparation_refuses_owed_p4_scope_without_writes >"$COMPOSER_LOG_DIR/c5-storage-postgres.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-storage-postgres.log"
test "$composer_exit" -eq 0
```

- [ ] **Step 5: Control the live observer-row oracle, then restore the complete normal source.** On the already-positive normal observation test, temporarily insert this complete conditional mutation immediately before `if (effect.disposition != "prepared" ...)` in `observe_provider_action`:

```python
            if effect.result_chunk_key is not None:
                conn.execute(update(sessions_table).where(sessions_table.c.id == sid).values(title="controlled observer mutation"))
```

Run the exact ordinary observation test command below. Its normal prepared positive must still pass; the first FK-valid wrong-codec case then performs the real UPDATE and the original integrity refusal, and the delegated original owner's complete live-row equality must fail before rollback. Preserve that actual assertion occurrence and exit. A setup error, missing import or after-rollback equality is not this witness. Restore the complete normal authority, verify the source binding, then run the identical selector GREEN and the corresponding ordinary PostgreSQL selector serially.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_initial_provider_observation_binds_actual_selected_input_without_writes >"$COMPOSER_LOG_DIR/c5-observer-control.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-observer-control.log"
test "$composer_exit" -eq 0
```

On the temporary mutant, the last status assertion is expected RED; require the stated live-row assertion, then preserve the full normal restoration/GREEN. Use distinct log paths for mutant and restored runs. The same restored SQLite/PG positives must be preserved around each genuine omission below:

* Remove only the shared six-tuple comparison: wrong supplied token/attempt with otherwise valid current context/head must reach the existing DID NOT RAISE assertion. Restore before GREEN. A second-session case can instead reach a later missing-parent/CAS backstop; do not relabel it a bypass witness.
* Remove only exactTrue handling: the no-history context still meets the unconditional history `.one()` diagnostic. This tests the precise refusal boundary; it is not an authorization bypass/DID NOT RAISE claim.
* Remove only the direct generic owed-P4 refusal: its independent accepted generic positive supplies the intended DID NOT RAISE witness. Provider-only omission has the generic path's intentional redundant refusal; do not remove both solely to manufacture a single-guard witness.
* Preserve original checkpoint/cursor/whole-root, input shape and physical lease refusal controls. A single omitted check may meet a redundant later backstop; qualify the actual assertion reached.

Same-creator observation and physical-bundle absence checks are source requirements, but this first-start fixture supplies no genuine later creator/physical producer. Do not fabricate history/attempt rows or call a constraint error their native witness. Their supported actual-producer controls belong with that later packet.

- [ ] **Step 6: Close affected existing checks before staging this bounded increment.** Run the existing canonical Ruff/mypy/contracts gate, relevant whole-tree attribute/masquerade/Reserve/reviewed-contract, Sessions-writer and Landscape measurements and keyless before/after comparison required by CONTRIBUTING and the existing qualification packet. Bind actual changed subjects and source coordinates through the original producers; do not change scanner algorithms, admit escaped connections, grant raw helpers, suppress errors or clear standing residuals. A nonzero standing keyless corpus is compared by actual finding identities and semantics, never to zero; signing remains operator-only. Read full logs/exits and reject unfrozen results. Stage only actual maintained paths owned by this increment after root's source/execution freeze closes and required review/native/gates are complete, using the existing branch-safety preflight. No source/import/static success alone establishes runtime, complete-plan, protected CI or release qualification.

**Deferred full variants remain below.** The original generic SQL/read helpers in C5.2, physical C5.3 binding/prepare/facades/response/attempt/ledger bodies, service/protocol/SDK hooks and later cold owners are retained as complete integration material, not active fallthrough or replacement definitions. Do not install a second copy of the initial types, helpers, load facade or named checkpoint test. Later integration must preserve the exactTrue/actual six-tuple/fresh-work guards, resolve present wrong response codecs with explicit integrity refusal, and establish real selected physical eligibility before consuming a storage binding. C3.8e's complete passive read-result prerequisite may precede activation after actual B1/C3.8c. Selected-read/result-writer/pending-reader/service/protocol writer owners remain deferred together until main step 9's actual B4/current-read dependencies exist.


### C5.3: Join durable provider ordinals to actual quota admission and dispatch

**Files:** Extend `sessions/continuation.py`, `coordination/composer_operation_authority.py`, `sessions/service.py`, `sessions/protocol.py`, `composer/provider_quota.py`, `composer/provider_gateway.py` and `web/required_work.py`.

**Deferred full physical variant, not initial fallthrough.** C5.2a above is the complete currently bounded prepared-only installation, including the five-field binding, outward receipt types, provider-only preparation, snapshot load, preparation/observation facades and named checkpoint test. Preserve the complete original bodies below for the actual provider/effect integration; do not add a duplicate definition or overwrite C5.2a's stricter current-claim, fresh-work and pre-write checks. The four shared utilities were already installed under storage1005 and remain single definitions. The raw response/attempt/ledger observation is not supported by a reserved initializer/arbitrary-digest positive and must not execute in the initial phase. Before its later installation, require actual selected frame/source/action/input/clock owners, real quota/response/settlement producers and positive-first controls, and explicitly refuse a present wrong owned response codec rather than projecting it as absent. The entire real read/writer/service/protocol cluster still waits together for main step 9. No physical consumer is enabled by the storage-only binding.

- [ ] **DEFERRED full physical SQL variant:** retain this complete binding/helper block for the later actual provider integration. C5.2a already installs the binding and strict provider-only preparation; storage1005 already installs the shared utilities. Do not add duplicate definitions or overwrite those initial bodies here. The binding is never serialized as a live lease/custody object: stored effects retain its creator sequence/generation and actual provider attempt ID. Import the provider attempt table and `ProviderAttempt` only when their real later producers are installed; initial C5.2a needs only the actual ordinal table.

```python
@dataclass(frozen=True, slots=True)
class ContinuationProviderBinding:
    running: ComposerOperationRunning
    effect_key: str
    family: int
    ordinal: int
    input_sha256: str


def bound_action_json(
    stack: ContinuationFrameStack, *, action_owner: Literal["primary_turn", "review", "read_tool"] | None,
    read_tool_name: str | None, read_tool_arguments_json: str | None,
) -> str | None:
    from elspeth.web.composer.advisor_output import PrimaryContinuationAction, ReadContinuationAction, ReviewContinuationAction
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    if action_owner is None:
        return None
    if stack.active_action_json is None:
        raise AuditIntegrityError("prescribed execution owner has no actual retained Advisor action")
    action = decode_prescribed_continuation_action(stack.active_action_json)
    matches = (action_owner == "primary_turn" and type(action) is PrimaryContinuationAction
               or action_owner == "review" and type(action) is ReviewContinuationAction)
    if action_owner == "read_tool" and type(action) is ReadContinuationAction:
        requested, request_error = parse_json_strict(action.arguments_json)
        actual, actual_error = parse_json_strict(read_tool_arguments_json) if read_tool_arguments_json is not None else (None, "absent")
        matches = (read_tool_name == action.tool_name and request_error is None and actual_error is None
                   and canonical_json(requested) == canonical_json(actual))
    if not matches:
        raise AuditIntegrityError("prescribed action differs from its actual execution owner")
    return stack.active_action_json


def retain_action_execution_receipt_on_connection(
    conn: Connection, *, session_id: str, operation_id: str, effect_key: str,
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"],
    snapshot_json: str,
) -> str:
    from elspeth.web.composer.continuation_codec import encode_private_chunk
    from elspeth.web.composer.continuation_progress import (
        ActionExecutionReceipt, continuation_action_key, decode_continuation_frontier,
        encode_continuation_frontier, record_action_receipt, decode_prescribed_continuation_action,
    )
    effect = conn.execute(select(effects).where(
        effects.c.session_id == session_id, effects.c.operation_id == operation_id,
        effects.c.effect_key == effect_key,
    )).one()
    creator = conn.execute(select(attempts).where(
        attempts.c.session_id == session_id, attempts.c.operation_id == operation_id,
        attempts.c.sequence == effect.creator_sequence, attempts.c.generation == effect.creator_generation,
    )).one()
    stack = decode_continuation_snapshot(snapshot_json)
    if ((stack.logical.session_id, stack.logical.operation_id) != (session_id, operation_id)
        or creator.operation_id != operation_id or effect.disposition != disposition):
        raise AuditIntegrityError("action receipt differs from actual request/effect creator")
    if effect.action_json is None:
        return snapshot_json
    action = decode_prescribed_continuation_action(effect.action_json)
    if stack.semantic_frontier is None:
        raise AuditIntegrityError("prepared action lost its retained semantic frontier")
    ref = stack.semantic_frontier
    canonical = read_chunk(conn, session_id=session_id, operation_id=operation_id,
                           key=f"value:{ref.codec}:{ref.digest}").decode("utf-8", errors="strict")
    verify_private_chunk(ref, canonical)
    frontier = decode_continuation_frontier(canonical)
    receipt = ActionExecutionReceipt(
        continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence}),
        effect.effect_key,
        disposition,
    )
    updated = record_action_receipt(frontier, receipt, actual_owner_receipt_id=effect.effect_key)
    chunk = encode_private_chunk(codec="composer.semantic-frontier.v1", canonical=encode_continuation_frontier(updated))
    put_chunk(conn, session_id=session_id, operation_id=operation_id, key=private_chunk_key(chunk),
              payload=chunk.canonical.encode("utf-8"), codec=chunk.reference.codec)
    updated_stack = stack.model_copy(update={"semantic_frontier": chunk.reference})
    if disposition in ("committed", "refused") and stack.active_action_json == effect.action_json:
        updated_stack = updated_stack.model_copy(update={"active_action_json": None, "active_action_source": None})
    return encode_continuation_snapshot(updated_stack)


def _effect_snapshot(
    encoded: str, *, effect_key: str,
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"], receipt_id: str | None,
    add_attempt_id: str | None = None, remove_attempt_id: str | None = None,
) -> str:
    from elspeth.web.composer.continuation import EffectFrontierEntry
    from elspeth.web.composer.continuation_codec import decode_continuation_snapshot, encode_continuation_snapshot
    stack = decode_continuation_snapshot(encoded)
    entry = EffectFrontierEntry(effect_id=effect_key, disposition=disposition, receipt_id=receipt_id)
    previous = tuple(item for item in stack.effect_frontier if item.effect_id != effect_key)
    pending = tuple(value for value in stack.pending_settlement_ids if value != remove_attempt_id)
    if add_attempt_id is not None and add_attempt_id not in pending:
        pending = (*pending, add_attempt_id)
    return encode_continuation_snapshot(stack.model_copy(update={
        "effect_frontier": (*previous, entry), "pending_settlement_ids": pending,
    }))


def prepare_provider_effect_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, family: int,
    input_sha256: str, snapshot_json: str, expected_checkpoint_id: str, expected_cursor: int,
) -> ContinuationProviderBinding:
    require_continuation_mutation(conn, running.session_operation_context)
    require_snapshot_cas(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
                         expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
    if type(family) is not int or family not in (0, 1, 2, 3):
        raise ValueError("unknown physical provider family")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    old = conn.execute(select(ordinals.c.next_ordinal).where(
        ordinals.c.session_id == sid, ordinals.c.operation_id == operation_id, ordinals.c.family == family,
    )).scalar_one_or_none()
    ordinal = 0 if old is None else old
    if old is None:
        conn.execute(insert(ordinals).values(session_id=sid, operation_id=operation_id, family=family, next_ordinal=1))
    else:
        conn.execute(update(ordinals).where(
            ordinals.c.session_id == sid, ordinals.c.operation_id == operation_id, ordinals.c.family == family,
        ).values(next_ordinal=ordinal + 1))
    key = f"provider:{family}:{ordinal}"
    prepared_stack = decode_continuation_snapshot(snapshot_json)
    from elspeth.web.composer.continuation import ContinuationProviderFrame
    selected = prepared_stack.frames[-1]
    if not isinstance(selected, ContinuationProviderFrame):
        raise AuditIntegrityError("provider preparation lacks its actual selected provider-owning frame")
    if selected.provider_effect_key is not None or selected.provider_input_sha256 is not None:
        raise AuditIntegrityError("new provider preparation would replace an unapplied selected effect")
    selected = selected.model_copy(update={"provider_effect_key": key, "provider_input_sha256": input_sha256})
    prepared_stack = prepared_stack.model_copy(update={"frames": (*prepared_stack.frames[:-1], selected)})
    next_snapshot = _effect_snapshot(encode_continuation_snapshot(prepared_stack), effect_key=key,
                                     disposition="prepared", receipt_id=None)
    from elspeth.web.composer.advisor_output import PrimaryContinuationAction
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    action_owner = None
    if family == 0 and prepared_stack.active_action_json is not None:
        if type(decode_prescribed_continuation_action(prepared_stack.active_action_json)) is PrimaryContinuationAction:
            action_owner = "primary_turn"
    prepare_effect_on_connection(conn, running, effect_key=key, input_sha256=input_sha256, owner="provider",
                                 next_snapshot_json=next_snapshot, expected_checkpoint_id=expected_checkpoint_id,
                                 expected_cursor=expected_cursor, action_owner=action_owner)
    retained_policy = decode_continuation_policy(prepared_stack.admission_policy_json, prepared_stack.policy_hash)
    action = (retained_policy.actions.primary, retained_policy.actions.advisor,
              retained_policy.actions.primary, retained_policy.actions.title)[family]
    conn.execute(update(segments).where(segments.c.session_id == sid, segments.c.operation_id == operation_id,
                                       segments.c.sequence == running.claim.segment_sequence,
                                       segments.c.generation == running.claim.ownership_generation).values(
        next_action_json=action.model_dump_json(), policy_sha256=prepared_stack.policy_hash,
    ))
    return ContinuationProviderBinding(running, key, family, ordinal, input_sha256)


def join_actual_provider_admission(
    conn: Connection, binding: ContinuationProviderBinding, attempt: ProviderAttempt, *, requested_model: str,
) -> None:
    running = binding.running
    require_continuation_mutation(conn, running.session_operation_context)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    if type(requested_model) is not str or not 1 <= len(requested_model) <= 1024:
        raise ValueError("actual continuation provider admission requires its bounded requested model")
    changed = conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id,
        effects.c.effect_key == binding.effect_key, effects.c.owner == "provider",
        effects.c.disposition == "prepared", effects.c.provider_attempt_id.is_(None),
    ).values(provider_attempt_id=attempt.attempt_id,
             physical_sequence=running.claim.segment_sequence, physical_generation=running.claim.ownership_generation,
             requested_model=requested_model))
    if changed.rowcount != 1:
        raise AuditIntegrityError("physical quota admission lost its prepared continuation effect")
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    next_snapshot = _effect_snapshot(stored.payload_json, effect_key=binding.effect_key, disposition="prepared",
                                     receipt_id=attempt.attempt_id, add_attempt_id=attempt.attempt_id)
    next_snapshot = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=binding.effect_key,
        disposition="prepared", snapshot_json=next_snapshot,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=next_snapshot,
        event_batch=(ContinuationEvent("provider_admitted", canonical_json({"effect_key": binding.effect_key, "attempt_id": attempt.attempt_id})),),
        handoff=False,
    )


def mark_provider_may_dispatch_on_connection(conn: Connection, binding: ContinuationProviderBinding) -> None:
    running = binding.running
    require_continuation_mutation(conn, running.session_operation_context)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(select(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    )).one()
    actual = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == effect.provider_attempt_id)).one()
    if effect.disposition != "prepared" or actual.settled_at is not None or actual.session_id != sid:
        raise AuditIntegrityError("provider dispatch lacks its actual unsettled admission")
    fence = running.session_operation_context.fence
    if (actual.operation_id, actual.operation_epoch, actual.lease_token) != (fence.operation_id, fence.operation_epoch, fence.lease_token):
        raise AuditIntegrityError("provider admission changed its physical creator")
    conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    ).values(disposition="dispatched"))
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    next_snapshot = _effect_snapshot(stored.payload_json, effect_key=binding.effect_key, disposition="dispatched", receipt_id=actual.attempt_id)
    next_snapshot = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=binding.effect_key,
        disposition="dispatched", snapshot_json=next_snapshot,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=next_snapshot,
        event_batch=(ContinuationEvent("may_dispatch", canonical_json({"effect_key": binding.effect_key, "attempt_id": actual.attempt_id})),), handoff=False,
    )
```

- [ ] **DEFERRED full-variant wrapper:** preserve the complete raw body below. C5.2a already installs this API with nominal/current SQL claim checks; do not add a duplicate or restore the raw weaker boundary. Its later actual phase owner must supply selected exact input and complete phase state before qualified physical custody is minted.

```python
def prepare_provider_effect(
    self, running: ComposerOperationRunning, *, family: int, input_sha256: str, snapshot_json: str,
    expected_checkpoint_id: str, expected_cursor: int,
) -> ContinuationProviderBinding:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        return prepare_provider_effect_on_connection(conn, running, family=family, input_sha256=input_sha256,
            snapshot_json=snapshot_json, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
```

- [ ] **DEFERRED complete physical receipt/facet variant:** preserve the complete types and raw methods below for integration with their real producers. C5.2a already installs the outward types and prepared-only atomic facets; do not add duplicates or activate these response/attempt/ledger branches now. In the later qualified phase, the actual driver uses `prepare_provider_action` to receive the complete checkpoint committed with preparation and `observe_provider_action` after actual response/audit settlement joins. It discards its previous root in favor of the actual complete observed root, incorporating independently committed frontier updates; it never assigns a new checkpoint ID to previously computed frames. Existing callers of the five-field binding retain their API.

```python
@dataclass(frozen=True, slots=True)
class PreparedContinuationProvider:
    binding: ContinuationProviderBinding
    snapshot: StoredContinuationSnapshot


@dataclass(frozen=True, slots=True)
class ProviderActionObservation:
    snapshot: StoredContinuationSnapshot
    effect_key: str
    disposition: Literal["prepared", "dispatched", "committed", "refused", "unknown"]
    response: PrivateChunk | None
    provider_attempt_id: str | None
    ledger_entry_id: str | None
```

```python
def prepare_provider_action(
    self, running: ComposerOperationRunning, *, family: int, input_sha256: str, snapshot_json: str,
    expected_checkpoint_id: str, expected_cursor: int,
) -> PreparedContinuationProvider:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        binding = prepare_provider_effect_on_connection(conn, running, family=family, input_sha256=input_sha256,
            snapshot_json=snapshot_json, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        snapshot = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        return PreparedContinuationProvider(binding, snapshot)


def observe_provider_action(self, binding: ContinuationProviderBinding) -> ProviderActionObservation:
    if type(binding) is not ContinuationProviderBinding:
        raise TypeError("provider observation requires its actual nominal binding")
    running = binding.running
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        effect = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key)).one()
        if (effect.owner != "provider" or effect.input_sha256 != binding.input_sha256
            or binding.effect_key != f"provider:{binding.family}:{binding.ordinal}"):
            raise AuditIntegrityError("provider observation differs from its actual prepared input")
        snapshot = load_snapshot(conn, session_id=sid, operation_id=operation_id)
        response = None
        if effect.result_chunk_key is not None:
            row = conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id,
                                                    chunks.c.chunk_key == effect.result_chunk_key)).one()
            if row.codec == "composer.response.v1":
                canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
                                       key=effect.result_chunk_key).decode("utf-8", errors="strict")
                response = PrivateChunk(PrivateChunkRef(codec=row.codec, digest=row.sha256, byte_length=row.byte_length), canonical)
        actual = conn.execute(select(provider_attempts).where(
            provider_attempts.c.attempt_id == effect.provider_attempt_id)).one() if effect.provider_attempt_id is not None else None
        if actual is not None:
            creator = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
                attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
            if (actual.session_id, actual.operation_id, actual.operation_epoch) != (
                sid, creator.session_operation_id, creator.session_operation_epoch):
                raise AuditIntegrityError("provider observation substituted another actual physical owner")
        return ProviderActionObservation(snapshot, binding.effect_key, effect.disposition, response,
            effect.provider_attempt_id, actual.ledger_entry_id if actual is not None else None)
```

These complete raw full-variant nominal types/facets remain deferred. C5.2a already installs the same outward types and stricter prepared-only methods; do not duplicate them or activate the physical branches before their actual owners and controls. Later expose qualified complete facets to their actual phase-driver owner. C5.7's `ProviderEffectReconciliation` likewise returns its actual complete snapshot. Restored execution never infers a locator from the request cursor or from a public revision.

- [ ] Preserve this complete original facade test as full-variant provenance; C5.2a already installs its formatted equivalent once and exports it through the real PostgreSQL collector. Do not add a duplicate. Its prepared-only storage assertion proves the returned locator belongs to the actual committed head and the read facet projects that complete head. Reserved family0/arbitrary input digest does not establish physical eligibility or qualify response/attempt/ledger producers.

```python
def test_provider_action_facades_return_the_actual_complete_checkpoint(operation_store):
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    selected = authority.load_continuation_snapshot(running)
    prepared = authority.prepare_provider_action(running, family=0, input_sha256="a" * 64,
        snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == prepared.snapshot
    assert prepared.snapshot.checkpoint_id != selected.checkpoint_id
    assert prepared.snapshot.cursor > selected.cursor
    observed = authority.observe_provider_action(prepared.binding)
    assert observed.snapshot == prepared.snapshot
    assert observed.effect_key == prepared.binding.effect_key and observed.disposition == "prepared"
    assert observed.response is None and observed.provider_attempt_id is None and observed.ledger_entry_id is None
    repository.release(running.session_operation_context)
```

- [ ] Add `continuation_binding: ContinuationProviderBinding | None = None` and `requested_model: str | None = None` to existing `begin_provider_attempt` and `_begin_provider_attempt_sync`, and pass both through the existing required-admission closure and normal `_sync` closure. Add both optional arguments to the protocol method. V1 leaves them `None`. Inside `_begin_provider_attempt_sync`, immediately after the existing `begin_provider_attempt_on_connection(...)` call and before its owner transaction exits, insert:

```python
if continuation_binding is not None:
    if source == "run" or continuation_binding.running.session_operation_context != session_operation_context:
        raise AuditIntegrityError("continuation provider admission changed its owning context")
    if requested_model is None:
        raise AuditIntegrityError("continuation admission omitted its actual requested model")
    join_actual_provider_admission(conn, continuation_binding, attempt, requested_model=requested_model)
```

The actual quota attempt ID and snapshot therefore commit together. Preserve the current database-clock `_bind_attempt_call` implementation, source/run custody tuple and ledger FK checks. A reserved ordinal never stands in for an actual quota admission.

- [ ] **HOLD — actual physical eligibility prerequisite, no storage-binding consumption:** preserve the complete custody/mint definitions below, but do not install or wire them from C5.2a. The raw mint checks nominality/context only; a reserved storage locator with an arbitrary digest can meet those conditions without being a selected physical action. Identify and qualify the actual selected frame/source/action/input/clock owner and fresh eligibility checks before this later method can consume a binding. Do not invent a DTO, seal, synthetic provider/clock or replacement owner. With those real prerequisites established, extend `ProviderCallCustody.__slots__` with `_continuation_binding` and `_continuation_finished_at`; add its optional typed constructor argument `continuation_binding: ContinuationProviderBinding | None = None`, assign it, and initialize `self._continuation_finished_at: datetime | None = None`. In its existing `admit` call to `service.begin_provider_attempt`, append `continuation_binding=self._continuation_binding, requested_model=model if self._continuation_binding is not None else None`. The complete raw method below remains integration material; normal `mint` retains its v1 counter behavior.

Before later physical activation, preserve the actual existing PRIMARY/ADVISOR mint's `cost_budget` forwarding and the separate post-admission `require_cost_dispatch` recheck. The raw C5/T8 mint and C5.4 replacement must not erase those current owners. Use the existing `test_physical_dispatch_recheck_disposes_admitted_attempt_without_sdk_or_charge` regression owner with the actual provider-readiness packet. Current V2 `not_applicable` cost policy is counterevidence to an installed-bypass claim; this is a future preservation prerequisite, not a change to the initial storage-only scope or authority for provider-quota edits here.

Expose this exact read-only property on `ProviderCallCustody` only in that qualified later physical packet, before C5.8's gateway or main C6 consumes it. Later packets preserve it rather than define another property.

```python
@property
def continuation_binding(self) -> ContinuationProviderBinding | None:
    return self._continuation_binding
```

```python
def mint_continuation(self, binding: ContinuationProviderBinding, *, clock: ContinuationActionClock) -> ProviderCallCustody:
    if type(binding) is not ContinuationProviderBinding or binding.running.session_operation_context != self.required_work.coordinator.authority.context:
        raise AuditIntegrityError("durable provider ordinal belongs to another execution fence")
    family = ProviderInvocationFamily(binding.family)
    diagonal = int(family) + binding.ordinal
    semantic = diagonal * (diagonal + 1) // 2 + binding.ordinal
    required = RequiredWorkBinding(
        self.required_work.coordinator, self.required_work.transition_ordinal, semantic,
        RequiredWorkRole.TITLE if family is ProviderInvocationFamily.TITLE else RequiredWorkRole.TURN,
        binding.running,
    )
    return ProviderCallCustody(service=self.service, context=required.coordinator.authority.context,
                               required_work=required, continuation_binding=binding, clock=clock)
```

- [ ] Add required-work sources 55/56 for actual continuation SQL/projection, with existing `REQUIRED_CONTINUATION_CHILD` stage. Add sources 57/58 for continuation read/read projection. This is runtime required-work custody, not a new test controller. The exact enum additions and mapping entries are:

```python
CONTINUATION_STATE_SQL = 55
CONTINUATION_STATE_PROJECTION = 56
CONTINUATION_READ_SQL = 57
CONTINUATION_READ_PROJECTION = 58
```

```python-expression
RequiredWorkSource.CONTINUATION_STATE_SQL: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.SQL_INITIAL),
RequiredWorkSource.CONTINUATION_STATE_PROJECTION: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.PROJECTION),
RequiredWorkSource.CONTINUATION_READ_SQL: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.SQL_READBACK),
RequiredWorkSource.CONTINUATION_READ_PROJECTION: (ComposerRequiredStage.REQUIRED_CONTINUATION_CHILD, RequiredWorkSubphase.PROJECTION),
```

Add this complete method on the existing `RequiredWorkCoordinator`; it verifies registered nominal tickets and their actual joined SQL carriers using the existing private verifier. Do not widen the legacy lifecycle validator or treat a Python return value as a physical SQL receipt.

```python
def validate_continuation_sql_outcome[T](
    self, *, ticket: RequiredWorkTicket, expected_source: RequiredWorkSource,
    actual_outcome: RequiredSQLFinishOnce[T],
) -> None:
    if type(expected_source) is not RequiredWorkSource or expected_source not in (
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_READ_SQL,
    ):
        raise AuditIntegrityError("Continuation SQL outcome has an unsupported source")
    with self._lock:
        self._registered(ticket, expected_source)
        self._verify_sql_outcome(ticket, actual_outcome)
```

- [ ] Add the complete service method below and its exact protocol declaration. Its SQL ticket is the actual deferred worker-future owner; it records the may-dispatch state before SDK entry. No secret or endpoint transport kwargs become snapshot bytes.

```python
async def mark_continuation_provider_may_dispatch(
    self, *, binding: ContinuationProviderBinding, required_work: RequiredWorkTicket,
) -> None:
    context = binding.running.session_operation_context
    sid = context.fence.session_id
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,), session_id=sid, context=context)
    def _sync() -> None:
        with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
            self._require_session_operation_context_on_connection(conn, context, session_id=sid, expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn))
            mark_provider_may_dispatch_on_connection(conn, binding)
    await run_required_sql_in_worker(required_work, _sync)
```

Add the following method to `ProviderCallCustody` and insert `await provider_custody.prepare_sdk_entry()` immediately before its existing `mark_sdk_entered()` call in `_litellm_acompletion`. The legacy path is a no-op; v2 joins the actual SQL result and preserves original cancellation instances before dispatch.

```python
async def prepare_sdk_entry(self) -> None:
    if self._continuation_binding is None:
        return
    sql, projection = self.required_work.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION,
    )
    try:
        _, cancellations = await self._join(self.service.mark_continuation_provider_may_dispatch(
            binding=self._continuation_binding, required_work=sql,
        ))
    except BaseException:
        if sql.complete:
            projection.complete_without_submission()
        raise
    projection.begin_projection()
    projection.complete_owned()
    self.raise_deferred_cancellations(cancellations)
```

The existing `on_provider_dispatch` callback remains before this marker; a callback refusal continues through its current proven-undispatched cancellation owner. No await can erase or reinterpret a committed may-dispatch marker as proof that the SDK was never called.

### C5.4: Enforce each actual action stage under one original action clock

**Files:** Extend `composer/continuation_clock.py`; modify `composer/provider_quota.py` and the two actual gateway bodies `_litellm_acompletion`/`ProviderGateway._call_llm_with_audit_body`. Extend `tests/unit/web/composer/test_continuation_action_admission.py`.

- [ ] Add this full clock. It is process-local and is never stored as a monotonic anchor. A fresh action receives a new clock only after structural action admission and a new physical effect preparation; re-guarding the same input cannot refresh the original deadline. Persist database timing observations and policy values, not this object.

```python
import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

type ContinuationTimedStage = Literal["guard", "quota", "physical", "retention"]


class ContinuationActionClock:
    def __init__(self, policy: ContinuationActionPolicy) -> None:
        self.policy = policy
        self.started = asyncio.get_running_loop().time()
        self.deadline = self.started + policy.total
        self._seen: set[str] = set()

    def finish_preparation(self) -> None:
        if self._seen:
            raise ValueError("action preparation cannot renew or follow another stage")
        if asyncio.get_running_loop().time() > self.started + self.policy.preparation:
            raise TimeoutError("continuation preparation allowance expired")
        self._seen.add("preparation")

    async def run[T](self, stage: ContinuationTimedStage, operation: Callable[[], Awaitable[T]]) -> T:
        if stage in self._seen or "preparation" not in self._seen:
            raise ValueError("continuation action stage repeated or preparation unbound")
        match stage:
            case "guard":
                seconds = self.policy.guard
            case "quota":
                seconds = self.policy.quota
            case "physical":
                seconds = self.policy.physical
            case "retention":
                seconds = self.policy.retention
        if seconds is None:
            raise ValueError("an inapplicable action stage cannot execute")
        self._seen.add(stage)
        deadline = min(self.deadline, asyncio.get_running_loop().time() + seconds)
        async with asyncio.timeout_at(deadline) as timeout:
            result = await operation()
            if timeout.expired() or asyncio.get_running_loop().time() >= deadline:
                raise TimeoutError("continuation stage expired while its actual owner was joined")
            return result
```

- [ ] Add this complete controlled joined-owner test to `tests/unit/web/composer/test_continuation_action_admission.py`. It models the exact finish-once behavior: cancellation ends the waiter only after its actual child outcome is joined. The clock must still refuse an expired stage after that owner deliberately returns its known result. It never asserts that cancellation killed a submitted future.

```python
@pytest.mark.asyncio
async def test_stage_expiry_survives_a_joined_owner_that_retains_cancellation():
    import asyncio
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    policy = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.05, retention=1)
    normal = ContinuationActionClock(policy)
    normal.finish_preparation()
    expected = object()
    async def immediate():
        return expected
    assert await normal.run("physical", immediate) is expected

    release = asyncio.Event()
    observed = asyncio.Event()
    retained = []
    async def actual_child():
        await release.wait()
        return expected
    child = asyncio.create_task(actual_child())
    async def joined_owner():
        try:
            return await asyncio.shield(child)
        except asyncio.CancelledError as original:
            retained.append(original)
            observed.set()
            return await asyncio.shield(child)
    clock = ContinuationActionClock(policy)
    clock.finish_preparation()
    task = asyncio.create_task(clock.run("physical", joined_owner))
    try:
        await asyncio.wait_for(observed.wait(), timeout=2)
        assert not child.done() and not task.done()
        release.set()
        with pytest.raises(TimeoutError, match="actual owner was joined"):
            await asyncio.wait_for(task, timeout=2)
        assert child.done() and child.result() is expected
        assert len(retained) == 1 and isinstance(retained[0], asyncio.CancelledError)
    finally:
        release.set()
        await asyncio.gather(child, task, return_exceptions=True)
```

Also add this complete synchronous-overrun counterexample to the same module. It prevents a busy actual synchronous phase from returning success before the event loop has delivered its timeout callback.

```python
@pytest.mark.asyncio
async def test_stage_expiry_checks_actual_monotonic_time_before_timeout_callback():
    import time
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    policy = ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.02, retention=1)
    clock = ContinuationActionClock(policy)
    clock.finish_preparation()
    async def synchronous_phase():
        time.sleep(0.04)
        return "late actual result"
    with pytest.raises(TimeoutError, match="actual owner was joined"):
        await clock.run("physical", synchronous_phase)
```

Behavioral RED: remove the post-return expired/deadline check from the importable clock. The normal control still passes, but the joined-owner `pytest.raises` assertion fails because the expired stage returns a value. Restore it, then omit only the monotonic comparison and require the synchronous-overrun test to fail. Restore both checks. Run the exact existing action-admission module command in C5's normal RED/GREEN packet, requiring native exit 0. Original deferred cancellation objects continue through each SQL projection; this post-return check does not suppress them or release their producer's custody.

- [ ] Append `clock: ContinuationActionClock | None = None` to `ProviderCallCustody`'s constructor and `_clock` to its slots; assign `self._clock = clock` and expose this exact read-only property. C5.3 already shows the final `mint_continuation` method with that required clock and actual running receipt; do not introduce a second mint path.

```python
@property
def continuation_clock(self) -> ContinuationActionClock | None:
    return self._clock
```

- [ ] In `_litellm_acompletion`, preserve all existing exact evidence detachment, credential/native-field scans, schema checks and endpoint configuration. Replace the existing `await enforce_prompt_guard(...)` statement with an inner function containing that exact statement and this complete stage dispatch; it does not change the guarded input after capture.

```python
async def guard_prepared_input() -> None:
    await enforce_prompt_guard(
        mount=prompt_guard_mount, mode=prompt_guard_mode, role=prompt_guard_role,
        provider_custody=provider_custody, messages=kwargs["messages"],
        tools=kwargs["tools"] if "tools" in kwargs else (), user_prompt=prompt_guard_user_prompt,
        trusted_message_indexes=evidence.trusted_message_indexes | prompt_guard_trusted_message_indexes,
        content_parts=content_parts, tool_argument_parts=argument_parts, thinking_parts=thinking_parts,
    )

clock = provider_custody.continuation_clock
if clock is None:
    await guard_prepared_input()
elif clock.policy.guard is None:
    if prompt_guard_mount is not None or prompt_guard_mode != "debug-when-unconfigured":
        raise ValueError("actual guard service has no declared independent allowance")
    await guard_prepared_input()
    clock.finish_preparation()
else:
    clock.finish_preparation()
    await clock.run("guard", guard_prepared_input)
```

Keep the existing identity/usage-accounting kwargs updates. Replace the full contiguous span from its `await provider_custody.admit(...)` through its SDK return with this complete block. It charges quota/admission/dispatch-intent work once under Q and actual SDK execution once under H. Positive live no-SDK custody can dispose a committed may-dispatch intent when Q expires; the intent alone never establishes that proof.

```python
async def admit_and_retain_dispatch_intent() -> None:
    await provider_custody.admit(model=kwargs["model"])
    if on_provider_dispatch is not None:
        on_provider_dispatch()
    await provider_custody.prepare_sdk_entry()

try:
    if clock is None:
        await admit_and_retain_dispatch_intent()
    else:
        await clock.run("quota", admit_and_retain_dispatch_intent)
except BaseException as admission_failure:
    if provider_custody.attempt is not None:
        try:
            cancellations = await provider_custody.cancel_before_sdk(model=kwargs["model"])
        except BaseException as disposition_failure:
            raise BaseExceptionGroup("Provider pre-dispatch and required disposition outcomes",
                                     [admission_failure, disposition_failure]) from None
        if cancellations:
            raise BaseExceptionGroup("Provider pre-dispatch and original cancellations",
                                     [admission_failure, *cancellations]) from None
    raise
provider_custody.mark_sdk_entered()
if clock is None:
    return await litellm.acompletion(**kwargs)
kwargs["timeout"] = clock.policy.physical
return await clock.run("physical", lambda: litellm.acompletion(**kwargs))
```

- [ ] In `_call_llm_with_audit_body`, replace only `completion = await asyncio.wait_for(completion_call, timeout=timeout)` with the following branch; make the corresponding exact change around the text gateway's `asyncio.wait_for` call. C5.5's retention hook enforces V after complete admission. All existing error/audit `finally` logic stays active.

```python
if provider_custody is not None and provider_custody.continuation_clock is not None:
    completion = await completion_call
else:
    completion = await asyncio.wait_for(completion_call, timeout=timeout)
```

V2 phase execution calls these gateway methods with the selected complete action allowance and explicitly reserved custody; it never enters `_call_llm_before_deadline`'s v1 remaining-segment retry loop. Keep that v1 method unchanged for synchronous/direct-library paths. Advisor/title physical calls receive their own role clocks and durable ordinals; a nested call is an explicit phase rather than a fresh hidden total budget.

- [ ] Add these real timeout/recurrence tests; `asyncio.Event` controls scheduling and no paid provider is called.

```python
@pytest.mark.asyncio
async def test_a_stage_cannot_replenish_the_same_action_clock():
    import asyncio
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    clock = ContinuationActionClock(ContinuationActionPolicy(preparation=1, guard=1, quota=1, physical=1, retention=1))
    clock.finish_preparation()
    async def done():
        return "complete"
    assert await clock.run("guard", done) == "complete"
    original = clock.deadline
    with pytest.raises(ValueError, match="repeated"):
        await clock.run("guard", done)
    assert clock.deadline == original


@pytest.mark.asyncio
async def test_physical_timeout_is_its_declared_bound_not_segment_remainder():
    import asyncio
    from elspeth.web.composer.continuation_clock import ContinuationActionClock
    clock = ContinuationActionClock(ContinuationActionPolicy(preparation=1, guard=None, quota=None, physical=0.01, retention=1))
    clock.finish_preparation()
    async def physical():
        await asyncio.sleep(0.05)
        return "too late"
    with pytest.raises(TimeoutError):
        await clock.run("physical", physical)
    with pytest.raises(ValueError, match="inapplicable"):
        await clock.run("quota", physical)
```

Behavioral RED: temporarily return `await operation()` without `asyncio.timeout_at` in the clock; the operation returns after 0.05 seconds and the named `pytest.raises(TimeoutError)` assertion must fail. Restore the real timeout, then run this module and existing complete-response/gateway/quota tests; native exit 0. The pure >300-second fit test is arithmetic; actual timeout enforcement uses a shorter controlled scale without sleeping for minutes.

### C5.5: Retain complete admitted response bytes before quota/audit settlement

**Files:** Extend `sessions/continuation.py`, `sessions/service.py`, `sessions/protocol.py`, `composer/provider_quota.py` and both complete-response gateway bodies. The frames companion owns `encode_continuation_response` and `snapshot_after_provider_response`, including exact native fields, tool arguments, provider metadata and omitted-value semantics.

- [ ] Add these complete SQL helpers. Import `PrivateChunk`, `verify_private_chunk`, `encode_continuation_snapshot`, `decode_continuation_snapshot`, and `snapshot_after_provider_response` from the frames companion's concrete codec module. `PrivateChunk` is the frozen `(reference: PrivateChunkRef, canonical: str)` value defined there.

```python
def private_chunk_key(chunk: PrivateChunk) -> str:
    return f"value:{chunk.reference.codec}:{chunk.reference.digest}"


def retain_provider_response_on_connection(
    conn: Connection, binding: ContinuationProviderBinding, chunk: PrivateChunk, *, finished_at: datetime,
) -> None:
    from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_retained_physical_audit_call
    from elspeth.web.composer.continuation_provider_outcomes import require_successful_response_audit
    running = binding.running
    require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
    verify_private_chunk(chunk.reference, chunk.canonical)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(select(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    )).one()
    if (effect.owner != "provider" or effect.disposition != "dispatched" or effect.provider_attempt_id is None
        or effect.input_sha256 != binding.input_sha256 or effect.physical_sequence is None or effect.physical_generation is None):
        raise AuditIntegrityError("response retention lacks its actual dispatched provider effect")
    actual = conn.execute(select(provider_attempts).where(
        provider_attempts.c.attempt_id == effect.provider_attempt_id,
    )).one()
    physical = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
        attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
    if (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token) != (
        sid, physical.session_operation_id, physical.session_operation_epoch, physical.session_operation_lease_token):
        raise AuditIntegrityError("response retention changed its actual original physical owner")
    actual_started = actual.started_at
    if type(finished_at) is not datetime or finished_at.tzinfo is None or finished_at < restore_utc(actual_started):
        raise AuditIntegrityError("retained response finish instant differs from its actual admission clock")
    retained = decode_continuation_response(chunk)
    call = decode_retained_physical_audit_call(chunk)
    require_successful_response_audit(call, retained.provider_metadata, attempt_id=actual.attempt_id,
        started_at=restore_utc(actual_started), finished_at=finished_at)
    key = private_chunk_key(chunk)
    put_chunk(conn, session_id=sid, operation_id=operation_id, key=key,
              payload=chunk.canonical.encode("utf-8"), codec=chunk.reference.codec)
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    stack = snapshot_after_provider_response(decode_continuation_snapshot(stored.payload_json), chunk.reference)
    encoded = _effect_snapshot(encode_continuation_snapshot(stack), effect_key=binding.effect_key,
                               disposition="committed", receipt_id=effect.provider_attempt_id)
    conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    ).values(disposition="committed", result_chunk_key=key, receipt_key=effect.provider_attempt_id,
             response_finished_at=finished_at))
    encoded = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=binding.effect_key,
        disposition="committed", snapshot_json=encoded,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("response_retained", canonical_json({"effect_key": binding.effect_key, "attempt_id": effect.provider_attempt_id})),),
        handoff=False,
    )


def join_provider_effect_settlement(conn: Connection, binding: ContinuationProviderBinding) -> None:
    sid, operation_id = str(binding.running.claim.session_id), binding.running.claim.operation_id
    effect = conn.execute(select(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    )).one()
    actual = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == effect.provider_attempt_id)).one()
    creator = conn.execute(select(attempts).where(
        attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
        attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation,
    )).one()
    if (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token) != (
        sid, creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token,
    ) or actual.settled_at is None or actual.ledger_entry_id is None:
        raise AuditIntegrityError("provider settlement lacks its actual original-creator ledger receipt")
    disposition = "committed" if effect.result_chunk_key is not None else "unknown"
    conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == binding.effect_key,
    ).values(disposition=disposition))
    parent = conn.execute(select(jobs.c.status).where(jobs.c.session_id == sid, jobs.c.operation_id == operation_id)).one()
    if parent.status in ("completed", "failed"):
        # Late financial settlement belongs to its actual ledger owner; the
        # immutable public terminal and its as-of-terminal snapshot stay fixed.
        return
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    next_snapshot = _effect_snapshot(stored.payload_json, effect_key=binding.effect_key,
                                     disposition=disposition, receipt_id=actual.attempt_id,
                                     remove_attempt_id=actual.attempt_id)
    next_snapshot = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=binding.effect_key,
        disposition=disposition, snapshot_json=next_snapshot,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=next_snapshot,
        event_batch=(ContinuationEvent("effect_committed", canonical_json({
            "effect_key": binding.effect_key, "attempt_id": actual.attempt_id,
            "ledger_entry_id": actual.ledger_entry_id, "response_retained": effect.result_chunk_key is not None,
        })),), handoff=False,
    )
```

- [ ] Add this exact service method and its matching protocol signature. It verifies the active current execution before inserting the private response. The physical audit ledger remains a separate real owner, joined by the next step.

```python
async def retain_continuation_provider_response(
    self, *, binding: ContinuationProviderBinding, chunk: PrivateChunk, finished_at: datetime, required_work: RequiredWorkTicket,
) -> None:
    context = binding.running.session_operation_context
    sid = context.fence.session_id
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,), session_id=sid, context=context)
    def _sync() -> None:
        with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
            self._require_session_operation_context_on_connection(conn, context, session_id=sid,
                expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn), audit_only=True)
            retain_provider_response_on_connection(conn, binding, chunk, finished_at=finished_at)
    await run_required_sql_in_worker(required_work, _sync)
```

- [ ] Add these complete custody methods. Import `_AdmittedLLMCompletion` and `_AdmittedLLMProviderMetadata` from `_compose_loop_carriers`, and `RetainedAdvisorResultMetadata` plus both response encoders from `continuation_codec`. The public entry accepts the actual fully admitted nominal completion, rather than caller-encoded bytes. Its private operation performs all owned encoding/credential checks and SQL retention inside the single original V stage. The text method selects the text encoder in that same operation; it does not call a second clock stage. Optional Advisor result metadata preserves the actual body's complete returned values; primary/title callers leave it `None`. The existing `RequiredWorkCoordinator.reserve_pair` allocates a distinct recurrence on each invocation, so response retention and the pre-SDK marker use separate actual SQL/projection tickets. Do not add a duplicate recurrence allocator.

```python
async def retain_continuation_response(self, completion: _AdmittedLLMCompletion) -> None:
    await self._retain_continuation_response_value(completion=completion, content=None, provider_metadata=None)


async def retain_continuation_text_response(
    self, *, content: str | None, provider_metadata: _AdmittedLLMProviderMetadata,
    advisor_result_metadata: RetainedAdvisorResultMetadata | None = None,
) -> None:
    await self._retain_continuation_response_value(completion=None, content=content, provider_metadata=provider_metadata,
                                                    advisor_result_metadata=advisor_result_metadata)


async def _retain_continuation_response_value(
    self, *, completion: _AdmittedLLMCompletion | None, content: str | None,
    provider_metadata: _AdmittedLLMProviderMetadata | None,
    advisor_result_metadata: RetainedAdvisorResultMetadata | None = None,
) -> None:
    if self._continuation_binding is None:
        return
    if self.attempt is None:
        raise AuditIntegrityError("retained continuation response lost its actual provider admission")
    if (type(self.call) is not ComposerLLMCall or self.call.call_id != self.attempt.attempt_id
        or self.call.started_at != self.attempt.started_at or self.call.status is not ComposerLLMCallStatus.SUCCESS):
        raise AuditIntegrityError("received response retention requires its actual complete frozen successful audit")
    self._continuation_finished_at = self.call.finished_at
    async def retain() -> None:
        if (completion is None) == (provider_metadata is None):
            raise AuditIntegrityError("response retention needs exactly one nominal completion variant")
        if completion is not None:
            if advisor_result_metadata is not None:
                raise AuditIntegrityError("primary response acquired unrelated Advisor result metadata")
            chunk = encode_continuation_response(completion, audit_call=self.call)
        elif provider_metadata is not None:
            chunk = encode_continuation_text_response(content=content, provider_metadata=provider_metadata,
                                                     advisor_result_metadata=advisor_result_metadata, audit_call=self.call)
        else:
            raise AuditIntegrityError("response retention lost its selected nominal completion")
        sql, projection = self.required_work.reserve_pair(
            RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION,
        )
        try:
            _, cancellations = await self._join(self.service.retain_continuation_provider_response(
                binding=self._continuation_binding, chunk=chunk, finished_at=self._continuation_finished_at, required_work=sql,
            ))
        except BaseException:
            if sql.complete:
                projection.complete_without_submission()
            raise
        projection.begin_projection()
        projection.complete_owned()
        self.raise_deferred_cancellations(cancellations)
    if self._clock is None:
        raise AuditIntegrityError("continuation response retention lost its original action clock")
    await self._clock.run("retention", retain)
```

Required-work recurrence is local custody identity; the durable request-wide provider ordinal is independent. Restore does not rerun this retention method for an already retained response.

- [ ] Add this complete behavioral control to C4b's physical accounting module after its real `make_custody` helper. The injected encoder is the actual selected production encoder symbol; it asserts V has begun, then fails before SQL. Removing the in-stage encoding must fail this assertion. The live no-SDK cancellation owner still settles the admitted physical attempt on scope exit.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ("completion", "text"))
async def test_complete_response_encoding_runs_inside_single_retention_stage(operation_store, monkeypatch, variant):
    from elspeth.web.composer import provider_quota as quota_module
    from tests.unit.web.composer.test_continuation_response import completion
    _engine, repository, _authority, _service, _sid = operation_store
    running, coordinator, binding, custody = make_custody(operation_store)
    actual = completion()
    original_failure = ValueError("controlled response encoder refusal")
    calls = []
    def reject_encoding(*args, **kwargs):
        assert custody.continuation_clock is not None
        assert "retention" in custody.continuation_clock._seen
        calls.append(variant)
        raise original_failure
    symbol = "encode_continuation_response" if variant == "completion" else "encode_continuation_text_response"
    monkeypatch.setattr(quota_module, symbol, reject_encoding)
    try:
        async with provider_call_scope(custody):
            await custody.admit(model="test-model")
            custody.continuation_clock.finish_preparation()
            with pytest.raises(ValueError) as caught:
                if variant == "completion":
                    await custody.retain_continuation_response(actual)
                else:
                    await custody.retain_continuation_text_response(content=actual.message.content,
                                                                    provider_metadata=actual.provider_metadata)
            assert caught.value is original_failure
        assert calls == [variant]
        assert_proven_no_sdk(operation_store, running, binding)
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
```

- [ ] In `ProviderGateway._call_llm`, immediately after its complete admitted-response credential/native-field checks and before `return completion`, insert:

```python
if provider_custody is not None and provider_custody.continuation_clock is not None:
    await provider_custody.retain_continuation_response(completion)
```

At the text/advisor/title gateway's corresponding fully admitted point, call `await provider_custody.retain_continuation_text_response(content=content, provider_metadata=response_metadata)` with the actual owned values already present there. Encoding occurs inside custody V. Retention never serializes a foreign SDK object, strips native signature blocks or derives a model response from an audit envelope.

- [ ] Append optional `continuation_binding: ContinuationProviderBinding | None = None` to `finish_provider_attempt`, `add_messages_atomic`, `_write_audit_cohort_on_connection` and the corresponding service protocol methods. Forward it from `ProviderCallCustody.settle` to `finish_provider_attempt`, from that method to `add_messages_atomic`, and from its `_write` closure to `_write_audit_cohort_on_connection`. Immediately after that helper's existing `record_token_usage_on_connection(...)` call, add:

```python
if continuation_binding is not None:
    join_provider_effect_settlement(conn, continuation_binding)
```

This preserves the existing complete audit cohort and actual ledger transaction, including `visible_response_gap` evidence and secret-scrubbed private diagnostics. A replayed ledger with a changed source/run/identity/session tuple still refuses under the current quota owner. Retaining a response and audit settlement are separate durable transitions with complete snapshots, not an invented distributed transaction.

### C5.6: Join verified cancellation before SDK entry to the same durable effect

**Files:** Modify `coordination/quota_authority.py::cancel_undispatched_provider_attempt_on_connection`, `sessions/service.py::cancel_undispatched_provider_attempt`, its protocol signature, `composer/provider_quota.py::ProviderCallCustody._cancel_undispatched`, and `sessions/continuation.py`.

- [ ] Add the frozen receipt below to `quota_authority.py`; change the existing cancellation SQL function's return annotation from `None` to `UndispatchedProviderReceipt`. Preserve its complete existing actual-event/identity/source/run/ledger replay validation. At the successful replay `return`, return this receipt. After its final successful attempt UPDATE, return the same expression. The receipt is issued only by this actual owner after verifying or inserting both zero-usage ledger and exact cancellation event; a pending row never issues it.

```python
@dataclass(frozen=True, slots=True)
class UndispatchedProviderReceipt:
    attempt_id: str
    ledger_entry_id: str
    cancellation_event_id: str

# Both successful return sites in cancel_undispatched_provider_attempt_on_connection:
return UndispatchedProviderReceipt(attempt_id, entry_id, event_id)
```

- [ ] Add the following SQL adapter. Import the nominal receipt from `coordination.quota_authority`, and `token_usage_ledger_table as usage` from the actual quota tables module already imported by that authority. The caller invokes the existing quota owner first in the same transaction, retaining its exact return receipt. This adapter checks the effect was never durably marked as possibly dispatched and uses the actual verified cancellation receipt; it does not use a zero balance or absent pending row as proof.

```python
def join_undispatched_provider_cancellation(
    conn: Connection, binding: ContinuationProviderBinding, receipt: UndispatchedProviderReceipt,
) -> None:
    if type(receipt) is not UndispatchedProviderReceipt:
        raise AuditIntegrityError("continuation cancellation lacks its actual quota-owner receipt")
    running = binding.running
    require_continuation_mutation(conn, running.session_operation_context,
                                  allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(select(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id,
        effects.c.effect_key == binding.effect_key,
    )).one()
    actual = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == receipt.attempt_id)).one()
    fence = running.session_operation_context.fence
    if (effect.owner != "provider" or effect.provider_attempt_id != receipt.attempt_id
        or effect.disposition not in ("prepared", "dispatched", "refused")
        or actual.settled_at is None or actual.ledger_entry_id != receipt.ledger_entry_id
        or (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token)
           != (sid, fence.operation_id, fence.operation_epoch, fence.lease_token)):
        raise AuditIntegrityError("continuation cancellation contradicts actual no-SDK custody")
    result_key = f"cancelled:{receipt.attempt_id}"
    payload = canonical_json({"attempt_id": receipt.attempt_id, "ledger_entry_id": receipt.ledger_entry_id,
                              "cancellation_event_id": receipt.cancellation_event_id,
                              "disposition": "cancelled_before_dispatch"})
    put_chunk(conn, session_id=sid, operation_id=operation_id, key=result_key,
              payload=payload.encode("utf-8"), codec="provider-cancellation.v1")
    conn.execute(update(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id,
        effects.c.effect_key == binding.effect_key,
    ).values(disposition="refused", receipt_key=receipt.cancellation_event_id, result_chunk_key=result_key))
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    encoded = _effect_snapshot(stored.payload_json, effect_key=binding.effect_key,
                               disposition="refused", receipt_id=receipt.cancellation_event_id,
                               remove_attempt_id=receipt.attempt_id)
    encoded = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=binding.effect_key,
        disposition="refused", snapshot_json=encoded,
    )
    append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=encoded,
        event_batch=(ContinuationEvent("effect_refused", payload),), handoff=False,
    )
```

- [ ] Append optional `continuation_binding: ContinuationProviderBinding | None = None` to the existing service/protocol cancellation signatures. In `_cancel_undispatched`, forward `continuation_binding=self._continuation_binding` in its existing service call. Preserve the joined physical SQL ticket, original cancellations, and clearing `attempt`/`call` only after the delivered SQL outcome. In the service's existing `_sync` transaction replace only its cancellation invocation with this exact call/adapter join:

```python
receipt = cancel_undispatched_provider_attempt_on_connection(
    conn, session_operation_context=session_operation_context, attempt_id=attempt_id,
    requested_model=requested_model, append_audit_event=_append_audit_event,
)
if continuation_binding is not None:
    if continuation_binding.running.session_operation_context != session_operation_context:
        raise AuditIntegrityError("undispatched continuation cancellation substituted its live context")
    join_undispatched_provider_cancellation(conn, continuation_binding, receipt)
```

- [ ] Add this actual SQL test to the cold-restore test module. The same test is imported into the PostgreSQL test packet. It uses the real service cancellation owner, so replay validation checks exact event and all ledger measures. C4b adds cancellation-delivered-during-admission and callback-refusal gateway fault points around this same durable assertion.

```python
@pytest.mark.asyncio
async def test_undispatched_cancellation_joins_real_zero_ledger_and_clears_pending(operation_store):
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.coordination.quota_authority import quota_provider_attempts_table as provider_attempts
    engine, repository, authority, service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    stored = authority.load_continuation_snapshot(running)
    binding = authority.prepare_provider_effect(
        running, family=0, input_sha256="c" * 64, snapshot_json=stored.payload_json,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
    )
    actual = await service.begin_provider_attempt(session_operation_context=running.session_operation_context,
                                                  source="composer", continuation_binding=binding, requested_model="test-model")
    await service.cancel_undispatched_provider_attempt(
        session_operation_context=running.session_operation_context, attempt_id=actual.attempt_id,
        requested_model="test-model", continuation_binding=binding,
    )
    await service.cancel_undispatched_provider_attempt(
        session_operation_context=running.session_operation_context, attempt_id=actual.attempt_id,
        requested_model="test-model", continuation_binding=binding,
    )
    with engine.connect() as conn:
        effect = conn.execute(select(effects).where(effects.c.operation_id == record.operation_id)).one()
        attempt = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == actual.attempt_id)).one()
    assert effect.disposition == "refused" and effect.result_chunk_key is not None
    assert attempt.settled_at is not None and attempt.ledger_entry_id is not None
    restored = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
    assert actual.attempt_id not in restored.pending_settlement_ids
    assert next(item for item in restored.effect_frontier if item.effect_id == binding.effect_key).disposition == "refused"
    repository.release(running.session_operation_context)
```

- [ ] Prove behavioral RED by temporarily omitting only the `join_undispatched_provider_cancellation` call while keeping the real quota cancellation. The test must reach and fail `effect.disposition == "refused"`. Restore it, run the command below, and require native exit 0 before committing the exact changed source/protocol/test paths as `fix(composer): join proven undispatched provider settlement`.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_undispatched_cancellation_joins_real_zero_ledger_and_clears_pending >"$COMPOSER_LOG_DIR/c5-undispatched.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-undispatched.log"
test "$composer_exit" -eq 0
```

### C3.10: Keep terminal publication on the existing owner transaction

**Files:** Modify `_running_predicates`, `_select_failure_on_connection`, `settle_composer_operation_on_connection` in `coordination/composer_operation_authority.py`; modify deadline comparisons in existing `SessionServiceImpl.complete_composer_async_operation` and `fail_composer_async_operation`.

- [ ] Add this v2 branch before `_running_predicates`' existing return. Preserve the old v1 return exactly. All callers now keep their nominal existing running/actor/publication APIs, while v2 compares the actual segment claim and current generation rather than the null parent claim token.

```python
if claim.segment_sequence is not None:
    return (
        *_key(claim.session_id, claim.operation_id), jobs.c.lifecycle_version == 2,
        jobs.c.status == "running", jobs.c.attempt == claim.attempt,
        jobs.c.current_segment_sequence == claim.segment_sequence,
        jobs.c.ownership_generation == claim.ownership_generation,
        jobs.c.session_operation_id == fence.operation_id,
        jobs.c.session_operation_epoch == fence.operation_epoch,
        jobs.c.session_operation_lease_token == fence.lease_token,
        select(segments.c.operation_id).where(
            segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
            segments.c.state == "running", segments.c.claim_token == claim.claim_token,
            segments.c.session_operation_id == fence.operation_id,
            segments.c.session_operation_epoch == fence.operation_epoch,
            segments.c.session_operation_lease_token == fence.lease_token,
        ).exists(),
    )
```

- [ ] Add `if row.lifecycle_version == 2: return failure` in `_select_failure_on_connection` immediately after the existing explicit Stop-marker branch and before its deadline branch. Retain `authoritative_failure` precedence and the exact Stop error construction. A v2 segment timeout never manufactures a logical whole-request deadline failure.
- [ ] Add this nominal deadline helper to `sessions/composer_operations.py` and import it in `sessions/service.py`. In the two existing terminal functions replace each `current.deadline_at <= now`/`current_job.deadline_at <= now` with this helper, and each `current.deadline_at > now` with its negation. No legacy worker timeout is changed.

```python
def composer_operation_deadline_expired(record: ComposerOperationRecord, now: datetime) -> bool:
    if record.lifecycle_version == 2:
        return False
    if record.deadline_at is None:
        raise AuditIntegrityError("legacy operation lost its actual deadline")
    return restore_utc(record.deadline_at) <= restore_utc(now)
```

- [ ] Add this complete v2 terminal helper to the SQL authority module. It is called inside the existing service's assistant/audit/proposal/terminal transaction, so no new publication writer is added. The source's `ComposerOperationError` versus `MessageWithStateResponse` outcome distinction stays nominal.

```python
def settle_continuation_on_connection(
    conn: Connection, running: ComposerOperationRunning, *,
    outcome: MessageWithStateResponse | ComposerOperationError,
    settled_at: datetime | None = None,
) -> ComposerOperationRecord:
    claim = running.claim
    parent = _read(conn, claim.session_id, claim.operation_id)
    if parent is None:
        raise AuditIntegrityError("terminal continuation parent missing")
    if parent.status in ("completed", "failed"):
        return _record_from_row(parent)
    failed = isinstance(outcome, ComposerOperationError)
    require_continuation_mutation(conn, running.session_operation_context,
                                  allow_cancel=failed, allow_expired_segment=failed)
    if failed and outcome.failure_code == "request_cancelled" and parent.cancel_requested_at is None:
        raise AuditIntegrityError("Stop terminal lacks a durable Stop marker")
    pending = conn.execute(select(provider_attempts.c.attempt_id).join(
        effects, effects.c.provider_attempt_id == provider_attempts.c.attempt_id,
    ).where(effects.c.session_id == str(claim.session_id), effects.c.operation_id == claim.operation_id,
            provider_attempts.c.settled_at.is_(None)).limit(1)).one_or_none()
    if pending is not None:
        raise AuditIntegrityError("terminal continuation still owes actual provider settlement")
    stored = load_snapshot(conn, session_id=str(claim.session_id), operation_id=claim.operation_id)
    now = database_now(conn) if settled_at is None else settled_at
    encoded = canonical_json(outcome.model_dump(mode="json"))
    append_complete_snapshot(
        conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor,
        snapshot_json=stored.payload_json,
        event_batch=(ContinuationEvent("settled", canonical_json({"result_sha256": composer_operation_result_hash(encoded)})),),
        handoff=False,
    )
    changed = conn.execute(update(jobs).where(*_running_predicates(running)).values(
        **_terminal_values(outcome, now=now, settled_by="owner_terminal"),
        phase="settled", status_revision=jobs.c.status_revision + 1,
    ))
    if changed.rowcount != 1:
        raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
    conn.execute(update(segments).where(
        segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
        segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
    ).values(state="settled", claim_token=None, claim_owner_instance_id=None, claim_expires_at=None))
    conn.execute(update(attempts).where(
        attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
        attempts.c.sequence == claim.segment_sequence, attempts.c.generation == claim.ownership_generation,
        attempts.c.revoked_at.is_(None),
    ).values(revoked_at=now, outcome="settled"))
    terminal = _read(conn, claim.session_id, claim.operation_id)
    if terminal is None:
        raise AuditIntegrityError("settled continuation vanished")
    return _record_from_row(terminal)
```

At the beginning of `settle_composer_operation_on_connection`, before its v1 branch, insert:

```python
if running.claim.segment_sequence is not None:
    if isinstance(outcome, ComposerOperationError):
        current = _read(conn, running.claim.session_id, running.claim.operation_id)
        if current is not None:
            outcome = _select_failure_on_connection(current, outcome, now=database_now(conn), authoritative_failure=authoritative_failure)
    return settle_continuation_on_connection(conn, running, outcome=outcome, settled_at=settled_at)
```

Keep the current exact immutable retry and native audit/visible-tool evidence paths in `complete_composer_async_operation`/`fail_composer_async_operation`. A cancelled business transaction still refuses before mutation; its audit-only failure owner can write the real Stop terminal after required settlement. No returned assistant or ComposerResult is fabricated merely to release a worker.

- [ ] Add the full cumulative logical-operation revision projection below in `coordination/composer_operation_authority.py`. It reads actual SQL state creators joined to immutable attempt history across every sequence/generation, rather than counting requested edits, retained Python witnesses or the parent's latest fence alone. In `list_failed_outcomes`' existing `for record in selected`, change its first `if record.session_operation_id is None:` to `if record.lifecycle_version == 2: saved = _saved_continuation_revisions(conn, record)` followed by `elif record.session_operation_id is None:`; preserve the existing remaining v1 branches.

```python
def _saved_continuation_revisions(conn: Connection, record: ComposerOperationRecord) -> ComposerOperationSavedRevisions:
    rows = conn.execute(select(composition_states_table.c.id, composition_states_table.c.version).join(
        attempts, and_(attempts.c.session_id == composition_states_table.c.session_id,
                       attempts.c.session_operation_id == composition_states_table.c.creator_operation_id),
    ).where(
        attempts.c.session_id == str(record.session_id), attempts.c.operation_id == record.operation_id,
    ).order_by(composition_states_table.c.version.desc())).all()
    latest = rows[0] if rows else None
    return ComposerOperationSavedRevisions(
        attribution="fenced" if record.started_at is not None else "unstarted", count=len(rows),
        latest_state_id=UUID(latest.id) if latest is not None else None,
        latest_version=latest.version if latest is not None else None,
    )
```

- [ ] At main step 9, after C3.4b/F10/F28.3, add this concrete projection test and its PostgreSQL re-export to the cold-restore module. Projection production remains available early. The real retained-title/apply-title fixture supplies the complete ready bootstrap and creates no composition revision; both original revision writers and repeated-read/count/latest-ID/version assertions stay intact.

```python
@pytest.mark.asyncio
async def test_saved_revision_projection_covers_both_segment_creators_once(operation_store, monkeypatch):
    from elspeth.web.coordination.composer_operation_authority import _saved_continuation_revisions
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.composer.continuation_policy import decode_continuation_policy
    from tests.unit.web.composer.test_continuation_title import retained_title, apply_title

    engine, repository, authority, service, sid = operation_store
    first, custody, binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None
    await apply_title(operation_store, first, custody, binding, observed, candidate)
    record = authority.get_for_turn(session_id=sid, operation_id=first.claim.operation_id)
    assert record is not None and record.user_message_id == record.reserved_user_message_id
    saved_one = await service.save_composition_state(sid, CompositionStateData(), provenance="session_seed",
                                                       session_operation_context=first.session_operation_context)
    stored = authority.load_continuation_snapshot(first)
    stack = decode_continuation_snapshot(stored.payload_json)
    assert stack.frames[0].ingress_phase == "ready" and stack.frames[0].bootstrap is not None
    ready = stack
    action = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash).actions.session_sql
    receipt = authority.commit_checkpoint_and_successor(
        first, expected_head=saved_one.id, expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor, snapshot_json=encode_continuation_snapshot(ready),
        next_action_json=action.model_dump_json(), policy_sha256=stack.policy_hash,
    )
    authority.verify_yield(receipt)
    repository.release(first.session_operation_context)
    second = start_continuation(repository, authority)
    try:
        saved_two = await service.save_composition_state(sid, CompositionStateData(), provenance="session_seed",
                                                           session_operation_context=second.session_operation_context)
        current = authority.get(session_id=sid, operation_id=record.operation_id)
        assert current is not None and current.user_message_id == record.user_message_id
        with engine.connect() as conn:
            projection = _saved_continuation_revisions(conn, current)
            repeated = _saved_continuation_revisions(conn, current)
        assert projection == repeated
        assert projection.count == 2 and projection.latest_state_id == saved_two.id
        assert projection.latest_version == saved_two.version
        assert len(calls) == 1
    finally:
        repository.release(second.session_operation_context)
```

### C3.11: Bind live proposals to their actual segment and retain historical proposal authority

**Files:** Modify `_proposal_composer_binding` and `verify_historical_proposal_composer_binding_on_connection` in `src/elspeth/web/coordination/composer_operation_authority.py`; extend `tests/unit/web/sessions/test_continuation_cold_restore.py`.

- [ ] Insert this helper in the SQL authority module. `attempts.claim_attempt` is the immutable public claim attempt stored by C3.4, distinct from sequence/generation. A historical fence is recognized before the legacy lookup even after the parent points at a later segment.

```python
def _continuation_proposal_binding(
    conn: Connection, context: SessionOperationContext, *, user_message_id: UUID | None,
    actor: str, running: ComposerOperationRunning | None,
) -> ComposerOperationBinding | None:
    fence = context.fence
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == fence.session_id,
        attempts.c.session_operation_epoch == fence.operation_epoch,
    ).limit(1)).one_or_none()
    if history is None:
        return None
    require_continuation_mutation(conn, context, allow_cancel=True, allow_expired_segment=True)
    parent = conn.execute(select(jobs).where(
        jobs.c.session_id == history.session_id, jobs.c.operation_id == history.operation_id,
        jobs.c.lifecycle_version == 2,
    )).one()
    segment = conn.execute(select(segments).where(
        segments.c.session_id == history.session_id, segments.c.operation_id == history.operation_id,
        segments.c.sequence == history.sequence, segments.c.generation == history.generation,
    )).one()
    if (parent.status != "running" or segment.state != "running"
        or parent.current_segment_sequence != history.sequence
        or parent.ownership_generation != history.generation
        or history.revoked_at is not None
        or history.session_operation_id != fence.operation_id
        or history.session_operation_lease_token != fence.lease_token
        or parent.attempt != history.claim_attempt or segment.claim_token is None):
        raise AuditIntegrityError("proposal continuation segment authority mismatch")
    if running is not None and (
        running.session_operation_context != context
        or running.claim.session_id != UUID(history.session_id)
        or running.claim.operation_id != history.operation_id
        or running.claim.segment_sequence != history.sequence
        or running.claim.ownership_generation != history.generation
        or running.claim.attempt != history.claim_attempt
        or running.claim.claim_token != segment.claim_token
    ):
        raise AuditIntegrityError("proposal continuation claim authority mismatch")
    _verify_continuation_proposal_ingress(conn, parent, user_message_id=user_message_id, actor=actor)
    return ComposerOperationBinding(
        operation_id=history.operation_id, session_operation_id=history.session_operation_id,
        session_operation_epoch=history.session_operation_epoch, attempt=history.claim_attempt,
    )


def _verify_continuation_proposal_ingress(
    conn: Connection, parent: Row[Any], *, user_message_id: UUID | None, actor: str | None,
) -> None:
    if (user_message_id is None or parent.user_message_id != str(user_message_id)
        or actor != f"composer-web:user:{parent.actor_user_id}"):
        raise AuditIntegrityError("proposal continuation actor/user binding mismatch")
    user = conn.execute(select(chat_messages_table.c.role, chat_messages_table.c.writer_principal).where(
        chat_messages_table.c.session_id == parent.session_id,
        chat_messages_table.c.id == str(user_message_id),
    )).one_or_none()
    if user is None or user.role != "user" or user.writer_principal != "route_user_message":
        raise AuditIntegrityError("proposal continuation user is not conversational ingress")
```

- [ ] In `_proposal_composer_binding`, immediately after `fence = context.fence`, add the following branch. The existing v1 body stays byte-for-byte below it. Its caller's normal mutation guard still blocks new proposals after Stop; the private revocation proof can verify the exact current owner without reauthorizing business mutation.

```python
continuation = _continuation_proposal_binding(
    conn, context, user_message_id=user_message_id, actor=actor, running=running,
)
if continuation is not None:
    return continuation
```

- [ ] In `verify_historical_proposal_composer_binding_on_connection`, immediately after its existing parent read and before the old combined `if`, add:

```python
if row is not None and row.lifecycle_version == 2:
    history = conn.execute(select(attempts).where(
        attempts.c.session_id == str(session_id), attempts.c.operation_id == binding.operation_id,
        attempts.c.session_operation_id == binding.session_operation_id,
        attempts.c.session_operation_epoch == binding.session_operation_epoch,
        attempts.c.claim_attempt == binding.attempt,
    ).limit(1)).one_or_none()
    if history is None:
        raise AuditIntegrityError("historical proposal continuation attempt mismatch")
    _verify_continuation_proposal_ingress(conn, row, user_message_id=user_message_id, actor=actor)
    return
```

- [ ] At main step 9, after C3.4b/F10/F28.3, add this complete test, its PostgreSQL re-export and the following omission control. Proposal production remains available early. Both real proposal writers, planner-authored test proposals and every original historical/current-authority refusal remain; the actual title/bootstrap fixture supplies valid ready initialization before rollover. Product code never constructs pipeline structure to bypass the provider.

```python
@pytest.mark.asyncio
async def test_both_v2_proposal_writers_keep_historical_binding_after_rollover(operation_store, monkeypatch):
    from elspeth.core.canonical import stable_hash
    from elspeth.web.composer.pipeline_planner import PipelinePlanResult
    from elspeth.web.composer.pipeline_proposal import AbsentBase, PipelineProposal
    from elspeth.web.coordination.composer_operation_authority import (
        derive_proposal_composer_binding_on_connection,
        verify_historical_proposal_composer_binding_on_connection,
    )
    engine, repository, authority, service, sid = operation_store
    from tests.unit.web.composer.test_continuation_title import retained_title, apply_title
    from elspeth.web.composer.continuation_policy import decode_continuation_policy

    first, custody, title_binding, observed, candidate, calls = await retained_title(operation_store, monkeypatch)
    assert candidate is not None
    await apply_title(operation_store, first, custody, title_binding, observed, candidate)
    record = authority.get_for_turn(session_id=sid, operation_id=first.claim.operation_id)
    assert record is not None and record.user_message_id == record.reserved_user_message_id
    original_user_message_id = record.user_message_id
    pipeline = {"sources": {}, "nodes": [], "edges": [], "outputs": []}
    direct = await service.create_composition_proposal(
        session_id=sid, tool_call_id="call_direct_v2", tool_name="set_pipeline",
        summary="Requested graph", rationale="Explicit user request", affects=("graph",),
        arguments_json=pipeline, arguments_redacted_json=pipeline, base_state_id=None,
        actor="composer-web:user:alice", user_message_id=original_user_message_id,
        session_operation_context=first.session_operation_context,
    )
    planned = PipelineProposal.create(pipeline=pipeline, base=AbsentBase(), repair_count=0,
                                      skill_hash=stable_hash("test-planner-skill"))
    plan = PipelinePlanResult(proposal=planned, tool_call_id="call_pipeline_v2",
                             custody_result="not_required", model_identifier="planner-model",
                             model_version="planner-model-v1", provider="test")
    composed = await service.create_pipeline_composition_proposal(
        session_id=sid, plan=plan, summary="Requested graph", rationale="Explicit user request",
        affects=("graph",), arguments_redacted_json=pipeline, actor="composer-web:user:alice",
        composer_model_identifier="planner-model", composer_model_version="planner-model-v1",
        composer_provider="test", user_message_id=original_user_message_id,
        session_operation_context=first.session_operation_context,
    )
    assert direct.id != composed.id
    with engine.connect() as conn:
        binding = derive_proposal_composer_binding_on_connection(
            conn, first.session_operation_context, user_message_id=original_user_message_id,
            actor="composer-web:user:alice", running=first,
        )
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
    assert binding is not None and binding.attempt == first.claim.attempt
    stack = decode_continuation_snapshot(stored.payload_json)
    assert stack.frames[0].ingress_phase == "ready" and stack.frames[0].bootstrap is not None
    ready = stack
    action = decode_continuation_policy(stack.admission_policy_json, stack.policy_hash).actions.session_sql
    receipt = authority.commit_checkpoint_and_successor(
        first, expected_head=None, expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor, snapshot_json=encode_continuation_snapshot(ready),
        next_action_json=action.model_dump_json(), policy_sha256=stack.policy_hash,
    )
    authority.verify_yield(receipt)
    repository.release(first.session_operation_context)
    second = start_continuation(repository, authority)
    try:
        with engine.connect() as conn:
            verify_historical_proposal_composer_binding_on_connection(
                conn, binding=binding, session_id=sid, user_message_id=original_user_message_id,
                actor="composer-web:user:alice",
            )
            with pytest.raises(ComposerOperationFenceLost):
                derive_proposal_composer_binding_on_connection(
                    conn, first.session_operation_context, user_message_id=original_user_message_id,
                    actor="composer-web:user:alice", running=first,
                )
            forged = binding.model_copy(update={"attempt": second.claim.attempt})
            with pytest.raises(AuditIntegrityError, match="historical proposal continuation attempt"):
                verify_historical_proposal_composer_binding_on_connection(
                    conn, binding=forged, session_id=sid, user_message_id=original_user_message_id,
                    actor="composer-web:user:alice",
                )
        assert len(calls) == 1
    finally:
        repository.release(second.session_operation_context)
```

- [ ] Bootstrap the complete importable packet, then temporarily remove only the historical v2 branch. The test must reach its post-rollover assertion and fail with `historical proposal operation authority mismatch`; an import/collection error is a setup error. Restore the branch and run the same command, requiring native exit 0:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_both_v2_proposal_writers_keep_historical_binding_after_rollover >"$COMPOSER_LOG_DIR/c3-proposals.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-proposals.log"
test "$composer_exit" -eq 0
```

- [ ] Commit the authority/continuation model/test paths with normal hooks as `fix(composer): bind continuation proposals to immutable segment attempts` after the PostgreSQL reuse packet in C4b also passes.

### C4a.1: Read complete private state and commit actual phase transitions

**Files:** Extend `sessions/continuation.py` and `coordination/composer_operation_authority.py`; add concrete new source imports from the frames companion. No worker receives a raw connection.

- [ ] Add this exact reference walk and private chunk loader. It is an explicit closed frame walk, not arbitrary attribute reflection or an event-suffix replay. Add imports of `InitializeFrame`, `ComposeFrame`, `ToolBatchFrame`, `AdvisorFrame`, `EndFrame`, `PrivateChunkRef`, `ContinuationFrameStack` and the nominal codec functions.

```python
def required_private_chunks(stack: ContinuationFrameStack) -> tuple[PrivateChunkRef, ...]:
    refs: list[PrivateChunkRef] = []
    refs.extend(item.result for item in stack.pending_owner_results)
    if stack.semantic_frontier is not None:
        refs.append(stack.semantic_frontier)
    if stack.active_action_source is not None:
        refs.append(stack.active_action_source.response)
    for frame in stack.frames:
        match frame:
            case InitializeFrame():
                if frame.bootstrap is not None:
                    refs.append(frame.bootstrap)
                if frame.title_response is not None:
                    refs.append(frame.title_response)
            case ComposeFrame():
                refs.extend((frame.bootstrap, frame.iteration, frame.messages, frame.initial_state, frame.state, frame.evidence,
                             frame.audit_buffer, frame.transcript, frame.caches, frame.attachment_admissions))
                if frame.tools is not None:
                    refs.append(frame.tools)
                if frame.pending_response is not None:
                    refs.append(frame.pending_response)
            case ToolBatchFrame():
                refs.extend((frame.response, frame.accumulator))
            case AdvisorFrame():
                refs.append(frame.review)
                if frame.response is not None:
                    refs.append(frame.response)
            case EndFrame():
                refs.extend((frame.state, frame.candidate))
    unique: dict[tuple[str, str], PrivateChunkRef] = {}
    for ref in refs:
        key = (ref.codec, ref.digest)
        previous = unique.get(key)
        if previous is not None and previous != ref:
            raise AuditIntegrityError("one private chunk identity has conflicting byte bounds")
        unique[key] = ref
    return tuple(unique.values())


def put_private_chunks(conn: Connection, *, session_id: str, operation_id: str, values: tuple[PrivateChunk, ...]) -> None:
    for chunk in values:
        verify_private_chunk(chunk.reference, chunk.canonical)
        put_chunk(conn, session_id=session_id, operation_id=operation_id, key=private_chunk_key(chunk),
                  payload=chunk.canonical.encode("utf-8"), codec=chunk.reference.codec)


def load_private_chunks(conn: Connection, *, session_id: str, operation_id: str, stack: ContinuationFrameStack) -> tuple[PrivateChunk, ...]:
    loaded: list[PrivateChunk] = []
    for ref in required_private_chunks(stack):
        key = f"value:{ref.codec}:{ref.digest}"
        canonical = read_chunk(conn, session_id=session_id, operation_id=operation_id, key=key).decode("utf-8", errors="strict")
        verify_private_chunk(ref, canonical)
        loaded.append(PrivateChunk(ref, canonical))
    return tuple(loaded)
```

- [ ] Add these complete synchronous authority facets only with their actual later private-chunk/transition/worker owners. `load_continuation_snapshot` was moved earlier and installed once in C5.2a with nominal/strictTrue/unconditional actual SQL claim binding; preserve that implementation rather than adding or replacing it with the weaker raw copy below. The original complete block remains here as later integration material. Private reads allow seeing a committed Stop or spent segment clock so that the current worker can reconcile/settle/yield; only normal mutation/admission uses the strict default guard. Every actual action still requires a fresh live fence, actor/policy and current head check.

```python
def load_continuation_snapshot(self, running: ComposerOperationRunning) -> StoredContinuationSnapshot:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        return load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)


def load_continuation_chunks(self, running: ComposerOperationRunning, snapshot: StoredContinuationSnapshot) -> tuple[PrivateChunk, ...]:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
        current = load_snapshot(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id)
        if current != snapshot:
            raise ContinuationCASConflict("private chunk read selected an obsolete snapshot")
        return load_private_chunks(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
                                   stack=decode_continuation_snapshot(snapshot.payload_json))


def commit_transition(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], event: ContinuationEvent,
) -> StoredContinuationSnapshot:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        require_continuation_mutation(conn, running.session_operation_context)
        stack = decode_continuation_snapshot(snapshot_json)
        if (stack.logical.session_id, stack.logical.operation_id) != (str(running.claim.session_id), running.claim.operation_id):
            raise AuditIntegrityError("phase transition substituted another logical request")
        put_private_chunks(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, values=chunks)
        load_private_chunks(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id, stack=stack)
        return append_complete_snapshot(
            conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
            snapshot_json=snapshot_json, event_batch=(event,), handoff=False,
        )
```

- [ ] Add these concrete claim maintenance facets and branch existing `renew_claim`/`release_claim` to them when `claim.segment_sequence is not None`. The ordinary COMPOSE job path is unchanged below each branch.

```python
def renew_segment_claim(self, claim: ComposerOperationClaim) -> ComposerOperationRecord:
    with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
        now = database_now(conn)
        changed = conn.execute(update(segments).where(
            segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
            segments.c.claim_token == claim.claim_token, segments.c.state.in_(("queued", "interrupted")),
            segments.c.claim_expires_at > now,
        ).values(claim_expires_at=now + timedelta(seconds=self._claim_lease_seconds)))
        if changed.rowcount != 1:
            raise ComposerOperationFenceLost(session_id=claim.session_id, operation_id=claim.operation_id, attempt=claim.attempt)
        row = _read(conn, claim.session_id, claim.operation_id)
        if row is None:
            raise AuditIntegrityError("renewed continuation parent missing")
        return _record_from_row(row)


def release_segment_claim(self, claim: ComposerOperationClaim) -> None:
    with locked_session_transaction(self._engine, str(claim.session_id)) as conn:
        conn.execute(update(segments).where(
            segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence, segments.c.generation == claim.ownership_generation,
            segments.c.claim_token == claim.claim_token, segments.c.state.in_(("queued", "interrupted")),
        ).values(claim_token=None, claim_owner_instance_id=None, claim_expires_at=None))
```

### C4a.2: Adopt, execute and release through the actual worker owners

# C4a Worker Adoption and Recovery Implementation Plan Fragment

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Preserve actual v2 worker SQL, lease, cancellation, watcher and producer custody through durable interrupted recovery.

**Architecture:** Adapt the existing worker and lifecycle owners. A current or exact released original fence produces a typed same-sequence recovery receipt; registered physical SQL readback and actual task completion precede process-lock release.

**Tech Stack:** Python asyncio, SQLAlchemy, SQLite/PostgreSQL, existing required-work and process-recovery owners, pytest.

---

Planning only. Integrate this literal replacement into the storage companion;
do not edit the current implementation while authoring. Existing source is
`b003e0cd9c6c6a7b0968635a9c03f71a83bc2e73`, tree
`6a34e912debc469e9ce7e2048d433c08d5e606a9`. Its worker blob
`366da504760b884aef22803c42c4c9baeb8e8431` equals the reviewed ce410 worker
blob. That is an extraction anchor. Local `.9` qualification has since
completed and the combined integration exists at the source recorded in the
main plan. No future worker implementation or tests ran while writing this
packet.

This closes Astra E11 (called E9 in the delegation) at
`composer_async_worker.py:498–555`: retain actual SQL-start/adoption
cancellations, use the actual required-work coordinator, join the actual
adoption/lease tasks, then obtain a real nominal recovery receipt. V2 never
uses `_settle_failure` or its v1 terminal adoption recovery. A verified durable
yield is a normal result; a recovery receipt retains the original exception
and remains visibly interrupted/retry-waiting.

#### Canonical dependencies

- `sessions.continuation.ComposerSegmentRecoveryReceipt` has exact fields
  `session_id`, `operation_id`, `sequence`, `previous_generation`, `generation`,
  `checkpoint_id`, `cursor`, `next_eligible_at`.
- `authority.suspend_segment(running, retry_delay_seconds, reason)` accepts a
  current live owner and retains the actual complete current SQL snapshot.
- `authority.suspend_failed_adoption(running, retry_delay_seconds=1)` accepts
  only the exact original released COMPOSE fence, current immutable attempt
  and same-generation segment; it is not a general relaxed mutation guard.
- `authority.verify_recovery(receipt)` and `verify_yield(receipt)` perform
  actual SQL readback. Merely constructing a receipt never establishes proof.
- `RequiredWorkCoordinator.validate_continuation_sql_outcome` validates only
  the actual registered `CONTINUATION_STATE_SQL`/`CONTINUATION_READ_SQL` carrier;
  their paired projections are sources 56 and 58. The existing finish-once
  producer supplies the actual future and cancellation observations.
- The C4a.3 runner's exact entry is
  `run_composer_continuation_segment(services, running, lease, observation,
  snapshot)` with the keyword-only signature already selected in storage.
  It returns a nominal terminal or yield; it may raise the explicit suspended
  outcome below after its actual owner completes a recovery transaction.

#### W0: Retain the actual COMPOSE lease before constructor task allocation

**Modify:** `src/elspeth/web/coordination/lifecycle.py:397–462,619–718,901,1039,1069,1500`.
The existing EXECUTE construction obligation remains unchanged. This optional
COMPOSE observation list is the actual worker's in-process custody; it carries
live objects and must never enter a checkpoint or HTTP response.

- [ ] Import the existing nominal `ProcessRecovery` from
  `elspeth.web.process_recovery`. Add `_adoption_observations` and
  `_adoption_process_recovery` to `SessionOperationLease.__slots__`. Add
  `adoption_observations: list[SessionOperationLease] | None = None` and
  `adoption_process_recovery: ProcessRecovery | None = None` to the
  keyword-only parameters of both `__init__` and `adopt`. In `__init__`, insert
  the following immediately after `_renewal_task = None` and before the existing
  `execution_obligation.declare_renewal_allocation` / `asyncio.create_task`:

```python
self._adoption_observations = adoption_observations
self._adoption_process_recovery = adoption_process_recovery
if adoption_observations is not None:
    if (type(adoption_observations) is not list or execution_obligation is not None
        or type(adoption_process_recovery) is not ProcessRecovery
        or context.operation_kind is not SessionOperationKind.COMPOSE):
        raise contract_errors.AuditIntegrityError("Adoption construction requires its separate owned COMPOSE observation list")
    if adoption_observations:
        raise contract_errors.AuditIntegrityError("Adoption construction cannot retry an already retained lease")
    adoption_observations.append(self)
elif adoption_process_recovery is not None:
    raise contract_errors.AuditIntegrityError("Adoption recovery lacks its actual construction observation list")
```

- [ ] Add `adoption_observations=adoption_observations` and
  `adoption_process_recovery=adoption_process_recovery` to `adopt`'s final
  `return cls(...)`. Keep its existing CAS task, cancellation cleanup, release
  and original-failure branches byte-for-byte unchanged. Add the following at
  the beginning of `_renew_forever`, before the existing EXECUTE branch:

```python
if self._adoption_observations is not None:
    actual_task = asyncio.current_task()
    if type(actual_task) is not asyncio.Task:
        raise contract_errors.AuditIntegrityError("Adopted renewal lacks its actual Task")
    if self._renewal_task is not None and self._renewal_task is not actual_task:
        raise contract_errors.AuditIntegrityError("Adopted renewal changed its actual producer")
    self._renewal_task = actual_task
```

This actual producer entry resolves allocation-then-raise without retrying task
creation. The observation list retains the lease even if `__init__` never
returns. If allocation never produces an entry, ownership stays unknown: the
worker requests its existing process recovery and retains the lock/list while
waiting. Absence of a Task is never interpreted as successful cleanup.

- [ ] Replace the constructor's final renewal allocation with this exact
  block. The existing process recovery is armed immediately on unknown
  allocation; waiting for a producer entry does not postpone that shutdown.

```python
try:
    self._renewal_task = asyncio.create_task(self._renew_forever(), name="session-operation-renewal")
except BaseException as original:
    if self._adoption_process_recovery is not None:
        try:
            self._adoption_process_recovery.request_shutdown()
        except BaseException as recovery_error:
            raise BaseExceptionGroup("Adopted renewal allocation and recovery failed", [original, recovery_error]) from None
    raise
```

- [ ] In `_stop_and_join_renewal`, change its existing missing-task refusal
  condition to the following; preserve the existing sleep, stop signal and
  actual task join:

```python
if self._execution_obligation is None and self._adoption_observations is None:
    raise contract_errors.AuditIntegrityError("Lease renewal task was not retained")
```

- [ ] At the beginning of `_close`, before the existing EXECUTE branch, add:

```python
if self._adoption_observations is not None:
    actual_task = asyncio.current_task()
    if type(actual_task) is not asyncio.Task:
        raise contract_errors.AuditIntegrityError("Adopted close lacks its actual Task")
    if self._close_task is not None and self._close_task is not actual_task:
        raise contract_errors.AuditIntegrityError("Adopted close changed its actual producer")
    self._close_task = actual_task
```

- [ ] In `close`, replace only the existing `if obligation is None:` task
  allocation arm with these exact two arms. The following EXECUTE `elif not
  obligation.lifecycle_required` arm stays unchanged:

```python
if obligation is None and self._adoption_observations is None:
    self._close_task = asyncio.create_task(self._close(), name="session-operation-close")
elif obligation is None:
    try:
        self._close_task = asyncio.create_task(self._close(), name="session-operation-close")
    except BaseException as original:
        allocation_failures.append(original)
        if self._adoption_process_recovery is not None:
            try:
                self._adoption_process_recovery.request_shutdown()
            except BaseException as recovery_error:
                allocation_failures.append(recovery_error)
    while self._close_task is None:
        try:
            await asyncio.sleep(0.01)
        except asyncio.CancelledError as original:
            _retain_cancellation(cancellations, original)
```

- [ ] Replace only `close`'s later `if obligation is None:` join/return arm
  with the following. It observes this exact retained close Task and preserves
  actual allocation/caller errors. The existing EXECUTE remainder stays intact:

```python
if obligation is None:
    if self._adoption_observations is None:
        await self._join_finish_task(close_task)
        return
    try:
        await _join_shielded_task_after_cancellation(close_task, cancellation_observations=cancellations)
    except BaseException as original:
        allocation_failures.append(original)
    _raise_lifecycle_originals(cancellations, allocation_failures)
    return
```

#### W1: Define the exceptional recovery outcome and worker completion custody

**Modify:** `src/elspeth/web/sessions/composer_continuation_turn.py` and
`src/elspeth/web/sessions/composer_async_worker.py`.

- [ ] Add this complete exception next to the new runner's imports.

```python
from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt


class ComposerContinuationSuspended(Exception):
    def __init__(self, receipt: ComposerSegmentRecoveryReceipt, original: BaseException) -> None:
        if type(receipt) is not ComposerSegmentRecoveryReceipt or not isinstance(original, BaseException):
            raise TypeError("suspension requires its actual recovery receipt and original failure")
        super().__init__("Composer continuation awaits its actual owner recovery")
        self.receipt = receipt
        self.original = original
```

The exception object is in-process worker custody only. Do not serialize its
`original`, traceback, live lease, SQL future or Tasks. SQL persists the closed
recovery reason and complete current frame/frontier, not Python exceptions.

- [ ] Add these exact imports to `composer_async_worker.py`, combining the
  disposition into its existing contracts import. The finish-once carrier,
  coordinator, ticket, exception and dataclass imports already exist there.

```python
import sys

from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.sessions.composer_continuation_turn import ComposerContinuationSuspended
from elspeth.web.sessions.continuation import (
    ComposerSegmentRecoveryReceipt,
    ComposerSegmentYieldReceipt,
    StoredContinuationSnapshot,
)
```

- [ ] Add this nominal completion dataclass next to `_AdoptionSettlementHandoff`.

```python
@dataclass(frozen=True, slots=True)
class _ContinuationJobOutcome:
    receipt: ComposerOperationRecord | ComposerSegmentYieldReceipt | ComposerSegmentRecoveryReceipt
    cancellations: tuple[asyncio.CancelledError, ...]
    original: BaseException | None
```

- [ ] Add these fields to the existing worker constructor; retain all of the
  current adoption/required-work/recovery maps and process recovery owner.

```python
self._continuation_claims: dict[tuple[UUID, str], ComposerOperationClaim] = {}
self._continuation_outcomes: dict[tuple[UUID, str], _ContinuationJobOutcome] = {}
self._continuation_start_attempted: set[tuple[UUID, str]] = set()
self._continuation_adoptions: dict[tuple[UUID, str], list[SessionOperationLease]] = {}
self._continuation_unstarted_released: set[tuple[UUID, str]] = set()
```

- [ ] Immediately before `_claim_once` allocates each existing `_job` Task,
  retain its nominal v2 claim. Keep the existing shared worker capacity and
  mixed v1/v2 selection; add no process, test or scheduler reservation.

```python
if claim.segment_sequence is not None:
    self._continuation_claims[key] = claim
```

#### W2: Concrete registered SQL/readback methods on the existing worker

Every method below uses the actual finish-once carrier and its registered
projection. `reserve_pair` itself assigns recurrence ordinals, so its keys
cannot collide with an earlier physical read under the same logical ordinal.
These are domain methods on the existing worker, not a replacement runner or
an injectable callback protocol.

- [ ] Add this exact authenticated stored-parent read for lifecycle custody.

```python
async def _read_continuation_parent(
    self, running: ComposerOperationRunning, coordinator: RequiredWorkCoordinator,
    cancellations: list[asyncio.CancelledError],
) -> ComposerOperationRecord:
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION,
        transition_ordinal=0, semantic_ordinal=0,
    )
    outcome = await run_required_sql_finish_once(
        sql, self._authority.get_for_turn,
        session_id=running.claim.session_id, operation_id=running.claim.operation_id,
    )
    coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome,
    )
    for cancellation in outcome.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not ComposerOperationRecord:
        invalid = AuditIntegrityError("Continuation lifecycle has no nominal stored parent")
        projection.complete_owned(invalid)
        raise invalid
    record = outcome.value
    fence = running.session_operation_context.fence
    if (record.lifecycle_version != 2 or record.session_id != running.claim.session_id
        or record.operation_id != running.claim.operation_id or record.attempt != running.claim.attempt
        or record.session_operation_id != fence.operation_id or record.session_operation_epoch != fence.operation_epoch):
        invalid = AuditIntegrityError("Continuation lifecycle parent differs from its exact running owner")
        projection.complete_owned(invalid)
        raise invalid
    projection.complete_owned()
    return record
```

- [ ] Add this exact private snapshot read method to `ComposerAsyncWorker`.

```python
async def _read_continuation_snapshot(
    self, running: ComposerOperationRunning, coordinator: RequiredWorkCoordinator,
    cancellations: list[asyncio.CancelledError],
) -> StoredContinuationSnapshot:
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION,
        transition_ordinal=0, semantic_ordinal=0,
    )
    outcome = await run_required_sql_finish_once(sql, self._authority.load_continuation_snapshot, running)
    coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome,
    )
    for cancellation in outcome.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not StoredContinuationSnapshot:
        invalid = AuditIntegrityError("Continuation private read has no nominal actual SQL result")
        projection.complete_owned(invalid)
        raise invalid
    projection.complete_owned()
    return outcome.value
```

- [ ] Add this complete nominal yield/recovery readback method. Terminal
  readback continues through the unchanged `_verify_terminal_before_release`.

```python
async def _verify_continuation_receipt(
    self, receipt: ComposerSegmentYieldReceipt | ComposerSegmentRecoveryReceipt,
    coordinator: RequiredWorkCoordinator, cancellations: list[asyncio.CancelledError],
) -> None:
    if type(receipt) not in (ComposerSegmentYieldReceipt, ComposerSegmentRecoveryReceipt):
        raise TypeError("Continuation readback requires its nominal durable receipt")
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.CONTINUATION_READ_SQL, RequiredWorkSource.CONTINUATION_READ_PROJECTION,
        transition_ordinal=0, semantic_ordinal=1,
    )
    if type(receipt) is ComposerSegmentYieldReceipt:
        outcome = await run_required_sql_finish_once(sql, self._authority.verify_yield, receipt)
    else:
        outcome = await run_required_sql_finish_once(sql, self._authority.verify_recovery, receipt)
    coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_READ_SQL, actual_outcome=outcome,
    )
    for cancellation in outcome.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned or outcome.value is not None:
        invalid = AuditIntegrityError("Continuation readback returned a foreign SQL value")
        projection.complete_owned(invalid)
        raise invalid
    projection.complete_owned()
```

- [ ] Add this exact quiescence method. Stop/join the existing renewal producer
  before testing the cohort; observing one renewal attempt while allowing the
  next tick to start would leave a race against `assert_completed`.

```python
async def _quiesce_continuation_lease(
    self, lease: SessionOperationLease, coordinator: RequiredWorkCoordinator,
    cancellations: list[asyncio.CancelledError],
    original: BaseException | None = None,
) -> tuple[BaseException, ...]:
    errors: tuple[BaseException, ...] = ()
    try:
        errors = await _owned(lease._join_owned_tasks(), cancellation_observations=cancellations)
        await _owned(lease._stop_and_join_renewal(), cancellation_observations=cancellations)
        coordinator.assert_completed()
    except BaseException as cleanup:
        retained = _combine_operation_failures(cleanup, errors + tuple(cancellations))
        if original is not None:
            retained = _combine_operation_failures(original, (retained,))
        raise retained from retained.__cause__
    return errors
```

These existing methods join only the lease's actual retained child/renewal
Tasks. They never cancel an unowned process or infer producer completion from
Task absence. Required work that is still incomplete prevents recovery writes
and process-lock release; its actual owner remains discoverable under shutdown.

#### W3: Failed-adoption cleanup through the exact released-owner SQL path

- [ ] Add this complete method to the existing worker. The fresh recovery
  coordinator is the same pattern as the existing `_recovery_coordinators`
  custody. Its three operations are registered actual SQL work. The first
  operation is exact original-fence release: already-released is accepted only
  from the authority's nominal RELEASED refusal and is re-proved by the next
  atomic failed-adoption guard. No replacement fence is accepted.

```python
async def _recover_continuation_adoption(
    self, services: ComposerAppServices, running: ComposerOperationRunning,
    original_work: RequiredWorkCoordinator, original: BaseException,
    cancellations: list[asyncio.CancelledError],
) -> ComposerSegmentRecoveryReceipt:
    original_work.assert_completed()
    key = (running.claim.session_id, running.claim.operation_id)
    recovery = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context,
        running.claim.operation_id, running.claim.attempt,
    ))
    self._recovery_coordinators.setdefault(key, []).append(recovery)
    release_sql, release_projection = recovery.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION,
        transition_ordinal=0, semantic_ordinal=0,
    )
    released = await run_required_sql_finish_once(
        release_sql, services.session_service.session_operation_authority.release,
        running.session_operation_context,
    )
    recovery.validate_continuation_sql_outcome(
        ticket=release_sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=released,
    )
    for cancellation in released.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    release_projection.begin_projection()
    if type(released) is RequiredSQLRaised:
        if not isinstance(released.error, SessionOperationFenceLost) or released.error.reason is not FenceLossReason.RELEASED:
            release_projection.complete_owned(released.error)
            raise _combine_operation_failures(original, (released.error,))
    elif type(released) is not RequiredSQLReturned or released.value is not None:
        invalid = AuditIntegrityError("Failed-adoption release has no actual known SQL outcome")
        release_projection.complete_owned(invalid)
        raise _combine_operation_failures(original, (invalid,))
    release_projection.complete_owned()
    sql, projection = recovery.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION,
        transition_ordinal=0, semantic_ordinal=1,
    )
    outcome = await run_required_sql_finish_once(
        sql, self._authority.suspend_failed_adoption, running,
    )
    recovery.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome,
    )
    for cancellation in outcome.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise _combine_operation_failures(original, (outcome.error,))
    if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not ComposerSegmentRecoveryReceipt:
        invalid = AuditIntegrityError("Failed adoption returned without its nominal recovery receipt")
        projection.complete_owned(invalid)
        raise _combine_operation_failures(original, (invalid,))
    receipt = outcome.value
    claim = running.claim
    if (receipt.session_id, receipt.operation_id, receipt.sequence, receipt.previous_generation, receipt.generation) != (
        claim.session_id, claim.operation_id, claim.segment_sequence, claim.ownership_generation,
        claim.ownership_generation + 1 if claim.ownership_generation is not None else None,
    ):
        invalid = AuditIntegrityError("Failed-adoption recovery changed its actual segment identity")
        projection.complete_owned(invalid)
        raise _combine_operation_failures(original, (invalid,))
    projection.complete_owned()
    await self._verify_continuation_receipt(receipt, recovery, cancellations)
    recovery.assert_completed()
    return receipt
```

`CONTINUATION_STATE_SQL` here tracks the actual continuation recovery fence
transition and suspension, on the fresh recovery cohort. It does not call the
lease's `prepare_lease_release` barrier and then register more work behind that
barrier. The original adoption coordinator is already complete and is retained
separately; it cannot be replaced with a freshly green empty coordinator.

#### W4: Join the actual started cohort and suspend only after quiescence

**Modify:** `src/elspeth/web/composer_watch_reads.py` and the existing worker
`_watch`; modify `src/elspeth/web/sessions/composer_turn.py`'s existing
`ComposerTurnObservation`. These edits retain the current observation reader,
no-submission provenance, loss child, cancellation handling, heartbeat and
progress lease. They do not replace those owners with a new polling loop.

- [ ] Add `request_lifecycle: ComposerRequestLifecycle | None = None` to
  `ComposerTurnObservation`. That module already imports the actual class.
  C4a.3 must claim its progress sink from this exact
  `observation.request_lifecycle.lease` via existing `composer_progress_sink_for_lease`;
  it must not open a second request lifecycle.

- [ ] In `ComposerOperationWatchReader.__init__` and `for_operation`, change
  `anchor: ComposerBudgetAnchor` to `anchor: ComposerBudgetAnchor | None`.
  Replace only `__init__`'s current deadline calculation/validation with:

```python
self.deadline = None if anchor is None else anchor.monotonic_at_running + anchor.remaining_at_running_seconds
if anchor is not None and (
    self.deadline is None or not math.isfinite(self.deadline)
    or not math.isfinite(anchor.remaining_at_running_seconds)
    or anchor.remaining_at_running_seconds <= 0
):
    raise AuditIntegrityError("Operation observation has invalid original budget")
```

- [ ] Replace `for_operation`'s current nominal condition and refusal with
  this complete condition. Keep its existing local imports and return:

```python
if (
    type(worker) is not ComposerAsyncWorker
    or type(running) is not ComposerOperationRunning
    or type(lease) is not SessionOperationLease
    or (anchor is not None and type(anchor) is not ComposerBudgetAnchor)
    or ((anchor is None) != (running.claim.segment_sequence is not None))
    or lease.context != running.session_operation_context
):
    raise AuditIntegrityError("Operation observation lacks exact owned running authority")
```

- [ ] In `check_cutoff`, retain all existing lease/closed/shutdown checks.
  Replace only its remaining-time/deadline block with:

```python
if self.deadline is not None:
    if self.anchor is None:
        raise AuditIntegrityError("Legacy operation observation lost its budget anchor")
    remaining = self.deadline - time.monotonic()
    if remaining <= 0:
        raise ComposerTurnDeadlineExpired(
            session_id=self.running.claim.session_id, operation_id=self.running.claim.operation_id,
            remaining_seconds=max(0.0, remaining),
            budget_seconds_at_running=self.anchor.remaining_at_running_seconds,
        )
```

- [ ] Change `_watch`'s anchor annotation to the same optional nominal type.
  Replace only its lines 946–953 parent-deadline block with:

```python
if anchor is not None:
    if current.deadline_at is None:
        raise AuditIntegrityError("Legacy operation lost its fixed parent deadline")
    remaining = anchor.remaining_seconds(monotonic_now=time.monotonic())
    if restore_utc(current.legacy_deadline_at) <= now or remaining <= 0:
        raise ComposerTurnDeadlineExpired(
            session_id=running.claim.session_id, operation_id=running.claim.operation_id,
            remaining_seconds=remaining, budget_seconds_at_running=anchor.remaining_at_running_seconds,
        )
```

- [ ] Replace the existing durable-Stop branch immediately before that
  deadline block with this complete branch:

```python
if current.cancel_requested_at is not None and anchor is not None:
    owner.cancel(COMPOSER_CANCEL_REQUESTED)
    return
```

Legacy owners keep their existing delivered Stop marker. A v2 watcher keeps
reading the actual operation and observing shutdown, lease loss, and physical
SQL failures after durable Stop; it does not cancel the private drain on every
fresh attempt. F28/F32 read the actual SQL Stop flag at the selected boundary,
B9 refuses an actually uncommitted prepared action, and B6/O9 permits only its
proved owed P4 under that flag. The post-prepare boundary check refuses fresh
business dispatch if Stop arrived during preparation. Ordinary business SQL
retains its same-transaction cancellation guard, covering Stop after that
read. Bounded provider/stage owners retain and join already dispatched work.
Actual external Task cancellation, shutdown and fence loss keep their existing
original exceptions and recovery paths; none is reclassified as durable Stop.

No other `_watch` branch changes. Its current retained segment clock and
cooperative boundary belong to C4a.3, never to the legacy parent deadline.
Queued canceled v2 work still acquires its cleanup-only fence; the runner
reconciles pending owners then uses the terminal owner without provider
dispatch. B9/O9 full-driver controls prove that this drain terminates.

- [ ] Add this per-purpose live suspension method to the existing worker.

```python
async def _suspend_continuation_owner(
    self, running: ComposerOperationRunning, coordinator: RequiredWorkCoordinator,
    cancellations: list[asyncio.CancelledError],
    *, retry_delay_seconds: float | None = None, reason: str = "required_work_failed",
) -> ComposerSegmentRecoveryReceipt:
    coordinator.assert_completed()
    sql, projection = coordinator.reserve_pair(
        RequiredWorkSource.CONTINUATION_STATE_SQL, RequiredWorkSource.CONTINUATION_STATE_PROJECTION,
        transition_ordinal=0, semantic_ordinal=2,
    )
    outcome = await run_required_sql_finish_once(
        sql, self._authority.suspend_segment, running,
        retry_delay_seconds=retry_delay_seconds, reason=reason,
    )
    coordinator.validate_continuation_sql_outcome(
        ticket=sql, expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome,
    )
    for cancellation in outcome.deferred_cancellations:
        self._retain_recovery_cancellations(cancellations, cancellation)
    projection.begin_projection()
    if type(outcome) is RequiredSQLRaised:
        projection.complete_owned(outcome.error)
        raise outcome.error
    if type(outcome) is not RequiredSQLReturned or type(outcome.value) is not ComposerSegmentRecoveryReceipt:
        invalid = AuditIntegrityError("Live interruption returned without its nominal recovery receipt")
        projection.complete_owned(invalid)
        raise invalid
    receipt = outcome.value
    claim = running.claim
    if (receipt.session_id, receipt.operation_id, receipt.sequence, receipt.previous_generation) != (
        claim.session_id, claim.operation_id, claim.segment_sequence, claim.ownership_generation,
    ) or receipt.generation != receipt.previous_generation + 1:
        invalid = AuditIntegrityError("Live interruption changed the original segment identity")
        projection.complete_owned(invalid)
        raise invalid
    projection.complete_owned()
    await self._verify_continuation_receipt(receipt, coordinator, cancellations)
    return receipt
```

- [ ] Add this complete started-owner method. It calls the core runner; it
  does not reconstruct a pipeline, hide a failed action or rerun provider work.
  The worker's own setup/continuation producers complete from their actual
  returns/errors before the required cohort can be considered quiescent.

```python
async def _run_continuation_started(
    self, services: ComposerAppServices, running: ComposerOperationRunning,
    lease: SessionOperationLease, cancellations: list[asyncio.CancelledError],
) -> None:
    from elspeth.web.sessions.composer_continuation_turn import run_composer_continuation_segment
    from elspeth.web.composer.continuation_driver import ContinuationProviderWait
    from elspeth.web.composer.continuation_progress import ContinuationProtocolError
    from elspeth.web.sessions.composer_continuation_stop import ComposerContinuationStopReady

    if type(lease) is not SessionOperationLease:
        raise AuditIntegrityError("Continuation requires its actual nominal lease")
    coordinator = lease.required_work
    if type(coordinator) is not RequiredWorkCoordinator:
        raise AuditIntegrityError("Continuation requires its actual nominal lease and required-work cohort")
    key = (running.claim.session_id, running.claim.operation_id)
    setup = coordinator.reserve(RequiredWorkSource.OWNED_TURN_SETUP_PRODUCER)
    turn: RequiredWorkTicket | None = None
    verified: ComposerOperationRecord | ComposerSegmentYieldReceipt | ComposerSegmentRecoveryReceipt | None = None
    durable_returned = False
    try:
        async with lease:
            try:
                record = await self._read_continuation_parent(running, coordinator, cancellations)
                snapshot = await self._read_continuation_snapshot(running, coordinator, cancellations)
                if cancellations:
                    if len(cancellations) == 1:
                        raise cancellations[0]
                    raise BaseExceptionGroup("Continuation setup retained original cancellations", list(cancellations))
                setup.complete_owned()
                turn = coordinator.reserve(RequiredWorkSource.REQUIRED_CONTINUATION_PRODUCER, semantic_ordinal=1)
                owner = asyncio.current_task()
                if owner is None:
                    raise AuditIntegrityError("Continuation requires its actual worker Task")
                try:
                    async with composer_request_lifecycle(
                        services.progress_registry, session_id=str(running.claim.session_id),
                        user_id=record.actor_user_id,
                        owner_task=owner,
                    ) as lifecycle:
                        observation = ComposerTurnObservation(required_work=coordinator, request_lifecycle=lifecycle)
                        watch_observation = _OperationWatchObservation()
                        watcher = asyncio.create_task(
                            self._watch(running, lease, owner, None, watch_observation), name="composer-operation-watch",
                        )
                        primary: BaseException | None = None
                        try:
                            outcome = await run_composer_continuation_segment(
                                services=services, running=running, lease=lease,
                                observation=observation, snapshot=snapshot,
                            )
                            if type(outcome) not in (ComposerOperationRecord, ComposerSegmentYieldReceipt):
                                raise AuditIntegrityError("Continuation returned without its nominal SQL outcome")
                            if (type(observation.continuation_outcome_failures) is not tuple
                                    or any(not isinstance(error, BaseException) for error in observation.continuation_outcome_failures)):
                                raise AuditIntegrityError("Continuation returned an invalid actual-outcome failure observation")
                            durable_returned = True
                            if type(outcome) is ComposerOperationRecord:
                                lifecycle.durable_completed = outcome.status == "completed"
                                lifecycle.durable_terminal_status = (
                                    "completed" if outcome.status == "completed"
                                    else "cancelled" if outcome.failure_code == "request_cancelled" else "failed"
                                )
                        except BaseException as original:
                            primary = original
                            raise
                        finally:
                            failures = await _finish_operation_watcher(watcher, watch_observation)
                            if failures:
                                if primary is not None:
                                    raise _combine_operation_failures(primary, failures)
                                if len(failures) == 1:
                                    raise failures[0]
                                raise BaseExceptionGroup("Continuation watcher retained actual originals", list(failures))
                except BaseException as original:
                    turn.complete_owned(original)
                    raise
                turn.complete_owned()
                child_errors = await self._quiesce_continuation_lease(lease, coordinator, cancellations)
                if type(outcome) is ComposerSegmentYieldReceipt:
                    await self._verify_continuation_receipt(outcome, coordinator, cancellations)
                    verified = outcome
                elif type(outcome) is ComposerOperationRecord:
                    settlement = _StartedSettlement(terminal_receipt=outcome)
                    try:
                        await self._verify_terminal_before_release(running, lease, outcome, settlement)
                    finally:
                        verified = settlement.verified_terminal
                else:
                    raise AuditIntegrityError("Continuation returned without a nominal durable outcome")
                if child_errors:
                    raise BaseExceptionGroup("Continuation retained actual child failures", list(child_errors))
                if cancellations:
                    if len(cancellations) == 1:
                        raise cancellations[0]
                    raise BaseExceptionGroup("Continuation retained original cancellations", list(cancellations))
                if observation.continuation_outcome_failures:
                    raise _combine_operation_failures(observation.continuation_outcome_failures[0],
                        observation.continuation_outcome_failures[1:])
            except ComposerContinuationSuspended as suspended:
                if not setup.complete:
                    setup.complete_owned(suspended.original)
                if turn is not None and not turn.complete:
                    turn.complete_owned(suspended.original)
                child_errors = await self._quiesce_continuation_lease(
                    lease, coordinator, cancellations, original=suspended.original,
                )
                try:
                    await self._verify_continuation_receipt(suspended.receipt, coordinator, cancellations)
                except BaseException as readback:
                    raise _combine_operation_failures(suspended.original, (readback, *child_errors, *cancellations))
                verified = suspended.receipt
                original = _combine_operation_failures(suspended.original, child_errors + tuple(cancellations))
                self._retain_recovery_cancellations(cancellations, original)
                raise ComposerContinuationSuspended(suspended.receipt, original) from original
            except BaseException as original:
                if not setup.complete:
                    setup.complete_owned(original)
                if turn is not None and not turn.complete:
                    turn.complete_owned(original)
                if verified is not None or durable_returned or _terminal_completion_unknown(original):
                    # A returned SQL outcome cannot be rewritten after failed readback.
                    raise
                child_errors = await self._quiesce_continuation_lease(
                    lease, coordinator, cancellations, original=original,
                )
                retained = _combine_operation_failures(original, child_errors + tuple(cancellations))
                self._retain_recovery_cancellations(cancellations, retained)
                if any(isinstance(leaf, ContinuationProtocolError) or type(leaf) is ComposerContinuationStopReady
                       for leaf in _failure_leaves(original)):
                    terminal = await self._settle_failure(
                        services, running, retained, cancellation_observations=cancellations,
                    )
                    settlement = _StartedSettlement(terminal_receipt=terminal)
                    try:
                        await self._verify_terminal_before_release(running, lease, terminal, settlement)
                    finally:
                        verified = settlement.verified_terminal
                    raise retained from retained.__cause__
                try:
                    if type(original) is ContinuationProviderWait and not child_errors:
                        receipt = await self._suspend_continuation_owner(
                            running, coordinator, cancellations,
                            reason=original.reason,
                        )
                    else:
                        receipt = await self._suspend_continuation_owner(running, coordinator, cancellations)
                except BaseException as recovery:
                    raise _combine_operation_failures(retained, (recovery, *cancellations))
                verified = receipt
                raise ComposerContinuationSuspended(receipt, retained) from retained
    except BaseException as escaping:
        if not lease.closed:
            try:
                await lease.close()
            except BaseException as cleanup:
                escaping = _combine_operation_failures(escaping, (cleanup,))
        self._retain_recovery_cancellations(cancellations, escaping)
        if (
            verified is not None and lease.closed
            and lease.disposition is SessionOperationLeaseDisposition.RELEASED
            and coordinator.all_completed
        ):
            self._continuation_outcomes[key] = _ContinuationJobOutcome(verified, tuple(cancellations), escaping)
        raise escaping from escaping.__cause__
    else:
        if verified is None or not lease.closed or lease.disposition is not SessionOperationLeaseDisposition.RELEASED:
            raise AuditIntegrityError("Continuation owner ended without a proved durable outcome and actual lease release")
        coordinator.assert_completed()
        self._continuation_outcomes[key] = _ContinuationJobOutcome(verified, tuple(cancellations), None)
```

The exception branches have deliberately different meaning. A runner's
already committed suspension is read back, not committed again. An ordinary
failure is suspended only after the actual child/renewal cohort joins. A
post-verification late error is propagated without trying to turn a yielded
or terminal owner into another generation. An actual protocol refusal is
terminal once through the existing failure owner. A closed provider wait
preserves its effect and uses its explicit reconciliation delay; it never
replays the SDK because a response was not found.

#### W4.1: Project finite protocol failures without inventing a logical deadline

**Modify:** `src/elspeth/web/sessions/composer_async_worker.py` and
`src/elspeth/web/required_work.py`; extend
`tests/unit/web/sessions/test_continuation_worker_custody.py`.

- [ ] Change the `timeout_seconds` parameter annotation from `float` to
  `float | None` in the existing `ComposerFailureReduction.project`,
  `_project_composer_failure_witnesses` and
  `project_composer_failure_witnesses`. Keep their other arguments and
  bodies. Replace only the existing rank-70 branch with this literal block.
  V1 continues supplying its exact original timeout. Other failure categories
  never consume a deadline argument.

```python
if category_rank == 70:
    if timeout_seconds is None:
        raise AuditIntegrityError("A continuation failure cannot acquire a whole-request deadline")
    return deadline_expired_error(request_id=request_id, timeout_seconds=timeout_seconds)
```

- [ ] At the beginning of the existing worker `_failure_for`, before its
  legacy deadline reads, add this branch. Add the complete member below;
  C3.1's legacy-only property substitutions still apply only after this
  branch. Receipt priority and actual Stop markers retain their existing
  meaning. No elapsed wall time becomes a logical-request limit.

```python
if current.lifecycle_version == 2:
    return self._continuation_failure_for(current, exc, recovery_receipts=recovery_receipts)
```

```python
def _continuation_failure_for(
    self, current: ComposerOperationRecord, exc: BaseException, *,
    recovery_receipts: tuple[ComposerFailureReceipt, ...] | None,
) -> ComposerOperationError:
    if type(current) is not ComposerOperationRecord or current.lifecycle_version != 2:
        raise AuditIntegrityError("Continuation failure requires its actual v2 parent")
    diagnostic_id = operation_failure_diagnostic_id(
        session_id=str(current.session_id), operation_id=current.operation_id,
    )
    coordinator = self._required_coordinators.get((current.session_id, current.operation_id))
    receipts = (
        coordinator.settlement_failure_receipts()
        if recovery_receipts is None and coordinator is not None else recovery_receipts or ()
    )
    reduction = reduce_composer_failures(receipts) if receipts else None
    if reduction is not None and reduction.category_rank <= 51:
        return reduction.project(request_id=current.request_id, timeout_seconds=None)
    if _authoritative_failure(exc):
        return project_composer_operation_error(exc, request_id=current.request_id, diagnostic_id=diagnostic_id)
    if current.cancel_requested_at is not None:
        return request_cancelled_error(request_id=current.request_id)
    if any(isinstance(leaf, ComposerTurnDeadlineExpired) for leaf in _failure_leaves(exc)):
        return project_composer_operation_error(
            AuditIntegrityError("Legacy whole-request deadline entered a continuation owner"),
            request_id=current.request_id, diagnostic_id=diagnostic_id,
        )
    if reduction is not None:
        return reduction.project(request_id=current.request_id, timeout_seconds=None)
    return project_composer_operation_error(exc, request_id=current.request_id, diagnostic_id=diagnostic_id)
```

- [ ] Add the complete worker-boundary test below after W6's actual helper
  definitions. This substitutes only the core runner's exceptional boundary;
  actual SQL, lease, required child, renewal join, terminal writer, readback,
  process lock and worker completion remain real. It does not claim driver
  equivalence or a provider-usage outcome from a fake SDK.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ("protocol", "owner_unavailable", "unknown_effect"))
async def test_protocol_is_terminal_once_and_provider_wait_keeps_exact_recovery_reason(tmp_path, monkeypatch, kind):
    import json
    from elspeth.web.composer.continuation_driver import ContinuationProviderWait
    from elspeth.web.composer.continuation_progress import ContinuationProtocolError
    from elspeth.web.sessions.composer_operations import ComposerOperationRecord
    from elspeth.web.sessions.models import (
        composer_continuation_chunks_table as continuation_chunks_table,
        composer_continuation_events_table as continuation_events_table,
    )
    from elspeth.web.sessions.time_normalization import restore_utc

    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = ContinuationProtocolError("Current authoring contract requires reinspection") if kind == "protocol" else ContinuationProviderWait(kind)
    children = []

    async def fail_after_actual_child(*, services, running, lease, observation, snapshot):
        ticket = lease.required_work.reserve(
            RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER, transition_ordinal=8, semantic_ordinal=3,
        )
        async def child():
            try:
                entered.set()
                await release.wait()
            except BaseException as failure:
                ticket.complete_owned(failure)
                raise
            else:
                ticket.complete_owned()
        children.append(lease.create_task(child(), name="test-protocol-required-child"))
        await entered.wait()
        raise original

    monkeypatch.setattr(composer_continuation_turn, "run_composer_continuation_segment", fail_after_actual_child)
    lock = await composer_session_lock_registry(app).get_lock(str(record.session_id))
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0)
        assert lock.locked() and not owner.done() and not children[0].done()
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current.status == "running" and current.settled_at is None
        release.set()
        failure = await actual_failure(owner)
        assert any(item is original for item in failure_objects(failure))
        proof = worker._continuation_outcomes[key]
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        if kind == "protocol":
            assert type(proof.receipt) is ComposerOperationRecord
            assert current == proof.receipt and current.status == "failed"
            assert current.failure_code == "operation_failed" and current.settled_at is not None
            assert current.deadline_at is None and current.phase == "settled"
            assert authority.claim_segment(limit=1) == ()
            with engine.connect() as conn:
                historical = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).all()
            assert len(historical) == 1 and historical[0].outcome == "settled"
        else:
            assert type(proof.receipt) is ComposerSegmentRecoveryReceipt
            authority.verify_recovery(proof.receipt)
            with engine.connect() as conn:
                segment = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).one()
                event = conn.execute(select(continuation_chunks_table.c.payload).join(
                    continuation_events_table,
                    (continuation_events_table.c.session_id == continuation_chunks_table.c.session_id)
                    & (continuation_events_table.c.operation_id == continuation_chunks_table.c.operation_id)
                    & (continuation_events_table.c.payload_chunk_key == continuation_chunks_table.c.chunk_key),
                ).where(
                    continuation_events_table.c.session_id == str(record.session_id),
                    continuation_events_table.c.operation_id == record.operation_id,
                    continuation_events_table.c.tag == "recovered",
                )).one()
            assert json.loads(bytes(event.payload).decode("utf-8"))["reason"] == kind
            delay = (restore_utc(segment.next_eligible_at) - restore_utc(segment.enqueued_at)).total_seconds()
            assert delay == 1.0  # First actual interruption of this sequence.
            assert current.status == "running" and current.failure_code is None
        assert children[0].done() and not lock.locked() and composer.calls == 0
        worker._required_coordinators[key].assert_completed()
        finish_callback(worker, key, owner)
    finally:
        release.set()
        if owner is not None and not owner.done():
            await actual_failure(owner)
        await worker._process_recovery.join_escalation()
        engine.dispose()
```

- [ ] Run W7's exact worker-custody command after the test is added. A control
  replacing only the protocol branch with the previous generic suspension
  must fail the `current.status == "failed"` assertion; restoring that branch
  must pass. No retry-loop count or shorter sleep substitutes for the SQL
  terminal/claim assertions.

#### W5: Replace the incomplete `_continuation_job` adoption gap

- [ ] Replace C4a.2's proposed new method with this complete version. Keep
  the real v1 `_job` body below its initial continuation branch unchanged.

```python
async def _continuation_job(self, claim: ComposerOperationClaim) -> None:
    services = composer_app_services(self._app)
    key = (claim.session_id, claim.operation_id)
    cancellations = self._adoption_recovery_cancellations.setdefault(key, [])
    adoptions = self._continuation_adoptions.setdefault(key, [])
    lock = await services.compose_locks.get_lock(str(claim.session_id))
    acquired = False
    started_handoff = False
    running: ComposerOperationRunning | None = None
    coordinator: RequiredWorkCoordinator | None = None
    lease: SessionOperationLease | None = None
    released_before_start = False
    release_attempted = False
    try:
        while not acquired:
            if self._stopping or self._instance_draining.is_set():
                release_attempted = True
                await _owned(run_sync_in_worker(self._authority.release_claim, claim), cancellation_observations=cancellations)
                released_before_start = True
                self._continuation_unstarted_released.add(key)
                if cancellations:
                    raise cancellations[0] if len(cancellations) == 1 else BaseExceptionGroup(
                        "Continuation unstarted claim retained cancellations", list(cancellations)
                    )
                return
            try:
                await asyncio.wait_for(lock.acquire(), timeout=min(0.25, self._claim_lease_seconds / 3))
                acquired = True
            except TimeoutError:
                await _owned(run_sync_in_worker(self._authority.renew_claim, claim), cancellation_observations=cancellations)
        self._continuation_start_attempted.add(key)
        context = await _owned(run_sync_in_worker(
            services.session_service.session_operation_authority.start_composer_segment,
            claim, owner_instance_id=self._owner_instance_id,
            lease_seconds=services.session_service.session_operation_lease_seconds,
            auth_provider_type=services.settings.auth_provider,
        ), cancellation_observations=cancellations)
        running = ComposerOperationRunning(claim=claim, session_operation_context=context)
        coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
            RequiredAuthorityKind.DURABLE_COMPOSE, context, claim.operation_id, claim.attempt,
        ))
        self._required_coordinators[key] = coordinator
        if cancellations:
            raise cancellations[0] if len(cancellations) == 1 else BaseExceptionGroup(
                "Continuation start retained original cancellations", list(cancellations)
            )
        if self._stopping or self._instance_draining.is_set():
            raise asyncio.CancelledError(COMPOSER_SHUTDOWN)
        lease = await SessionOperationLease.adopt(
            services.session_service.session_operation_authority, context,
            lease_seconds=services.session_service.session_operation_lease_seconds,
            required_work=coordinator, cancellation_observations=cancellations,
            adoption_observations=adoptions, adoption_process_recovery=self._process_recovery,
        )
        if type(lease) is not SessionOperationLease:
            raise AuditIntegrityError("Continuation adoption returned a foreign lease owner")
        started_handoff = True
        await self._run_continuation_started(services, running, lease, cancellations)
    except BaseException as original:
        self._retain_recovery_cancellations(cancellations, original)
        if adoptions:
            if (len(adoptions) != 1 or type(adoptions[0]) is not SessionOperationLease
                or running is None or adoptions[0].context != running.session_operation_context
                or (lease is not None and lease is not adoptions[0])):
                raise _combine_operation_failures(original, (AuditIntegrityError("Continuation adoption changed its retained lease"),))
            lease = adoptions[0]
            if not lease.closed:
                try:
                    await lease.close()
                except BaseException as cleanup:
                    original = _combine_operation_failures(original, (cleanup,))
                    self._retain_recovery_cancellations(cancellations, cleanup)
            if not lease.closed or lease.disposition is not SessionOperationLeaseDisposition.RELEASED:
                raise original from original.__cause__
        if running is not None and not started_handoff:
            self._begin_adoption_failure_custody(running, original)
            if coordinator is None:
                raise AuditIntegrityError("Started continuation lost its actual required-work coordinator") from original
            try:
                receipt = await self._recover_continuation_adoption(services, running, coordinator, original, cancellations)
            except BaseException as recovery:
                retained = _combine_operation_failures(original, (recovery, *cancellations))
                self._retain_recovery_cancellations(cancellations, retained)
                raise retained from retained.__cause__
            retained = _combine_operation_failures(original, tuple(cancellations))
            suspended = ComposerContinuationSuspended(receipt, retained)
            self._continuation_outcomes[key] = _ContinuationJobOutcome(receipt, tuple(cancellations), suspended)
            raise suspended from retained
        if running is None and key not in self._continuation_start_attempted and not release_attempted:
            release_attempted = True
            try:
                await _owned(run_sync_in_worker(self._authority.release_claim, claim), cancellation_observations=cancellations)
            except BaseException as release:
                raise _combine_operation_failures(original, (release, *cancellations))
            released_before_start = True
            self._continuation_unstarted_released.add(key)
        retained = _combine_operation_failures(original, tuple(cancellations))
        proof = self._continuation_outcomes.get(key)
        if proof is not None:
            self._continuation_outcomes[key] = replace(proof, original=retained, cancellations=tuple(cancellations))
        raise retained from retained.__cause__
    finally:
        # Actual start outcome unknown or actual adoption/cleanup still owned:
        # retain the lock/custody and use the existing process recovery owner.
        # A timer, absent Task, caught exception or in-memory receipt is no proof.
        if acquired:
            proof = self._continuation_outcomes.get(key)
            if proof is not None or released_before_start:
                lock.release()
            else:
                active_failure = sys.exception()
                try:
                    self._process_recovery.request_shutdown()
                except BaseException as shutdown:
                    if active_failure is None:
                        raise
                    raise _combine_operation_failures(active_failure, (shutdown, *cancellations))
```

If SQL start raises after its commit but before returning a nominal context,
`running` is absent while `start_attempted` is set. This branch deliberately
does not issue a speculative release/recovery against a guessed fence. It
retains the process lock and actual coordinator/cancellation custody, requests
the existing process recovery, and leaves the SQL segment discoverable for its
ordinary expired-owner recovery scan. No unknown committed start is relabelled
as an unstarted request.

##### W5.1: Complete the actual `_job_finished` ownership branch

- [ ] Add this branch immediately after the existing `_job_finished` deletes
  `_jobs[key]` and `_local_cancels[key]`, before its legacy adoption logic.
  It consumes only that actual Task's outcome. Keep the full legacy branch
  below it unchanged.

```python
if key in self._continuation_claims:
    self._continuation_job_finished(key, task)
    return
```

- [ ] Add this complete callback. Do not clear an unverified adoption or
  incomplete original cohort merely because a fresh recovery cohort completed.

```python
def _continuation_job_finished(
    self, key: tuple[UUID, str], task: asyncio.Task[_AdoptionSettlementHandoff | None],
) -> None:
    failure = None if task.cancelled() else task.exception()
    proof = self._continuation_outcomes.get(key)
    coordinator = self._required_coordinators.get(key)
    recovery = self._recovery_coordinators.get(key, [])
    completed = (coordinator is None or coordinator.all_completed) and all(item.all_completed for item in recovery)
    started = key in self._continuation_start_attempted
    observed = self._adoption_recovery_cancellations.get(key, [])
    same_cancellations = (
        proof is not None and len(proof.cancellations) == len(observed)
        and all(left is right for left, right in zip(proof.cancellations, observed, strict=True))
    )
    actual_original = proof is not None and (
        (not task.cancelled() and proof.original is failure)
        or (task.cancelled() and isinstance(proof.original, asyncio.CancelledError)
            and any(proof.original is original for original in observed))
    )
    verified = (
        type(proof) is _ContinuationJobOutcome
        and proof.receipt.session_id == key[0] and proof.receipt.operation_id == key[1]
        and actual_original and same_cancellations
    )
    if not completed or (started and not verified) or (not started and key not in self._continuation_unstarted_released):
        self._process_recovery.request_shutdown()
        if failure is not None:
            slog.error("composer_operation.continuation_owner_unsettled", exc_class=type(failure).__name__)
        self._wake.set()
        return
    if failure is not None:
        slog.info("composer_operation.continuation_owner_recovered", exc_class=type(failure).__name__)
    self._continuation_claims.pop(key, None)
    self._continuation_start_attempted.discard(key)
    self._continuation_outcomes.pop(key, None)
    self._continuation_adoptions.pop(key, None)
    self._continuation_unstarted_released.discard(key)
    self._adoption_recovery_pending.discard(key)
    self._adoption_recovery_cancellations.pop(key, None)
    self._required_coordinators.pop(key, None)
    self._recovery_coordinators.pop(key, None)
    self._wake.set()
```

The exception wrapper is not mistaken for a terminal or ordinary yield. Its
exact `original` remains on the actual Task exception/cause and on the required
work receipts. A successful normal result has `proof.original is None` and
`task.exception() is None`. A late canceled Task is matched only against the
actual retained cancellation object; the callback never calls `task.result()`
to manufacture a new cancellation. All list comparisons use object identity.

- [ ] Extend `assert_shutdown_complete` before its coordinator assertions:

```python
if (self._continuation_claims or self._continuation_start_attempted
    or self._continuation_adoptions or self._continuation_outcomes or self._continuation_unstarted_released):
    raise AuditIntegrityError("Continuation shutdown retains an unverified started/adoption owner")
```

#### W6: Exercise the actual SQL, lease, producer and process-lock fault boundaries

**Create:** `tests/unit/web/sessions/test_composer_continuation_worker_adoption.py`.
**Existing fixture owners:** `_file_app` and `_worker` in
`tests/unit/web/sessions/test_composer_async_worker.py` at the bound worker
source. `_file_app` creates a real file SQLite engine, schema, identity and
session service; `_worker` installs the existing in-process test watchdog. The
new `minimal_continuation_policy` is fully defined in storage C5.2. No helper
below relies on an undeclared fixture or a fake SQL receipt.

- [ ] Write this complete test module before W0–W5 implementation. Make the
  dependency modules importable, then establish the behavioral RED controls
  in W7. An absent module or collection error is setup work, never the RED.

```python
from __future__ import annotations

import asyncio
import threading
from uuid import uuid4

import pytest
from sqlalchemy import select

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.coordination import lifecycle as lifecycle_module
from elspeth.web.coordination.composer_operation_authority import ComposerAsyncOperationAuthority
from elspeth.web.coordination.contracts import SessionOperationLeaseDisposition
from elspeth.web.coordination.lifecycle import SessionOperationLease
from elspeth.web.required_work import RequiredWorkSource
from elspeth.web.sessions import composer_continuation_turn
from elspeth.web.sessions.composer_continuation_turn import ComposerContinuationSuspended
from elspeth.web.sessions.composer_operations import (
    COMPOSER_CANCEL_REQUESTED,
    ComposerOperationRunning,
    composer_operation_v2_request_hash,
)
from elspeth.web.sessions.continuation import ComposerSegmentRecoveryReceipt, load_snapshot
from elspeth.web.sessions.models import (
    composer_async_operations_table as parents,
    composer_operation_segments_table as segments,
    composer_segment_attempts_table as attempts,
    session_operation_fences_table as fences,
)
from elspeth.web.sessions.routes._helpers import composer_session_lock_registry
from elspeth.web.sessions.schemas import SendMessageRequest
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.sessions.test_composer_async_worker import _file_app, _worker


def failure_objects(root: BaseException) -> tuple[BaseException, ...]:
    result: list[BaseException] = []
    pending = [root]
    while pending:
        current = pending.pop()
        if any(current is earlier for earlier in result):
            continue
        result.append(current)
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        if current.__cause__ is not None:
            pending.append(current.__cause__)
        if current.__context__ is not None:
            pending.append(current.__context__)
    return tuple(result)


async def prepare_case(tmp_path):
    app, service, engine, composer = _file_app(tmp_path)
    authority = ComposerAsyncOperationAuthority(
        engine, owner_instance_id=service.session_operation_owner_instance_id,
        claim_lease_seconds=30, continuation_policy=minimal_continuation_policy(),
    )
    app.state.composer_async_operation_authority = authority
    worker = _worker(app, authority)
    app.state.composer_async_worker = worker
    session = await service.create_session("alice", "Continuation custody", app.state.settings.auth_provider)
    request = SendMessageRequest(operation_id=str(uuid4()), content="Preserve the original request", state_id=None)
    record, fresh = authority.admit(
        quota=app.state.rate_limiter.composer_admission, session_id=session.id,
        operation_id=request.operation_id, kind="compose_message",
        request_hash=composer_operation_v2_request_hash(session_id=session.id, kind="compose_message", request=request),
        actor_user_id="alice", request_id="worker-adoption-test", base_state_id=None,
        request_json=request.model_dump_json(), deadline_seconds=85.0, max_nonterminal=64,
        auth_provider_type=app.state.settings.auth_provider, lifecycle_version=2, continuation_enabled=True,
    )
    assert fresh and record.lifecycle_version == 2
    (claim,) = authority.claim_segment(limit=1)
    return app, service, engine, composer, authority, worker, record, claim


def begin_job(worker, claim):
    key = (claim.session_id, claim.operation_id)
    worker._continuation_claims[key] = claim
    worker._local_cancels[key] = asyncio.Event()
    task = asyncio.create_task(worker._job(claim), name="test-actual-continuation-job")
    worker._jobs[key] = task
    return key, task


async def actual_failure(task):
    with pytest.raises(BaseException) as raised:
        await asyncio.wait_for(asyncio.shield(task), timeout=10)
    assert task.done(), "A waiter timeout is not the actual job outcome"
    return raised.value


async def assert_recovered(app, service, engine, composer, authority, worker, claim, failure):
    key = (claim.session_id, claim.operation_id)
    proof = worker._continuation_outcomes[key]
    assert type(proof.receipt) is ComposerSegmentRecoveryReceipt
    receipt = proof.receipt
    authority.verify_recovery(receipt)
    with engine.connect() as conn:
        parent = conn.execute(select(parents).where(
            parents.c.session_id == str(claim.session_id), parents.c.operation_id == claim.operation_id,
        )).one()
        segment = conn.execute(select(segments).where(
            segments.c.session_id == str(claim.session_id), segments.c.operation_id == claim.operation_id,
            segments.c.sequence == claim.segment_sequence,
        )).one()
        history = conn.execute(select(attempts).where(
            attempts.c.session_id == str(claim.session_id), attempts.c.operation_id == claim.operation_id,
        )).all()
        fence = conn.execute(select(fences).where(fences.c.session_id == str(claim.session_id))).one()
        stored = load_snapshot(conn, session_id=str(claim.session_id), operation_id=claim.operation_id)
    assert parent.status == "running" and parent.phase == "retry_waiting"
    assert parent.result_json is None and parent.failure_code is None and parent.settled_at is None
    assert parent.current_segment_sequence == claim.segment_sequence
    assert parent.ownership_generation == receipt.generation == claim.ownership_generation + 1
    assert segment.state == "interrupted" and segment.generation == receipt.generation
    assert segment.claim_token is None and segment.claim_owner_instance_id is None and segment.claim_expires_at is None
    assert segment.checkpoint_id == stored.checkpoint_id == receipt.checkpoint_id
    assert stored.cursor == receipt.cursor and receipt.cursor > 0
    assert len(history) == 1
    assert history[0].outcome == "interrupted" and history[0].revoked_at is not None
    assert history[0].generation == claim.ownership_generation
    assert history[0].claim_attempt == claim.attempt
    assert (history[0].session_operation_id, history[0].session_operation_epoch,
            history[0].session_operation_lease_token) == (fence.operation_id, fence.operation_epoch, fence.lease_token)
    assert fence.released_at is not None
    assert worker._required_coordinators[key].all_completed
    worker._required_coordinators[key].assert_completed()
    for recovery in worker._recovery_coordinators.get(key, []):
        recovery.assert_completed()
    for lease in worker._continuation_adoptions[key]:
        assert type(lease) is SessionOperationLease and lease.closed
        assert lease.disposition is SessionOperationLeaseDisposition.RELEASED
        assert not lease._owned_tasks
        assert lease._renewal_task is not None and lease._renewal_task.done()
        assert lease._close_task is not None and lease._close_task.done()
    observed = worker._adoption_recovery_cancellations[key]
    assert len(proof.cancellations) == len(observed)
    assert all(left is right for left, right in zip(proof.cancellations, observed, strict=True))
    objects = failure_objects(failure)
    assert proof.original is failure
    assert all(any(item is original for item in objects) for original in observed)
    lock = await composer_session_lock_registry(app).get_lock(str(claim.session_id))
    assert not lock.locked()
    assert composer.calls == 0
    assert await service.get_messages(claim.session_id, limit=None) == []


def finish_callback(worker, key, task):
    worker._job_finished(key, task)
    assert not worker._jobs and key not in worker._continuation_claims
    assert key not in worker._required_coordinators and key not in worker._recovery_coordinators
    assert key not in worker._continuation_adoptions and key not in worker._continuation_outcomes
    assert key not in worker._adoption_recovery_cancellations and key not in worker._adoption_recovery_pending
    worker._stopping = True
    worker.assert_shutdown_complete()


@pytest.mark.asyncio
@pytest.mark.parametrize("boundary", ("sql_start_cancel", "adopt_cancel", "adopt_failure"))
async def test_actual_start_and_adoption_preserve_originals_before_lock_release(tmp_path, monkeypatch, boundary):
    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    repository = service.session_operation_authority
    loop = asyncio.get_running_loop()
    marker = object()
    original = RuntimeError("ACTUAL_ADOPTION_FAILURE")
    entered = asyncio.Event()
    release = threading.Event()
    actual_start = repository.start_composer_segment
    actual_cas = repository.compare_and_swap
    actual_verify = authority.verify_recovery
    verified = []
    runner_calls = []
    owner = None

    async def must_not_enter_runner(*, services, running, lease, observation, snapshot):
        runner_calls.append(running)
        raise AssertionError("A retained setup/adoption failure cannot dispatch continuation work")

    def start(*args, **kwargs):
        context = actual_start(*args, **kwargs)
        if boundary == "sql_start_cancel":
            assert owner is not None
            loop.call_soon_threadsafe(owner.cancel, marker)
        return context

    def cas(context):
        result = actual_cas(context)
        if boundary == "adopt_failure":
            raise original
        if boundary == "adopt_cancel":
            loop.call_soon_threadsafe(entered.set)
            assert release.wait(timeout=5), "Test did not release its own actual adoption SQL worker"
        return result

    def verify(receipt):
        assert lock.locked(), "Process lock must cover actual recovery readback"
        actual_verify(receipt)
        verified.append(receipt)

    monkeypatch.setattr(repository, "start_composer_segment", start)
    monkeypatch.setattr(repository, "compare_and_swap", cas)
    monkeypatch.setattr(authority, "verify_recovery", verify)
    monkeypatch.setattr(composer_continuation_turn, "run_composer_continuation_segment", must_not_enter_runner)
    lock = await composer_session_lock_registry(app).get_lock(str(record.session_id))
    try:
        key, owner = begin_job(worker, claim)
        if boundary == "adopt_cancel":
            await asyncio.wait_for(entered.wait(), timeout=5)
            owner.cancel(marker)
            await asyncio.sleep(0)
            assert not owner.done() and lock.locked() and not verified
            release.set()
        failure = await actual_failure(owner)
        objects = failure_objects(failure)
        if boundary == "adopt_failure":
            assert any(item is original for item in objects)
        else:
            observations = worker._adoption_recovery_cancellations[key]
            assert observations
            assert any(item.args == (marker,) for item in observations)
            assert all(any(item is retained for item in objects) for retained in observations)
        assert any(type(item) is ComposerContinuationSuspended for item in objects)
        assert len(verified) == 1 and not runner_calls
        await assert_recovered(app, service, engine, composer, authority, worker, claim, failure)
        finish_callback(worker, key, owner)
    finally:
        release.set()
        if owner is not None and not owner.done():
            await actual_failure(owner)
        await worker._process_recovery.join_escalation()
        engine.dispose()


@pytest.mark.asyncio
@pytest.mark.parametrize("child_fails", (False, True))
async def test_actual_lease_owned_producer_finishes_before_generation_and_lock_release(tmp_path, monkeypatch, child_fails):
    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    entered, release = asyncio.Event(), asyncio.Event()
    original = RuntimeError("ACTUAL_RUNNER_FAILURE")
    child_original = RuntimeError("ACTUAL_CHILD_FAILURE")
    leases, children, checks = [], [], []
    actual_verify = authority.verify_recovery

    async def fail_after_owned_child(*, services, running, lease, observation, snapshot):
        assert observation.request_lifecycle is not None
        assert observation.required_work is lease.required_work
        leases.append(lease)
        ticket = lease.required_work.reserve(
            RequiredWorkSource.PREPARATION_VALIDATION_PRODUCER, transition_ordinal=7, semantic_ordinal=3,
        )

        async def actual_child():
            try:
                entered.set()
                await release.wait()
                if child_fails:
                    raise child_original
            except BaseException as failure:
                ticket.complete_owned(failure)
                raise
            else:
                ticket.complete_owned()

        children.append(lease.create_task(actual_child(), name="test-owned-preparation"))
        await entered.wait()
        raise original

    def verify(receipt):
        assert lock.locked() and all(child.done() for child in children)
        assert leases[0]._renewal_task is not None and leases[0]._renewal_task.done()
        actual_verify(receipt)
        checks.append(receipt)

    monkeypatch.setattr(composer_continuation_turn, "run_composer_continuation_segment", fail_after_owned_child)
    monkeypatch.setattr(authority, "verify_recovery", verify)
    lock = await composer_session_lock_registry(app).get_lock(str(record.session_id))
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        await asyncio.wait_for(entered.wait(), timeout=5)
        await asyncio.sleep(0)
        assert lock.locked() and not owner.done() and not checks
        assert not worker._required_coordinators[key].all_completed
        with engine.connect() as conn:
            segment = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).one()
            fence = conn.execute(select(fences).where(fences.c.session_id == str(record.session_id))).one()
        assert segment.state == "running" and segment.generation == claim.ownership_generation
        assert fence.released_at is None
        assert children[0] in leases[0]._owned_tasks and not children[0].done()
        release.set()
        failure = await actual_failure(owner)
        objects = failure_objects(failure)
        assert any(item is original for item in objects)
        assert any(item is child_original for item in objects) is child_fails
        assert len(checks) == 1 and children[0].done()
        await assert_recovered(app, service, engine, composer, authority, worker, claim, failure)
        finish_callback(worker, key, owner)
    finally:
        release.set()
        if owner is not None and not owner.done():
            await actual_failure(owner)
        await worker._process_recovery.join_escalation()
        engine.dispose()


@pytest.mark.asyncio
async def test_failed_actual_readback_retains_original_process_lock_and_custody(tmp_path, monkeypatch):
    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    original = RuntimeError("ACTUAL_RUNNER_FAILURE")
    readback_original = RuntimeError("ACTUAL_RECOVERY_READBACK_FAILURE")
    actual_verify = authority.verify_recovery
    readbacks = []

    async def fail_runner(*, services, running, lease, observation, snapshot):
        raise original

    def fail_readback(receipt):
        assert lock.locked()
        actual_verify(receipt)
        readbacks.append(receipt)
        raise readback_original

    monkeypatch.setattr(composer_continuation_turn, "run_composer_continuation_segment", fail_runner)
    monkeypatch.setattr(authority, "verify_recovery", fail_readback)
    lock = await composer_session_lock_registry(app).get_lock(str(record.session_id))
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        failure = await actual_failure(owner)
        objects = failure_objects(failure)
        assert any(item is original for item in objects) and any(item is readback_original for item in objects)
        assert len(readbacks) == 1
        assert key not in worker._continuation_outcomes
        assert lock.locked() and worker._instance_draining.is_set()
        worker._required_coordinators[key].assert_completed()
        for lease in worker._continuation_adoptions[key]:
            assert lease.closed and not lease._owned_tasks
            assert lease._renewal_task is not None and lease._renewal_task.done()
        worker._job_finished(key, owner)
        assert key in worker._continuation_claims and key in worker._required_coordinators
        worker._stopping = True
        with pytest.raises(AuditIntegrityError, match="unverified started/adoption owner"):
            worker.assert_shutdown_complete()
        assert composer.calls == 0
    finally:
        if owner is not None and not owner.done():
            await actual_failure(owner)
        if lock.locked():
            lock.release()  # This lock and SQLite fixture belong only to this test.
        await worker._process_recovery.join_escalation()
        engine.dispose()


@pytest.mark.asyncio
async def test_constructor_allocation_then_raise_retains_and_joins_actual_renewal(tmp_path, monkeypatch):
    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    original = RuntimeError("ACTUAL_RENEWAL_ALLOCATION_RETURN_FAILURE")
    actual_create_task = asyncio.create_task
    renewals = []
    injected = False

    def create_task(coro, *, name=None, context=None):
        nonlocal injected
        task = actual_create_task(coro, name=name, context=context)
        if name == "session-operation-renewal" and not injected:
            injected = True
            renewals.append(task)
            raise original
        return task

    monkeypatch.setattr(lifecycle_module.asyncio, "create_task", create_task)
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        failure = await actual_failure(owner)
        assert any(item is original for item in failure_objects(failure))
        assert len(renewals) == 1 and renewals[0].done()
        (lease,) = worker._continuation_adoptions[key]
        assert lease._renewal_task is renewals[0] and lease.closed
        assert worker._instance_draining.is_set(), "Unknown factory allocation must arm the existing recovery owner"
        await assert_recovered(app, service, engine, composer, authority, worker, claim, failure)
        finish_callback(worker, key, owner)
    finally:
        if owner is not None and not owner.done():
            await actual_failure(owner)
        await worker._process_recovery.join_escalation()
        engine.dispose()


@pytest.mark.asyncio
async def test_sql_start_commit_without_context_return_never_fabricates_unstarted_release(tmp_path, monkeypatch):
    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    repository = service.session_operation_authority
    actual_start = repository.start_composer_segment
    original = RuntimeError("ACTUAL_SQL_START_RETURN_FAILURE")
    contexts = []

    def fail_after_start(*args, **kwargs):
        contexts.append(actual_start(*args, **kwargs))
        raise original

    monkeypatch.setattr(repository, "start_composer_segment", fail_after_start)
    lock = await composer_session_lock_registry(app).get_lock(str(record.session_id))
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        failure = await actual_failure(owner)
        assert any(item is original for item in failure_objects(failure))
        with engine.connect() as conn:
            segment = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).one()
            history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).one()
            fence = conn.execute(select(fences).where(fences.c.session_id == str(record.session_id))).one()
        assert segment.state == "running" and segment.generation == claim.ownership_generation
        assert history.revoked_at is None and history.outcome is None and fence.released_at is None
        assert key in worker._continuation_start_attempted and key not in worker._continuation_outcomes
        assert lock.locked() and worker._instance_draining.is_set() and composer.calls == 0
        worker._job_finished(key, owner)
        assert key in worker._continuation_claims
    finally:
        if owner is not None and not owner.done():
            await actual_failure(owner)
        for context in contexts:
            repository.release(context)  # Only this test's exact retained SQL context.
        if lock.locked():
            lock.release()
        await worker._process_recovery.join_escalation()
        engine.dispose()


@pytest.mark.asyncio
async def test_v2_watch_preserves_stop_drain_and_still_delivers_shutdown(tmp_path, monkeypatch):
    from elspeth.web.composer_watch_reads import ComposerOperationWatchReader

    app, service, engine, composer, authority, worker, record, claim = await prepare_case(tmp_path)
    entered = asyncio.Event()
    observed_stop = asyncio.Event()
    never = asyncio.Event()
    running_owners = []
    actual_read = ComposerOperationWatchReader.read

    async def read_actual_stop(reader):
        current, now = await actual_read(reader)
        if current is not None and current.cancel_requested_at is not None:
            observed_stop.set()
        return current, now

    monkeypatch.setattr(ComposerOperationWatchReader, "read", read_actual_stop)

    async def wait_for_actual_stop(*, services, running, lease, observation, snapshot):
        running_owners.append(running)
        assert observation.request_lifecycle is not None
        entered.set()
        await never.wait()
        raise AssertionError("Test continuation must remain owned until actual shutdown cancellation")

    monkeypatch.setattr(composer_continuation_turn, "run_composer_continuation_segment", wait_for_actual_stop)
    owner = None
    try:
        key, owner = begin_job(worker, claim)
        await asyncio.wait_for(entered.wait(), timeout=5)
        marked = authority.request_v2_cancel(
            session_id=record.session_id, operation_id=record.operation_id,
            actor_user_id="alice", auth_provider_type=app.state.settings.auth_provider,
        )
        assert marked is not None and marked[0].cancel_requested
        await asyncio.wait_for(observed_stop.wait(), timeout=5)
        await asyncio.sleep(0)
        assert not owner.done() and owner.cancelling() == 0
        worker._stopping = True
        failure = await actual_failure(owner)
        assert any(isinstance(item, asyncio.CancelledError) and item.args == (COMPOSER_SHUTDOWN,)
                   for item in failure_objects(failure))
        assert not any(isinstance(item, asyncio.CancelledError) and item.args == (COMPOSER_CANCEL_REQUESTED,)
                       for item in failure_objects(failure))
        assert len(running_owners) == 1 and type(running_owners[0]) is ComposerOperationRunning
        await assert_recovered(app, service, engine, composer, authority, worker, claim, failure)
        current = authority.get(session_id=record.session_id, operation_id=record.operation_id)
        assert current.cancel_requested_at is not None and current.status == "running"
        finish_callback(worker, key, owner)
    finally:
        if owner is not None and not owner.done():
            owner.cancel("test cleanup")
            await actual_failure(owner)
        await worker._process_recovery.join_escalation()
        engine.dispose()
```

The final test deliberately holds the runner while the real watch reader has
observed the actual committed Stop. It proves that v2 private draining is not
interrupted by that flag and that shutdown still delivers its actual marker
through joined recovery. Behavioral RED: restore only the unconditional
`owner.cancel(COMPOSER_CANCEL_REQUESTED)` branch; the no-cancellation assertion
must fail. Restore the v2 condition. Existing legacy Stop and lease-loss watch
tests remain required. B9/O9 separately execute the real driver to prove
pending-effect reconciliation, owed P4, actual terminal Stop, no new provider
dispatch, and terminal public replay; the held-runner test does not substitute
for those controls.

#### W7: Establish behavioral RED, GREEN, affected gates and ordinary commits

All commands in this section are future execution steps, after main P0 and
the preceding storage/frame dependencies. Nothing here ran while writing the
plan. The main plan records the qualified `.9` input and current combined
integration. Check actual CPU, memory, database and port conflicts before
execution; this fragment imposes no extra test-capacity reservation or fixed
worker count.

- [ ] Use the main's ordinary execution setup in the approved isolated tree:

```bash
COMPOSER_WORKTREE="$(git rev-parse --show-toplevel)"
COMPOSER_PYTHON="$COMPOSER_WORKTREE/.venv/bin/python"
export COMPOSER_WORKTREE COMPOSER_PYTHON
export PYTHONPATH="$COMPOSER_WORKTREE/src:$COMPOSER_WORKTREE/elspeth-lints/src"
mkdir -p "$COMPOSER_WORKTREE/.claude/lanes"
COMPOSER_LOG_DIR="$(mktemp -d "$COMPOSER_WORKTREE/.claude/lanes/continuation-worker.XXXXXX")"
export COMPOSER_LOG_DIR
test -x "$COMPOSER_PYTHON"
COMPOSER_COMMON_GIT_DIR="$(git rev-parse --path-format=absolute --git-common-dir)"
COMPOSER_TEMP_PARENT="$(dirname "$COMPOSER_COMMON_GIT_DIR")/.claude/lanes"
mkdir -p "$COMPOSER_TEMP_PARENT"
COMPOSER_TMP_DIR="$(mktemp -d "$COMPOSER_TEMP_PARENT/t.XXXXXX")"
export TMPDIR="$COMPOSER_TMP_DIR"
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -c 'import elspeth, elspeth_lints; print(elspeth.__file__); print(elspeth_lints.__file__)'
```

Both printed package paths must be inside this exact execution worktree. A
different editable install or an unavailable interpreter is a blocked setup,
not a test result; do not install/rebuild a shared environment to conceal it.

- [ ] Establish the first behavioral RED by applying this deliberate one-line
  mutation to the `_continuation_job` SQL-start call with the ordinary editor:

```diff
-        ), cancellation_observations=cancellations)
+        ))
         running = ComposerOperationRunning(claim=claim, session_operation_context=context)
```

This changes only the real SQL-start call's cancellation handoff. It must
cause a validly collected `sql_start_cancel` case to lose the actual marker
and/or enter the forbidden runner, failing the named assertions below.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_continuation_worker_adoption.py -k sql_start_cancel -n 0 -v > "$COMPOSER_LOG_DIR/red-start-cancellation.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/red-start-cancellation.log"
test "$COMPOSER_RC" -eq 1
```

- [ ] Restore that one-line mutation using the editor. Establish the second
  behavioral RED by replacing only `_suspend_continuation_owner`'s final real
  readback with the deliberate mutation below:

```diff
-    await self._verify_continuation_receipt(receipt, coordinator, cancellations)
     return receipt
```

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_continuation_worker_adoption.py::test_failed_actual_readback_retains_original_process_lock_and_custody -n 0 -v > "$COMPOSER_LOG_DIR/red-recovery-readback.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/red-recovery-readback.log"
test "$COMPOSER_RC" -eq 1
```

The failure must be the missing actual readback/original/retained-lock
assertion. Exit 2, a missing fixture/module or an unrelated traceback does not
establish either RED. Read the full logs and restore the second mutation.

- [ ] Run the whole new behavioral packet with the corrected implementation:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_continuation_worker_adoption.py -n 0 -v > "$COMPOSER_LOG_DIR/green-worker-adoption.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-worker-adoption.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Preserve the existing lease/adoption/physical-receipt/watcher behavior:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_composer_async_worker.py tests/unit/web/coordination/test_session_operation_lifecycle.py tests/unit/web/sessions/test_lifecycle_physical_receipts.py tests/unit/web/test_lifecycle_original_outcomes.py tests/unit/web/test_composer_watch_admission_custody.py -n 0 -v > "$COMPOSER_LOG_DIR/green-existing-owners.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-existing-owners.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Run the relevant whole-tree attribute/masquerade/contract gates and
  focused code quality. These paths exist at the bound candidate; no new
  reflection sites or suppressions are part of W0–W6:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/test_sessions_composer_attribute_contracts.py tests/unit/elspeth_lints/test_masquerade_gate.py tests/unit/scripts/test_check_contracts.py -n 0 -v > "$COMPOSER_LOG_DIR/green-owner-gates.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/green-owner-gates.log"
test "$COMPOSER_RC" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m scripts.check_contracts > "$COMPOSER_LOG_DIR/contracts.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/contracts.log"
test "$COMPOSER_RC" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m ruff check src/elspeth/web/coordination/lifecycle.py src/elspeth/web/composer_watch_reads.py src/elspeth/web/sessions/composer_async_worker.py src/elspeth/web/sessions/composer_continuation_turn.py src/elspeth/web/sessions/composer_turn.py tests/unit/web/sessions/test_composer_continuation_worker_adoption.py > "$COMPOSER_LOG_DIR/ruff-worker.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/ruff-worker.log"
test "$COMPOSER_RC" -eq 0
```

- [ ] Commit this cohesive worker/lease boundary after the full focused
  packet is GREEN. Its schema, required-work, clock and runner dependency
  commits precede it. Use the main's ordinary status/diff/branch-safety sequence
  and keep hooks enabled; do not create an intermediate commit that cannot
  import or omits its new constructor/worker regression tests.

```bash
cd "$COMPOSER_WORKTREE" && git status --short
cd "$COMPOSER_WORKTREE" && git diff --check
cd "$COMPOSER_WORKTREE" && git add src/elspeth/web/coordination/lifecycle.py src/elspeth/web/composer_watch_reads.py src/elspeth/web/sessions/composer_async_worker.py src/elspeth/web/sessions/composer_continuation_turn.py src/elspeth/web/sessions/composer_turn.py tests/unit/web/sessions/test_composer_continuation_worker_adoption.py
cd "$COMPOSER_WORKTREE" && git diff --cached --stat
cd "$COMPOSER_WORKTREE" && scripts/branch-safety-check.sh --intent commit
cd "$COMPOSER_WORKTREE" && git commit -m "feat(composer): preserve continuation worker failure custody"
```

Stage only owned, reviewed changes; `composer_continuation_turn.py` is jointly
sequenced with C4a.3's runner owner. Never stage this lane fragment, another
writer's partial runner, private logs or owner state. These future commits
grant no present Git authority.

- [ ] Before merge, combine this shared lease/worker change with main Q's
  ordinary full-suite and serial PostgreSQL/testcontainer qualification. The
  new SQL recovery tests in storage C4b must also pass on PostgreSQL. Use the
  existing full-suite gate's normal detached invocation after checking actual
  owner/capacity, read its native per-stage exits and frozen-tree result, and
  preserve the operator's separate key/signature boundary. No Python full
  suite or PostgreSQL execution ran during this planning task.

#### W8: Persist retry backoff without an age-based eligibility cutoff

**Modify:** `src/elspeth/web/sessions/continuation.py`, the existing authority
and worker facades above; `tests/unit/web/sessions/test_continuation_cold_restore.py`.

- [ ] Apply C3.6's literal optional-delay signature and SQL count branch above
  before running this test. The ordinary W3/W4 paths pass no delay override:
  the same transaction reads actual revoked interrupted attempts for this
  sequence and chooses `1,2,4,8,16,30,30,...` seconds. A fresh successor has a
  different sequence and therefore starts at one second. Historical attempts
  remain immutable. The optional finite internal delay is retained for existing
  direct owner tests and explicitly selected owner waits; no route exposes it.
  Repeated infrastructure failure never becomes a terminal age refusal.
**Prerequisites for this test and its PostgreSQL alias:** corrected C3.6 live-owner suspension plus C3.4b, C3.9 and F28.3. Run it in main step 9 before worker activation. Keep its complete three-owner controlled UTC clock, all eight actual suspend/release/claim cycles, both 0/301-second parent ages, backoff/early-claim/history assertions and both existing semantic omission controls unchanged. Initial C3.4 alone cannot qualify this module.

- [ ] Add this complete actual-store test. It replaces only the database clock
  reads with one controlled UTC clock, retaining every claim/fence, history,
  snapshot, suspension, release and receipt transaction. Both age cases must
  traverse the same schedule and remain claimable; this does not claim a real
  five-minute wall-clock outage experiment.

```python
from datetime import timedelta

from sqlalchemy import select

from elspeth.web.coordination import composer_operation_authority as operation_module
from elspeth.web.sessions import continuation as continuation_module
from elspeth.web.sessions.time_normalization import restore_utc


@pytest.mark.parametrize("initial_age_seconds", (0, 301))
def test_actual_retry_history_backs_off_without_parent_age_expiry(
    operation_store, monkeypatch, initial_age_seconds,
):
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    with engine.connect() as conn:
        real_now = continuation_module.database_now(conn)
    clock = [real_now + timedelta(seconds=initial_age_seconds)]

    def observed_database_now(conn):
        assert conn.engine is engine
        return clock[0]

    monkeypatch.setattr(continuation_module, "database_now", observed_database_now)
    monkeypatch.setattr(operation_module, "database_now", observed_database_now)
    monkeypatch.setattr(type(repository), "_database_now", staticmethod(observed_database_now))
    running = None
    delays = []
    try:
        for index, expected in enumerate((1, 2, 4, 8, 16, 30, 30, 30)):
            running = start_continuation(repository, authority)
            assert running.claim.segment_sequence == 1
            before = clock[0]
            receipt = authority.suspend_segment(running, reason="owner_unavailable")
            authority.verify_recovery(receipt)
            with engine.connect() as conn:
                actual = conn.execute(select(segments).where(
                    segments.c.session_id == str(sid), segments.c.operation_id == record.operation_id,
                    segments.c.sequence == 1)).one()
                history = conn.execute(select(attempts).where(
                    attempts.c.session_id == str(sid), attempts.c.operation_id == record.operation_id,
                    attempts.c.sequence == 1).order_by(attempts.c.generation)).all()
            assert len(history) == index + 1
            assert all(item.revoked_at is not None and item.outcome == "interrupted" for item in history)
            delay = (restore_utc(actual.next_eligible_at) - restore_utc(actual.enqueued_at)).total_seconds()
            delays.append(delay)
            assert delay == expected and receipt.next_eligible_at == before + timedelta(seconds=expected)
            repository.release(running.session_operation_context)
            running = None
            clock[0] = receipt.next_eligible_at - timedelta(microseconds=1)
            assert authority.claim_segment(limit=1) == ()
            clock[0] = receipt.next_eligible_at
        running = start_continuation(repository, authority)
        current = authority.get(session_id=sid, operation_id=record.operation_id)
        assert current is not None and current.status == "running" and current.failure_code is None
        assert current.current_segment_sequence == 1 and current.deadline_at is None
        assert running.claim.ownership_generation == record.ownership_generation + len(delays)
        assert delays == [1, 2, 4, 8, 16, 30, 30, 30]
    finally:
        if running is not None:
            repository.release(running.session_operation_context)
```

- [ ] Import this exact test into the existing serial PostgreSQL continuation
  module with its own `operation_store`, beside the other shared SQL proofs.
  The SQLite and PostgreSQL clock replacements have the same declared limit.
- [ ] Behavioral RED: replace only C3.6's schedule selection with `1.0`;
  require this test to fail on its second actual suspension delay. Restore it.
  Add only a `parent.started_at`/creation-age cutoff at 300 seconds in the v2
  claim query and require the 301-second case to fail at the named actual
  claim, then restore the original query. Do not treat a setup/import failure
  as either control. Run the exact focused command with native exit 0:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest tests/unit/web/sessions/test_continuation_cold_restore.py::test_actual_retry_history_backs_off_without_parent_age_expiry -n 0 -v > "$COMPOSER_LOG_DIR/retry-backoff.log" 2>&1
COMPOSER_RC=$?
cat "$COMPOSER_LOG_DIR/retry-backoff.log"
test "$COMPOSER_RC" -eq 0
```

Run the existing serial PostgreSQL module command from C4b as well. This is
ordinary source/test work in the same checkpoint; it adds no scheduler,
capacity reservation, controller, timer-based semantic progress, or retry cap.

### C4a.3: Execute the concrete phase driver supplied by the frames packet

Implement the actual bootstrap, phase execution, retained-result application,
current segment clock and terminal publication adapter in the frames
companion's driver task, using the canonical runner signature above. It
consumes `observation.request_lifecycle` and completes only its actual child
work before returning/raising. The worker's outer producer completes before
whole-cohort assertions and recovery. The SQL effect reconciliation port is
C5.7's nominal `ProviderEffectReconciliation`; the selected checkpoint/cursor
must be reloaded after its actual transaction. C4a.4 below owns the ordinary
expired-owner discovery scan. Both driver and worker tasks must be complete
before enabling new v2 admissions or running complete end-to-end tests.

The construction patch retains an uncertain allocation until actual coroutine
entry, arms the existing process recovery and never reports a completed
producer on timeout. A factory that never produces an actual entry remains
owned unknown during shutdown; it is not turned into a successful adoption or
safe durable successor. This is the same preserve/unknown direction as the
existing watcher and EXECUTE owners, without a new controller or service.

### C4a.4: Discover expired owners without starving work behind a live owner

**Files:** Extend `sessions/continuation.py`, `coordination/composer_operation_authority.py` and the existing worker's `reap_once`. The repository's C3.6 `recover_composer_segment` remains the sole expired-owner transition; its exact live-instance/takeover checks run under the session lock. A discovery row does not revoke authority by itself.

- [ ] Add the following immutable private candidate in `sessions/continuation.py` and the complete read facet on `ComposerAsyncOperationAuthority`. The stable cursor advances through running parents even if an older lease is still live. It therefore cannot repeatedly inspect a blocked oldest prefix forever. The transactional recovery rechecks actual expiry/owner state.

```python
@dataclass(frozen=True, slots=True)
class ComposerSegmentRecoveryCandidate:
    session_id: UUID
    operation_id: str
    generation: int
    actor_user_id: str


def list_running_continuation_segments(
    self, *, limit: int, after: tuple[str, str] | None = None,
) -> tuple[ComposerSegmentRecoveryCandidate, ...]:
    _bounded(limit)
    query = select(jobs.c.session_id, jobs.c.operation_id, jobs.c.ownership_generation, jobs.c.actor_user_id).join(
        segments, and_(segments.c.session_id == jobs.c.session_id, segments.c.operation_id == jobs.c.operation_id,
                       segments.c.sequence == jobs.c.current_segment_sequence, segments.c.generation == jobs.c.ownership_generation),
    ).where(jobs.c.lifecycle_version == 2, jobs.c.status == "running", segments.c.state == "running")
    if after is not None:
        query = query.where(or_(jobs.c.session_id > after[0],
                                and_(jobs.c.session_id == after[0], jobs.c.operation_id > after[1])))
    with self._engine.connect() as conn:
        rows = conn.execute(query.order_by(jobs.c.session_id, jobs.c.operation_id).limit(limit)).all()
    return tuple(ComposerSegmentRecoveryCandidate(UUID(row.session_id), row.operation_id,
                                                 row.ownership_generation, row.actor_user_id) for row in rows)
```

- [ ] Add `self._continuation_reap_cursor: tuple[str, str] | None = None` in the worker constructor. Immediately before the existing `return count` at the end of `reap_once`, insert this full discovery/recovery loop. It skips actual local jobs or adoption recovery owned by this process. It never calls a v1 deadline failure publisher for v2. Losing notifications is harmless because the existing background loop invokes the same SQL scan again.

```python
candidates = await _owned(run_sync_in_worker(
    self._authority.list_running_continuation_segments, limit=64, after=self._continuation_reap_cursor,
))
for candidate in candidates:
    key = (candidate.session_id, candidate.operation_id)
    if key in self._jobs or key in self._adoption_recovery_pending:
        continue
    recovered = await _owned(run_sync_in_worker(
        services.session_service.session_operation_authority.recover_composer_segment,
        session_id=candidate.session_id, operation_id=candidate.operation_id,
        expected_generation=candidate.generation, actor_user_id=candidate.actor_user_id,
        auth_provider_type=services.settings.auth_provider,
    ))
    if recovered:
        count += 1
        self.notify_work()
self._continuation_reap_cursor = ((str(candidates[-1].session_id), candidates[-1].operation_id)
                                   if len(candidates) == 64 else None)
```

**Prerequisites for `test_recovery_preserves_live_owner_then_recovers_same_sequence_once` and its PostgreSQL alias:** corrected C3.6 recovery plus C3.4b/C3.9/F28.3. Run in main step 9. Preserve its complete live-owner no-recovery positive, explicit physical release, exactly one same-sequence generation advance, second start and immutable two-attempt/one-segment assertions and live-lease omission control. Do not rewrite this original body or count first-start tests as its GREEN.

- [ ] Add this complete database recovery test to the cold-restore module. It positively proves a live owner is preserved, then uses the actual explicit repository release as a deterministic process-owner exit boundary and proves recovery advances only the same sequence/generation once. The PostgreSQL module imports this test too.

```python
def test_recovery_preserves_live_owner_then_recovers_same_sequence_once(operation_store):
    engine, repository, authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    first = start_continuation(repository, authority)
    candidates = authority.list_running_continuation_segments(limit=1)
    assert len(candidates) == 1 and candidates[0].operation_id == record.operation_id
    kwargs = dict(session_id=sid, operation_id=record.operation_id,
                  expected_generation=first.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
    assert repository.recover_composer_segment(**kwargs) is False
    repository.release(first.session_operation_context)
    assert repository.recover_composer_segment(**kwargs) is True
    assert repository.recover_composer_segment(**kwargs) is False
    assert authority.list_running_continuation_segments(limit=1) == ()
    second = start_continuation(repository, authority)
    try:
        assert second.claim.segment_sequence == first.claim.segment_sequence
        assert second.claim.ownership_generation == first.claim.ownership_generation + 1
        assert second.session_operation_context.fence.operation_id != first.session_operation_context.fence.operation_id
        with engine.connect() as conn:
            history = conn.execute(select(attempts).where(attempts.c.operation_id == record.operation_id)).all()
            actual_segments = conn.execute(select(segments).where(segments.c.operation_id == record.operation_id)).all()
        assert len(history) == 2 and len(actual_segments) == 1
    finally:
        repository.release(second.session_operation_context)
```

- [ ] Bootstrap this importable packet, then temporarily remove only the repository recovery's live-lease check. The test must fail its first `is False` assertion; restore the check before GREEN. Run the focused recovery test through the C4b SQLite and PostgreSQL commands, requiring native exit 0 before committing its source/protocol/test paths.

### C4b.1: Exercise exact database values, selection fairness, CAS and failed adoption on both stores

**Files:** Extend `tests/unit/web/sessions/test_continuation_cold_restore.py`; create `tests/testcontainer/web/test_composer_continuation_postgres.py`. These tests use the existing real SQLite and PostgreSQL operation-store fixtures and actual authority/service calls. They do not start another test environment during plan authoring.

- [ ] Add these full database tests to the cold-restore module:

```python
def test_attachment_codec_is_stored_and_read_exactly(operation_store):
    from elspeth.web.sessions.continuation import put_chunk, read_chunk
    from elspeth.web.sessions.models import composer_continuation_chunks_table as chunks
    engine, _repository, _authority, _service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    codec = "composer.attachment-admissions.v1"
    with engine.begin() as conn:
        put_chunk(conn, session_id=str(sid), operation_id=record.operation_id,
                  key="attachment-admission-exact", payload=b'{"files":[]}', codec=codec)
        stored = conn.execute(select(chunks.c.codec, chunks.c.payload).where(
            chunks.c.operation_id == record.operation_id, chunks.c.chunk_key == "attachment-admission-exact",
        )).one()
        assert stored.codec == codec
        assert bytes(stored.payload) == b'{"files":[]}'
        assert read_chunk(conn, session_id=str(sid), operation_id=record.operation_id,
                          key="attachment-admission-exact") == b'{"files":[]}'


@pytest.mark.asyncio
async def test_unexpired_old_claim_does_not_starve_younger_session(operation_store):
    engine, repository, authority, service, sid = operation_store
    older, _seed, _action = admit_continuation(operation_store)
    (old_claim,) = authority.claim_segment(limit=1)
    assert old_claim.operation_id == older.operation_id
    younger_sid = (await service.create_session("alice", "Younger eligible continuation", "local")).id
    younger_store = (engine, repository, authority, service, younger_sid)
    younger, _younger_seed, _younger_action = admit_continuation(younger_store)
    (selected,) = authority.claim_segment(limit=1)
    assert selected.operation_id == younger.operation_id
    authority.release_segment_claim(selected)
    authority.release_segment_claim(old_claim)
    (eligible_again,) = authority.claim_segment(limit=1)
    assert eligible_again.operation_id == older.operation_id
    authority.release_segment_claim(eligible_again)


@pytest.mark.asyncio
async def test_late_actual_settlement_refuses_stale_provider_prepare_without_losing_frames(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from elspeth.web.sessions.continuation import ContinuationCASConflict
    from elspeth.web.sessions.models import (
        composer_continuation_effects_table as effects,
        composer_continuation_provider_ordinals_table as ordinals,
    )
    engine, repository, authority, service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    selected = authority.load_continuation_snapshot(running)
    binding = authority.prepare_provider_effect(
        running, family=0, input_sha256="d" * 64, snapshot_json=selected.payload_json,
        expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor,
    )
    actual = await service.begin_provider_attempt(session_operation_context=running.session_operation_context,
                                                  source="composer", continuation_binding=binding, requested_model="test-model")
    stale = authority.load_continuation_snapshot(running)
    await service.cancel_undispatched_provider_attempt(
        session_operation_context=running.session_operation_context, attempt_id=actual.attempt_id,
        requested_model="test-model", continuation_binding=binding,
    )
    current = authority.load_continuation_snapshot(running)
    with engine.connect() as conn:
        before_effects = conn.execute(select(effects).where(effects.c.operation_id == record.operation_id)).all()
        before_ordinals = conn.execute(select(ordinals).where(ordinals.c.operation_id == record.operation_id)).all()
    with pytest.raises(ContinuationCASConflict):
        authority.prepare_provider_effect(
            running, family=0, input_sha256="e" * 64, snapshot_json=stale.payload_json,
            expected_checkpoint_id=stale.checkpoint_id, expected_cursor=stale.cursor,
        )
    assert authority.load_continuation_snapshot(running) == current
    with engine.connect() as conn:
        assert conn.execute(select(effects).where(effects.c.operation_id == record.operation_id)).all() == before_effects
        assert conn.execute(select(ordinals).where(ordinals.c.operation_id == record.operation_id)).all() == before_ordinals
    restored = decode_continuation_snapshot(current.payload_json)
    assert actual.attempt_id not in restored.pending_settlement_ids
    with pytest.raises(AuditIntegrityError, match="new provider preparation would replace an unapplied selected effect"):
        authority.prepare_provider_effect(
            running, family=0, input_sha256="e" * 64, snapshot_json=current.payload_json,
            expected_checkpoint_id=current.checkpoint_id, expected_cursor=current.cursor,
        )
    assert authority.load_continuation_snapshot(running) == current
    with engine.connect() as conn:
        assert conn.execute(select(effects).where(effects.c.operation_id == record.operation_id)).all() == before_effects
        assert conn.execute(select(ordinals).where(ordinals.c.operation_id == record.operation_id)).all() == before_ordinals
    repository.release(running.session_operation_context)


def test_failed_adoption_recovery_accepts_released_exact_owner_and_refuses_old_generation(operation_store):
    engine, repository, authority, _service, _sid = operation_store
    _record, _seed, _action = admit_continuation(operation_store)
    first = start_continuation(repository, authority)
    repository.release(first.session_operation_context)
    receipt = authority.suspend_failed_adoption(first)
    authority.verify_recovery(receipt)
    assert receipt.sequence == first.claim.segment_sequence
    assert receipt.generation == first.claim.ownership_generation + 1
    with pytest.raises(ComposerOperationFenceLost):
        authority.suspend_failed_adoption(first)
    with engine.connect() as conn:
        history = conn.execute(select(attempts).where(
            attempts.c.operation_id == first.claim.operation_id,
        )).one()
        rows = conn.execute(select(segments).where(segments.c.operation_id == first.claim.operation_id)).all()
    assert history.outcome == "interrupted" and history.revoked_at is not None
    assert len(rows) == 1 and rows[0].state == "interrupted"
```

- [ ] Create the full PostgreSQL module below. Explicit same-name fixture imports are pytest's existing fixture-sharing pattern, not a fabricated factory. Every imported behavioral test uses PostgreSQL through the existing `operation_store` fixture. The real default selection deselects this module; run it with `-m testcontainer -n 0`.

```python
"""PostgreSQL continuation SQL values, lineage, cancellation and CAS proofs."""
import pytest

from tests.testcontainer.web.test_composer_operations_postgres import (
    operation_postgres_target as operation_postgres_target,
    operation_store as operation_store,
)
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_actual_owner_result_and_mutation_share_one_transaction as test_actual_owner_result_and_mutation_share_one_transaction,
    test_attachment_codec_is_stored_and_read_exactly as test_attachment_codec_is_stored_and_read_exactly,
    test_both_v2_proposal_writers_keep_historical_binding_after_rollover as test_both_v2_proposal_writers_keep_historical_binding_after_rollover,
    test_cold_restore_preserves_real_ingress_and_revokes_old_fence as test_cold_restore_preserves_real_ingress_and_revokes_old_fence,
    test_failed_adoption_recovery_accepts_released_exact_owner_and_refuses_old_generation as test_failed_adoption_recovery_accepts_released_exact_owner_and_refuses_old_generation,
    test_interpretation_review_retains_actual_event_without_redispatch as test_interpretation_review_retains_actual_event_without_redispatch,
    test_late_actual_settlement_refuses_stale_provider_prepare_without_losing_frames as test_late_actual_settlement_refuses_stale_provider_prepare_without_losing_frames,
    test_saved_revision_projection_covers_both_segment_creators_once as test_saved_revision_projection_covers_both_segment_creators_once,
    test_recovery_preserves_live_owner_then_recovers_same_sequence_once as test_recovery_preserves_live_owner_then_recovers_same_sequence_once,
    test_undispatched_cancellation_joins_real_zero_ledger_and_clears_pending as test_undispatched_cancellation_joins_real_zero_ledger_and_clears_pending,
    test_unexpired_old_claim_does_not_starve_younger_session as test_unexpired_old_claim_does_not_starve_younger_session,
)

pytestmark = pytest.mark.testcontainer
```

- [ ] Behavioral RED controls, one at a time in the already-importable packet: remove the outer claim-expiry eligibility predicate and observe `test_unexpired_old_claim_does_not_starve_younger_session` fail its younger-operation assertion; remove the provider preparation CAS check and observe the stale-prepare `pytest.raises` assertion fail; temporarily narrow the codec column to `String(32)` and observe PostgreSQL reject the exact attachment codec value. Restore each controlled mutation before the next. A setup/import/collection error does not satisfy any RED assertion.
- [ ] Run the focused SQLite module, then the serial PostgreSQL module after the actual test-capacity owner releases contention. Require native exit 0 for each; retain both complete logs. These checks do not replace the repository-wide schema/session persistence gates required before eventual merge.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py >"$COMPOSER_LOG_DIR/c4b-sqlite.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c4b-sqlite.log"
test "$composer_exit" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -m testcontainer -n 0 tests/testcontainer/web/test_composer_continuation_postgres.py >"$COMPOSER_LOG_DIR/c4b-postgres.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c4b-postgres.log"
test "$composer_exit" -eq 0
```

- [ ] Commit only the two test paths and reviewed source fixes with normal hooks as `test(composer): prove continuation SQL authority on SQLite and PostgreSQL`.

### C4b.2: Prove physical custody and actual accounting at cancellation and SDK boundaries

**Files:** Create `tests/unit/web/composer/test_continuation_provider_accounting.py`. Reuse the real `operation_store` fixture and `admit_continuation`/`start_continuation` helpers. The controlled transport replaces only the paid SDK function; guard admission, required-work tickets, SQL attempts, audit construction and quota settlement remain real.

- [ ] Add this complete test module. It checks the actual persisted attempts/ledger/effect/frontier, preserves original cancellation identity, and proves a callback refusal never reaches the SDK. Unknown SDK usage stays NULL rather than becoming free quota.

```python
from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from sqlalchemy import select

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.continuation_clock import ContinuationActionClock
from elspeth.web.composer.continuation_codec import decode_continuation_snapshot
from elspeth.web.composer.llm_response_parsing import build_llm_call_record
from elspeth.web.composer.prompt_guard import prompt_guard_user_scope
from elspeth.web.composer.provider_gateway import _litellm_acompletion
from elspeth.web.composer.provider_quota import ProviderInvocationOwner, provider_call_scope
from elspeth.web.coordination.quota_authority import quota_provider_attempts_table as attempts
from elspeth.web.coordination.quota_authority import token_usage_ledger_table as ledger
from elspeth.web.required_work import RequiredAuthorityKind, RequiredWorkAuthority, RequiredWorkBinding, RequiredWorkCoordinator, RequiredWorkRole
from elspeth.web.sessions.models import composer_continuation_effects_table as effects
from tests.helpers.continuation_policy import minimal_continuation_policy
from tests.unit.web.coordination.test_composer_operation_authority import operation_store as operation_store
from tests.unit.web.sessions.test_continuation_cold_restore import admit_continuation, start_continuation


def make_custody(store):
    from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
    _engine, repository, authority, service, _sid = store
    _record, _seed, _action = admit_continuation(store)
    running = start_continuation(repository, authority)
    coordinator = RequiredWorkCoordinator(RequiredWorkAuthority(
        RequiredAuthorityKind.DURABLE_COMPOSE, running.session_operation_context,
        running.claim.operation_id, running.claim.attempt,
    ))
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    owner = ProviderInvocationOwner(service=service, required_work=work)
    snapshot = authority.load_continuation_snapshot(running)
    input_sha256 = continuation_provider_input_hash({
        "model": "test-model", "messages": [{"role": "user", "content": "Read the actual plugin contract"}],
    })
    binding = authority.prepare_provider_effect(
        running, family=0, input_sha256=input_sha256, snapshot_json=snapshot.payload_json,
        expected_checkpoint_id=snapshot.checkpoint_id, expected_cursor=snapshot.cursor,
    )
    clock = ContinuationActionClock(minimal_continuation_policy().actions.primary)
    return running, coordinator, binding, owner.mint_continuation(binding, clock=clock)


def assert_proven_no_sdk(store, running, binding):
    engine, _repository, authority, _service, _sid = store
    with engine.connect() as conn:
        actual = conn.execute(select(attempts).where(attempts.c.operation_id == running.session_operation_context.fence.operation_id)).one()
        usage = conn.execute(select(ledger).where(ledger.c.entry_id == actual.ledger_entry_id)).one()
        effect = conn.execute(select(effects).where(effects.c.effect_key == binding.effect_key,
                                                   effects.c.operation_id == running.claim.operation_id)).one()
    assert actual.settled_at is not None
    assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens) == (0, 0, 0, 0)
    assert effect.disposition == "refused" and effect.provider_attempt_id == actual.attempt_id
    stack = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
    assert actual.attempt_id not in stack.pending_settlement_ids
    assert next(item for item in stack.effect_frontier if item.effect_id == binding.effect_key).disposition == "refused"


@pytest.mark.asyncio
async def test_delivered_cancel_during_actual_admission_joins_no_sdk_settlement(operation_store, monkeypatch):
    _engine, repository, _authority, service, _sid = operation_store
    running, coordinator, binding, custody = make_custody(operation_store)
    admitted, release = asyncio.Event(), asyncio.Event()
    original_begin = service.begin_provider_attempt
    async def delayed_begin(**kwargs):
        actual = await original_begin(**kwargs)
        admitted.set()
        await release.wait()
        return actual
    monkeypatch.setattr(service, "begin_provider_attempt", delayed_begin)
    task = asyncio.create_task(custody.admit(model="test-model"))
    try:
        await asyncio.wait_for(admitted.wait(), timeout=5)
        task.cancel("actual admission cancellation")
        release.set()
        with pytest.raises(asyncio.CancelledError) as caught:
            await task
        assert caught.value.args == ("actual admission cancellation",)
        assert custody.attempt is None and custody.call is None
        assert_proven_no_sdk(operation_store, running, binding)
        coordinator.assert_completed()
    finally:
        release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_callback_refusal_has_no_sdk_and_one_real_zero_ledger(operation_store):
    _engine, repository, _authority, _service, _sid = operation_store
    running, coordinator, binding, custody = make_custody(operation_store)
    sdk_entries = []
    refusal = ValueError("controlled callback refusal")
    def refuse():
        raise refusal
    async def sdk(**kwargs):
        sdk_entries.append(kwargs)
        raise AssertionError("SDK must not start after callback refusal")
    try:
        with prompt_guard_user_scope("Read the actual plugin contract"), patch("litellm.acompletion", new=sdk):
            with pytest.raises(ValueError) as caught:
                await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer", provider_custody=custody, on_provider_dispatch=refuse,
                    model="test-model", messages=[{"role": "user", "content": "Read the actual plugin contract"}],
                )
        assert caught.value is refusal and sdk_entries == []
        assert_proven_no_sdk(operation_store, running, binding)
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)


@pytest.mark.asyncio
async def test_actual_sdk_unknown_usage_stays_unknown_and_cannot_yield(operation_store):
    engine, repository, authority, _service, _sid = operation_store
    running, coordinator, binding, custody = make_custody(operation_store)
    recorder = BufferingRecorder()
    sdk_entries = []
    messages = [{"role": "user", "content": "Read the actual plugin contract"}]
    async def sdk(**kwargs):
        sdk_entries.append(kwargs)
        return SimpleNamespace(usage=None)
    try:
        with prompt_guard_user_scope(messages[0]["content"]), patch("litellm.acompletion", new=sdk):
            async with provider_call_scope(custody):
                response = await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer", provider_custody=custody, model="test-model", messages=messages,
                )
                recorder.record_llm_call(build_llm_call_record(
                    model_requested="test-model", messages=messages, tools=None,
                    status=ComposerLLMCallStatus.SUCCESS, started_at=datetime.now(UTC), started_ns=time.monotonic_ns(),
                    temperature=None, seed=None, response=response, provider_custody=custody,
                ), provider_custody=custody)
        with engine.connect() as conn:
            actual = conn.execute(select(attempts).where(attempts.c.operation_id == running.session_operation_context.fence.operation_id)).one()
            usage = conn.execute(select(ledger).where(ledger.c.entry_id == actual.ledger_entry_id)).one()
            effect = conn.execute(select(effects).where(effects.c.effect_key == binding.effect_key,
                                                       effects.c.operation_id == running.claim.operation_id)).one()
        assert len(sdk_entries) == 1
        assert usage.prompt_tokens is None and usage.completion_tokens is None
        assert actual.settled_at is not None and effect.disposition == "unknown"
        stack = decode_continuation_snapshot(authority.load_continuation_snapshot(running).payload_json)
        assert not stack.is_handoff_safe
        coordinator.assert_completed()
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Bootstrap the complete packet and prove RED by temporarily omitting forwarding of `continuation_binding` from custody's undispatched cancellation. Both first tests must reach their persisted `refused` assertions and fail; the quota ledger itself still settles, exposing the join gap. Restore forwarding. Temporarily replace unknown audit measures with zeros only in a disposable controlled mutation and observe the third test's NULL assertion fail. Restore the original unknown measures before GREEN.
- [ ] Run the focused packet and existing quota/guard tests using normal pytest, requiring native exit 0. Commit only these tests and source fixes after both SQLite and serial PostgreSQL storage packets pass.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_provider_accounting.py tests/unit/web/composer/test_provider_quota.py tests/unit/web/composer/test_prompt_guard_admission.py >"$COMPOSER_LOG_DIR/c4b-physical-accounting.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c4b-physical-accounting.log"
test "$composer_exit" -eq 0
```

### C3.8c: Refuse unsupported effect owners before entering a handler

**Files:** Add `src/elspeth/web/composer/tools/continuation_support.py`; add the read-only registry facet below in `tools/_registry.py`; use it at the batch driver's pre-dispatch seam before handler preparation or invocation. The main batch packet owns the complete supported read-result application/receipt path.

| Handler boundary | V2 behavior |
| --- | --- |
| Ordinary catalog/session-state discovery | Execute with the retained read-result owner; retain exact response before application. |
| Graph/validation/YAML mutation in memory | Execute once from retained prepared dispatch; P4 audit/state hook retains actual SQL result atomically. |
| Direct/pipeline proposal creation | Existing SQL writer plus C3.8b actual proposal-result retention. |
| Secret reference discovery and graph wiring | Existing credential/current-policy validation remains active; graph changes use P4. No plaintext secret value is retained. |
| Advisor hint | Explicit resumable Advisor phase and actual provider custody; no generic synchronous handler replay. |
| `list_blobs`, `list_composer_blobs`, `get_blob_metadata`, `get_blob_content`, `read_attachment`, `inspect_source` | Existing current-session discovery/locked content owner, exact admitted response and observed version receipts from C3.8e; cold use requires fresh selected-contract/trust materialization. |
| Blob create/update/delete, upload preparation and other blob mutation | Precisely refuse before handler entry; these external writers have no continuation receipt in this delivery. |
| Interpretation-review staging | Existing exact session interpretation mutation owner plus C3.8d actual event-result retention. |
| Preview/runtime execution | Precisely refuse before dispatch until those external owners have durable receipts. |
| Unknown/new handler or unclassified effect domain | Refuse before dispatch; registry additions do not silently widen continuation support. |

- [ ] Add this concrete registry facet. It returns the existing nominal declaration without loading a handler through reflection. Keep it private to the composer tools package.

```python
def continuation_declaration(tool_name: str) -> ToolDeclaration | None:
    """Return the actual declaration for prospective continuation classification."""
    return _DECLARATIONS_BY_NAME.get(tool_name)
```

- [ ] Add the complete support classifier below. It classifies actual registered effects without executing a handler. `ToolEffects` is prospective and never serves as a commit receipt. Secret graph wiring stays supported because it changes only owned graph/reference state. The exact read-only blob allowlist uses C3.8e's actual observed-version owner; all other blob declarations refuse before dispatch.

```python
"""Prospective continuation support; never dispatch or current-use authority."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from elspeth.web.composer.tools._registry import ASYNC_TOOL_EFFECTS, continuation_declaration, resolve_tool_effects
from elspeth.web.composer.tools.declarations import EffectDomain, ToolKind

ContinuationToolSupport = Literal["read", "graph", "advisor", "session_sql", "unsupported"]


def continuation_tool_support(tool_name: str, arguments: Mapping[str, Any]) -> ContinuationToolSupport:
    if tool_name == "request_advisor_hint":
        effects = ASYNC_TOOL_EFFECTS.get(tool_name)
        return "advisor" if effects is not None and not effects.domains else "unsupported"
    if tool_name == "request_interpretation_review":
        effects = ASYNC_TOOL_EFFECTS.get(tool_name)
        return "session_sql" if effects is not None and effects.domains == (EffectDomain.INTERPRETATION,) else "unsupported"
    if tool_name == "preview_pipeline":
        return "unsupported"
    declaration = continuation_declaration(tool_name)
    if declaration is None:
        return "unsupported"
    domains = resolve_tool_effects(tool_name, arguments).domains
    if declaration.kind is ToolKind.BLOB_DISCOVERY:
        if (
            tool_name in ("list_blobs", "list_composer_blobs", "get_blob_metadata", "get_blob_content", "read_attachment", "inspect_source")
            and not domains
            and declaration.response_contract is not None
        ):
            return "read"
        return "unsupported"
    if declaration.kind is ToolKind.BLOB_MUTATION:
        return "unsupported"
    if any(domain not in (EffectDomain.GRAPH, EffectDomain.VALIDATION, EffectDomain.YAML) for domain in domains):
        return "unsupported"
    if domains:
        return "graph"
    if declaration.kind in (ToolKind.DISCOVERY, ToolKind.SECRET_DISCOVERY) and declaration.response_contract is not None:
        return "read"
    return "unsupported"
```

- [ ] The batch packet uses the actual existing failure-result constructor for unsupported handlers and records that precise refusal through its prepared tool effect. Insert this exact guard before entering any `execute_tool`, blob preparer, preview admission, session helper or nested provider branch; keep normal argument/manifest/current-policy validation before this guard. It never synthesizes graph structure.

```python
support = continuation_tool_support(tool_name, arguments)
if support == "unsupported":
    result = _failure_result(
        state, "This tool's effect owner does not support durable continuation. No handler was dispatched.",
        error_code="composer_continuation_effect_unsupported",
    )
```

The driver sends this `result` through its existing exact visible-tool/audit/P4 path and advances to the next call without entering the unsupported handler. This is a declared feature boundary, not a substitute for finishing normal graph/catalog/read receipts. Newly supported external handlers require their actual owner adapter before changing this classifier.

- [ ] Add these complete classifier controls to `tests/unit/web/composer/test_continuation_tool_support.py`. The main batch test additionally replaces the real unsupported handler with a raising counterexample and asserts it was never invoked.

```python
from elspeth.web.composer.tools.continuation_support import continuation_tool_support


def test_core_graph_catalog_and_advisor_remain_supported():
    assert continuation_tool_support("get_plugin_schema", {"plugin_type": "source", "name": "csv"}) == "read"
    assert continuation_tool_support("get_expression_grammar", {}) == "read"
    assert continuation_tool_support("set_pipeline", {"sources": {}, "nodes": [], "edges": [], "outputs": []}) == "graph"
    assert continuation_tool_support("request_advisor_hint", {}) == "advisor"


def test_unsupported_external_owners_are_refused_before_dispatch():
    assert continuation_tool_support("preview_pipeline", {}) == "unsupported"
    assert continuation_tool_support("request_interpretation_review", {}) == "session_sql"
    assert continuation_tool_support("create_blob", {}) == "unsupported"
    assert continuation_tool_support("get_blob_content", {}) == "read"
    assert continuation_tool_support("read_attachment", {}) == "read"
    assert continuation_tool_support("inspect_source", {}) == "read"
    assert continuation_tool_support("unregistered_future_handler", {}) == "unsupported"
```

- [ ] Bootstrap the importable packet, temporarily remove only the preview exclusion and observe the second test fail if it is classified as a read. Restore the exclusion. Run normal pytest and require native exit 0 before committing the support classifier, registry facet and tests with the actual batch integration.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_tool_support.py >"$COMPOSER_LOG_DIR/c3-tool-support.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-tool-support.log"
test "$composer_exit" -eq 0
```

### C3.8d: Retain the ordinary interpretation-review staging result under its actual session owner

**Files:** Modify `sessions/service.py::create_pending_interpretation_event` and `_prepare_or_create_pending_interpretation_event`, `coordination/repository.py::_RepositoryInterpretationMutations.create_or_reconcile_pending`, `sessions/protocol.py`'s service/mutation signatures and `composer/session_tool.py`'s two dispatch methods. Extend `sessions/continuation_owner_results.py` and the cold-restore tests. The actual handler still validates the staged draft, component, dedup/caps and current plugin/preflight inputs; this packet does not bypass those controls.

- [ ] Add these complete codecs to `continuation_owner_results.py`. `InterpretationEventRecord` is the actual frozen owned record in `elspeth.contracts.composer_interpretation`.

```python
from elspeth.contracts.composer_interpretation import InterpretationEventRecord

_INTERPRETATION = TypeAdapter(InterpretationEventRecord)


def encode_interpretation_owner_result(record: InterpretationEventRecord) -> str:
    if type(record) is not InterpretationEventRecord:
        raise TypeError("interpretation retention requires its actual nominal writer result")
    return canonical_json(_INTERPRETATION.dump_python(record, mode="json", warnings="error"))


def decode_interpretation_owner_result(encoded: str) -> InterpretationEventRecord:
    value, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained interpretation result is not strict JSON")
    try:
        result = _INTERPRETATION.validate_json(canonical_json(value), strict=True)
    except ValidationError as exc:
        raise AuditIntegrityError("retained interpretation result violates its owned codec") from exc
    if encode_interpretation_owner_result(result) != canonical_json(value):
        raise AuditIntegrityError("retained interpretation result lost fields during decode")
    return result
```

- [ ] Append optional `continuation: ContinuationOwnerCommit | None = None` to the two named service methods and matching protocol signatures. Forward it from `create_pending_interpretation_event` to `_prepare_or_create_pending_interpretation_event` and append it to the existing `transaction.interpretations.create_or_reconcile_pending(command, validator, continuation=continuation)` call in the latter's final mutation callback. Preserve the complete existing preparation and `_prepare_only` branch; v2 uses this commit only on the actual non-prepare mutation. No prepared callable, validator or service is serialized.
- [ ] Replace only `_RepositoryInterpretationMutations.create_or_reconcile_pending` with this full method and add its exact optional parameter to the mutation protocol. It uses the existing private mutation-connection registry and current context inside the existing transaction. Its complete canonical `_create_pending` implementation, pending reconciliation policy and fresh validator remain unchanged.

```python
def create_or_reconcile_pending(
    self, command: SessionPendingInterpretationCommand, validator: SessionPendingInterpretationValidator,
    *, continuation: ContinuationOwnerCommit | None = None,
) -> InterpretationEventRecord:
    from elspeth.web.sessions.continuation import require_owner_commit, retain_owner_result_on_connection
    from elspeth.web.sessions.continuation_owner_results import encode_interpretation_owner_result
    state = self.__state
    state._require_active()
    connection = _resolve_mutation_connection(state._connection_token)
    if continuation is not None:
        require_owner_commit(connection, continuation, state._operation_context)
    result = self._create_pending(command, validator, policy=PendingInterpretationPolicy.RECONCILE).event
    if continuation is not None:
        if str(result.session_id) != state._session_id:
            raise AuditIntegrityError("interpretation owner returned a foreign session")
        retain_owner_result_on_connection(
            connection, continuation, kind="interpretation", receipt_id=str(result.id),
            canonical_result=encode_interpretation_owner_result(result),
        )
    return result
```

- [ ] Append optional `continuation: ContinuationOwnerCommit | None = None` to `SessionToolOwner._dispatch_session_aware_tool` and `_build_session_aware_kwargs`. Forward it in the former's one call to the latter. In the latter's actual `functools.partial(sessions_service.create_pending_interpretation_event, session_operation_context=session_operation_context)` append `continuation=continuation`. The v2 batch supplies its actual prepared commit once; v1 passes the default. The exact retained DispatchAudit/tool-call ID remains the original call's identity across cold restore. After a committed pending owner result is restored, the driver decodes this actual event and applies the original handler's response projection rather than invoking the handler or its rate/dedup preparation again.

- [ ] Add this full real-service test. `_llm_node` is the existing production-serialized graph fixture helper, not an invented placeholder. It exercises a valid ordinary LLM interpretation site through the current session validator and mutation facade, then checks the retained actual event and repeat refusal.

```python
@pytest.mark.asyncio
async def test_interpretation_review_retains_actual_event_without_redispatch(operation_store):
    from elspeth.contracts.composer_interpretation import InterpretationKind
    from elspeth.web.sessions.continuation import ContinuationCASConflict, ContinuationOwnerCommit, prepare_effect_on_connection
    from elspeth.web.sessions.continuation_owner_results import decode_interpretation_owner_result
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import interpretation_events_table
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web.sessions.test_interpretation_events_service import _llm_node
    engine, repository, authority, service, sid = operation_store
    _record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    saved = await service.save_composition_state(
        sid, CompositionStateData(nodes=[_llm_node()], sources={}, edges=[], outputs=[]),
        provenance="session_seed", session_operation_context=running.session_operation_context,
    )
    selected = authority.load_continuation_snapshot(running)
    with locked_session_transaction(engine, str(sid)) as conn:
        prepared = prepare_effect_on_connection(
            conn, running, effect_key="response:1:interpretation:0", input_sha256="b" * 64, owner="session_sql",
            next_snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor,
        )
    commit = ContinuationOwnerCommit(running, "response:1:interpretation:0", "b" * 64, prepared.checkpoint_id, prepared.cursor)
    async def write():
        return await service.create_pending_interpretation_event(
            session_id=sid, composition_state_id=saved.id, affected_node_id="llm_transform_1",
            tool_call_id="actual_interpretation_call", user_term="cool", kind=InterpretationKind.VAGUE_TERM,
            llm_draft="Evaluate whether the content is composed and polite.", model_identifier="planner",
            model_version="planner-v1", provider="test", composer_skill_hash="a" * 64,
            session_operation_context=running.session_operation_context, continuation=commit,
        )
    try:
        actual = await write()
        selected = authority.load_continuation_snapshot(running)
        kind, encoded = authority.load_pending_owner_result(
            running, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor, effect_key=commit.effect_key,
        )
        assert kind == "interpretation" and decode_interpretation_owner_result(encoded) == actual
        with pytest.raises(ContinuationCASConflict):
            await write()
        with engine.connect() as conn:
            events = conn.execute(select(interpretation_events_table).where(interpretation_events_table.c.session_id == str(sid))).all()
        assert [row.id for row in events] == [str(actual.id)]
        assert not decode_continuation_snapshot(selected.payload_json).is_handoff_safe
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Bootstrap this importable packet, temporarily remove only the same-connection interpretation retention call and observe `load_pending_owner_result` refuse the absent selected result. Restore the hook. Run normal pytest and require native exit 0; add the same-name test import to the serial PostgreSQL continuation module before the persistence gate.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_interpretation_review_retains_actual_event_without_redispatch tests/unit/web/sessions/test_interpretation_events_service.py >"$COMPOSER_LOG_DIR/c3-interpretation-owner.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-interpretation-owner.log"
test "$composer_exit" -eq 0
```

### C3.3b: Refuse corrupt or malformed actual v2 base before fresh admission writes

- [ ] Add this complete shared SQL behavioral test to the existing authority module and export it through the PostgreSQL collector. The two fixtures distinguish publication corruption from malformed typed graph data with an internally consistent publication. No provider, graph reconstruction or recovery initializer runs. The precondition lock still validates principal/current base first. This fresh fixture explicitly has no quota expiry data, so complete registered-row equality measures refusal without confounding existing expiry cleanup. Separately populate expired quota rows in replay/disabled/refusal controls and measure their permitted cleanup alongside unchanged protected admission rows and no new charge.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("consistent_publication", [False, True])
async def test_malformed_v2_base_refuses_before_fresh_admission_writes(operation_store, consistent_publication):
    from dataclasses import replace
    from sqlalchemy import func, select, update
    from elspeth.contracts.composer_content import ContentPublication
    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata
    from elspeth.web.sessions.content_publication import read_content_publication, storage_content_hash
    from elspeth.web.sessions.models import (
        composition_states_table, metadata, rate_limit_buckets_table, rate_limit_events_table, sessions_table,
    )
    from elspeth.web.sessions.protocol import CompositionStateData
    from elspeth.web.sessions.state_envelope import envelope_state_column
    engine, repository, authority, service, sid = operation_store
    from elspeth.contracts.session_operation import SessionOperationKind
    # A normal prior owned row exists before the new request is admitted.
    prior = repository.acquire(session_id=sid, operation_kind=SessionOperationKind.COMPOSE,
                               owner_instance_id="prior-state-writer", lease_seconds=30)
    try:
        raw = CompositionState(nodes=(), edges=(), outputs=(), version=1, metadata=PipelineMetadata()).to_dict()
        saved = await service.save_composition_state(
            sid, CompositionStateData(sources=raw["sources"], nodes=raw["nodes"], edges=raw["edges"],
                                      outputs=raw["outputs"], metadata_=raw["metadata"]),
            provenance="session_seed", session_operation_context=prior,
        )
    finally:
        repository.release(prior)
    assert read_content_publication(saved) is not None
    node = NodeSpec(
        id="invalid-owned-node", node_type="transform", plugin="field_mapper", input="source",
        on_success="output", on_error="discard", options={}, condition=None, routes=None,
        fork_to=None, branches=None, policy=None, merge=None,
    )
    bad_nodes = CompositionState(nodes=(node,), edges=(), outputs=(), version=1,
                                 metadata=PipelineMetadata()).to_dict()["nodes"]
    # Only this container is malformed. Unknown node_type is not rejected by
    # the existing hydration boundary; graph semantic validation is separate.
    bad_nodes[0]["branches"] = 17
    changes = {"nodes": envelope_state_column(bad_nodes)}
    if consistent_publication:
        corrupted = replace(saved, nodes=bad_nodes)
        composer_meta = deep_thaw(saved.composer_meta)
        composer_meta["content_publication"] = ContentPublication(
            saved.session_id, saved.id, saved.version, storage_content_hash(corrupted),
        ).to_payload()
        changes["composer_meta"] = envelope_state_column(composer_meta)
    with engine.begin() as conn:
        conn.execute(update(composition_states_table).where(composition_states_table.c.id == str(saved.id)).values(**changes))
        initial_trust_mode = conn.execute(select(sessions_table.c.trust_mode).where(sessions_table.c.id == str(sid))).scalar_one()
        assert conn.execute(select(func.count()).select_from(rate_limit_buckets_table)).scalar_one() == 0
        assert conn.execute(select(func.count()).select_from(rate_limit_events_table)).scalar_one() == 0
    request = SendMessageRequest(operation_id=str(uuid4()), state_id=saved.id, content="Inspect this current graph")
    request_hash = composer_operation_v2_request_hash(session_id=sid, kind="compose_message", request=request)
    policy = minimal_continuation_policy()
    seed = prepare_composer_continuation_admission(session_id=sid, operation_id=request.operation_id,
        kind="compose_message", request_json=request.model_dump_json(), request_hash=request_hash,
        base_state_id=saved.id, policy_hash=policy.policy_hash, admission_policy_json=policy.model_dump_json(),
        initial_trust_mode=initial_trust_mode,
        initial_action_json=policy.actions.session_sql.model_dump_json())
    def rows():
        with engine.connect() as conn:
            return {
                table.name: sorted((tuple(row) for row in conn.execute(select(table))), key=repr)
                for table in metadata.sorted_tables
            }
    before = rows()
    expected = "base state cannot be hydrated" if consistent_publication else "Content publication does not match"
    with pytest.raises(AuditIntegrityError, match=expected) as refused:
        authority.admit(
            quota=ComposerQuotaAdmission(RepositoryRateLimitAuthority(engine, signing_key=b"operation-test-quota-key-32-bytes"), 10000),
            session_id=sid, operation_id=request.operation_id, kind="compose_message", request_hash=request_hash,
            actor_user_id="alice", request_id="malformed-base", base_state_id=saved.id,
            request_json=request.model_dump_json(), deadline_seconds=180, max_nonterminal=64,
            auth_provider_type="local", lifecycle_version=2, continuation_enabled=True, seed=seed,
        )
    if consistent_publication:
        assert type(refused.value.__cause__) is TypeError
    assert rows() == before
```

Run both cases through the existing authority unit module and real PostgreSQL
collector. For behavioral RED, remove only V2's actual hydration call in a
process-local copy; the internally consistent malformed graph must fail its
refusal assertion with native exit 1 before any provider is invoked. Restore
the complete hydration and independently retain the stale-publication refusal.
Do not catch or relabel `AuditIntegrityError` to satisfy a hydration-message
regex. Preserve legacy malformed-graph recovery tests unchanged.

### C3.8e: Retain immutable attachment and source observations through their actual read owners

**Files:** Extend `sessions/continuation_owner_results.py`, `sessions/continuation.py`, `composer/tools/_common.py`, `composer/tools/_dispatch.py`, `composer/tools/blobs.py`, `composer/tools/sources.py` and the C3 real-store tests. Install the pure value/observation classes during the initial increment. Install all four complete read-result functions only with actual B1's nominal result codec and C3.8c's support classifier at main step 9, before installing or testing the full C3.8b writer/pending-reader cluster: its unconditional decoder imports require these real definitions. Use the existing owned `BlobToolRecord` shape for observed blob inputs, with strict `ContinuationBlobVersion` validation; its type-only import avoids a ToolContext cycle and is not a runtime authority check. The read effect is prepared with `owner="read"`; its result never proves a mutation committed. Exact original bytes are observation provenance, while current authorization, response admission, trust materialization and Prompt Shield remain fresh at every restored use.


**Executable passive prerequisite after installed B1.** The actual registry facet, complete support classifier and all four read-result functions may be installed together before the full main-step-9 integration. They provide complete value admission and same-session row equality only: no current-user authorization, transaction/lock acquisition, observation forwarding, private-chunk/source binding, dispatch, result writer, or restored-use clearance. Keep every current `require_owner_commit` refusal and all service/protocol signatures unchanged. Qualify the ordinary `test_continuation_tool_support.py`, `test_continuation_read_owner_results.py` and its PostgreSQL re-export separately from later real-writer tests. B1's existing credential-state checks remain active with their current default empty `env_ref_names`. This passive API therefore refuses otherwise legitimate states containing credential references such as `${DB_URL}` until the real current metadata owner supplies their inventory; it does not yet support every live read state. Do not weaken that credential guard. Actual current secret-reference metadata and complete response credential checks belong to the later selected owner integration. No placeholder or partial public decoder is installed.

The classifier now requires the actual declaration, current selected response contract and empty effects before classifying a synchronous read; named blob reads do not bypass this registry join. Async classification checks its actual registered prospective effect shape. These are refusal-preserving source corrections, not new runtime eligibility. Read re-admission validates every retained observation against the actual `BLOB_STATUSES` and `CreationModality` vocabularies before admitting it, including on pure outer decode. It also refuses affected graph nodes and requires the complete B1 nominal re-encoding under the outer tool name to equal the retained value, preventing a compatible failed envelope from hiding a different inner tool name. The existing current-contract failure types and complete response comparison remain intact.

- [ ] Add these complete read DTOs and owner functions to `continuation_owner_results.py`. `response_json` is the complete actually admitted provider envelope, including validation/version metadata; it is never treated as a newly dispatched handler. The batch carrier retains the exact pre-call state and applies this envelope once. The selected response contract re-admits its data on both live retention and cold decode. Import `Connection`, `select` and `blobs_table` as shown.

```python
from collections.abc import Mapping
from typing import TYPE_CHECKING, Any

from pydantic import Field
from sqlalchemy import select
from sqlalchemy.engine import Connection

from elspeth.web.sessions.models import blobs_table

if TYPE_CHECKING:
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.composer.tools.blobs import BlobToolRecord


class ContinuationBlobVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    blob_id: UUID
    content_hash: str | None
    size_bytes: int = Field(ge=0)
    filename: str
    mime_type: str
    status: Literal["pending", "ready", "error"]
    created_by: Literal["user", "assistant", "pipeline"]
    creation_modality: str


class ContinuationReadObservations:
    __slots__ = ("_versions",)

    def __init__(self) -> None:
        self._versions: dict[UUID, ContinuationBlobVersion] = {}

    def observe_blob(self, record: BlobToolRecord) -> None:
        from elspeth.contracts.blobs import BLOB_STATUSES
        from elspeth.contracts.enums import CreationModality
        value = ContinuationBlobVersion.model_validate(
            {
                "blob_id": UUID(record["id"]), "content_hash": record["content_hash"], "size_bytes": record["size_bytes"],
                "filename": record["filename"], "mime_type": record["mime_type"], "status": record["status"],
                "created_by": record["created_by"], "creation_modality": record["creation_modality"],
            }
        )
        if value.status not in BLOB_STATUSES or value.creation_modality not in CreationModality:
            raise AuditIntegrityError("read observation contains an invalid recorded blob vocabulary")
        if value.blob_id in self._versions and self._versions[value.blob_id] != value:
            raise AuditIntegrityError("one read observed two versions of the same blob")
        self._versions[value.blob_id] = value

    @property
    def versions(self) -> tuple[ContinuationBlobVersion, ...]:
        return tuple(self._versions[key] for key in sorted(self._versions, key=str))


class ContinuationReadOwnerResult(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, frozen=True)
    kind: Literal["read"] = "read"
    tool_name: str
    arguments_json: str
    response_json: str
    tool_result_json: str
    blob_versions: tuple[ContinuationBlobVersion, ...]
    prescribing_provider_call_id: str | None = None


def re_admit_read_owner_result(result: ContinuationReadOwnerResult) -> dict[str, Any]:
    from elspeth.contracts.blobs import BLOB_STATUSES
    from elspeth.contracts.enums import CreationModality
    from elspeth.web.composer.continuation_tool_batch import decode_tool_result_json, encode_tool_result_value
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._registry import response_contract_for
    from elspeth.web.composer.tools.continuation_support import continuation_tool_support

    arguments, argument_error = parse_json_strict(result.arguments_json)
    envelope, response_error = parse_json_strict(result.response_json)
    if (
        argument_error is not None
        or type(arguments) is not dict
        or response_error is not None
        or type(envelope) is not dict
        or type(envelope.get("success")) is not bool
    ):
        raise AuditIntegrityError("retained read has an invalid arguments/response envelope")
    if continuation_tool_support(result.tool_name, arguments) != "read":
        raise AuditIntegrityError("retained read no longer has a supported actual read owner")
    contract = response_contract_for(result.tool_name)
    if contract is None:
        raise AuditIntegrityError("retained read no longer has its selected response contract")
    if envelope["success"]:
        if "data" not in envelope:
            raise AuditIntegrityError("retained successful read has no actual data")
        admitted = contract.admit(envelope["data"])
        if canonical_json(admitted.to_wire()) != canonical_json(envelope["data"]):
            raise AuditIntegrityError("retained read changed under current selected response admission")
    elif "data" in envelope:
        raise AuditIntegrityError("retained failed read invented data")
    nominal = decode_tool_result_json(result.tool_result_json)
    if nominal.affected_nodes:
        raise AuditIntegrityError("retained read contains an authored graph effect")
    if canonical_json(encode_tool_result_value(result.tool_name, nominal).model_dump(mode="json")) != result.tool_result_json:
        raise AuditIntegrityError("retained read changed its complete selected nominal result")
    if canonical_json(admit_discovery_result(result.tool_name, nominal).to_dict()) != result.response_json:
        raise AuditIntegrityError("retained read envelope differs from its actual complete nominal result")
    for observed in result.blob_versions:
        if observed.status not in BLOB_STATUSES or observed.creation_modality not in CreationModality:
            raise AuditIntegrityError("retained read contains an invalid recorded blob vocabulary")
    if len({item.blob_id for item in result.blob_versions}) != len(result.blob_versions):
        raise AuditIntegrityError("retained read duplicated a blob observation")
    return envelope


def decode_read_owner_result(encoded: str) -> ContinuationReadOwnerResult:
    parsed, error = parse_json_strict(encoded)
    if error is not None:
        raise AuditIntegrityError("retained read is not strict JSON")
    try:
        result = ContinuationReadOwnerResult.model_validate_json(canonical_json(parsed))
    except ValidationError as exc:
        raise AuditIntegrityError("retained read violates its owned codec") from exc
    if canonical_json(result.model_dump(mode="json")) != canonical_json(parsed):
        raise AuditIntegrityError("retained read lost fields during decode")
    re_admit_read_owner_result(result)
    return result


def build_read_owner_result(
    *,
    tool_name: str,
    arguments: Mapping[str, Any],
    result: ToolResult,
    pre_state: CompositionState,
    observations: ContinuationReadObservations,
) -> ContinuationReadOwnerResult:
    from elspeth.web.composer.continuation_tool_batch import encode_tool_result_value
    from elspeth.web.composer.discovery_response import admit_discovery_result
    from elspeth.web.composer.tools._common import ToolResult
    from elspeth.web.composer.tools.continuation_support import continuation_tool_support

    if (
        type(result) is not ToolResult
        or result.updated_state is not pre_state
        or result.affected_nodes
        or continuation_tool_support(tool_name, arguments) != "read"
        or type(observations) is not ContinuationReadObservations
    ):
        raise AuditIntegrityError("read owner did not receive the actual nonmutating handler result")
    admitted = admit_discovery_result(tool_name, result)
    value = ContinuationReadOwnerResult(
        tool_name=tool_name,
        arguments_json=canonical_json(arguments),
        response_json=canonical_json(admitted.to_dict()),
        blob_versions=observations.versions,
        tool_result_json=canonical_json(encode_tool_result_value(tool_name, result).model_dump(mode="json")),
    )
    re_admit_read_owner_result(value)
    return value


def require_read_versions_on_connection(conn: Connection, *, session_id: str, result: ContinuationReadOwnerResult) -> None:
    from elspeth.web.blobs.service import _guard_blob_row_literals

    re_admit_read_owner_result(result)
    for observed in result.blob_versions:
        row = conn.execute(
            select(blobs_table).where(
                blobs_table.c.session_id == session_id,
                blobs_table.c.id == str(observed.blob_id),
            )
        ).one_or_none()
        if row is None:
            raise AuditIntegrityError("retained blob observation is no longer present in this session")
        _guard_blob_row_literals(row)
        actual = ContinuationBlobVersion(
            blob_id=UUID(row.id),
            content_hash=row.content_hash,
            size_bytes=row.size_bytes,
            filename=row.filename,
            mime_type=row.mime_type,
            status=row.status,
            created_by=row.created_by,
            creation_modality=row.creation_modality,
        )
        if actual != observed:
            raise AuditIntegrityError("retained blob observation differs from its current immutable version")
```

- [ ] Add `continuation_read_observations: ContinuationReadObservations | None = None` to the existing `ToolContext` and `execute_tool` keyword parameters. Import this concrete class normally in `_common.py` and `_dispatch.py`; `continuation_owner_results.py` imports `ToolResult` only inside `build_read_owner_result`, avoiding a module cycle. Forward the field in `execute_tool`'s actual `context = ToolContext(...)` constructor. Legacy callers preserve `None`. The batch packet creates exactly one observations owner for each prepared read and passes it through the actual ordinary dispatch.

- [ ] In `blobs.py`, add optional keyword-only `observations: ContinuationReadObservations | None = None` to `_sync_get_blob`, `_sync_list_blobs`, and `_sync_list_ready_blob_inline_descriptors`, with a normal concrete import. Apply these exact edits at the named existing seams:

```python
# _sync_get_blob: replace only its final return statement.
record = _blob_row_to_tool_dict(row)
if observations is not None:
    observations.observe_blob(record)
return record
```

```python
# _sync_list_blobs: insert after fetchall(), inside the existing connection.
if observations is not None:
    for row in rows:
        observations.observe_blob(_blob_row_to_tool_dict(row))
```

```python
# _sync_list_ready_blob_inline_descriptors: immediately after blob conversion.
if observations is not None:
    observations.observe_blob(blob)
```

Forward `observations=context.continuation_read_observations` in the three corresponding metadata handlers' existing calls. For `_execute_get_blob_content` and `_execute_read_attachment`, insert this identical block immediately after their actual `if data is None: return _failure_result(...)` arm, before decoding/credential admission. Those handlers already read/hash the same ready row and bytes under `_locked_read_ready_blob`'s existing actual custody lock. No credential/size/hash/origin/whole-attachment/page guard is removed.

```python
if context.continuation_read_observations is not None:
    context.continuation_read_observations.observe_blob(blob)
```

- [ ] Make v2 `inspect_source` use the same actual blob service custody while preserving its bounded streaming read and v1 behavior. Add the complete helper below to `tools/sources.py`; import `BlobServiceImpl`, `BlobNotFoundError`, `_blob_record_to_tool_dict`, `SessionOperationContext` and `AuditIntegrityError` from their existing canonical modules used by `blobs.py`. Replace only `_execute_inspect_source`'s initial `blob = _sync_get_blob(...)` statement with the following block. Keep the existing not-found/status guards immediately after it unchanged.

```python
data: bytes | None = None
if context.continuation_read_observations is not None:
    blob, data = _locked_read_inspection_blob(context, blob_id)
else:
    blob = _sync_get_blob(context.session_engine, blob_id, context.session_id)
```

Indent its exact existing contiguous span from `storage_path = Path(blob["storage_path"])` through `_verify_blob_content_hash(blob, actual_hash)` under `if context.continuation_read_observations is None:`. This preserves the complete legacy lookup/streaming behavior. Immediately after that conditional, add the exact missing-file guard below; then keep the complete existing facts/encoding/credential/result body unchanged. Every v2 success therefore has full SHA-256 verified under its actual lock, and every v1 path follows its prior owner.

```python
if data is None:
    return _failure_result(state, f"Blob storage file missing for '{blob_id}'.")
```

```python
def _locked_read_inspection_blob(context: ToolContext, blob_id: str) -> tuple[BlobToolRecord | None, bytes | None]:
    if (context.session_engine is None or context.session_id is None or context.data_dir is None
        or type(context.session_operation_context) is not SessionOperationContext
        or context.session_operation_authority is None
        or context.session_operation_context.fence.session_id != context.session_id):
        raise AuditIntegrityError("source inspection requires the actual current session/blob custody")
    service = BlobServiceImpl(context.session_engine, Path(context.data_dir),
                              session_operation_authority=context.session_operation_authority)
    try:
        with service._locked_blob_row_for_read(blob_id, context.session_operation_context) as row:
            record = _blob_record_to_tool_dict(row)
            if record["status"] != "ready":
                return record, None
            try:
                prefix, actual_hash = _read_inspection_prefix_and_hash(Path(row.storage_path))
            except FileNotFoundError:
                return record, None
            _verify_blob_content_hash(record, actual_hash)
            if context.continuation_read_observations is not None:
                context.continuation_read_observations.observe_blob(record)
            return record, prefix
    except BlobNotFoundError:
        return None, None
```

- [ ] Add this complete authority write facet. The real batch read stage calls it through its existing required SQL finish-once owner, with the selected prepared checkpoint returned by effect preparation. A concurrent changed blob or late snapshot transition refuses the entire retention transaction; the caller reloads and reconciles, never blesses its old envelope with the latest checkpoint. Import `ContinuationReadOwnerResult` for this method's nominal parameter and expose this exact method on the authority used by the batch driver.

```python
def retain_read_owner_result(self, commit: ContinuationOwnerCommit, *, result: ContinuationReadOwnerResult) -> StoredContinuationSnapshot:
    from elspeth.web.sessions.continuation_owner_results import ContinuationReadOwnerResult
    from elspeth.web.composer.advisor_output import ReadContinuationAction
    from elspeth.web.composer.continuation import ComposeFrame
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    if type(result) is not ContinuationReadOwnerResult:
        raise TypeError("read retention requires the actual selected owner result")
    with locked_session_transaction(self._engine, str(commit.running.claim.session_id)) as conn:
        stored = require_owner_commit(conn, commit, commit.running.session_operation_context, allow_completed_read=True)
        root = decode_continuation_snapshot(stored.payload_json)
        if commit.tool_completion is None and root.active_action_json is not None:
            action = decode_prescribed_continuation_action(root.active_action_json)
            if (type(root.frames[-1]) is not ComposeFrame or type(action) is not ReadContinuationAction
                or action.tool_name != result.tool_name or action.arguments_json != result.arguments_json):
                raise AuditIntegrityError("direct read result differs from its exact active prescribed owner")
            actual_call_id = require_continuation_action_source_on_connection(conn, commit.running, stored=stored)
            if result.prescribing_provider_call_id not in (None, actual_call_id):
                raise AuditIntegrityError("direct read result substituted another prescribing physical call")
            result = result.model_copy(update={"prescribing_provider_call_id": actual_call_id})
        elif result.prescribing_provider_call_id is not None:
            raise AuditIntegrityError("ordinary tool read invented a prescribing Advisor call")
        return retain_owner_result_on_connection(
            conn, commit, kind="read", receipt_id=commit.effect_key,
            canonical_result=canonical_json(result.model_dump(mode="json")),
        )
```

`load_pending_owner_result` in C3.8b already includes its exact read-kind decoder/version check inside that same selected-head transaction. This supplies current same-session/version authorization before pure cold application. The batch packet then re-admits the selected response and runs the actual current-context trust/guard materializer; prior provider delivery or guard receipts do not grant clearance.

- [ ] Add this complete real-store test to `tests/unit/web/sessions/test_continuation_cold_restore.py`, adding an explicit fixture import `from tests.unit.web.composer.conftest import tool_context as tool_context` at module scope. It enters the real existing locked content readers using a real continuation fence, retains their exact actual response and verifies that a later row version cannot be silently replayed. It also checks bounded source inspection's actual full-content identity rather than its eight-character display prefix. Add the same test and `tool_context` fixture imports to the PostgreSQL module; the storage path is lane-private pytest temporary data, never production storage.

```python
@pytest.mark.parametrize("tool_name", ("read_attachment", "get_blob_content", "inspect_source"))
def test_blob_read_owner_retains_actual_version_and_refuses_changed_cold_read(operation_store, tmp_path, tool_context, tool_name):
    import hashlib
    from dataclasses import replace
    from datetime import UTC, datetime
    from sqlalchemy import insert, update
    from elspeth.contracts.hashing import canonical_json
    from elspeth.web.composer.state import CompositionState
    from elspeth.web.composer.tools.blobs import _execute_get_blob_content, _execute_read_attachment
    from elspeth.web.composer.tools.sources import _execute_inspect_source
    from elspeth.web.sessions.models import blobs_table
    from elspeth.web.sessions.continuation import ContinuationOwnerCommit, load_snapshot, prepare_effect_on_connection
    from elspeth.web.sessions.continuation_owner_results import (
        ContinuationReadObservations, build_read_owner_result, decode_read_owner_result,
    )
    engine, repository, authority, service, sid = operation_store
    record, _seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    blob_id = str(uuid4())
    body = b"customer_id,amount\nA,7\n"
    path = tmp_path / "uploaded.csv"
    path.write_bytes(body)
    digest = hashlib.sha256(body).hexdigest()
    with engine.begin() as conn:
        conn.execute(insert(blobs_table).values(id=blob_id, session_id=str(sid), filename=path.name,
            mime_type="text/csv", size_bytes=len(body), content_hash=digest, storage_path=str(path),
            created_at=datetime.now(UTC), created_by="user", creation_modality="verbatim", status="ready"))
        stored = load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id)
        prepared = prepare_effect_on_connection(conn, running, effect_key="tool:blob-read", input_sha256="e" * 64,
            owner="read", next_snapshot_json=stored.payload_json,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    observations = ContinuationReadObservations()
    state = CompositionState.from_dict({"sources": {}, "nodes": [], "edges": [], "outputs": [], "version": 1})
    context = replace(tool_context, session_engine=engine, session_id=str(sid),
        data_dir=str(tmp_path), session_operation_context=running.session_operation_context,
        session_operation_authority=repository, continuation_read_observations=observations)
    handler = {"read_attachment": _execute_read_attachment, "get_blob_content": _execute_get_blob_content,
               "inspect_source": _execute_inspect_source}[tool_name]
    actual = handler({"blob_id": blob_id}, state, context)
    assert actual.success
    result = build_read_owner_result(tool_name=tool_name, arguments={"blob_id": blob_id}, result=actual,
                                     pre_state=state, observations=observations)
    assert result.blob_versions[0].content_hash == digest
    assert decode_read_owner_result(canonical_json(result.model_dump(mode="json"))) == result
    commit = ContinuationOwnerCommit(running, "tool:blob-read", "e" * 64, prepared.checkpoint_id, prepared.cursor)
    retained = authority.retain_read_owner_result(commit, result=result)
    kind, encoded = authority.load_pending_owner_result(running, expected_checkpoint_id=retained.checkpoint_id,
                                                       expected_cursor=retained.cursor, effect_key=commit.effect_key)
    assert kind == "read" and decode_read_owner_result(encoded) == result
    with engine.begin() as conn:
        conn.execute(update(blobs_table).where(blobs_table.c.id == blob_id).values(content_hash="a" * 64))
    with pytest.raises(AuditIntegrityError, match="immutable version"):
        authority.load_pending_owner_result(running, expected_checkpoint_id=retained.checkpoint_id,
                                           expected_cursor=retained.cursor, effect_key=commit.effect_key)
    with engine.connect() as conn:
        assert load_snapshot(conn, session_id=str(sid), operation_id=record.operation_id) == retained
    repository.release(running.session_operation_context)
```

- [ ] Add this complete actual-writer proposal credential control in the same cold-restore module. It uses the existing field-mapper helper, real ingress/proposal writers and selected-head effect binding. The unsafe case proves the writer transaction leaves both result rows and complete snapshot unchanged. No generic graph-field exemption is added.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("with_password", (False, True))
async def test_proposal_retention_uses_tool_specific_credential_projection(operation_store, with_password):
    from tests.unit.web.test_credential_guard import _field_mapper_node
    from elspeth.web.credential_guard import CredentialMaterialRefused
    from elspeth.web.sessions.continuation import ContinuationOwnerCommit, prepare_effect_on_connection
    from elspeth.web.sessions.continuation_owner_results import decode_proposal_owner_result
    from elspeth.web.sessions.models import composition_proposals_table, proposal_events_table
    engine, repository, authority, service, sid = operation_store
    record, seed, _action = admit_continuation(operation_store)
    running = start_continuation(repository, authority)
    ingress = await service.add_user_message_for_operation(
        session_id=sid, content="Preserve café and exact context", request_id="field-mapper-continuation",
        operation_id=record.operation_id, reserved_user_message_id=UUID(seed.reserved_user_message_id),
        session_operation_context=running.session_operation_context,
    )
    selected = authority.load_continuation_snapshot(running)
    with engine.begin() as conn:
        prepared = prepare_effect_on_connection(conn, running, effect_key="tool:proposal-fields", input_sha256="e" * 64,
            owner="session_sql", next_snapshot_json=selected.payload_json,
            expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
    commit = ContinuationOwnerCommit(running, "tool:proposal-fields", "e" * 64, prepared.checkpoint_id, prepared.cursor)
    node = _field_mapper_node({"customer_key": "customer_id"})
    if with_password:
        node["options"]["password"] = "low-entropy-secret"
    arguments = {"sources": {}, "nodes": [node], "edges": [], "outputs": []}
    def rows():
        with engine.connect() as conn:
            return (conn.execute(select(composition_proposals_table).where(
                        composition_proposals_table.c.session_id == str(sid))).all(),
                    conn.execute(select(proposal_events_table).where(proposal_events_table.c.session_id == str(sid))).all())
    before = rows()
    async def write():
        return await service.create_composition_proposal(
            session_id=sid, tool_call_id="field_mapper_call", tool_name="set_pipeline", summary="Map customer fields",
            rationale="User request", affects=("graph",), arguments_json=arguments, arguments_redacted_json=arguments,
            base_state_id=None, actor="composer-web:user:alice", user_message_id=ingress.message.id,
            session_operation_context=running.session_operation_context, continuation=commit,
        )
    try:
        if with_password:
            with pytest.raises(CredentialMaterialRefused):
                await write()
            assert rows() == before
            assert authority.load_continuation_snapshot(running) == prepared
        else:
            actual = await write()
            retained = authority.load_continuation_snapshot(running)
            kind, encoded = authority.load_pending_owner_result(running, expected_checkpoint_id=retained.checkpoint_id,
                expected_cursor=retained.cursor, effect_key=commit.effect_key)
            decoded = decode_proposal_owner_result(encoded)
            assert kind == "proposal" and decoded == actual
            assert decoded.arguments_json["nodes"][0]["options"]["mapping"] == {"customer_key": "customer_id"}
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Establish behavioral RED by temporarily skipping only the actual-row version comparison in `require_read_versions_on_connection`; the changed-version cold-read assertion must fail with native exit 1. Restore it, run the exact SQLite/native selected tests, then the PostgreSQL selection when the integration owner grants test capacity. The existing mixed-version blob reader contention tests remain in the affected gate because source inspection now shares that actual lock.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py tests/unit/web/test_credential_guard.py tests/unit/web/composer/test_tools_blobs.py tests/unit/web/composer/test_source_inspection.py >"$COMPOSER_LOG_DIR/c3-read-owner.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-read-owner.log"
test "$composer_exit" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -m testcontainer -n 0 tests/testcontainer/web/test_composer_continuation_postgres.py >"$COMPOSER_LOG_DIR/c3-read-owner-pg.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c3-read-owner-pg.log"
test "$composer_exit" -eq 0
```

### C5.7: Reconcile the original physical attempt under fresh current authorization

**Files:** Extend `sessions/continuation.py`, `coordination/quota_authority.py`, `sessions/service.py`, `sessions/protocol.py`, and the actual response/custody hooks. This packet distinguishes stable prepared-effect creator from the creator of its one actual physical quota attempt. `effects.physical_sequence/physical_generation`, requested model and response finish time are included in C2's table, and C5.3 sets the physical bundle only when actual quota admission commits. A prepared effect without an actual attempt can resume under a new current fence; its original creator and stable action receipt identity never change.

- [ ] In `ProviderCallCustody.bind_call`, immediately after its existing `_bind_attempt_call(...)` assignment and before assigning `self.call`, insert the block below. The response-retention method captures this DB-admission-plus-monotonic finish instant before response SQL. Both normal and recovered usage therefore use the same actual completion instant and UTC day; no recovery clock replaces it. Import `datetime` in the custody module's existing datetime import.

```python
if self._continuation_finished_at is not None:
    bound = replace(bound, finished_at=self._continuation_finished_at)
```

- [ ] Refactor the existing proven-undispatched quota writer into a shared actual-row writer. Move the complete contiguous body from `owner = connection.execute(select(sessions_table.c.user_id)...` through its final successful receipt return in `cancel_undispatched_provider_attempt_on_connection` into `_cancel_undispatched_provider_attempt_for_row(connection:Connection, *, attempt:Row[Any], requested_model:str, append_audit_event:Callable[[str,str,datetime],None])->UndispatchedProviderReceipt`. Prepend this exact local binding/validation to that moved body, then replace only `fence.session_id/operation_id/operation_epoch/lease_token` references in the preserved span with `attempt.session_id/operation_id/operation_epoch/lease_token`. Keep every existing event/ledger/time/identity/source/run/measure replay check and insert unchanged.

```python
attempt_id = attempt.attempt_id
if type(requested_model) is not str or not requested_model.strip():
    raise ValueError("requested_model must be a non-blank exact string")
```

At the original function's relocation point, return the following. This preserves v1's exact live-custody contract; the new recovery function below supplies an independently proved actual historical row and never constructs an old live lease.

```python
return _cancel_undispatched_provider_attempt_for_row(
    connection, attempt=attempt, requested_model=requested_model, append_audit_event=append_audit_event,
)
```

- [ ] Add the complete financial recovery owner below in `quota_authority.py`. Add `Row` to its SQLAlchemy imports. It validates the current live continuation authority and original physical attempt history in the same caller-owned transaction. Marker absence proves no SDK only after that original owner was revoked by the actual recovery transaction; a pending attempt row alone proves nothing. A retained complete response supplies actual admitted usage; an unretained dispatched result supplies unknown measures. No fake `ComposerLLMCall` or old `SessionOperationContext` is created.

```python
@dataclass(frozen=True, slots=True)
class RecoveredProviderSettlement:
    attempt_id: str
    ledger_entry_id: str | None
    disposition: Literal["committed", "refused", "unknown", "owner_active"]
    audit_event_id: str | None


def reconcile_interrupted_provider_attempt_on_connection(
    connection: Connection, *, running: ComposerOperationRunning, effect_key: str,
    append_audit_event: Callable[[str, str, datetime], None],
) -> RecoveredProviderSettlement:
    from elspeth.web.composer.continuation import PrivateChunkRef
    from elspeth.web.composer.continuation_codec import PrivateChunk, decode_retained_provider_metadata
    from elspeth.web.sessions.continuation import read_chunk, require_continuation_mutation
    from elspeth.web.sessions.models import (
        composer_continuation_effects_table as effects,
        composer_continuation_chunks_table as chunks,
        composer_segment_attempts_table as history,
    )
    require_continuation_mutation(connection, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = connection.execute(select(effects).where(
        effects.c.session_id == sid, effects.c.operation_id == operation_id, effects.c.effect_key == effect_key,
    )).one()
    actual = connection.execute(select(quota_provider_attempts_table).where(
        quota_provider_attempts_table.c.attempt_id == effect.provider_attempt_id,
    ).with_for_update()).one()
    creator = connection.execute(select(history).where(
        history.c.session_id == sid, history.c.operation_id == operation_id,
        history.c.sequence == effect.physical_sequence, history.c.generation == effect.physical_generation,
    )).one()
    if (effect.owner != "provider" or actual.source not in ("composer", "auto_title") or actual.run_id is not None
        or (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token)
           != (sid, creator.session_operation_id, creator.session_operation_epoch, creator.session_operation_lease_token)
        or effect.requested_model is None):
        raise AuditIntegrityError("provider recovery changed the actual physical attempt creator/source")
    if actual.settled_at is not None:
        cancellation = connection.execute(select(chat_messages_table.c.id).where(
            chat_messages_table.c.id == _undispatched_cancellation_event_id(actual.attempt_id),
        )).one_or_none()
        if cancellation is not None:
            verified = _cancel_undispatched_provider_attempt_for_row(
                connection, attempt=actual, requested_model=effect.requested_model, append_audit_event=append_audit_event,
            )
            return RecoveredProviderSettlement(actual.attempt_id, verified.ledger_entry_id, "refused", verified.cancellation_event_id)
        ledger = connection.execute(select(token_usage_ledger_table).where(
            token_usage_ledger_table.c.entry_id == actual.ledger_entry_id,
        )).one()
        if (ledger.identity_id != actual.identity_id or ledger.session_id != sid
            or ledger.source != actual.source or ledger.run_id != actual.run_id):
            raise AuditIntegrityError("settled provider recovery lacks its actual identity/source ledger")
        if effect.result_chunk_key is not None:
            stored = connection.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id,
                                                            chunks.c.chunk_key == effect.result_chunk_key)).one()
            canonical = read_chunk(connection, session_id=sid, operation_id=operation_id,
                                   key=effect.result_chunk_key).decode("utf-8", errors="strict")
            metadata = decode_retained_provider_metadata(PrivateChunk(
                PrivateChunkRef(codec=stored.codec, digest=stored.sha256, byte_length=stored.byte_length), canonical))
            if (effect.response_finished_at is None
                or (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens)
                   != (metadata.usage.prompt_tokens, metadata.usage.completion_tokens,
                       metadata.usage.cached_prompt_tokens, metadata.usage.reasoning_tokens)
                or ledger.model != (metadata.model_returned if metadata.model_returned is not None else effect.requested_model)
                or restore_utc(ledger.recorded_at) != restore_utc(effect.response_finished_at)):
                raise AuditIntegrityError("retained response contradicts the actual settled financial receipt")
        disposition = "refused" if effect.disposition == "refused" else "committed" if effect.result_chunk_key is not None else "unknown"
        return RecoveredProviderSettlement(actual.attempt_id, actual.ledger_entry_id, disposition, None)
    if creator.revoked_at is None or creator.outcome != "interrupted":
        return RecoveredProviderSettlement(actual.attempt_id, None, "owner_active", None)
    if effect.disposition == "prepared":
        receipt = _cancel_undispatched_provider_attempt_for_row(
            connection, attempt=actual, requested_model=effect.requested_model, append_audit_event=append_audit_event,
        )
        return RecoveredProviderSettlement(actual.attempt_id, receipt.ledger_entry_id, "refused", receipt.cancellation_event_id)
    now = database_now(connection)
    if effect.result_chunk_key is not None:
        stored = connection.execute(select(chunks).where(
            chunks.c.session_id == sid, chunks.c.operation_id == operation_id,
            chunks.c.chunk_key == effect.result_chunk_key,
        )).one()
        canonical = read_chunk(connection, session_id=sid, operation_id=operation_id,
                               key=effect.result_chunk_key).decode("utf-8", errors="strict")
        chunk = PrivateChunk(PrivateChunkRef(codec=stored.codec, digest=stored.sha256, byte_length=stored.byte_length), canonical)
        metadata = decode_retained_provider_metadata(chunk)
        if effect.response_finished_at is None:
            raise AuditIntegrityError("retained response lost its actual anchored finish instant")
        usage = metadata.usage
        entry = TokenUsageEntry(
            model=metadata.model_returned if metadata.model_returned is not None else effect.requested_model,
            prompt_tokens=usage.prompt_tokens, completion_tokens=usage.completion_tokens,
            cached_prompt_tokens=usage.cached_prompt_tokens, reasoning_tokens=usage.reasoning_tokens,
            recorded_at=restore_utc(effect.response_finished_at), call_id=actual.attempt_id,
        )
        disposition = "committed"
    else:
        entry = TokenUsageEntry(model=effect.requested_model, prompt_tokens=None, completion_tokens=None,
                                cached_prompt_tokens=None, reasoning_tokens=None, recorded_at=now, call_id=actual.attempt_id)
        disposition = "unknown"
    event_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"elspeth:provider-attempt:recovered:{actual.attempt_id}"))
    if entry.recorded_at is None:
        raise AuditIntegrityError("recovered physical usage lost its authoritative observation timestamp")
    payload = canonical_json({"kind": "provider_attempt.recovered", "attempt_id": actual.attempt_id,
                              "source": actual.source, "run_id": actual.run_id, "disposition": disposition,
                              "physical_operation_id": actual.operation_id, "physical_operation_epoch": actual.operation_epoch,
                              "response_chunk_key": effect.result_chunk_key,
                              "model": entry.model, "prompt_tokens": entry.prompt_tokens,
                              "completion_tokens": entry.completion_tokens, "cached_prompt_tokens": entry.cached_prompt_tokens,
                              "reasoning_tokens": entry.reasoning_tokens, "recorded_at": entry.recorded_at.isoformat()})
    append_audit_event(event_id, payload, now)
    settle_provider_attempt_on_connection(connection, session_id=sid, attempt_id=actual.attempt_id, entry=entry)
    settled = connection.execute(select(quota_provider_attempts_table.c.ledger_entry_id).where(
        quota_provider_attempts_table.c.attempt_id == actual.attempt_id,
    )).scalar_one()
    return RecoveredProviderSettlement(actual.attempt_id, settled, disposition, event_id)
```

Import `restore_utc` from `sessions/time_normalization.py` in `quota_authority.py`. Add the nominal `ComposerOperationRunning` import under `TYPE_CHECKING` there; module future annotations already defer it. The physical row's `byte_length` comes from the actual continuation chunk schema, not a guessed bound.

- [ ] Add the following complete continuation reconciliation adapter. It derives the original durable provider family/ordinal from the canonical key; it never allocates another physical ordinal just to recover an existing one. Ready prepared effects retain their immutable creator but acquire a separate actual physical creator only at a future real quota admission. Unknown outcomes are retained and returned as actionable failure custody; callers must suspend/back off or follow fresh evidence, never replay the SDK blindly.

```python
@dataclass(frozen=True, slots=True)
class ProviderEffectReconciliation:
    binding: ContinuationProviderBinding
    disposition: Literal["ready", "committed", "refused", "unknown", "owner_active"]
    attempt_id: str | None
    ledger_entry_id: str | None
    response: PrivateChunk | None
    snapshot: StoredContinuationSnapshot


def reconcile_provider_effect_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, effect_key: str,
    append_audit_event: Callable[[str, str, datetime], None],
) -> ProviderEffectReconciliation:
    from elspeth.web.coordination.quota_authority import reconcile_interrupted_provider_attempt_on_connection
    require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    effect = conn.execute(select(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id,
                                                effects.c.effect_key == effect_key)).one()
    parts = effect_key.split(":")
    if len(parts) != 3 or parts[0] != "provider":
        raise AuditIntegrityError("restored provider effect key is not canonical")
    try:
        family, ordinal = int(parts[1]), int(parts[2])
    except ValueError as exc:
        raise AuditIntegrityError("restored provider effect ordinal is not an owned integer") from exc
    if family not in (0, 1, 2, 3) or ordinal < 0 or effect_key != f"provider:{family}:{ordinal}" or effect.owner != "provider":
        raise AuditIntegrityError("restored provider effect family/ordinal changed")
    binding = ContinuationProviderBinding(running, effect_key, family, ordinal, effect.input_sha256)
    if effect.provider_attempt_id is None:
        if effect.disposition != "prepared":
            raise AuditIntegrityError("unadmitted provider effect has an impossible disposition")
        return ProviderEffectReconciliation(binding, "ready", None, None, None,
            load_snapshot(conn, session_id=sid, operation_id=operation_id))
    receipt = reconcile_interrupted_provider_attempt_on_connection(
        conn, running=running, effect_key=effect_key, append_audit_event=append_audit_event,
    )
    if receipt.disposition == "owner_active":
        return ProviderEffectReconciliation(binding, "owner_active", receipt.attempt_id, None, None,
            load_snapshot(conn, session_id=sid, operation_id=operation_id))
    response = None
    if receipt.disposition == "committed":
        row = conn.execute(select(chunks).where(chunks.c.session_id == sid, chunks.c.operation_id == operation_id,
                                                chunks.c.chunk_key == effect.result_chunk_key)).one()
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
                               key=effect.result_chunk_key).decode("utf-8", errors="strict")
        response = PrivateChunk(PrivateChunkRef(codec=row.codec, digest=row.sha256, byte_length=row.byte_length), canonical)
    selected = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    selected_stack = decode_continuation_snapshot(selected.payload_json)
    previous_frontier = tuple(item for item in selected_stack.effect_frontier if item.effect_id == effect_key)
    if (effect.disposition == receipt.disposition and len(previous_frontier) == 1
        and previous_frontier[0].disposition == receipt.disposition
        and receipt.attempt_id not in selected_stack.pending_settlement_ids):
        return ProviderEffectReconciliation(binding, receipt.disposition, receipt.attempt_id, receipt.ledger_entry_id, response, selected)
    result_key = effect.result_chunk_key
    if receipt.disposition == "refused" and result_key is None:
        result_key = f"cancelled:{receipt.attempt_id}"
        put_chunk(conn, session_id=sid, operation_id=operation_id, key=result_key,
                  payload=canonical_json({"attempt_id": receipt.attempt_id, "ledger_entry_id": receipt.ledger_entry_id,
                                          "disposition": "cancelled_before_dispatch"}).encode("utf-8"),
                  codec="provider-cancellation.v1")
    conn.execute(update(effects).where(effects.c.session_id == sid, effects.c.operation_id == operation_id,
                                       effects.c.effect_key == effect_key).values(
        disposition=receipt.disposition, result_chunk_key=result_key,
        receipt_key=effect.receipt_key or receipt.audit_event_id or receipt.attempt_id,
    ))
    stored = load_snapshot(conn, session_id=sid, operation_id=operation_id)
    encoded = _effect_snapshot(stored.payload_json, effect_key=effect_key, disposition=receipt.disposition,
                               receipt_id=receipt.attempt_id, remove_attempt_id=receipt.attempt_id)
    encoded = retain_action_execution_receipt_on_connection(
        conn, session_id=sid, operation_id=operation_id, effect_key=effect_key,
        disposition=receipt.disposition, snapshot_json=encoded,
    )
    updated = append_complete_snapshot(
        conn, session_id=sid, operation_id=operation_id, expected_checkpoint_id=stored.checkpoint_id,
        expected_cursor=stored.cursor, snapshot_json=encoded,
        event_batch=(ContinuationEvent("effect_committed" if receipt.disposition == "committed" else "effect_refused" if receipt.disposition == "refused" else "effect_unknown",
                                      canonical_json({"effect_key": effect_key, "attempt_id": receipt.attempt_id,
                                                      "ledger_entry_id": receipt.ledger_entry_id})),), handoff=False,
    )
    return ProviderEffectReconciliation(binding, receipt.disposition, receipt.attempt_id, receipt.ledger_entry_id, response, updated)
```

- [ ] Add the exact service/protocol method below. Its actual audit callback reuses the existing sequence/message writer on the current context; the event explicitly records original physical lineage. The old physical lease is never presented as current authority. The driver calls this service through actual continuation SQL/projection tickets before attempting an interrupted effect.

```python
def _reconcile_continuation_provider_effect_sync(
    self, *, running: ComposerOperationRunning, effect_key: str,
) -> ProviderEffectReconciliation:
    context, sid = running.session_operation_context, str(running.claim.session_id)
    with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
        self._require_session_operation_context_on_connection(conn, context, session_id=sid,
            expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn), audit_only=True)
        def append_event(event_id: str, content: str, created_at: datetime) -> None:
            sequence = self._reserve_sequence_range(conn, sid, count=1)
            self._insert_chat_message(conn, session_id=sid, role="audit", content=content, raw_content=None,
                tool_calls=None, sequence_no=sequence, writer_principal="compose_loop", composition_state_id=None,
                tool_call_id=None, parent_assistant_id=None, created_at=created_at,
                session_operation_context=context, message_id=event_id, audit_only=True)
        return reconcile_provider_effect_on_connection(conn, running, effect_key=effect_key, append_audit_event=append_event)


async def reconcile_continuation_provider_effect(
    self, *, running: ComposerOperationRunning, effect_key: str, required_work: RequiredWorkTicket | None = None,
) -> ProviderEffectReconciliation:
    context, sid = running.session_operation_context, str(running.claim.session_id)
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,), session_id=sid, context=context)
    def _sync() -> ProviderEffectReconciliation:
        return self._reconcile_continuation_provider_effect_sync(running=running, effect_key=effect_key)
    return await run_required_sql_in_worker(required_work, _sync) if required_work is not None else await self._run_composer_sql(_sync)


async def reconcile_continuation_provider_effect_finish_once(
    self, *, running: ComposerOperationRunning, effect_key: str, required_work: RequiredWorkTicket,
) -> RequiredSQLFinishOnce[ProviderEffectReconciliation]:
    context, sid = running.session_operation_context, str(running.claim.session_id)
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,), session_id=sid, context=context)
    return await run_required_sql_finish_once(required_work, self._reconcile_continuation_provider_effect_sync,
                                               running=running, effect_key=effect_key)
```

Import `RequiredSQLFinishOnce` from `web.required_sql_outcomes` and `run_required_sql_finish_once` from `web.async_workers`. Add both public async signatures to `SessionServiceProtocol` with the same return types; the sync helper remains the actual service owner. F17 uses the finish-once entry so it projects the returned complete snapshot before delivering its exact deferred cancellation objects. The old plain wrapper retains its ordinary cancellation behavior.

- [ ] Add this complete test to `tests/unit/web/composer/test_continuation_provider_accounting.py`. It holds the actual service-owned SQL result after the real reconciliation transaction, delivers cancellation to its waiter, and verifies the actual registered result and original cancellation are both available for projection. It never substitutes a reconciliation value or infers a receipt from an exception.

```python
@pytest.mark.asyncio
async def test_reconciliation_finish_once_projects_actual_snapshot_before_original_cancel(operation_store, monkeypatch):
    import asyncio
    import threading
    from elspeth.web.required_work import RequiredWorkSource
    from elspeth.web.required_sql_outcomes import RequiredSQLReturned
    from elspeth.web.sessions.continuation import ProviderEffectReconciliation
    _engine, repository, authority, service, _sid = operation_store
    running, coordinator, binding, _custody = make_custody(operation_store)
    work = RequiredWorkBinding(coordinator, 0, 0, RequiredWorkRole.TURN, running)
    sql, projection = work.reserve_pair(RequiredWorkSource.CONTINUATION_STATE_SQL,
                                       RequiredWorkSource.CONTINUATION_STATE_PROJECTION)
    actual_sync = service._reconcile_continuation_provider_effect_sync
    entered, release = threading.Event(), threading.Event()
    actual_values = []
    def held_actual(**kwargs):
        value = actual_sync(**kwargs)
        actual_values.append(value)
        entered.set()
        if not release.wait(5):
            raise AssertionError("actual reconciliation result was never released")
        return value
    monkeypatch.setattr(service, "_reconcile_continuation_provider_effect_sync", held_actual)
    task = asyncio.create_task(service.reconcile_continuation_provider_effect_finish_once(
        running=running, effect_key=binding.effect_key, required_work=sql))
    marker = object()
    try:
        assert await asyncio.wait_for(asyncio.to_thread(entered.wait, 5), timeout=6)
        task.cancel(marker)
        await asyncio.sleep(0)
        release.set()
        outcome = await asyncio.wait_for(task, timeout=5)
        coordinator.validate_continuation_sql_outcome(ticket=sql,
            expected_source=RequiredWorkSource.CONTINUATION_STATE_SQL, actual_outcome=outcome)
        assert type(outcome) is RequiredSQLReturned and type(outcome.value) is ProviderEffectReconciliation
        assert actual_values == [outcome.value] and outcome.value.disposition == "ready"
        assert outcome.value.snapshot == authority.load_continuation_snapshot(running)
        assert len(outcome.deferred_cancellations) == 1
        original = outcome.deferred_cancellations[0]
        assert type(original) is asyncio.CancelledError and original.args == (marker,)
        projection.begin_projection()
        selected = decode_continuation_snapshot(outcome.value.snapshot.payload_json)
        assert selected.cursor == outcome.value.snapshot.cursor
        projection.complete_owned()
        coordinator.assert_completed()
    finally:
        release.set()
        if not task.done():
            await asyncio.gather(task, return_exceptions=True)
        repository.release(running.session_operation_context)
```

- [ ] Bootstrap the complete importable packet; temporarily replace only the new facade's finish-once call with `run_required_sql_in_worker`. The test must fail because cancellation escapes before its actual nominal result is projected. Restore the exact facade and run its focused command through the C4b native-log setup, requiring exit 0:

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/composer/test_continuation_provider_accounting.py::test_reconciliation_finish_once_projects_actual_snapshot_before_original_cancel >"$COMPOSER_LOG_DIR/c5-reconcile-finish-once.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-reconcile-finish-once.log"
test "$composer_exit" -eq 0
```

- [ ] Add this reconciliation test and its PostgreSQL alias at main step 9, AFTER corrected C3.6 recovery/C3.4b/C3.9/F28.3 and F8's actual bootstrap codec. C5.7 provider-recovery production can be prepared at step 7, but this actual recover-and-start test cannot be qualified with first-only C3.4. Retain all four prepared/admitted/dispatched/retained cuts, real provider-attempt/quota/recovery owners, active-owner refusal, immutable creator/physical-generation checks, zero-versus-unknown-versus-exact usage, repeated reconciliation/no-extra-ledger assertions and all existing omission controls. Build its complete retained title-pending bootstrap before preparing that original effect; do not call `retained_title`, which would perform the provider cut prematurely.

```python
@pytest.mark.asyncio
@pytest.mark.parametrize("cut", ["prepared", "admitted", "dispatched", "retained"])
async def test_physical_recovery_preserves_actual_attempt_and_known_or_unknown_usage(operation_store, cut):
    from elspeth.web.composer.continuation import InitializeFrame
    from elspeth.web.composer.continuation_bootstrap import capture_continuation_bootstrap, encode_continuation_bootstrap
    from elspeth.web.composer.protocol import RetainedOperationContext
    from elspeth.web.composer.state import CompositionState, PipelineMetadata
    from dataclasses import replace
    from datetime import timedelta
    from elspeth.contracts.token_usage import TokenUsage
    from elspeth.web.composer._compose_loop_carriers import _AdmittedAssistantMessage, _AdmittedToolBatch
    from elspeth.web.composer.continuation_codec import encode_continuation_response
    from elspeth.web.sessions.continuation import (
        ContinuationEvent, mark_provider_may_dispatch_on_connection, retain_provider_response_on_connection,
    )
    from elspeth.web.sessions.locking import locked_session_transaction
    from elspeth.web.sessions.models import composer_continuation_effects_table as effects
    from elspeth.web.coordination.quota_authority import quota_provider_attempts_table as provider_attempts
    from elspeth.web.coordination.quota_authority import token_usage_ledger_table as ledger
    from tests.unit.web.composer.test_continuation_response import completion
    engine, repository, authority, service, sid = operation_store
    record, seed, _action = admit_continuation(operation_store)
    first = start_continuation(repository, authority)
    ingress = await service.add_message_with_transcript(
        sid, "user", "Preserve café and exact context", operation_id=UUID(record.operation_id),
        requested_state_id=None, writer_principal="route_user_message", running=first,
        reserved_user_message_id=UUID(seed.reserved_user_message_id), session_operation_context=first.session_operation_context,
    )
    request_record = authority.get_for_turn(session_id=sid, operation_id=record.operation_id)
    assert request_record is not None and request_record.request_json is not None
    request = SendMessageRequest.model_validate_json(request_record.request_json, strict=True)
    selected = authority.load_continuation_snapshot(first)
    stack = decode_continuation_snapshot(selected.payload_json)
    frame = stack.frames[-1]
    assert type(frame) is InitializeFrame
    recovery = await service.load_graph_recovery_context(sid, turn_start_state_id=record.base_state_id,
        session_operation_context=first.session_operation_context)
    session = await service.get_session(sid)
    assert session is not None
    bootstrap = encode_continuation_bootstrap(capture_continuation_bootstrap(
        logical=stack.logical, actor_user_id=record.actor_user_id, request=request, records=ingress.transcript,
        initial_state=CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1),
        recovery_context=recovery.context, completion_gates=recovery.effective_completion_gates,
        retained_context=RetainedOperationContext(sid, record.actor_user_id, "unavailable"), session_title=session.title,
        initial_schema_disclosure=()))
    title = stack.model_copy(update={"frames": (frame.model_copy(update={
        "ingress_phase": "title_pending", "bootstrap": bootstrap.reference}),)})
    selected = authority.commit_transition(
        first, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor,
        snapshot_json=encode_continuation_snapshot(title), chunks=(bootstrap,),
        event=ContinuationEvent("input_prepared", '{"kind":"title"}'),
    )
    binding = authority.prepare_provider_effect(first, family=3, input_sha256="a" * 64,
        snapshot_json=selected.payload_json, expected_checkpoint_id=selected.checkpoint_id, expected_cursor=selected.cursor)
    actual = None
    finished_at = None
    if cut != "prepared":
        actual = await service.begin_provider_attempt(session_operation_context=first.session_operation_context,
            source="auto_title", continuation_binding=binding, requested_model="test/model")
        before = await service.reconcile_continuation_provider_effect(running=first, effect_key=binding.effect_key)
        assert before.disposition == "owner_active" and before.ledger_entry_id is None
    if cut in ("dispatched", "retained"):
        with locked_session_transaction(engine, str(sid)) as conn:
            mark_provider_may_dispatch_on_connection(conn, binding)
    if cut == "retained":
        original = completion()
        reply = replace(original, message=_AdmittedAssistantMessage("Pipeline title"),
            tool_batch=_AdmittedToolBatch((), frozenset()),
            provider_metadata=replace(original.provider_metadata, finish_reason="stop",
                usage=TokenUsage(prompt_tokens=13, completion_tokens=7, cached_prompt_tokens=0, reasoning_tokens=2)))
        finished_at = actual.started_at + timedelta(milliseconds=1)
        with locked_session_transaction(engine, str(sid)) as conn:
            retain_provider_response_on_connection(conn, binding, encode_continuation_response(reply), finished_at=finished_at)
    repository.release(first.session_operation_context)
    assert repository.recover_composer_segment(session_id=sid, operation_id=record.operation_id,
        expected_generation=first.claim.ownership_generation, actor_user_id="alice", auth_provider_type="local")
    second = start_continuation(repository, authority)
    try:
        recovered = await service.reconcile_continuation_provider_effect(running=second, effect_key=binding.effect_key)
        if cut == "prepared":
            assert recovered.disposition == "ready" and recovered.attempt_id is None
            resumed = await service.begin_provider_attempt(session_operation_context=second.session_operation_context,
                source="auto_title", continuation_binding=recovered.binding, requested_model="test/model")
            with engine.connect() as conn:
                effect = conn.execute(select(effects).where(effects.c.operation_id == record.operation_id)).one()
                physical = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == resumed.attempt_id)).one()
            assert effect.creator_generation == first.claim.ownership_generation
            assert effect.physical_generation == second.claim.ownership_generation
            assert physical.operation_id == second.session_operation_context.fence.operation_id
            await service.cancel_undispatched_provider_attempt(session_operation_context=second.session_operation_context,
                attempt_id=resumed.attempt_id, requested_model="test/model", continuation_binding=recovered.binding)
        else:
            with engine.connect() as conn:
                usage = conn.execute(select(ledger).where(ledger.c.entry_id == recovered.ledger_entry_id)).one()
            assert recovered.attempt_id == actual.attempt_id
            assert usage.source == "auto_title" and usage.run_id is None
            if cut == "admitted":
                assert recovered.disposition == "refused"
                assert (usage.prompt_tokens, usage.completion_tokens) == (0, 0)
            elif cut == "dispatched":
                assert recovered.disposition == "unknown" and recovered.response is None
                assert usage.prompt_tokens is None and usage.completion_tokens is None
            else:
                assert recovered.disposition == "committed" and recovered.response is not None
                assert (usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens) == (13, 7, 0, 2)
                assert usage.recorded_at.replace(tzinfo=finished_at.tzinfo) == finished_at
            selected = authority.load_continuation_snapshot(second)
            repeated = await service.reconcile_continuation_provider_effect(running=second, effect_key=binding.effect_key)
            assert repeated == recovered
            assert authority.load_continuation_snapshot(second) == selected
            with engine.connect() as conn:
                assert len(conn.execute(select(ledger).where(ledger.c.entry_id == recovered.ledger_entry_id)).all()) == 1
    finally:
        repository.release(second.session_operation_context)
```

- [ ] Add this exact same-name import to the PostgreSQL continuation module:

```python
from tests.unit.web.sessions.test_continuation_cold_restore import (
    test_physical_recovery_preserves_actual_attempt_and_known_or_unknown_usage as test_physical_recovery_preserves_actual_attempt_and_known_or_unknown_usage,
)
```

- [ ] Bootstrap the complete packet. Temporarily replace the `creator.revoked_at` guard with an unconditional recovery branch; the active-owner test must fail before any release. Restore it. Temporarily replace dispatched unknown measures with zeros; the dispatched branch must fail its NULL assertions. Restore them. Temporarily use the current generation for stable `creator_generation`; the prepared branch must fail its immutable-creator assertion. Restore all controls before GREEN.

```bash
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -n 0 tests/unit/web/sessions/test_continuation_cold_restore.py::test_physical_recovery_preserves_actual_attempt_and_known_or_unknown_usage >"$COMPOSER_LOG_DIR/c5-provider-recovery.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-provider-recovery.log"
test "$composer_exit" -eq 0
cd "$COMPOSER_WORKTREE" && "$COMPOSER_PYTHON" -m pytest -m testcontainer -n 0 tests/testcontainer/web/test_composer_continuation_postgres.py >"$COMPOSER_LOG_DIR/c5-provider-recovery-postgres.log" 2>&1
composer_exit=$?
cat "$COMPOSER_LOG_DIR/c5-provider-recovery-postgres.log"
test "$composer_exit" -eq 0
```

### C5.8: Compare the actual prepared wire input before guard, quota and SDK entry

**Files:** Create `composer/continuation_provider_input.py`; modify the actual `_litellm_acompletion` prepared-input seam; use the same pure hash in the phase preparation owner. `ContinuationProviderBinding.input_sha256` is the value read from the actual prepared SQL effect, not a caller's later digest.

- [ ] Add this full pure helper. Missing tools and explicitly empty tools remain distinct. Requested model, `custom_llm_provider`, `api_base` and all actual semantic kwargs bind the digest; only the separate API key, app headers and independently enforced physical timeout are excluded. The endpoint remains in the process-local hash input and only its digest is persisted. Never log or retain the raw kwargs or an endpoint URL as snapshot data. A changed endpoint/provider/model refuses even when message bytes match. Existing native-field/credential admission and current endpoint authorization still execute before this comparison in the real gateway. This binding does not replace those gates.

```python
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from elspeth.contracts.hashing import stable_hash


def continuation_provider_input_hash(kwargs: Mapping[str, Any]) -> str:
    options = {key: value for key, value in kwargs.items()
               if key not in ("messages", "tools", "api_key", "extra_headers", "timeout")}
    return stable_hash({"schema": "composer.provider-input.v1", "messages": kwargs["messages"],
                        "tools_present": "tools" in kwargs,
                        "tools": kwargs["tools"] if "tools" in kwargs else None,
                        "options": options})
```

- [ ] Move `_apply_openrouter_app_identity(kwargs)` and `_apply_openrouter_usage_accounting(kwargs)` in `_litellm_acompletion` from immediately after the guard to immediately before the guard, after complete evidence detachment/native/credential checks. Immediately after these two calls, insert the exact comparison below. Do not leave duplicate calls at their old position. The phase owner builds the same actual cache-marked/detached messages/tools/request options, invokes those same existing pure kwargs enrichers, and stores this helper's digest in `prepare_provider_effect`; it never hashes a sketch of the request or a stale pre-cache payload.

```python
binding = provider_custody.continuation_binding
if binding is not None:
    from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
    if continuation_provider_input_hash(kwargs) != binding.input_sha256:
        raise AuditIntegrityError("actual continuation provider input differs from its prepared effect")
```

- [ ] In the C4b `make_custody` helper, replace its opaque input digest with this complete actual direct-gateway input hash. Those physical tests invoke `_litellm_acompletion` directly with these exact kwargs; ordinary production preparation uses the complete existing role request builder and marked input.

```python
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash
input_sha256 = continuation_provider_input_hash({
    "model": "test-model", "messages": [{"role": "user", "content": "Read the actual plugin contract"}],
})
```

- [ ] Add these complete pure controls in `tests/unit/web/composer/test_continuation_provider_input.py` and a physical mismatch assertion below. A fresh changed actual message must refuse before both guard audit and quota attempt creation.

```python
from elspeth.web.composer.continuation_provider_input import continuation_provider_input_hash


def test_complete_native_input_and_semantic_options_bind_the_prepared_effect():
    original = {"model": "model", "messages": [{"role": "user", "content": "exact original"}],
                "temperature": 0.1, "reasoning_effort": "high"}
    expected = continuation_provider_input_hash(original)
    assert continuation_provider_input_hash(original | {"timeout": 10, "api_key": "fixture-transport"}) == expected
    assert continuation_provider_input_hash(original | {"temperature": 0.2}) != expected
    assert continuation_provider_input_hash(original | {"messages": [{"role": "user", "content": "changed"}]}) != expected
    assert continuation_provider_input_hash(original | {"tools": []}) != expected
    assert continuation_provider_input_hash(original | {"api_base": "https://other-provider.example/v1"}) != expected
    assert continuation_provider_input_hash(original | {"custom_llm_provider": "another-provider"}) != expected
    assert continuation_provider_input_hash(original | {"model": "another-model"}) != expected
```

Append this test to `test_continuation_provider_accounting.py`; its fixture helper is the complete existing C4b helper with the hash replacement above.

```python
@pytest.mark.asyncio
async def test_changed_actual_input_refuses_before_guard_quota_or_sdk(operation_store):
    from elspeth.contracts.errors import AuditIntegrityError
    from tests.unit.web.composer.test_prompt_guard_admission import _checks
    engine, repository, _authority, _service, _sid = operation_store
    running, _coordinator, _binding, custody = make_custody(operation_store)
    physical = []
    async def sdk(**kwargs):
        physical.append(kwargs)
        raise AssertionError("changed input reached SDK")
    try:
        with patch("litellm.acompletion", new=sdk), prompt_guard_user_scope("Changed input"):
            with pytest.raises(AuditIntegrityError, match="differs from its prepared effect"):
                await _litellm_acompletion(
                    prompt_guard_mount=None, prompt_guard_mode="debug-when-unconfigured",
                    prompt_guard_role="composer", provider_custody=custody, model="test-model",
                    messages=[{"role": "user", "content": "Changed input"}],
                )
        assert physical == [] and _checks(engine) == []
        with engine.connect() as conn:
            assert conn.execute(select(attempts).where(attempts.c.operation_id == running.session_operation_context.fence.operation_id)).all() == []
    finally:
        repository.release(running.session_operation_context)
```

- [ ] Bootstrap, remove only the actual-wire digest comparison, and observe the mismatch test fail its precise refusal assertion or reach the raising SDK counterexample. Restore the comparison. Run both test modules through the ordinary C4b physical command with the new pure test path appended, requiring native exit 0.

### C5.9: Apply actual Advisor pass audit and selected caller state in one transaction

**Files:** Modify `sessions/continuation.py`, `sessions/service.py`, `sessions/protocol.py`, and the batch companion's manual application facade. Run after F13's pure source codec and B3's selected-bundle owner. This is the existing product pass audit, not a plan approval record.

- [ ] Add this complete same-connection validator to `sessions/continuation.py`. A currently selected child or an exact retained active-action source must name the provider receipt. The actual physical attempt, original immutable segment lineage and settled usage ledger are verified independently; the returned UUID is the real provider call identity. Nothing is chosen by response equality, most-recent ordinal or an absent row.

```python
def require_retained_provider_application_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
    effect_key: str, input_sha256: str, response: PrivateChunkRef,
) -> str:
    from elspeth.web.composer.continuation import ContinuationProviderFrame
    require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True)
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    actual_head = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    if actual_head != stored:
        raise ContinuationCASConflict("provider application supplied different selected root bytes")
    root = decode_continuation_snapshot(stored.payload_json)
    top, source = root.frames[-1], root.active_action_source
    selected_child = (isinstance(top, ContinuationProviderFrame)
        and (top.provider_effect_key, top.provider_input_sha256) == (effect_key, input_sha256)
        and ((type(top) is AdvisorFrame and top.response == response)
             or (type(top) is ComposeFrame and top.pending_response == response)
             or (type(top) is InitializeFrame and top.title_response == response)))
    selected_action = (source is not None
        and (source.effect_key, source.input_sha256, source.response) == (effect_key, input_sha256, response))
    selected_candidate = False
    from elspeth.web.composer.continuation import EndFrame
    from elspeth.web.composer.continuation_end_values import decode_end_candidate
    end_frames = tuple(frame for frame in root.frames if type(frame) is EndFrame)
    if len(end_frames) == 1:
        ref = end_frames[0].candidate
        canonical = read_chunk(conn, session_id=sid, operation_id=operation_id,
            key=f"value:{ref.codec}:{ref.digest}").decode("utf-8", errors="strict")
        candidate = decode_end_candidate(PrivateChunk(ref, canonical))
        selected_candidate = (candidate.primary_effect_key, candidate.primary_input_sha256, candidate.response) == (effect_key, input_sha256, response) or any(retained is not None
            and (retained.effect_key, retained.input_sha256, retained.response) == (effect_key, input_sha256, response)
            for retained in (candidate.publication_source, candidate.semantic_source))
    if not selected_child and not selected_action and not selected_candidate:
        raise AuditIntegrityError("provider response has no exact selected caller/action source")
    return require_settled_provider_source_on_connection(conn, running, stored=stored,
        effect_key=effect_key, input_sha256=input_sha256, response=response)


def require_settled_provider_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
    effect_key: str, input_sha256: str, response: PrivateChunkRef,
) -> str:
    from elspeth.web.composer.continuation_codec import decode_continuation_response, decode_retained_physical_audit_call
    from elspeth.web.composer.continuation_provider_outcomes import require_successful_response_audit
    from elspeth.web.sessions.models import token_usage_ledger_table
    if not require_continuation_mutation(conn, running.session_operation_context, allow_cancel=True, allow_expired_segment=True):
        raise AuditIntegrityError("provider source requires actual continuation attempt history")
    sid, operation_id = str(running.claim.session_id), running.claim.operation_id
    actual_head = require_snapshot_cas(conn, session_id=sid, operation_id=operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    if actual_head != stored or response.codec != "composer.response.v1":
        raise ContinuationCASConflict("provider source supplied different selected root bytes or response codec")
    root = decode_continuation_snapshot(stored.payload_json)
    effect = conn.execute(select(effects).where(effects.c.session_id == sid,
        effects.c.operation_id == operation_id, effects.c.effect_key == effect_key)).one()
    if (effect.owner != "provider" or effect.disposition != "committed" or effect.input_sha256 != input_sha256
        or effect.result_chunk_key != f"value:{response.codec}:{response.digest}" or effect.provider_attempt_id is None
        or effect.physical_sequence is None or effect.physical_generation is None):
        raise AuditIntegrityError("provider application lacks its exact actual response effect")
    actual = conn.execute(select(provider_attempts).where(provider_attempts.c.attempt_id == effect.provider_attempt_id)).one()
    physical = conn.execute(select(attempts).where(attempts.c.session_id == sid, attempts.c.operation_id == operation_id,
        attempts.c.sequence == effect.physical_sequence, attempts.c.generation == effect.physical_generation)).one()
    ledger = conn.execute(select(token_usage_ledger_table).where(token_usage_ledger_table.c.entry_id == actual.ledger_entry_id)).one_or_none()
    if (actual.settled_at is None or ledger is None or actual.attempt_id in root.pending_settlement_ids
        or (actual.session_id, actual.operation_id, actual.operation_epoch, actual.lease_token) != (
            sid, physical.session_operation_id, physical.session_operation_epoch, physical.session_operation_lease_token)
        or (ledger.identity_id, ledger.session_id, ledger.source, ledger.run_id) != (actual.identity_id, sid, actual.source, actual.run_id)):
        raise AuditIntegrityError("provider application has no exact settled actual physical ledger join")
    canonical = read_chunk(conn, session_id=sid, operation_id=operation_id, key=effect.result_chunk_key).decode("utf-8", errors="strict")
    retained_chunk = PrivateChunk(response, canonical)
    retained = decode_continuation_response(retained_chunk)
    metadata, usage = retained.provider_metadata, retained.provider_metadata.usage
    if (effect.response_finished_at is None
        or restore_utc(ledger.recorded_at) != restore_utc(effect.response_finished_at)
        or ledger.model != (metadata.model_returned or effect.requested_model)
        or (ledger.prompt_tokens, ledger.completion_tokens, ledger.cached_prompt_tokens, ledger.reasoning_tokens) != (
            usage.prompt_tokens, usage.completion_tokens, usage.cached_prompt_tokens, usage.reasoning_tokens)):
        raise AuditIntegrityError("provider application changed the actual retained usage/model ledger")
    call = decode_retained_physical_audit_call(retained_chunk)
    require_successful_response_audit(call, metadata, attempt_id=actual.attempt_id,
        started_at=restore_utc(actual.started_at), finished_at=restore_utc(effect.response_finished_at))
    return actual.attempt_id


def require_continuation_action_source_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
) -> str:
    from elspeth.web.composer.advisor_output import ReadContinuationAction, parse_continuation_advisor_decision
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_progress import decode_prescribed_continuation_action
    root = decode_continuation_snapshot(stored.payload_json)
    source = root.active_action_source
    if root.active_action_json is None or source is None:
        raise AuditIntegrityError("prescribed action has no exact actual Advisor source")
    physical_call_id = require_retained_provider_application_on_connection(conn, running, stored=stored,
        effect_key=source.effect_key, input_sha256=source.input_sha256, response=source.response)
    action = decode_prescribed_continuation_action(root.active_action_json)
    response_json = read_chunk(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
        key=f"value:{source.response.codec}:{source.response.digest}").decode("utf-8", errors="strict")
    actual = decode_continuation_response(PrivateChunk(source.response, response_json))
    if type(actual.message.content) is not str:
        raise AuditIntegrityError("prescribing Advisor receipt has no admitted semantic answer")
    decision = parse_continuation_advisor_decision(actual.message.content, actual_advisor_call_id=physical_call_id)
    prescribed = None if decision is None else decision.prescribed_action
    if (type(action) is not ReadContinuationAction or type(prescribed) is not ReadContinuationAction
        or action.tool_name != prescribed.tool_name or action.arguments_json != prescribed.arguments_json):
        raise AuditIntegrityError("retained read differs from the actual prescribing Advisor response")
    return physical_call_id


def resolve_continuation_action_source(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> str:
    with locked_session_transaction(self._engine, str(running.claim.session_id)) as conn:
        stored = require_snapshot_cas(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        return require_continuation_action_source_on_connection(conn, running, stored=stored)
```

Place the last method on `ComposerAsyncOperationAuthority`; import the shared helper normally. Import the closed `InitializeFrame`, `ComposeFrame` and `AdvisorFrame` types for the helper's explicit selectors. Batch B5's manual facade keeps its complete caller/next-root checks and replaces only its inline effect/attempt/ledger/response block with one call to this helper using the already selected `stored`. Manual writes no checkpoint-pass row. Every pure application comparison also preserves `active_action_source` together with `active_action_json`.

```python
require_retained_provider_application_on_connection(conn, running, stored=stored,
    effect_key=effect_key, input_sha256=input_sha256, response=response)
```

- [ ] Add the complete shared writer below to `SessionServiceImpl`, and add its exact public async signature to `SessionServiceProtocol` using deferred imports for continuation types. It regenerates the pass from the actual admitted response and exact retained state, then inserts the existing audit envelope before appending the advanced complete checkpoint in the SAME transaction. The currently exposed per-action Advisor source is one physical attempt; a caller cannot claim two attempts or a format reprompt which this source does not retain. A new independently prescribed review is another source/effect, never a fabricated first attempt.

```python
def _persist_continuation_advisor_pass_sync(
    self, *, running: ComposerOperationRunning, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], effect_key: str, input_sha256: str,
    response: PrivateChunkRef, record: AdvisorCheckpointPassRecord,
) -> StoredContinuationSnapshot:
    import json
    from elspeth.web.composer import advisor_policy
    from elspeth.web.composer.advisor_audit import AdvisorCheckpointPassRecord, advisor_checkpoint_pass_audit_envelope
    from elspeth.web.composer.advisor_checkpoint import _parse_advisor_checkpoint_guidance
    from elspeth.web.composer.continuation import AdvisorFrame, ComposeFrame, EndFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_advisor import decode_continuation_advisor_invocation
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_evidence import state_from_evidence_json
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import decode_batch_chunk
    if type(record) is not AdvisorCheckpointPassRecord or AdvisorCheckpointPassRecord.from_dict(record.to_dict()) != record:
        raise AuditIntegrityError("Advisor pass application requires the complete actual nominal record")
    context, sid = running.session_operation_context, str(running.claim.session_id)
    with self._session_process_locked_begin(sid) as conn, self._session_write_lock(conn, sid):
        self._require_session_operation_context_on_connection(conn, context, session_id=sid,
            expected_kind=SessionOperationKind.COMPOSE, now=database_now(conn), audit_only=True)
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=running.claim.operation_id,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        prior, following = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if (len(prior.frames) < 2 or type(prior.frames[-1]) is not AdvisorFrame
            or prior.frames[-1].phase != "response_retained" or prior.frames[-1].response != response
            or len(following.frames) != len(prior.frames)-1 or following.cursor != prior.cursor):
            raise AuditIntegrityError("Advisor pass lost its exact selected completed child/pop")
        child = prior.frames[-1]
        physical_call_id = require_retained_provider_application_on_connection(conn, running, stored=stored,
            effect_key=effect_key, input_sha256=input_sha256, response=response)
        old_chunks = load_private_chunks(conn, session_id=sid, operation_id=running.claim.operation_id, stack=prior)
        source = decode_continuation_advisor_invocation(continuation_chunk(old_chunks, child.review))
        if source.coverage_binding != child.coverage_binding or source.kind not in ("early", "publication"):
            raise AuditIntegrityError("checkpoint pass substituted manual/semantic or another retained review")
        phase = "early" if source.kind == "early" else "end"
        compose = tuple(frame for frame in prior.frames if type(frame) is ComposeFrame)
        if len(compose) != 1:
            raise AuditIntegrityError("Advisor pass lost its one paused logical compose owner")
        state = state_from_evidence_json(continuation_chunk(old_chunks, source.state).canonical)
        retained = decode_continuation_response(continuation_chunk(old_chunks, response))
        if type(retained.message.content) is not str or not retained.message.content.strip() or retained.tool_batch.calls:
            raise AuditIntegrityError("Advisor pass has no fully admitted actual text response")
        verdict = _parse_advisor_checkpoint_guidance(retained.message.content, phase=phase)
        accepted = verdict.ok
        expected = AdvisorCheckpointPassRecord.from_findings(phase=phase,
            pass_index=0 if phase == "early" else compose[0].advisor_checkpoint_passes + 1,
            verdict=("flagged" if verdict.blocking else "clean") if accepted else "malformed", source="model",
            findings_text=verdict.findings_text, provider_attempts=1,
            first_attempt_schema_valid=verdict.response_schema_valid,
            first_attempt_accepted=accepted if verdict.response_schema_valid is not None else None,
            format_reprompt_sent=False,
            step_ids_offered=len(verdict.affected_step_ids) if accepted else None,
            step_ids_kept=len(advisor_policy.validated_advisor_step_ids(state, verdict.affected_step_ids)) if accepted else None,
            note_present=verdict.response_note_present if accepted else None,
            url_redactions=verdict.url_redactions if accepted else None, email_redactions=verdict.email_redactions if accepted else None,
            publication_verdict=verdict.publication_verdict if phase == "end" and accepted else None,
            publication_binding=child.coverage_binding if phase == "end" and accepted else None)
        if record != expected:
            raise AuditIntegrityError("Advisor pass differs from its actual selected response/state/counts")
        if phase == "early":
            if type(prior.frames[-2]) is not ToolBatchFrame or type(following.frames[-1]) is not ToolBatchFrame:
                raise AuditIntegrityError("EARLY pass lost its same paused batch")
            before = decode_batch_chunk(continuation_chunk(old_chunks, prior.frames[-2].accumulator))
            after = decode_batch_chunk(continuation_chunk(chunks, following.frames[-1].accumulator))
            if (before.phase != "dispatch_ready" or before.early_reviewed or before.current_call is not None
                or after != before.model_copy(update={"early_reviewed": True})
                or prior.frames[-2].response != following.frames[-1].response):
                raise AuditIntegrityError("EARLY pass changed its issued prefix or repeated a reviewed batch")
        else:
            if type(prior.frames[-2]) is not EndFrame or type(following.frames[-1]) is not EndFrame:
                raise AuditIntegrityError("publication pass lost its same actual candidate")
            before, after = prior.frames[-2], following.frames[-1]
            next_compose = tuple(frame for frame in following.frames if type(frame) is ComposeFrame)
            if (len(next_compose) != 1 or next_compose[0].state != compose[0].state
                or next_compose[0].current_state_id != compose[0].current_state_id
                or next_compose[0].advisor_checkpoint_passes != compose[0].advisor_checkpoint_passes + 1
                or (next_compose[0].composition_total, next_compose[0].discovery_total,
                    next_compose[0].composition_slice, next_compose[0].discovery_slice,
                    next_compose[0].advisor_calls, next_compose[0].repair_turns)
                != (compose[0].composition_total, compose[0].discovery_total,
                    compose[0].composition_slice, compose[0].discovery_slice,
                    compose[0].advisor_calls, compose[0].repair_turns)):
                raise AuditIntegrityError("publication pass changed graph or another action counter")
            from elspeth.web.composer.continuation import ContinuationActionSource
            from elspeth.web.composer.continuation_end_values import decode_end_candidate
            candidate_before = decode_end_candidate(continuation_chunk(old_chunks, before.candidate))
            candidate_after = decode_end_candidate(continuation_chunk(chunks, after.candidate))
            admitted = accepted and verdict.publication_verdict == "ADMIT"
            actual_source = ContinuationActionSource(effect_key=effect_key, input_sha256=input_sha256, response=response)
            expected_candidate = candidate_before.model_copy(update={
                "publication_binding": child.coverage_binding if admitted else None,
                "publication_source": actual_source, "publication_record_json": canonical_json(record.to_dict()),
                "semantic_source": None, "semantic_decision_json": None})
            if (before.state != after.state or candidate_after != expected_candidate
                or after.publication_binding != (child.coverage_binding if admitted else None)
                or after.phase != ("publication_ready" if admitted else "repair_needed")):
                raise AuditIntegrityError("publication pass changed its actual reviewed candidate/binding")
        if (following.logical != prior.logical or following.policy_hash != prior.policy_hash
            or following.admission_policy_json != prior.admission_policy_json
            or following.effect_frontier != prior.effect_frontier
            or following.pending_settlement_ids != prior.pending_settlement_ids
            or following.pending_owner_results != prior.pending_owner_results
            or following.semantic_frontier != prior.semantic_frontier
            or following.active_action_json != prior.active_action_json
            or following.active_action_source != prior.active_action_source):
            raise AuditIntegrityError("checkpoint pass application changed another execution/action owner")
        put_private_chunks(conn, session_id=sid, operation_id=running.claim.operation_id, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=running.claim.operation_id, stack=following)
        envelope = advisor_checkpoint_pass_audit_envelope(record)
        message_id = self._insert_chat_message(conn, session_id=sid, role="audit",
            content=json.dumps(envelope, sort_keys=True), raw_content=None, tool_calls=[envelope],
            sequence_no=self._reserve_sequence_range(conn, sid, count=1), writer_principal="compose_loop",
            composition_state_id=compose[0].current_state_id, tool_call_id=None, parent_assistant_id=None,
            created_at=database_now(conn), session_operation_context=context, audit_only=True)
        if phase == "end":
            from dataclasses import replace
            from elspeth.web.composer.advisor_output import ReviewContinuationAction
            from elspeth.web.composer.continuation_codec import decode_frontier_chunk, encode_frontier_chunk
            from elspeth.web.composer.continuation_progress import continuation_action_key, decode_prescribed_continuation_action
            if prior.semantic_frontier is None:
                raise AuditIntegrityError("publication pass lost its complete semantic frontier")
            frontier = decode_frontier_chunk(continuation_chunk(old_chunks, prior.semantic_frontier))
            debt = tuple(binding for binding in frontier.pending_review_bindings if not binding.startswith("publication:"))
            if not admitted:
                debt = (*debt, f"publication:{child.coverage_binding}")
            frontier_chunk = encode_frontier_chunk(replace(frontier, pending_review_bindings=debt))
            put_private_chunks(conn, session_id=sid, operation_id=running.claim.operation_id, values=(frontier_chunk,))
            following = following.model_copy(update={"semantic_frontier": frontier_chunk.reference})
            if prior.active_action_json is not None:
                action = decode_prescribed_continuation_action(prior.active_action_json)
                if type(action) is not ReviewContinuationAction:
                    raise AuditIntegrityError("publication pass attempted to settle another prescribed owner")
                action_key = continuation_action_key(action, retained_evidence={item.receipt_id: item for item in frontier.evidence})
                outer_key = f"action:review:{action_key}"
                outer = conn.execute(select(effects).where(effects.c.session_id == sid,
                    effects.c.operation_id == running.claim.operation_id, effects.c.effect_key == outer_key)).one()
                expected_outer_input = stable_hash({"schema": "composer.prescribed-review-input.v1",
                    "logical": prior.logical.model_dump(mode="json"), "action": action.model_dump(mode="json"),
                    "candidate": before.candidate.digest, "coverage_binding": child.coverage_binding})
                if (outer.owner != "session_sql" or outer.disposition != "prepared" or outer.action_json != prior.active_action_json
                    or outer.input_sha256 != expected_outer_input):
                    raise AuditIntegrityError("publication pass lost its exact prepared outer review action")
                result_key = f"advisor-pass:{outer_key}"
                put_chunk(conn, session_id=sid, operation_id=running.claim.operation_id, key=result_key,
                    payload=canonical_json({"pass": record.to_dict(), "audit_message_id": message_id,
                        "physical_call_id": physical_call_id, "provider_effect_key": effect_key}).encode("utf-8"),
                    codec="composer.advisor-pass.v1")
                conn.execute(update(effects).where(effects.c.session_id == sid,
                    effects.c.operation_id == running.claim.operation_id, effects.c.effect_key == outer_key,
                    effects.c.disposition == "prepared").values(disposition="committed", receipt_key=message_id,
                        result_chunk_key=result_key, committer_sequence=running.claim.segment_sequence,
                        committer_generation=running.claim.ownership_generation))
                encoded = _effect_snapshot(encode_continuation_snapshot(following), effect_key=outer_key,
                    disposition="committed", receipt_id=message_id)
                following = decode_continuation_snapshot(retain_action_execution_receipt_on_connection(conn,
                    session_id=sid, operation_id=running.claim.operation_id, effect_key=outer_key,
                    disposition="committed", snapshot_json=encoded))
            snapshot_json = encode_continuation_snapshot(following)
        return append_complete_snapshot(conn, session_id=sid, operation_id=running.claim.operation_id,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("review_retained", canonical_json({"effect_key": effect_key,
                "physical_call_id": physical_call_id, "pass_message_id": message_id, "phase": phase})),), handoff=False)


async def persist_continuation_advisor_pass_finish_once(
    self, *, running: ComposerOperationRunning, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...], effect_key: str, input_sha256: str,
    response: PrivateChunkRef, record: AdvisorCheckpointPassRecord, required_work: RequiredWorkTicket,
) -> RequiredSQLFinishOnce[StoredContinuationSnapshot]:
    _validate_required_sql_ticket(required_work, sources=(RequiredWorkSource.CONTINUATION_STATE_SQL,),
        session_id=str(running.claim.session_id), context=running.session_operation_context)
    return await run_required_sql_finish_once(required_work, self._persist_continuation_advisor_pass_sync,
        running=running, expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor,
        snapshot_json=snapshot_json, chunks=chunks, effect_key=effect_key, input_sha256=input_sha256,
        response=response, record=record)
```

Normal source imports are `ComposerOperationRunning`, `PrivateChunk`, `PrivateChunkRef`, `StoredContinuationSnapshot`, the actual pass record, SQL owner helpers and finish-once types; protocol annotations use `TYPE_CHECKING` to avoid cycles. F27 uses this exact writer for EARLY. The END companion uses it for publication. Both project the registered actual SQL outcome and only then mirror `record_advisor_checkpoint_pass`; no second `persist_advisor_checkpoint_pass`/`add_message` is called. A same-head CAS retry after commit refuses before reserving another chat sequence.

- [ ] Add the actual real-driver rollback/replay tests from the END companion's E6 packet after its completed publication fixture and transport D1–D3 exist. Those tests inject after this actual `_insert_chat_message` returns and compare complete SQL/audit/head bytes on rollback, then cold-resume the exact received child and require one real pass row. Use the actual default and PostgreSQL persistence gates from Q before integration; a document syntax check is not execution evidence.

## C3.8c: Admit only the P4 write already owed by an actual settled issued prefix

Apply after B6's complete codecs and before B7/physical O8 driver tests. Ordinary `commit_transition`, `prepare_tool_action`, proposal/interpretation writers and fresh handler dispatch retain their strict cancellation gates. The three methods below accept only the exact original received primary batch, a settled closed prefix and its actual prepared P4 bytes. They do not issue another call. The ordinary nested session guards use the connection-local `_OwedP4Scope` defined in C3.2 only during this actual P4 transaction; it is never serialized, returned to a model, attached to another connection or kept after the transaction.

- [ ] Add this complete selected-prefix verifier and scoped owner to `sessions/continuation.py`. Import `contextmanager`, `Iterator` and `ContinuationBatchValue` under the existing future-annotation/type-only convention.

```python
from contextlib import contextmanager
from collections.abc import Iterator


def require_owed_p4_selected_on_connection(
    conn: Connection, running: ComposerOperationRunning, *, stored: StoredContinuationSnapshot,
) -> ContinuationBatchValue:
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_codec import decode_continuation_response
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import validate_batch_frame
    from elspeth.web.composer.tool_batch import restore_continuation_batch_value
    if not require_continuation_mutation(conn, running.session_operation_context,
        allow_cancel=True, allow_expired_segment=True):
        raise AuditIntegrityError("owed P4 requires actual continuation attempt history")
    claim = running.claim
    segment = conn.execute(select(segments).where(segments.c.session_id == str(claim.session_id),
        segments.c.operation_id == claim.operation_id, segments.c.sequence == claim.segment_sequence)).one()
    history = conn.execute(select(attempts).where(attempts.c.session_id == str(claim.session_id),
        attempts.c.operation_id == claim.operation_id, attempts.c.sequence == claim.segment_sequence,
        attempts.c.generation == claim.ownership_generation)).one()
    if (segment.claim_token != claim.claim_token or segment.generation != claim.ownership_generation
        or history.claim_attempt != claim.attempt):
        raise AuditIntegrityError("owed P4 changed its exact current claim")
    current = require_snapshot_cas(conn, session_id=str(claim.session_id), operation_id=claim.operation_id,
        expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor)
    if current != stored:
        raise ContinuationCASConflict("owed P4 changed its selected complete snapshot bytes")
    root = decode_continuation_snapshot(stored.payload_json)
    if (len(root.frames) < 2 or type(root.frames[-1]) is not ToolBatchFrame
        or type(root.frames[-2]) is not ComposeFrame):
        raise AuditIntegrityError("owed P4 has no actual settled issued batch")
    parent, frame = root.frames[-2:]
    values = load_private_chunks(conn, session_id=str(claim.session_id), operation_id=claim.operation_id, stack=root)
    value = validate_batch_frame(frame, continuation_chunk(values, frame.accumulator))
    response = decode_continuation_response(continuation_chunk(values, frame.response))
    restore_continuation_batch_value(value, response.tool_batch.calls)
    if (value.current_call is not None or value.phase not in ("dispatch_ready", "p4_pending")
        or value.next_tool_index != len(value.tool_outcomes)
        or (value.next_tool_index != len(response.tool_batch.calls) and not value.stop_issued_batch)
        or parent.pending_response != frame.response or parent.provider_effect_key is None
        or parent.provider_input_sha256 is None or root.pending_owner_results):
        raise AuditIntegrityError("owed P4 tried to issue, omit or replay another actual owner")
    require_settled_provider_source_on_connection(conn, running, stored=stored,
        effect_key=parent.provider_effect_key, input_sha256=parent.provider_input_sha256, response=frame.response)
    return value


@contextmanager
def owed_continuation_p4_write(
    conn: Connection, commit: ContinuationOwnerCommit,
) -> Iterator[StoredContinuationSnapshot]:
    from elspeth.web.composer.continuation_tool_batch import decode_prepared_p4, p4_semantic_input, _read_json_value
    running = commit.running
    stored = require_snapshot_cas(conn, session_id=str(running.claim.session_id), operation_id=running.claim.operation_id,
        expected_checkpoint_id=commit.expected_checkpoint_id, expected_cursor=commit.expected_cursor)
    batch = require_owed_p4_selected_on_connection(conn, running, stored=stored)
    root = decode_continuation_snapshot(stored.payload_json)
    if batch.phase != "p4_pending" or batch.p4_input_json is None or commit.tool_completion is not None:
        raise AuditIntegrityError("owed P4 write lost its exact prepared audit input")
    prepared = decode_prepared_p4(batch.p4_input_json)
    if (commit.effect_key != f"batch:{root.frames[-1].response.digest}:p4"
        or commit.input_sha256 != stable_hash(_read_json_value(p4_semantic_input(prepared)))):
        raise AuditIntegrityError("owed P4 write changed its actual prepared effect/input")
    effect = conn.execute(select(effects).where(effects.c.session_id == str(running.claim.session_id),
        effects.c.operation_id == running.claim.operation_id, effects.c.effect_key == commit.effect_key)).one()
    if (effect.owner != "session_sql" or effect.disposition != "prepared" or effect.input_sha256 != commit.input_sha256
        or effect.committer_sequence is not None or effect.committer_generation is not None):
        raise ContinuationCASConflict("owed P4 effect was already committed or changed")
    if conn.info.get(_OWED_P4_SCOPE_KEY) is not None:
        raise AuditIntegrityError("owed P4 scope cannot replace or nest a different SQL owner")
    proof = _OwedP4Scope(conn, running.session_operation_context, _OWED_P4_SEAL)
    conn.info[_OWED_P4_SCOPE_KEY] = proof
    try:
        yield stored
    finally:
        if conn.info.pop(_OWED_P4_SCOPE_KEY, None) is not proof:
            raise AuditIntegrityError("owed P4 scope lost its exact connection-local lifetime")
```

The held transaction and current live session fence remain mandatory. An expired segment can drain its already issued P4 while its session lease still lives; a released/replaced fence cannot use this scope. The selected complete snapshot, original received response, actual physical audit/ledger and immutable prepared P4 identity are all checked before installing it. The later actual writer still passes `require_owner_commit`: this cancellation/expiry scope does not waive business-SQL preparing-creator equality. If an existing prepared P4 belongs to another sequence/generation, executing that old effect remains HOLD under B4's refusal policy; preserve the complete owed-P4 bodies without treating them as cross-creator permission.

- [ ] Add these complete authority methods. The checkpoint changes only the existing batch's prepared P4 projection and captured current owners. The preparation method derives its whole new root from that exact current stored value and has no caller-supplied replacement snapshot.

```python
def commit_owed_continuation_p4_checkpoint(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
    snapshot_json: str, chunks: tuple[PrivateChunk, ...],
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation import ComposeFrame, ToolBatchFrame
    from elspeth.web.composer.continuation_execution import continuation_chunk
    from elspeth.web.composer.continuation_tool_batch import decode_prepared_p4, p4_semantic_input, validate_batch_frame
    sid, op = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        old = require_owed_p4_selected_on_connection(conn, running, stored=stored)
        before, after = decode_continuation_snapshot(stored.payload_json), decode_continuation_snapshot(snapshot_json)
        if (len(after.frames) != len(before.frames) or type(after.frames[-1]) is not ToolBatchFrame
            or type(after.frames[-2]) is not ComposeFrame or after.model_copy(update={"frames": before.frames}) != before):
            raise AuditIntegrityError("owed P4 checkpoint changed another logical/action/financial owner")
        new = validate_batch_frame(after.frames[-1], continuation_chunk(chunks, after.frames[-1].accumulator))
        if (new.phase != "p4_pending" or new.p4_input_json is None
            or new.model_copy(update={"phase": old.phase, "p4_input_json": old.p4_input_json}) != old
            or after.frames[-1].model_copy(update={"phase": before.frames[-1].phase,
                "accumulator": before.frames[-1].accumulator}) != before.frames[-1]):
            raise AuditIntegrityError("owed P4 checkpoint altered its actual completed prefix")
        prepared = decode_prepared_p4(new.p4_input_json)
        if old.p4_input_json is not None and p4_semantic_input(prepared) != p4_semantic_input(decode_prepared_p4(old.p4_input_json)):
            raise AuditIntegrityError("owed P4 checkpoint changed its actual retained logical input")
        old_compose, new_compose = before.frames[-2], after.frames[-2]
        retained = {"messages": old_compose.messages, "state": old_compose.state,
            "evidence": old_compose.evidence, "transcript": old_compose.transcript,
            "audit_buffer": old_compose.audit_buffer, "caches": old_compose.caches,
            "iteration": old_compose.iteration, "advisor_calls": old_compose.advisor_calls}
        if (new_compose.model_copy(update=retained) != old_compose
            or after.frames[:-2] != before.frames[:-2] or new_compose.advisor_calls != old.advisor_calls_used):
            raise AuditIntegrityError("owed P4 checkpoint changed another actual phase/counter")
        put_private_chunks(conn, session_id=sid, operation_id=op, values=chunks)
        load_private_chunks(conn, session_id=sid, operation_id=op, stack=after)
        return append_complete_snapshot(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=snapshot_json,
            event_batch=(ContinuationEvent("checkpoint", '{"phase":"owed_p4_input"}'),), handoff=False)


def prepare_owed_continuation_p4(
    self, running: ComposerOperationRunning, *, expected_checkpoint_id: str, expected_cursor: int,
) -> StoredContinuationSnapshot:
    from elspeth.web.composer.continuation_tool_batch import decode_prepared_p4, p4_semantic_input, _read_json_value
    sid, op = str(running.claim.session_id), running.claim.operation_id
    with locked_session_transaction(self._engine, sid) as conn:
        stored = require_snapshot_cas(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=expected_checkpoint_id, expected_cursor=expected_cursor)
        value = require_owed_p4_selected_on_connection(conn, running, stored=stored)
        if value.phase != "p4_pending" or value.p4_input_json is None:
            raise AuditIntegrityError("owed P4 preparation has no exact retained input")
        root = decode_continuation_snapshot(stored.payload_json)
        key = f"batch:{root.frames[-1].response.digest}:p4"
        digest = stable_hash(_read_json_value(p4_semantic_input(decode_prepared_p4(value.p4_input_json))))
        existing = conn.execute(select(effects).where(effects.c.session_id == sid,
            effects.c.operation_id == op, effects.c.effect_key == key)).one_or_none()
        if existing is not None:
            raise ContinuationCASConflict("owed P4 effect is already prepared; restore its actual disposition")
        conn.execute(insert(effects).values(session_id=sid, operation_id=op, effect_key=key,
            input_sha256=digest, owner="session_sql", disposition="prepared", action_json=None,
            creator_sequence=running.claim.segment_sequence, creator_generation=running.claim.ownership_generation))
        encoded = _effect_snapshot(stored.payload_json, effect_key=key, disposition="prepared", receipt_id=None)
        policy = decode_continuation_policy(root.admission_policy_json, root.policy_hash)
        conn.execute(update(segments).where(segments.c.session_id == sid, segments.c.operation_id == op,
            segments.c.sequence == running.claim.segment_sequence,
            segments.c.generation == running.claim.ownership_generation).values(
                next_action_json=policy.actions.session_sql.model_dump_json(), policy_sha256=root.policy_hash))
        return append_complete_snapshot(conn, session_id=sid, operation_id=op,
            expected_checkpoint_id=stored.checkpoint_id, expected_cursor=stored.cursor, snapshot_json=encoded,
            event_batch=(ContinuationEvent("tool_prepared", canonical_json({"effect_key": key, "input_sha256": digest})),), handoff=False)
```

- [ ] At the actual `persist_compose_turn` transaction, preserve the complete existing body and insert only the scoped lifetime below. Import `nullcontext` normally. Split the existing combined `with` into these nested owners, retaining every original statement under the mutation context. The `require_selected_p4_input` call with all actual arguments remains before sequence reservation, as B6.4 specifies. The same scoped owner wraps `_retain_unavailable_continuation_p4`'s exact fresh receipt-only transaction; it must additionally require `decode_prepared_p4(require_owed_p4_selected_on_connection(...).p4_input_json).crash_pending` before writing an unavailable receipt.

```python-relocation
with self._session_process_locked_begin(session_id) as conn:
    with self._session_write_lock(conn, session_id):
        with (owed_continuation_p4_write(conn, continuation) if continuation is not None else nullcontext()):
            with self._session_composer_mutation_transaction(conn, session_id=session_id,
                session_operation_context=session_operation_context, expected_kind=SessionOperationKind.COMPOSE):
```

Move the exact preserved b003 `persist_compose_turn` transaction body, from its `# B5: if a parent` comment through the final checkpoint/continuation receipt hook, under that last context. Existing v1 exception arms and return remain in their original positions outside these owners.

In the unavailable receipt owner the equivalent exact outer preamble is:

```python-statement
with self._session_process_locked_begin(sid) as conn:
    with self._session_write_lock(conn, sid), owed_continuation_p4_write(conn, continuation) as stored:
        value = require_owed_p4_selected_on_connection(conn, continuation.running, stored=stored)
        if value.p4_input_json is None or not decode_prepared_p4(value.p4_input_json).crash_pending:
            raise AuditIntegrityError("unavailable P4 receipt has no actual retained crash input")
        with self._session_composer_mutation_transaction(conn, session_operation_context=context,
            session_id=sid, expected_kind=SessionOperationKind.COMPOSE):
            require_owner_commit(conn, continuation, context)
```

Move B6.2's exact existing receipt construction and `retain_owner_result_on_connection` call under that last context. No failed business row survives the rollback; the unavailable receipt and root are the actual owed write.

- [ ] In B8 `ContinuationBatchExecution.checkpoint` and its retained `_checkpoint_value`, add `owed_p4:bool=False`, forward it unchanged, and require `type(owed_p4) is bool`. Before the admitted-primary branch (after the mutually exclusive O8 retained-failure branch), add this exact branch. It remains inside the same cumulative V stage and existing registered SQL projection/deferred-cancellation suffix.

```python-statement
elif owed_p4:
    if (retained_failure is not None or admitted_primary is not None or retained_provider is not None
        or consumed_effect_key is not None or completed_memory):
        raise AuditIntegrityError("owed P4 checkpoint cannot consume a different actual owner")
    outcome = await run_required_sql_finish_once(sql, self.authority.commit_owed_continuation_p4_checkpoint,
        running, expected_checkpoint_id=self.checkpoint_id, expected_cursor=self.stack.cursor,
        snapshot_json=encode_continuation_snapshot(root), chunks=values)
```

In B6.5 replace both `await execution.checkpoint()` calls immediately after assignment to `batch.p4_input_json` with `await execution.checkpoint(owed_p4=True)`. Replace only its actual preparation submission with:

```python-statement
result = await run_required_sql_finish_once(sql, execution.authority.prepare_owed_continuation_p4, running,
    expected_checkpoint_id=execution.checkpoint_id, expected_cursor=execution.stack.cursor)
```

The consumed P4 pending-result checkpoint already goes through the exact `commit_owner_application` receipt path, which permits only owed application. P4 does not call a fresh business handler, Advisor or provider after Stop. The complete actual Stop-at-P4 and forged-prefix/rollback controls are in physical O9 and run with B7.1's actual received-batch Stop test.
