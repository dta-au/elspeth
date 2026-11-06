"""The detached metadata value must share ordinary dispatch semantics."""

from __future__ import annotations

import pytest

from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.tools import _dispatch
from tests.unit.web.composer.test_tools import _empty_state, _trained_tool_context


def test_prepared_metadata_value_matches_ordinary_dispatch_result() -> None:
    state = _empty_state()
    context = _trained_tool_context()
    arguments = {"patch": {"name": "After", "description": "A retained change"}}
    prior = context.catalog.validate_composition_state(state).validation

    prepared = _dispatch.prepare_set_metadata_result_value(
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        prior_validation=prior,
    )
    ordinary = _dispatch.execute_tool(
        "set_metadata",
        arguments,
        state,
        context.catalog,
        plugin_snapshot=context.plugin_snapshot,
        prior_validation=prior,
        validate_arguments=True,
        require_data_dir_for_paths=True,
        raise_schema_argument_errors=True,
    )

    assert state.version == 1
    assert state.metadata.name == "Untitled Pipeline"
    assert prepared.success and ordinary.success
    assert prepared.updated_state is not state
    assert prepared.updated_state.version == 2
    assert prepared.to_dict() == ordinary.to_dict()


def test_prepared_metadata_rejects_the_same_closed_schema() -> None:
    state = _empty_state()
    context = _trained_tool_context()
    arguments = {"name": "not-a-patch"}

    with pytest.raises(ToolArgumentError) as prepared_error:
        _dispatch.prepare_set_metadata_result_value(arguments, state, context.catalog, plugin_snapshot=context.plugin_snapshot)
    with pytest.raises(ToolArgumentError) as ordinary_error:
        _dispatch.execute_tool(
            "set_metadata",
            arguments,
            state,
            context.catalog,
            plugin_snapshot=context.plugin_snapshot,
            validate_arguments=True,
            raise_schema_argument_errors=True,
        )

    assert prepared_error.value.category == ordinary_error.value.category
    assert state.version == 1


def test_prepared_metadata_rejects_a_foreign_catalog_snapshot() -> None:
    state = _empty_state()
    context = _trained_tool_context()
    foreign = _trained_tool_context()

    with pytest.raises(ValueError, match="plugin_snapshot_catalog_mismatch"):
        _dispatch.prepare_set_metadata_result_value(
            {"patch": {"name": "After"}}, state, context.catalog, plugin_snapshot=foreign.plugin_snapshot
        )
    assert state.version == 1


def test_prepared_metadata_closes_if_the_registered_handler_changes(monkeypatch: pytest.MonkeyPatch) -> None:
    state = _empty_state()
    context = _trained_tool_context()
    monkeypatch.setattr(_dispatch, "_handle_set_metadata", lambda *_args: None)

    with pytest.raises(AssertionError, match="closed declaration"):
        _dispatch.prepare_set_metadata_result_value(
            {"patch": {"name": "After"}}, state, context.catalog, plugin_snapshot=context.plugin_snapshot
        )
    assert state.version == 1
