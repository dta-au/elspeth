from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import pytest

from elspeth.contracts.freeze import deep_freeze
from elspeth.web.composer.proposals import build_tool_proposal_summary
from elspeth.web.composer.tools import get_tool_definitions, is_mutation_tool
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS, ASYNC_TOOL_EFFECTS, resolve_tool_effects
from elspeth.web.composer.tools.declarations import EffectDomain, ToolDeclaration, ToolKind
from elspeth.web.composer.tools.sessions import _SESSION_AWARE_TOOL_HANDLERS


def test_is_mutation_tool_uses_closed_registries() -> None:
    assert is_mutation_tool("set_pipeline") is True
    assert is_mutation_tool("set_source_from_blob") is True
    assert is_mutation_tool("get_pipeline_state") is False
    assert is_mutation_tool("preview_pipeline") is False


def test_set_pipeline_summary_is_plain_language() -> None:
    summary = build_tool_proposal_summary(
        tool_name="set_pipeline",
        arguments={
            "source": {"plugin": "csv", "options": {}},
            "nodes": [{"id": "classify_severity", "plugin": "llm_classifier"}],
            "outputs": [{"name": "out", "plugin": "json"}],
        },
        redacted_arguments={
            "source": {"plugin": "csv", "options": {}},
            "nodes": [{"id": "classify_severity", "plugin": "llm_classifier"}],
            "outputs": [{"name": "out", "plugin": "json"}],
        },
    )

    assert summary.summary == "Replace the pipeline with 1 input, 1 processing node, and 1 output."
    assert summary.rationale == "Requested by the current composer turn."
    assert summary.affects == ("graph", "validation", "yaml")


@pytest.mark.parametrize("declaration", _REGISTERED_TOOLS, ids=lambda declaration: declaration.name)
def test_every_declared_tool_has_owned_effects(declaration: ToolDeclaration) -> None:
    effects = resolve_tool_effects(declaration.name, {})
    assert all(isinstance(domain, EffectDomain) for domain in effects.domains)
    if declaration.kind in {ToolKind.DISCOVERY, ToolKind.BLOB_DISCOVERY, ToolKind.SECRET_DISCOVERY}:
        expected = ()
    elif declaration.blob_store_only:
        expected = ("blob_store",)
    else:
        expected = ("graph", "validation", "yaml")
    assert tuple(domain.value for domain in effects.domains) == expected


def test_effect_authority_matches_shipped_and_async_tools() -> None:
    assert set(ASYNC_TOOL_EFFECTS) == set(_SESSION_AWARE_TOOL_HANDLERS) | {"request_advisor_hint"}
    assert {declaration.name for declaration in _REGISTERED_TOOLS} | set(ASYNC_TOOL_EFFECTS) == {
        definition["name"] for definition in get_tool_definitions()
    }


@pytest.mark.parametrize(
    ("name", "arguments", "expected"),
    [
        ("get_pipeline_state", {}, ()),
        ("create_blob", {}, ("blob_store",)),
        ("update_blob", {"content": None}, ("blob_store",)),
        ("delete_blob", {}, ("blob_store",)),
        ("wire_blob_inline_ref", {}, ("graph", "validation", "yaml")),
        ("set_pipeline", {"source": {"inline_blob": {}}}, ("graph", "validation", "yaml", "blob_store")),
        ("set_pipeline", {"source": {"inline_blob": None}}, ("graph", "validation", "yaml")),
        ("set_pipeline", {"source": {"blob_id": "custodied"}}, ("graph", "validation", "yaml")),
        ("set_pipeline", {"source": "invalid"}, ("graph", "validation", "yaml")),
        ("request_interpretation_review", {}, ("interpretation",)),
        ("request_advisor_hint", {}, ()),
    ],
)
@pytest.mark.parametrize("frozen", [False, True])
def test_summary_uses_prospective_effects(name: str, arguments: dict[str, Any], expected: tuple[str, ...], frozen: bool) -> None:
    owned: Mapping[str, Any] = deep_freeze(arguments) if frozen else arguments
    summary = build_tool_proposal_summary(tool_name=name, arguments=owned, redacted_arguments={})
    assert summary.affects == expected


def test_unknown_tool_rejects_summary_and_effect_lookup() -> None:
    with pytest.raises(AssertionError, match="Unknown composer tool"):
        resolve_tool_effects("unknown", {})
    with pytest.raises(AssertionError, match="Unknown composer tool"):
        build_tool_proposal_summary(tool_name="unknown", arguments={}, redacted_arguments={})


def test_effect_resolution_rejects_corrupted_owned_kind() -> None:
    declaration = ToolDeclaration(
        name="corrupted",
        handler=_REGISTERED_TOOLS[0].handler,
        kind=ToolKind.DISCOVERY,
        description="Exercise an owned declaration corrupted after admission.",
        json_schema={"type": "object", "properties": {}, "additionalProperties": False},
    )
    object.__setattr__(declaration, "kind", "unknown")
    with pytest.raises(AssertionError, match="Unsupported tool kind"):
        declaration.resolve_effects({})


def test_named_inputs_and_mixed_processing_nodes() -> None:
    arguments = deep_freeze(
        {
            "sources": {"first": {}, "second": {}},
            "nodes": [{"node_type": "transform"}, {"node_type": "gate"}, {"node_type": "collector"}],
            "outputs": [{}, {}],
        }
    )
    summary = build_tool_proposal_summary(tool_name="set_pipeline", arguments=arguments, redacted_arguments={})
    assert summary.summary == "Replace the pipeline with 2 inputs, 3 processing nodes, and 2 outputs."


@pytest.mark.parametrize(
    ("name", "key", "expected"),
    [
        ("set_source", "plugin", "Set the pipeline source to safe-label."),
        ("patch_node_options", "node_id", 'Update options for node "safe-label".'),
        ("patch_output_options", "sink_name", 'Update options for output "safe-label".'),
        ("update_blob", "blob_id", 'Overwrite session file "safe-label".'),
        ("delete_blob", "blob_id", 'Delete session file "safe-label".'),
    ],
)
def test_labels_only_use_redacted_arguments(name: str, key: str, expected: str) -> None:
    summary = build_tool_proposal_summary(
        tool_name=name,
        arguments={key: "SECRET_SENTINEL_RAW", "content": "SECRET_SENTINEL_RAW"},
        redacted_arguments={key: "safe-label"},
    )
    assert summary.summary == expected
    assert "SECRET_SENTINEL_RAW" not in repr(summary)


def test_redacted_blob_identifier_uses_generic_file_label() -> None:
    summary = build_tool_proposal_summary(tool_name="delete_blob", arguments={}, redacted_arguments={"blob_id": "<redacted>"})
    assert summary.summary == "Delete a session file."


def test_patch_node_options_summary_names_target_node() -> None:
    summary = build_tool_proposal_summary(
        tool_name="patch_node_options",
        arguments={"node_id": "classify_severity", "patch": {"model": "claude-haiku-4-5"}},
        redacted_arguments={"node_id": "classify_severity", "patch": {"model": "claude-haiku-4-5"}},
    )

    assert summary.summary == 'Update options for node "classify_severity".'
    assert summary.affects == ("graph", "validation", "yaml")
