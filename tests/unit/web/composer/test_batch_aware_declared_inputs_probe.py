"""``required_input_fields`` on a batch-aware transform is a typed validation error, never a crash.

A batch-aware transform refuses ADR-013 declared input fields in its
constructor with ``FrameworkBugError`` (``plugins/infrastructure/base.py``),
an engine-defect class no probe site tolerates. Authored as a row
``transform``, the node is constructed by the validation probe, so a plain
string in an authored option made ``CompositionState.validate()`` raise
(deferred-contract audit 5.5). The node rules already name the defect with
public codes (``batch_transform_misplaced``, ``batch_required_fields_invalid``);
the probe now treats it as a config failure, so those are what the author sees.
Plugins come from the live registry, so a new batch-aware transform is covered.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer.state import CompositionState
from tests.unit.web.composer import test_deferred_contract_notice as graphs

_BATCH_AWARE = sorted(cls.name for cls in get_shared_plugin_manager().get_transforms() if cls.is_batch_aware)


def _graph(tmp_path: Path, plugin: str, node_type: str, *, required_input_fields: list[str] | None) -> CompositionState:
    options: dict[str, Any] = dict(get_shared_plugin_manager().get_transform_by_name(plugin).probe_config())
    if required_input_fields is not None:
        options["required_input_fields"] = required_input_fields
    if node_type == "aggregation":
        options = {"trigger": {"count": 2}, "output_mode": "transform", "options": options}
    node = graphs._node(
        id="t", node_type=node_type, plugin=plugin, input="src_out", on_success="output", on_error="discard", options=options
    )
    return graphs._graph(tmp_path, node, source_schema=graphs.FIXED_SOURCE)


def _codes(state: CompositionState) -> set[str | None]:
    return {entry.error_code for entry in state.validate().errors}


def test_the_registry_has_batch_aware_transforms() -> None:
    assert len(_BATCH_AWARE) >= 10, _BATCH_AWARE


@pytest.mark.parametrize("plugin", _BATCH_AWARE)
def test_declared_input_fields_on_a_misplaced_batch_aware_transform_are_typed_errors(tmp_path: Path, plugin: str) -> None:
    codes = _codes(_graph(tmp_path, plugin, "transform", required_input_fields=["k"]))

    assert {"batch_transform_misplaced", "batch_required_fields_invalid"} <= codes, codes


@pytest.mark.parametrize("plugin", _BATCH_AWARE)
def test_without_declared_input_fields_only_the_placement_is_reported(tmp_path: Path, plugin: str) -> None:
    """Control: the same node without the option reports the placement alone."""
    codes = _codes(_graph(tmp_path, plugin, "transform", required_input_fields=None))

    assert "batch_transform_misplaced" in codes
    assert "batch_required_fields_invalid" not in codes


@pytest.mark.parametrize("plugin", _BATCH_AWARE)
def test_an_aggregation_with_declared_input_fields_keeps_its_typed_error(tmp_path: Path, plugin: str) -> None:
    """Control: the aggregation placement, unchanged by this fix."""
    codes = _codes(_graph(tmp_path, plugin, "aggregation", required_input_fields=["k"]))

    assert codes == {"batch_required_fields_invalid"}, codes


def test_a_non_batch_transform_still_declares_its_input_fields(tmp_path: Path) -> None:
    """Control: the probe still constructs a non-batch transform with declared input fields."""
    node = graphs._transform("t", "passthrough", "src_out", "output", {"schema": {"mode": "observed"}, "required_input_fields": ["nope"]})
    summary = graphs._graph(tmp_path, node, source_schema=graphs.FIXED_SOURCE).validate()

    assert any("requires fields: [nope]" in entry.message for entry in summary.errors), [entry.message for entry in summary.errors]


def test_a_non_batch_transform_with_declared_input_fields_still_constructs_and_forwards(tmp_path: Path) -> None:
    """Control: the probe constructs it, so its forwarded guarantees reach a sink that requires one (no fail-closed vote)."""
    node = graphs._transform("t", "passthrough", "src_out", "output", {"schema": {"mode": "observed"}, "required_input_fields": ["k"]})
    summary = graphs._graph(
        tmp_path, node, source_schema=graphs.FIXED_SOURCE, sink_schema={"mode": "observed", "required_fields": ["k"]}
    ).validate()

    assert summary.is_valid, [entry.to_dict() for entry in summary.errors]
    assert [entry.error_code for entry in summary.warnings if entry.error_code == "contract_probe_failed"] == []
