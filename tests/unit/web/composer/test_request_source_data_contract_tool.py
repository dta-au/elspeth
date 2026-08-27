"""Tool-boundary tests for requesting a source_data_contract review.

The planner may REQUEST the review; the demanded field set and the card
draft are computed server-side from the graph (elspeth-da68332faf work
item 2). These tests pin the ``_assert_affected_component`` arm and the tool
schema advertisement.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.source_demand import (
    SOURCE_DATA_CONTRACT_USER_TERM,
    build_source_data_contract_draft,
)
from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._dispatch import get_tool_definitions
from elspeth.web.composer.tools.sessions import _assert_affected_component


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
    with pytest.raises(ToolArgumentError):
        _assert_affected_component(
            state,
            "source",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            forged,
        )


def test_boundary_rejects_when_no_demand_exists() -> None:
    state = _uploaded_state(required=[])
    with pytest.raises(ToolArgumentError):
        _assert_affected_component(
            state,
            "source",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            None,
        )


def test_boundary_rejects_node_targets() -> None:
    state = _uploaded_state(required=["colour"])
    with pytest.raises(ToolArgumentError):
        _assert_affected_component(
            state,
            "rate",
            InterpretationKind.SOURCE_DATA_CONTRACT,
            SOURCE_DATA_CONTRACT_USER_TERM,
            None,
        )


def test_tool_schema_advertises_the_kind() -> None:
    definitions = {defn["name"]: defn for defn in get_tool_definitions()}
    kinds = definitions["request_interpretation_review"]["parameters"]["properties"]["kind"]["enum"]
    assert "source_data_contract" in kinds
