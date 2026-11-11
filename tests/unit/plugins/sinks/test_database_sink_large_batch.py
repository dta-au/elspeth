"""DatabaseSink must publish a sink batch whose rows exceed the bounded plan evidence.

Sweep finding #39 (20261111-plugins-review): ``prepare_effect`` embedded every
serialized member row in ``SinkEffectPlan.safe_evidence``, which is bounded at
64 KiB, so any table load whose rows pass ~64 KiB failed at prepare with an
opaque contract ``ValueError``. The plan must bind the rows by hash and commit
must take row values from the durable member payloads (restaged before commit).
"""

from __future__ import annotations

from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from sqlalchemy import Column, Integer, MetaData, Table, Text, create_engine, func, select

from elspeth.contracts.hashing import canonical_json, canonical_json_loads
from elspeth.contracts.sink_effects import (
    RestagingSinkEffectCapability,
    RestrictedSinkEffectContext,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPlan,
    SinkEffectPrepareRequest,
)
from elspeth.plugins.sinks.database_sink import DatabaseEffectLedgerError, DatabaseSink, database_effect_ledger_table
from tests.fixtures.base_classes import inject_write_failure

_EVIDENCE_LIMIT_BYTES = 64 * 1024
_LEDGER = {"table": "_elspeth_sink_effects", "schema_version": 1, "permissions": ["select", "insert"]}
_CTX = RestrictedSinkEffectContext(
    run_id="run-database-large-batch",
    run_started_at=datetime(2026, 11, 11, tzinfo=UTC),
    operation_id="operation-database-large-batch",
    sink_node_id="sink-database-large-batch",
)


def _member(ordinal: int, row: dict[str, object]) -> SinkEffectMember:
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-{ordinal}",
        row_id=f"row-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json="[]",
        lineage_hash=sha256(b"[]").hexdigest(),
        payload_hash=sha256(canonical_json(row).encode()).hexdigest(),
        row=row,
        member_effect_id=sha256(f"member-{ordinal}".encode()).hexdigest(),
    )


def _effect_input(rows: list[dict[str, object]]) -> SinkEffectPipelineMembersInput:
    members = tuple(_member(ordinal, row) for ordinal, row in enumerate(rows))
    return SinkEffectPipelineMembersInput(members=members, target_snapshot_members=members, target_delivered_member_count=len(members))


def _hydrated(effect_input: SinkEffectPipelineMembersInput) -> SinkEffectPipelineMembersInput:
    """Rebuild the input the way recovery does: rows round-trip the canonical payload store."""
    members = tuple(_member(member.ordinal, canonical_json_loads(canonical_json(member.row))) for member in effect_input.members)
    return SinkEffectPipelineMembersInput(members=members, target_snapshot_members=members, target_delivered_member_count=len(members))


def _sink(url: str, schema: dict[str, object]) -> DatabaseSink:
    return inject_write_failure(
        DatabaseSink({"url": url, "table": "output", "schema": schema, "if_exists": "append", "effect_ledger": _LEDGER})
    )


def _provision(url: str, *extra_text_columns: str) -> None:
    engine = create_engine(url)
    metadata = MetaData()
    Table(
        "output",
        metadata,
        Column("id", Integer, nullable=False, unique=True),
        Column("name", Text, nullable=False),
        *(Column(name, Text, nullable=True) for name in extra_text_columns),
    )
    database_effect_ledger_table(metadata, "_elspeth_sink_effects")
    metadata.create_all(engine)
    engine.dispose()


def _try_prepare(sink: DatabaseSink, effect_input: SinkEffectPipelineMembersInput, effect_id: str) -> SinkEffectPlan | ValueError:
    inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=None), _CTX)
    try:
        return sink.prepare_effect(SinkEffectPrepareRequest(effect_id=effect_id, effect_input=effect_input, inspection=inspection), _CTX)
    except ValueError as exc:
        return exc


def _large_rows(count: int = 2000) -> list[dict[str, object]]:
    return [{"id": index, "name": f"name-{index}-" + "x" * 100} for index in range(count)]


