"""Tool-boundary tests for requesting a source_data_contract review.

The planner may REQUEST the review; the demanded field set and the card
draft are computed server-side from the graph (elspeth-da68332faf work
item 2). These tests pin the ``_assert_affected_component`` arm and the tool
schema advertisement.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest

from elspeth.contracts.composer_interpretation import InterpretationEventRecord, InterpretationKind
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    build_source_data_contract_draft,
)
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.sessions import (
    _assert_affected_component,
    _handle_request_interpretation_review,
)


def _uploaded_state(*, required: list[str], source_options: dict[str, Any] | None = None) -> CompositionState:
    return CompositionState(
        source=None,
        sources={
            "source": SourceSpec(
                plugin="csv",
                on_success="source",
                options=source_options if source_options is not None else {"path": "/tmp/nonexistent-upload.csv"},
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="rate",
                node_type="transform",
                plugin="llm",
                input="source",
                on_success="rated",
                on_error="discard",
                options={
                    "prompt_template": "Rate {{ row.colour }}",
                    "model": "gpt-test",
                    "schema": {"mode": "observed"},
                    "required_input_fields": required,
                },
                condition=None,
                routes=None,
                fork_to=None,
                branches=None,
                policy=None,
                merge=None,
            ),
        ),
        edges=(),
        outputs=(),
        metadata=PipelineMetadata(),
        version=1,
    )


def test_boundary_returns_server_computed_draft() -> None:
    state = _uploaded_state(required=["colour"])
    draft = _assert_affected_component(
        state,
        "source",
        InterpretationKind.SOURCE_DATA_CONTRACT,
        SOURCE_DATA_CONTRACT_USER_TERM,
        None,
    )
    # No sample file exists, so the draft carries the demand with no sample.
    assert draft == build_source_data_contract_draft(["colour"], None)


def test_boundary_rejects_caller_supplied_field_list() -> None:
    state = _uploaded_state(required=["colour"])
    forged = build_source_data_contract_draft(["colour", "extra"], None)
    with pytest.raises(ToolArgumentError) as caught:
        _assert_affected_component(
            state,
            "source",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            forged,
        )

    assert caught.value.expected == "omitted — the server computes the data-contract card from the graph's demand backtrace"
    assert caught.value.actual_type == "caller-supplied draft that does not match the server-computed data contract"


def test_boundary_rejects_when_no_demand_exists() -> None:
    state = _uploaded_state(required=[])
    with pytest.raises(ToolArgumentError) as caught:
        _assert_affected_component(
            state,
            "source",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            None,
        )

    assert "outstanding data-contract demand" in caught.value.expected
    assert caught.value.actual_type == "missing pending source_data_contract review site"


def test_boundary_rejects_node_targets() -> None:
    state = _uploaded_state(required=["colour"])
    with pytest.raises(ToolArgumentError) as caught:
        _assert_affected_component(
            state,
            "rate",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            None,
        )

    assert caught.value.expected == "'source' for source_data_contract or 'source:<name>' for a named source"
    assert caught.value.actual_type == "node id"


def test_boundary_preserves_missing_named_source_repair_without_echoing_name() -> None:
    state = _uploaded_state(required=["colour"])

    with pytest.raises(ToolArgumentError) as caught:
        _assert_affected_component(
            state,
            "source:operator-private-name",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            None,
        )

    assert caught.value.expected == "an existing source component"
    assert caught.value.actual_type == "missing source component"
    assert "operator-private-name" not in caught.value.safe_message


@pytest.mark.asyncio
async def test_backend_only_kind_rejection_lists_every_requestable_kind() -> None:
    async def unused_create_event(**_kwargs: object) -> InterpretationEventRecord:
        raise AssertionError("backend-only kind must be rejected before event creation")

    async def unused_list_events(*_args: object, **_kwargs: object) -> list[InterpretationEventRecord]:
        raise AssertionError("backend-only kind must be rejected before event lookup")

    state = _uploaded_state(required=["colour"])
    expected_kinds = ", ".join(kind.value for kind in InterpretationKind if kind is not InterpretationKind.LLM_PROMPT_TEMPLATE)

    with pytest.raises(ToolArgumentError) as caught:
        await _handle_request_interpretation_review(
            {
                "affected_node_id": "rate",
                "kind": InterpretationKind.LLM_PROMPT_TEMPLATE.value,
                "user_term": "llm_prompt_template:rate",
            },
            state,
            session_id=uuid4(),
            composition_state_id=uuid4(),
            tool_call_id="call_prompt_template",
            now=datetime(2026, 1, 1, tzinfo=UTC),
            per_term_cap=3,
            per_session_day_cap=10,
            model_identifier="test-model",
            model_version="test-version",
            provider="test-provider",
            composer_skill_hash="a" * 64,
            create_pending_interpretation_event=unused_create_event,
            list_interpretation_events=unused_list_events,
        )

    assert caught.value.argument == "kind"
    assert caught.value.expected == expected_kinds


def test_tool_schema_advertises_the_kind() -> None:
    definitions = {defn["name"]: defn for defn in get_tool_definitions()}
    kinds = definitions["request_interpretation_review"]["parameters"]["properties"]["kind"]["enum"]
    assert "source_data_contract" in kinds
