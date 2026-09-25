"""The collector group-failure hold: one builder, one strict parser (elspeth-5887fb7928 S2).

Resume and the counting authority both read a failed member's hold back.
They share this parser, so they cannot disagree on its shape, and the hold is
written by the builder beside it, so writer and reader cannot drift.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from elspeth.contracts.enums import CollectorGroupFailureReason
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json
from elspeth.core.landscape.collector_group_failure_holds import (
    COLLECTOR_GROUP_FAILURE_TYPE,
    RecordedCollectorGroupFailureHold,
    collector_group_failure_hold_error,
    parse_collector_group_failure_hold,
)


def _written(**overrides: Any) -> str:
    """The error_json the verdict writes for one hold (what node_states stores)."""
    error = collector_group_failure_hold_error(
        group_id="g-1", failure_reason=CollectorGroupFailureReason.COLLECTOR_MISSING_MEMBERS, lost_members=("m-2", "m-1")
    )
    data = error.to_dict()
    data["context"] = {**data["context"], **overrides}
    return canonical_json(data)


@pytest.mark.parametrize("reason", list(CollectorGroupFailureReason))
def test_the_parser_reads_back_exactly_what_the_builder_writes(reason: CollectorGroupFailureReason) -> None:
    error = collector_group_failure_hold_error(group_id="g-1", failure_reason=reason, lost_members=())

    parsed = parse_collector_group_failure_hold("t-1", "collector-1", canonical_json(error.to_dict()))

    assert parsed == RecordedCollectorGroupFailureHold(node_id="collector-1", group_id="g-1", failure_reason=reason)
    assert type(parsed.failure_reason) is CollectorGroupFailureReason


def test_the_hold_names_only_minted_identifiers_and_the_reason_code() -> None:
    error = collector_group_failure_hold_error(
        group_id="g-1", failure_reason=CollectorGroupFailureReason.COLLECTOR_MISSING_MEMBERS, lost_members=("m-2", "m-1")
    )

    assert error.exception_type == COLLECTOR_GROUP_FAILURE_TYPE
    assert error.exception == "Collector group 'g-1' failed (collector_missing_members): lost members ['m-1', 'm-2']"
    assert error.context is not None
    assert dict(error.context) == {
        "group_id": "g-1",
        "failure_reason": "collector_missing_members",
        "lost_members": ("m-1", "m-2"),  # the frozen DTO; it serializes as a JSON array
        "member_disposition": "scope_group_failed",
    }


@pytest.mark.parametrize(
    "error_type",
    ["TransformError", "PluginContractViolation", "QuarantinedMember"],
)
def test_any_other_failed_state_is_not_a_group_verdict(error_type: str) -> None:
    """A flush state's own error, or a quarantined member of a successful flush."""
    assert parse_collector_group_failure_hold("t-1", "collector-1", json.dumps({"type": error_type, "exception": "x"})) is None


@pytest.mark.parametrize(
    "overrides",
    [
        {"group_id": ""},
        {"group_id": 7},
        {"failure_reason": "not_a_reason"},
        {"failure_reason": ""},
        {"member_disposition": "late_arrival_after_merge"},
    ],
    ids=["empty-group", "non-str-group", "unknown-reason", "empty-reason", "wrong-disposition"],
)
def test_a_group_failure_hold_that_deviates_from_the_written_shape_is_corruption(overrides: dict[str, Any]) -> None:
    with pytest.raises(AuditIntegrityError, match="does not carry the verdict's failure_reason, group_id"):
        parse_collector_group_failure_hold("t-1", "collector-1", _written(**overrides))


@pytest.mark.parametrize("missing", ["group_id", "failure_reason", "member_disposition"])
def test_a_group_failure_hold_missing_a_verdict_key_is_corruption(missing: str) -> None:
    data = json.loads(_written())
    del data["context"][missing]

    with pytest.raises(AuditIntegrityError, match="does not carry the verdict's failure_reason, group_id"):
        parse_collector_group_failure_hold("t-1", "collector-1", json.dumps(data))


@pytest.mark.parametrize(("error_json", "match"), [(None, "has no error_json"), ("[1]", "malformed error_json"), ("{}", "malformed")])
def test_a_failed_collector_state_without_a_typed_error_is_corruption(error_json: str | None, match: str) -> None:
    with pytest.raises(AuditIntegrityError, match=match):
        parse_collector_group_failure_hold("t-1", "collector-1", error_json)


def _empty_group_run(run_id: str) -> tuple[Any, str]:
    """A run with a collector node and one recorded zero-member group, through the real writers."""
    from elspeth.contracts import NodeType
    from elspeth.contracts.audit import TokenRef
    from tests.fixtures.landscape import make_recorder_with_run, register_test_node

    setup = make_recorder_with_run(run_id=run_id, source_node_id="src")
    register_test_node(setup.factory.data_flow, run_id, "collector-1", node_type=NodeType.COLLECTOR, plugin_name="batch_stats")
    _row, token = setup.factory.data_flow.create_row_with_token(
        "src", row_index=0, data={"value": 1}, source_row_index=0, ingest_sequence=0, coordination_token=setup.coordination_token
    )
    group_id = setup.factory.data_flow.record_empty_expansion(
        TokenRef(token_id=token.token_id, run_id=run_id), member_token=setup.coordination_token.membership
    )
    return setup, group_id


@pytest.mark.parametrize(
    ("hold_group", "hold_reason"),
    [("another-group", CollectorGroupFailureReason.EMPTY_EXPANSION), (None, CollectorGroupFailureReason.COLLECTOR_TRANSFORM_ERROR)],
    ids=["other-group", "other-reason"],
)
def test_the_verdict_writer_refuses_a_hold_error_that_names_another_group_or_reason(
    hold_group: str | None, hold_reason: CollectorGroupFailureReason
) -> None:
    """The group row and every hold are written together; the writer refuses to make them disagree."""
    from sqlalchemy import func, select

    from elspeth.core.landscape.schema import collector_group_failures_table

    setup, group_id = _empty_group_run(f"writer-guard-{hold_reason.value}")

    def record(hold_error: Any) -> None:
        setup.factory.execution.complete_collector_failure(
            coordination_token=setup.coordination_token,
            group_id=group_id,
            collector_node_id="collector-1",
            failure_reason=CollectorGroupFailureReason.EMPTY_EXPANSION,
            flush_state_id=None,
            flush_error=None,
            flush_duration_ms=None,
            member_holds=(),
            hold_error=hold_error,
        )

    with pytest.raises(AuditIntegrityError, match="hold error does not record group"):
        record(collector_group_failure_hold_error(group_id=hold_group or group_id, failure_reason=hold_reason, lost_members=()))
    record(
        collector_group_failure_hold_error(group_id=group_id, failure_reason=CollectorGroupFailureReason.EMPTY_EXPANSION, lost_members=())
    )
    with setup.db.connection() as conn:
        assert conn.execute(select(func.count()).select_from(collector_group_failures_table)).scalar_one() == 1
