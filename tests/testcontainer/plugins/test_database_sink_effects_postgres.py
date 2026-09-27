"""Real PostgreSQL proof for atomic Database sink effect markers."""

from __future__ import annotations

from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from hashlib import sha256

import pytest
from sqlalchemy import CheckConstraint, Column, Integer, MetaData, Table, Text, create_engine, func, insert, select
from sqlalchemy.engine import make_url
from tests.fixtures.base_classes import inject_write_failure
from tests.helpers.postgres_target import postgres_test_target

from elspeth.contracts.hashing import canonical_json
from elspeth.contracts.sink_effects import (
    RestrictedSinkEffectContext,
    SinkEffectInspectionRequest,
    SinkEffectMember,
    SinkEffectPipelineMembersInput,
    SinkEffectPrepareRequest,
    SinkEffectReconcileKind,
)
from elspeth.plugins.sinks.database_sink import DatabaseSink, database_effect_ledger_table

pytestmark = pytest.mark.testcontainer

_CTX = RestrictedSinkEffectContext(
    run_id="run-postgres-effect",
    run_started_at=datetime(2026, 7, 16, tzinfo=UTC),
    operation_id="operation-postgres-effect",
    sink_node_id="sink-postgres-effect",
)


@pytest.fixture(scope="module")
def postgres_url() -> Iterator[str]:
    with postgres_test_target(driver="psycopg") as postgres_url:
        yield postgres_url


def _member(ordinal: int, row: dict[str, object]) -> SinkEffectMember:
    payload = canonical_json(row).encode()
    return SinkEffectMember(
        ordinal=ordinal,
        token_id=f"token-pg-{ordinal}",
        row_id=f"row-pg-{ordinal}",
        ingest_sequence=ordinal,
        lineage_json="[]",
        lineage_hash=sha256(b"[]").hexdigest(),
        payload_hash=sha256(payload).hexdigest(),
        row=row,
        member_effect_id=sha256(f"member-pg-{ordinal}".encode()).hexdigest(),
    )


def test_postgres_marker_rows_and_result_partition_commit_atomically(postgres_url: str) -> None:
    engine = create_engine(postgres_url)
    metadata = MetaData()
    target = Table(
        "database_effect_output",
        metadata,
        Column("id", Integer, nullable=False, unique=True),
        Column("name", Text, nullable=False),
    )
    ledger = database_effect_ledger_table(metadata, "_elspeth_sink_effects")
    metadata.drop_all(engine, checkfirst=True)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(target), [{"id": 2, "name": "existing"}])

    config = {
        "url": postgres_url,
        "table": "database_effect_output",
        "schema": {"mode": "fixed", "fields": ["id: int", "name: str"]},
        "if_exists": "append",
        "effect_ledger": {
            "table": "_elspeth_sink_effects",
            "schema_version": 1,
            "permissions": ["select", "insert"],
        },
    }
    sink = inject_write_failure(DatabaseSink(config))
    members = tuple(
        _member(ordinal, row)
        for ordinal, row in enumerate(({"id": 1, "name": "one"}, {"id": 2, "name": "duplicate"}, {"id": 3, "name": "three"}))
    )
    inspection = sink.inspect_effect(
        SinkEffectInspectionRequest(effect_id="d" * 64, target="{}", predecessor_descriptor=None),
        _CTX,
    )
    plan = sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id="d" * 64,
            effect_input=SinkEffectPipelineMembersInput(
                members=members, target_snapshot_members=members, target_delivered_member_count=len(members)
            ),
            inspection=inspection,
        ),
        _CTX,
    )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(inject_write_failure(DatabaseSink(config)).commit_effect, plan, _CTX) for _ in range(2)]
        committed, concurrent_replay = (future.result(timeout=10) for future in futures)
    recovered = inject_write_failure(DatabaseSink(config)).reconcile_effect(plan, _CTX)

    assert concurrent_replay == committed
    assert committed.accepted_ordinals == (0, 2)
    assert committed.diverted_ordinals == (1,)
    assert recovered.kind is SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
    assert recovered.descriptor == committed.descriptor
    assert recovered.accepted_ordinals == committed.accepted_ordinals
    assert recovered.diverted_ordinals == committed.diverted_ordinals
    with engine.connect() as conn:
        assert conn.scalar(select(func.count()).select_from(target)) == 3
        assert conn.scalar(select(func.count()).select_from(ledger)) == 1
    engine.dispose()


