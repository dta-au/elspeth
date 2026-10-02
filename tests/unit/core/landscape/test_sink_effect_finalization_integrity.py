"""Finalization refuses divergent durable evidence without partial audit writes."""

from __future__ import annotations

import json
from collections.abc import Iterator
from dataclasses import replace
from datetime import timedelta

import pytest
from sqlalchemy import delete, select, update

from elspeth.contracts.sink_effects import SinkEffectDescriptorMode, SinkEffectReconcileKind
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape._helpers import now
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.errors import LandscapeRecordError
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import (
    artifacts_table,
    node_states_table,
    operations_table,
    sink_effect_attempts_table,
    sink_effect_members_table,
    sink_effect_streams_table,
    sink_effects_table,
    token_outcomes_table,
)
from tests.fixtures.landscape import leader_coordination_token, make_factory, make_landscape_db
from tests.unit.core.landscape.test_sink_effect_finalization import _descriptor, _prepared, _request


@pytest.fixture
def recorder() -> Iterator[tuple[LandscapeDB, RecorderFactory]]:
    db = make_landscape_db()
    try:
        yield db, make_factory(db)
    finally:
        db.close()


def _audit_snapshot(db: LandscapeDB) -> tuple[tuple[tuple[object, ...], ...], ...]:
    """Compare every durable row in the transaction's publication footprint."""
    tables = (
        artifacts_table,
        node_states_table,
        operations_table,
        sink_effect_attempts_table,
        sink_effect_members_table,
        sink_effect_streams_table,
        sink_effects_table,
        token_outcomes_table,
    )
    with db.read_only_connection() as conn:
        return tuple(tuple(sorted((tuple(row) for row in conn.execute(select(table))), key=repr)) for table in tables)


@pytest.mark.parametrize(
    "corruption",
    [
        "expired-lease",
        "not-in-flight",
        "invalid-plan-json",
        "non-object-plan",
        "plan-effect",
        "plan-input-kind",
        "plan-hash",
        "plan-mode",
        "missing-attempt",
        "unknown-attempt",
        "attempt-not-returned",
        "attempt-future-generation",
        "attempt-wrong-action",
        "attempt-hash",
        "request-reconcile-kind",
        "request-virtual",
        "missing-operation",
        "closed-operation",
        "state-input",
    ],
)
def test_first_finalization_refuses_corruption_atomically(
    recorder: tuple[LandscapeDB, RecorderFactory],
    corruption: str,
) -> None:
    db, factory = recorder
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    with db.engine.begin() as conn:
        effect_where = sink_effects_table.c.effect_id == effect.effect_id
        attempt_where = sink_effect_attempts_table.c.attempt_id == request.attempt_id
        if corruption == "expired-lease":
            conn.execute(
                update(sink_effects_table)
                .where(effect_where)
                .values(lease_expires_at=now() - timedelta(days=1), lease_heartbeat_at=now() - timedelta(days=2))
            )
        elif corruption == "not-in-flight":
            conn.execute(
                update(sink_effects_table)
                .where(effect_where)
                .values(state="prepared", lease_owner=None, lease_expires_at=None, lease_heartbeat_at=None)
            )
        elif corruption == "invalid-plan-json":
            conn.execute(update(sink_effects_table).where(effect_where).values(plan_json="{"))
        elif corruption == "non-object-plan":
            conn.execute(update(sink_effects_table).where(effect_where).values(plan_json="[]"))
        elif corruption.startswith("plan-"):
            plan = json.loads(conn.scalar(select(sink_effects_table.c.plan_json).where(effect_where)))
            field = {"plan-effect": "effect_id", "plan-input-kind": "input_kind", "plan-hash": "plan_hash", "plan-mode": "descriptor_mode"}[
                corruption
            ]
            plan[field] = "divergent"
            conn.execute(update(sink_effects_table).where(effect_where).values(plan_json=canonical_json(plan)))
        elif corruption == "missing-attempt":
            request = replace(request, attempt_id=None)
        elif corruption == "unknown-attempt":
            request = replace(request, attempt_id="0" * 64)
        elif corruption == "attempt-not-returned":
            conn.execute(update(sink_effect_attempts_table).where(attempt_where).values(state="response_lost"))
        elif corruption == "attempt-future-generation":
            conn.execute(update(sink_effect_attempts_table).where(attempt_where).values(generation=lease.generation + 1))
        elif corruption == "attempt-wrong-action":
            conn.execute(update(sink_effect_attempts_table).where(attempt_where).values(action="reconcile"))
        elif corruption == "attempt-hash":
            conn.execute(update(sink_effect_attempts_table).where(attempt_where).values(evidence_hash="0" * 64))
        elif corruption == "request-reconcile-kind":
            request = replace(request, reconcile_kind=SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR)
        elif corruption == "request-virtual":
            request = replace(request, publication_performed=False, publication_evidence_kind="virtual")
        elif corruption == "missing-operation":
            conn.execute(update(operations_table).where(operations_table.c.sink_effect_id == effect.effect_id).values(sink_effect_id=None))
        elif corruption == "closed-operation":
            conn.execute(update(operations_table).where(operations_table.c.sink_effect_id == effect.effect_id).values(status="completed"))
        elif corruption == "state-input":
            conn.execute(update(node_states_table).where(node_states_table.c.token_id == members[0].token_id).values(input_hash="0" * 64))
        else:
            raise AssertionError(corruption)
    before = _audit_snapshot(db)
    with pytest.raises(LandscapeRecordError):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    assert _audit_snapshot(db) == before


