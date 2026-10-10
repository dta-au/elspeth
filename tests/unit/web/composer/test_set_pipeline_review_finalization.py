"""Validated review authority survives normal set_pipeline proposal preparation.

These are pure candidate/finalization proofs. The unchanged HTTP R4 regression
separately requires the physical SDK authoring and all three durable receipts.
"""

from __future__ import annotations

import socket
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from typing import Any

import litellm
import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.freeze import deep_thaw
from elspeth.web.composer.authority_hashing import composer_authority_hash
from elspeth.web.composer.pipeline_custody import inline_custody_audit_projection
from elspeth.web.composer.tool_batch import (
    _finalize_complete_set_pipeline_candidate,
    _preserve_candidate_review_requirements,
)
from elspeth.web.composer.tools import build_set_pipeline_candidate
from elspeth.web.interpretation_state import INTERPRETATION_REQUIREMENTS_KEY, REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
from tests.unit.web.composer.test_planner_authoring_aids import _custody_context, _guardrail_profile_view
from tests.unit.web.composer.test_required_control_autowire import _INLINE_CONTENT, _bare_llm_candidate
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _linear_args, _structured_llm_args, _trained_context


@pytest.fixture(autouse=True)
def prohibit_provider_or_network(monkeypatch: pytest.MonkeyPatch) -> None:
    def forbidden(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("pure candidate normalization attempted provider or network execution")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    monkeypatch.setattr(litellm, "acompletion", forbidden)
    monkeypatch.setattr(litellm, "completion", forbidden)


def _wire_pending_tone_review(node: dict[str, Any]) -> None:
    options = node["options"]
    draft = "Use a concise factual tone."
    text = options["prompt_template"] + " "
    options["prompt_template"] = text + draft
    options["prompt_template_parts"] = [
        {"kind": "text", "text": text},
        {"kind": "interpretation_ref", "requirement_id": f"tone:{node['id']}"},
    ]
    options[INTERPRETATION_REQUIREMENTS_KEY] = [{"kind": "vague_term", "user_term": "tone", "draft": draft}]


def _pending_arguments(tmp_path: Path, container: str) -> dict[str, Any]:
    arguments = _structured_llm_args(tmp_path)
    _wire_pending_tone_review(arguments["nodes"][0])
    source = arguments["source"]
    source["options"][INTERPRETATION_REQUIREMENTS_KEY] = [
        {"kind": "vague_term", "user_term": "source_contract", "draft": "The text column contains operator-supplied statements."}
    ]
    if container == "sources":
        arguments["sources"] = {"statements": arguments.pop("source")}
    return arguments


def _without_reviews(arguments: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(arguments)
    blocks = list(result["nodes"])
    if "source" in result:
        blocks.append(result["source"])
    if "sources" in result:
        blocks.extend(result["sources"].values())
    for block in blocks:
        options = block["options"]
        if INTERPRETATION_REQUIREMENTS_KEY in options:
            del options[INTERPRETATION_REQUIREMENTS_KEY]
    return result


@pytest.mark.asyncio
@pytest.mark.parametrize("container", ["source", "sources"])
async def test_finalization_preserves_actual_candidate_reviews_and_other_authored_bytes(tmp_path: Path, container: str) -> None:
    arguments = _pending_arguments(tmp_path, container)
    before = deepcopy(arguments)
    state = _empty_state()
    context = _trained_context(data_dir=tmp_path)
    public = build_set_pipeline_candidate(arguments, state, context)
    assert public.acceptable is True, public.result.to_dict()
    result = await _finalize_complete_set_pipeline_candidate(
        arguments, state, context, plugin_snapshot=context.plugin_snapshot, policy_catalog=context.catalog
    )
    assert result.changed is True
    assert result.context._interpretation_requirements_are_internal is True
    assert result.context.tool_arguments_hash == composer_authority_hash(inline_custody_audit_projection(result.arguments))
    assert arguments == before
    assert _without_reviews(dict(result.arguments)) == _without_reviews(before)
    node_rows = result.arguments["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY]
    assert node_rows == deep_thaw(public.result.updated_state.nodes[0].options[INTERPRETATION_REQUIREMENTS_KEY])
    assert {row["kind"] for row in node_rows} == {"vague_term", "llm_prompt_template"}
    source_name = "source" if container == "source" else "statements"
    source = result.arguments["source"] if container == "source" else result.arguments["sources"][source_name]
    assert source["options"][INTERPRETATION_REQUIREMENTS_KEY] == deep_thaw(
        public.result.updated_state.sources[source_name].options[INTERPRETATION_REQUIREMENTS_KEY]
    )
    trusted = build_set_pipeline_candidate(result.arguments, state, result.context)
    assert trusted.acceptable is True, trusted.result.to_dict()
    assert trusted.result.updated_state == public.result.updated_state == result.candidate.result.updated_state
    refused = build_set_pipeline_candidate(result.arguments, state, context)
    assert refused.acceptable is False
    assert {entry.error_code for entry in refused.result.validation.errors} == {"interpretation_requirements_invalid"}


@pytest.mark.asyncio
async def test_no_review_no_control_finalization_keeps_original_arguments_and_context(tmp_path: Path) -> None:
    arguments = _linear_args(tmp_path)
    context = _trained_context(data_dir=tmp_path)
    result = await _finalize_complete_set_pipeline_candidate(
        arguments, _empty_state(), context, plugin_snapshot=context.plugin_snapshot, policy_catalog=context.catalog
    )
    assert result.candidate.acceptable is True
    assert result.changed is False
    assert result.arguments is arguments
    assert result.context is context


@pytest.mark.asyncio
async def test_public_canonical_rows_are_rejected_before_internal_handoff(tmp_path: Path) -> None:
    arguments = _pending_arguments(tmp_path, "source")
    context = _trained_context(data_dir=tmp_path)
    state = _empty_state()
    admitted = build_set_pipeline_candidate(arguments, state, context)
    assert admitted.acceptable is True
    canonical = _preserve_candidate_review_requirements(arguments, admitted.result.updated_state)
    refused = await _finalize_complete_set_pipeline_candidate(
        canonical, state, context, plugin_snapshot=context.plugin_snapshot, policy_catalog=context.catalog
    )
    assert refused.candidate.acceptable is False
    assert refused.changed is False
    assert refused.context is context
    assert refused.arguments is canonical
    assert {entry.error_code for entry in refused.candidate.result.validation.errors} == {"interpretation_requirements_invalid"}


@pytest.mark.asyncio
@pytest.mark.parametrize("owned_field", ["id", "status", "event_id"])
async def test_public_resolver_field_refusal_never_gets_internal_authority(tmp_path: Path, owned_field: str) -> None:
    arguments = _pending_arguments(tmp_path, "source")
    arguments["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY][0][owned_field] = "forged"
    before = deepcopy(arguments)
    context = _trained_context(data_dir=tmp_path)
    result = await _finalize_complete_set_pipeline_candidate(
        arguments, _empty_state(), context, plugin_snapshot=context.plugin_snapshot, policy_catalog=context.catalog
    )
    assert result.candidate.acceptable is False
    assert {entry.error_code for entry in result.candidate.result.validation.errors} == {"interpretation_requirements_invalid"}
    assert result.changed is False
    assert result.context is context
    assert result.arguments is arguments
    assert arguments == before


@pytest.mark.asyncio
async def test_existing_state_review_identity_is_retained_on_recompose_finalization(tmp_path: Path) -> None:
    arguments = _pending_arguments(tmp_path, "source")
    context = _trained_context(data_dir=tmp_path)
    initial = build_set_pipeline_candidate(arguments, _empty_state(), context)
    assert initial.acceptable is True
    node = initial.result.updated_state.nodes[0]
    options = deep_thaw(node.options)
    (tone,) = [row for row in options[INTERPRETATION_REQUIREMENTS_KEY] if row["user_term"] == "tone"]
    tone["id"] = "persisted-tone-review"
    # Both the persisted state and the public recompose input refer to this
    # existing review. The provider still authors only the compact shell.
    options["prompt_template_parts"][1]["requirement_id"] = "persisted-tone-review"
    arguments["nodes"][0]["options"]["prompt_template_parts"][1]["requirement_id"] = "persisted-tone-review"
    current = replace(initial.result.updated_state, nodes=(replace(node, options=options),))
    result = await _finalize_complete_set_pipeline_candidate(
        arguments, current, context, plugin_snapshot=context.plugin_snapshot, policy_catalog=context.catalog
    )
    assert result.candidate.acceptable is True
    (retained,) = [row for row in result.arguments["nodes"][0]["options"][INTERPRETATION_REQUIREMENTS_KEY] if row["user_term"] == "tone"]
    assert retained["id"] == "persisted-tone-review"
    assert retained["status"] == "pending"
    assert retained["draft"] == "Use a concise factual tone."
    assert result.candidate.result.updated_state.version == current.version + 1


@pytest.mark.asyncio
async def test_required_control_finalization_keeps_canonical_control_and_authored_reviews(tmp_path: Path) -> None:
    (tmp_path / "outputs").mkdir()
    view, snapshot = _guardrail_profile_view(tmp_path)
    arguments = _bare_llm_candidate()
    _wire_pending_tone_review(arguments["nodes"][0])
    before = deepcopy(arguments)
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    result = await _finalize_complete_set_pipeline_candidate(
        arguments, _empty_state(), context, plugin_snapshot=snapshot, policy_catalog=view
    )
    assert result.candidate.acceptable is True, result.candidate.result.to_dict()
    assert result.changed is True
    assert arguments == before
    nodes = {node["id"]: node for node in result.arguments["nodes"]}
    assert set(nodes) == {"assess_ticket", "prompt_shield_auto_1", "content_safety_auto_1"}
    for node_id in ("prompt_shield_auto_1", "content_safety_auto_1"):
        (disclosure,) = [
            row
            for row in nodes[node_id]["options"][INTERPRETATION_REQUIREMENTS_KEY]
            if row["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        ]
        assert disclosure["status"] == "pending"
        assert set(disclosure) == {
            "id",
            "kind",
            "user_term",
            "status",
            "draft",
            "event_id",
            "accepted_value",
            "accepted_artifact_hash",
            "resolved_prompt_template_hash",
        }
    assert {row["kind"] for row in nodes["assess_ticket"]["options"][INTERPRETATION_REQUIREMENTS_KEY]} == {
        "vague_term",
        "llm_prompt_template",
    }
    validation = view.validate_authored_state(result.candidate.result.updated_state)
    assert [finding for finding in validation.findings if finding.stage == "required_control_coverage"] == []


def test_review_copy_refuses_another_candidate_graph(tmp_path: Path) -> None:
    arguments = _pending_arguments(tmp_path, "source")
    context = _trained_context(data_dir=tmp_path)
    candidate = build_set_pipeline_candidate(arguments, _empty_state(), context)
    assert candidate.acceptable is True
    node = candidate.result.updated_state.nodes[0]
    foreign = replace(candidate.result.updated_state, nodes=(replace(node, id="foreign_node"),))
    with pytest.raises(AuditIntegrityError, match="node identities differ"):
        _preserve_candidate_review_requirements(arguments, foreign)
