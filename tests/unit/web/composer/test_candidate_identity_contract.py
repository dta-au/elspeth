"""Live candidate identity, review canonicalization and shared budget contracts."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from elspeth.web.composer.state import CompositionState, NodeSpec, PipelineMetadata, SourceSpec
from elspeth.web.composer.tools._common import ToolResult
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate, canonicalize_authored_node_review_requirements
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, RAW_HTML_CLEANUP_REVIEW_DRAFT, RAW_HTML_CLEANUP_USER_TERM
from tests.unit.web.composer._scripted_sdk import _INJECTED_CLAUSE, _bad_source, _budget, _candidate_context, _empty_state


def test_state_aware_canonicalization_uses_trusted_existing_source_and_node_ids() -> None:
    source_requirement = {
        "id": "trusted-source-custom-id",
        "kind": "invented_source",
        "user_term": "inline_source_data",
        "status": "resolved",
        "draft": "name,score\nada,42\n",
        "event_id": "source-event",
        "accepted_value": "approved",
        "accepted_artifact_hash": "a" * 64,
        "resolved_prompt_template_hash": None,
    }
    node_requirement = {
        "id": "trusted-node-custom-id",
        "kind": "pipeline_decision",
        "user_term": RAW_HTML_CLEANUP_USER_TERM,
        "status": "resolved",
        "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
        "event_id": "node-event",
        "accepted_value": "approved",
        "accepted_artifact_hash": "b" * 64,
        "resolved_prompt_template_hash": None,
    }
    current = CompositionState(
        sources={
            "orders": SourceSpec(
                plugin="csv",
                on_success="rows",
                options={
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [source_requirement],
                },
                on_validation_failure="discard",
            )
        },
        nodes=(
            NodeSpec(
                id="cleanup",
                node_type="transform",
                plugin="field_mapper",
                input="rows",
                on_success="clean",
                on_error="discard",
                options={
                    "schema": {"mode": "observed"},
                    "mapping": {"name": "name"},
                    "select_only": True,
                    INTERPRETATION_REQUIREMENTS_KEY: [node_requirement],
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
        version=4,
    )
    pipeline = {
        "sources": {
            "orders": {
                "plugin": "csv",
                "on_success": "rows",
                "options": {
                    "schema": {"mode": "observed"},
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": "invented_source",
                            "user_term": "inline_source_data",
                            "draft": "name,score\nada,42\n",
                        }
                    ],
                },
                "on_validation_failure": "discard",
            }
        },
        "nodes": [
            {
                "id": "cleanup",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "rows",
                "on_success": "clean",
                "on_error": "discard",
                "options": {
                    "schema": {"mode": "observed"},
                    "mapping": {"name": "name"},
                    "select_only": True,
                    INTERPRETATION_REQUIREMENTS_KEY: [
                        {
                            "kind": "pipeline_decision",
                            "user_term": RAW_HTML_CLEANUP_USER_TERM,
                            "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
                        }
                    ],
                },
            }
        ],
        "edges": [],
        "outputs": [],
    }

    canonical = canonicalize_authored_node_review_requirements(pipeline, current_state=current)

    source = canonical["sources"]["orders"]["options"][INTERPRETATION_REQUIREMENTS_KEY][0]
    node = canonical["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY][0]
    assert source == {
        "kind": "invented_source",
        "user_term": "inline_source_data",
        "draft": "name,score\nada,42\n",
        "id": "trusted-source-custom-id",
        "status": "pending",
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }
    assert node == {
        "kind": "pipeline_decision",
        "user_term": RAW_HTML_CLEANUP_USER_TERM,
        "draft": RAW_HTML_CLEANUP_REVIEW_DRAFT,
        "id": "trusted-node-custom-id",
        "status": "pending",
        "event_id": None,
        "accepted_value": None,
        "accepted_artifact_hash": None,
        "resolved_prompt_template_hash": None,
    }


def test_budget_policy_is_frozen_slotted_and_rejects_non_decimal_cost() -> None:
    policy = _budget()
    with pytest.raises(TypeError):
        vars(policy)
    with pytest.raises((TypeError, ValueError)):
        _budget(max_cumulative_provider_cost=1.0)


def test_tool_result_envelope_omits_data_without_traversal() -> None:
    from inspect import signature

    from elspeth.contracts.freeze import deep_thaw

    assert "include_data" in signature(ToolResult.to_dict).parameters
    state = _empty_state()
    result = ToolResult(success=True, updated_state=state, validation=state.validate(), affected_nodes=(), data={"grammar": "text"})
    expected = result.to_dict()
    assert result.to_dict(include_data=True) == expected
    del expected["data"]
    with patch("elspeth.web.composer.tools._common.deep_thaw", spec=deep_thaw, side_effect=AssertionError("data traversed")):
        assert result.to_dict(include_data=False) == expected
        with pytest.raises(AssertionError, match="data traversed"):
            result.to_dict()


@pytest.mark.parametrize(
    ("label", "sources", "nodes", "outputs", "expected"),
    (
        pytest.param(
            "default-source",
            None,
            [],
            None,
            [("source", "csv")],
            id="producer-default-source",
        ),
        pytest.param(
            "named-source",
            {"alt": None},
            [],
            None,
            [("source", "csv")],
            id="producer-named-source",
        ),
        pytest.param(
            "node",
            None,
            [
                {
                    "id": "clean",
                    "node_type": "transform",
                    "plugin": "field_mapper",
                    "input": "clean",
                    "on_success": "out",
                    "on_error": "discard",
                    "options": {"bogus_option": True},
                }
            ],
            None,
            [("source", "csv"), ("transform", "field_mapper")],
            id="producer-node",
        ),
        pytest.param(
            "output",
            None,
            [],
            [{"sink_name": "out", "plugin": "json", "options": {}, "on_write_failure": "discard"}],
            [("source", "csv"), ("sink", "json")],
            id="producer-output",
        ),
    ),
)
def test_every_producer_records_the_identity_it_resolved(
    tmp_path: Path,
    label: str,
    sources: dict[str, Any] | None,
    nodes: list[dict[str, Any]],
    outputs: list[dict[str, Any]] | None,
    expected: list[tuple[str, str]],
) -> None:
    """Each ``plugin_options_invalid`` producer carries its ``(kind, plugin)``.

    The consumer fails closed on absence, so a producer that forgets the
    carrier loses its attach SILENTLY — no error, just a repair turn that
    never learns the contract. These positive pins per producer shape are the
    only thing that catches that, which is why they exist per shape rather
    than as one representative case.
    """
    del label
    context, _policy_catalog, blobs = _candidate_context(tmp_path, "session-producer-pins")
    source_block = _bad_source(blobs)
    arguments: dict[str, Any] = {
        "nodes": nodes,
        "edges": [],
        "outputs": outputs
        if outputs is not None
        else [
            {
                "sink_name": "out",
                "plugin": "json",
                "options": {"path": str(blobs / "o.json"), "schema": {"mode": "observed"}},
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "p"},
    }
    if sources is None:
        arguments["source"] = source_block
    else:
        arguments["sources"] = dict.fromkeys(sources, source_block)

    result = build_set_pipeline_candidate(arguments, _empty_state(), context).result

    assert result.success is False
    carried = [entry.plugin_identity for entry in result.validation.errors if entry.error_code == "plugin_options_invalid"]
    assert carried == expected, [e.message[:80] for e in result.validation.errors]


def test_the_secret_ref_placement_producer_records_its_identity_too(tmp_path: Path) -> None:
    """The head with no subject clause still carries the identity.

    ``_prevalidate_plugin_options``'s first branch emits
    ``Invalid secret_ref placement for <kind> '<plugin>': <option-keys> ...``,
    whose head no option-shape parser recognises — but its CALLER was
    validating a known plugin, so the carrier is populated regardless of which
    branch produced the text. That is the structural point: the producer's
    knowledge does not depend on how it phrased the message.
    """
    context, _policy_catalog, blobs = _candidate_context(tmp_path, "session-secret-ref-pin")
    result = build_set_pipeline_candidate(
        {
            "source": _bad_source(blobs, **{"schema": {"mode": "observed"}, _INJECTED_CLAUSE: {"secret_ref": "N"}}),
            "nodes": [],
            "edges": [],
            "outputs": [
                {
                    "sink_name": "out",
                    "plugin": "json",
                    "options": {"path": str(blobs / "o.json"), "schema": {"mode": "observed"}},
                    "on_write_failure": "discard",
                }
            ],
            "metadata": {"name": "p"},
        },
        _empty_state(),
        context,
    ).result

    entry = next(e for e in result.validation.errors if e.error_code == "plugin_options_invalid")
    assert "Invalid secret_ref placement for source 'csv'" in entry.message
    assert entry.plugin_identity == ("source", "csv")