@pytest.mark.parametrize(
    "corruption",
    [
        "generation",
        "descriptor-hash",
        "publication",
        "artifact-missing",
        "artifact-path",
        "artifact-hash",
        "state-missing",
        "outcome-missing",
    ],
)
def test_finalized_retry_requires_unchanged_winner_evidence(
    recorder: tuple[LandscapeDB, RecorderFactory],
    corruption: str,
) -> None:
    db, factory = recorder
    effect, members, lease = _prepared(factory, count=1)
    request = _request(factory, effect, members, lease)
    winner = factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    assert factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id)) == winner
    with db.engine.begin() as conn:
        if corruption == "generation":
            request = replace(request, generation=lease.generation + 1)
        elif corruption == "descriptor-hash":
            conn.execute(
                update(sink_effects_table).where(sink_effects_table.c.effect_id == effect.effect_id).values(result_descriptor_hash="0" * 64)
            )
        elif corruption == "publication":
            request = replace(
                request, publication_evidence_kind="reconciled", reconcile_kind=SinkEffectReconcileKind.APPLIED_WITH_EXACT_DESCRIPTOR
            )
        elif corruption == "artifact-missing":
            conn.execute(delete(artifacts_table).where(artifacts_table.c.artifact_id == winner.artifact.artifact_id))
        elif corruption == "artifact-path":
            conn.execute(
                update(artifacts_table)
                .where(artifacts_table.c.artifact_id == winner.artifact.artifact_id)
                .values(path_or_uri="file:///tmp/divergent")
            )
        elif corruption == "artifact-hash":
            conn.execute(
                update(artifacts_table).where(artifacts_table.c.artifact_id == winner.artifact.artifact_id).values(content_hash="0" * 64)
            )
        elif corruption == "state-missing":
            conn.execute(delete(node_states_table).where(node_states_table.c.token_id == members[0].token_id))
        elif corruption == "outcome-missing":
            conn.execute(delete(token_outcomes_table).where(token_outcomes_table.c.token_id == members[0].token_id))
        else:
            raise AssertionError(corruption)
    before = _audit_snapshot(db)
    with pytest.raises(LandscapeRecordError):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    assert _audit_snapshot(db) == before


@pytest.mark.parametrize(
    "attribution",
    [
        None,
        {},
        [],
        [None],
        [{"ordinal": 1, "reason_hash": "a" * 64, "error_hash": "b" * 16}, {"ordinal": 1, "reason_hash": "a" * 64, "error_hash": "b" * 16}],
        [{"ordinal": 1, "reason_hash": "A" * 64, "error_hash": "b" * 16}],
        [{"ordinal": 1, "reason_hash": "a" * 64, "error_hash": "b" * 15}],
        [{"ordinal": 0, "reason_hash": "a" * 64, "error_hash": "b" * 16}],
    ],
)
def test_result_derived_diversion_attribution_is_exact_and_atomic(
    recorder: tuple[LandscapeDB, RecorderFactory],
    attribution: object,
) -> None:
    db, factory = recorder
    effect, members, lease = _prepared(factory, count=2, descriptor_mode=SinkEffectDescriptorMode.RESULT_DERIVED)
    descriptor = _descriptor()
    evidence = {
        "accepted_ordinals": [0],
        "diverted_ordinals": [1],
        "descriptor": {
            "artifact_type": descriptor.artifact_type,
            "path_or_uri": descriptor.path_or_uri,
            "content_hash": descriptor.content_hash,
            "size_bytes": descriptor.size_bytes,
            "metadata": None,
        },
        "diversion_attribution": attribution,
    }
    request = _request(factory, effect, members, lease, evidence=evidence)
    request = replace(request, accepted_ordinals=(0,), diverted_ordinals=(1,), members=request.members[:1])
    before = _audit_snapshot(db)
    with pytest.raises(LandscapeRecordError, match="attribution"):
        factory.execution.sink_effects.finalize(request, coordination_token=leader_coordination_token(factory, effect.run_id))
    assert _audit_snapshot(db) == before