def _row_count(url: str) -> int:
    engine = create_engine(url)
    target = Table("output", MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        count = conn.scalar(select(func.count()).select_from(target))
    engine.dispose()
    assert type(count) is int
    return count


_FIXED_SCHEMA: dict[str, object] = {"mode": "fixed", "fields": ["id: int", "name: str"]}


def test_large_batch_prepares_with_row_count_independent_evidence(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'large.db'}"
    _provision(url)
    rows = _large_rows()
    assert len(canonical_json(rows).encode("utf-8")) > 2 * _EVIDENCE_LIMIT_BYTES

    large = _try_prepare(_sink(url, _FIXED_SCHEMA), _effect_input(rows), "a" * 64)
    assert not isinstance(large, ValueError), f"prepare_effect refused a batch above the evidence bound: {large}"
    small = _try_prepare(_sink(url, _FIXED_SCHEMA), _effect_input(rows[:3]), "b" * 64)
    assert not isinstance(small, ValueError), f"prepare_effect refused a three-row batch: {small}"

    # Row values never enter the bounded evidence: its size does not grow with the batch.
    large_size = len(canonical_json(dict(large.safe_evidence)).encode("utf-8"))
    small_size = len(canonical_json(dict(small.safe_evidence)).encode("utf-8"))
    assert large_size - small_size < 16
    assert "name-1999-" not in canonical_json(dict(large.safe_evidence))


def test_large_batch_commits_every_row_on_the_preparing_instance(tmp_path: Path) -> None:
    url = f"sqlite:///{tmp_path / 'commit.db'}"
    _provision(url)
    sink = _sink(url, _FIXED_SCHEMA)
    plan = _try_prepare(sink, _effect_input(_large_rows()), "c" * 64)
    assert not isinstance(plan, ValueError), f"prepare_effect refused a batch above the evidence bound: {plan}"

    result = sink.commit_effect(plan, _CTX)

    assert result.accepted_ordinals == tuple(range(2000))
    assert result.diverted_ordinals == ()
    assert _row_count(url) == 2000


def test_recovery_restages_rows_from_durable_members_before_commit(tmp_path: Path) -> None:
    assert issubclass(DatabaseSink, RestagingSinkEffectCapability), "DatabaseSink must rebuild commit rows from durable members"
    url = f"sqlite:///{tmp_path / 'recover.db'}"
    _provision(url, "meta")
    observed: dict[str, object] = {"mode": "observed"}
    # A nested value with unsorted keys and an integral float: the payload store
    # hands recovery the canonical form, so the SQL serialization must agree.
    rows: list[dict[str, object]] = [
        {"id": index, "name": f"name-{index}-" + "y" * 100, "meta": {"z": 1.0, "a": [index, "é"]}} for index in range(1500)
    ]
    effect_input = _effect_input(rows)
    plan = _try_prepare(_sink(url, observed), effect_input, "d" * 64)
    assert not isinstance(plan, ValueError), f"prepare_effect refused a batch above the evidence bound: {plan}"

    # A fresh instance (resume / another worker) has no rows until restaged.
    unstaged = _sink(url, observed)
    with pytest.raises(DatabaseEffectLedgerError, match="restage"):
        unstaged.commit_effect(plan, _CTX)
    assert _row_count(url) == 0

    # Restaging from a divergent member set is refused before any write.
    tampered = _effect_input([*rows[:-1], {**rows[-1], "name": "tampered"}])
    with pytest.raises(DatabaseEffectLedgerError, match="payload hash"):
        unstaged.restage_effect(plan, tampered, _CTX)

    recovering = _sink(url, observed)
    recovering.restage_effect(plan, _hydrated(effect_input), _CTX)
    result = recovering.commit_effect(plan, _CTX)

    assert result.accepted_ordinals == tuple(range(1500))
    assert _row_count(url) == 1500
    engine = create_engine(url)
    target = Table("output", MetaData(), autoload_with=engine)
    with engine.connect() as conn:
        stored_meta = conn.scalar(select(target.c.meta).where(target.c.id == 7))
    engine.dispose()
    assert stored_meta == canonical_json({"a": [7, "é"], "z": 1})