_DUPLICATE_EMAIL = "SNTL_PG_DUP_EMAIL@example.com"
_CHECK_ROW_EMAIL = "SNTL_PG_CHECK_ROW@example.com"
_NOT_NULL_ROW_EMAIL = "SNTL_PG_NOTNULL_ROW@example.com"
_NOT_A_NUMBER = "SNTL_PG_NOT_A_NUMBER"


@pytest.mark.parametrize("driver", ["psycopg", "psycopg2"])
def test_postgres_constraint_diversion_reasons_carry_no_row_value(postgres_url: str, driver: str) -> None:
    """A diverted row's reason names the failure kind, never the driver's message (review-C1C3-residuals-r1 F2).

    PostgreSQL puts row data in its constraint errors: a unique violation's
    DETAIL is ``Key (email)=(<value>) already exists``, a CHECK or NOT NULL
    violation's is ``Failing row contains (<the whole row>)``, and an invalid
    integer's message quotes the input. The reason is recorded in the audit
    trail and routed, so it is built from the driver's per-SQLSTATE error
    class only. Both PostgreSQL drivers ELSPETH accepts are exercised: bare
    ``postgresql://`` URLs use psycopg2, ``postgresql+psycopg://`` psycopg 3.
    """
    url = make_url(postgres_url).set(drivername=f"postgresql+{driver}").render_as_string(hide_password=False)
    engine = create_engine(url)
    metadata = MetaData()
    target = Table(
        "database_effect_reasons",
        metadata,
        Column("id", Integer, nullable=False, unique=True),
        Column("email", Text, nullable=False, unique=True),
        Column("qty", Integer, nullable=False),
        CheckConstraint("qty >= 0", name="qty_non_negative"),
    )
    database_effect_ledger_table(metadata, "_elspeth_sink_effects_reasons")
    metadata.drop_all(engine, checkfirst=True)
    metadata.create_all(engine)
    with engine.begin() as conn:
        conn.execute(insert(target), [{"id": 100, "email": _DUPLICATE_EMAIL, "qty": 1}])
    config = {
        "url": url,
        "table": "database_effect_reasons",
        "schema": {"mode": "fixed", "fields": ["id: int", "email: str", "qty: int?"]},
        "if_exists": "append",
        "effect_ledger": {"table": "_elspeth_sink_effects_reasons", "schema_version": 1, "permissions": ["select", "insert"]},
    }
    sink = inject_write_failure(DatabaseSink(config))
    rows: tuple[dict[str, object], ...] = (
        {"id": 1, "email": "ok-1@example.com", "qty": 1},
        {"id": 2, "email": _DUPLICATE_EMAIL, "qty": 1},
        {"id": 3, "email": _CHECK_ROW_EMAIL, "qty": -5},
        {"id": 4, "email": _NOT_NULL_ROW_EMAIL, "qty": None},
        {"id": 5, "email": "ok-5@example.com", "qty": _NOT_A_NUMBER},
    )
    members = tuple(_member(ordinal, row) for ordinal, row in enumerate(rows))
    effect_id = ("e" if driver == "psycopg" else "f") * 64
    inspection = sink.inspect_effect(SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=None), _CTX)
    plan = sink.prepare_effect(
        SinkEffectPrepareRequest(
            effect_id=effect_id,
            effect_input=SinkEffectPipelineMembersInput(
                members=members, target_snapshot_members=members, target_delivered_member_count=len(members)
            ),
            inspection=inspection,
        ),
        _CTX,
    )

    committed = sink.commit_effect(plan, _CTX)

    assert committed.accepted_ordinals == (0,)
    assert committed.diverted_ordinals == (1, 2, 3, 4)
    reasons = [item.reason for item in sink._get_diversions()]
    assert reasons == [
        "Constraint violation: unique_violation (UniqueViolation)",
        "Constraint violation: check_violation (CheckViolation)",
        "Constraint violation: not_null_violation (NotNullViolation)",
        "Constraint violation: data_error (InvalidTextRepresentation)",
    ]
    for sentinel in (_DUPLICATE_EMAIL, _CHECK_ROW_EMAIL, _NOT_NULL_ROW_EMAIL, _NOT_A_NUMBER, "SNTL_PG"):
        assert not any(sentinel in reason for reason in reasons)
    engine.dispose()
