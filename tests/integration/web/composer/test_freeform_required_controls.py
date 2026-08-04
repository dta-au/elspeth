"""Freeform compose-loop required-control finalization regressions."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Literal
from unittest.mock import patch
from uuid import UUID

import pytest
from pydantic import SecretBytes

from elspeth.contracts.freeze import deep_thaw
from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
from elspeth.core.canonical import stable_hash
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.web.blobs.service import BlobServiceImpl
from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.composer.implicit_decisions import build_implicit_decisions_report
from elspeth.web.composer.protocol import ComposerPluginCrashError
from elspeth.web.composer.required_controls import (
    wire_required_controls as real_wire_required_controls,
)
from elspeth.web.composer.required_controls import (
    wire_required_controls_state,
)
from elspeth.web.composer.state import CompositionState, OutputSpec, SourceSpec
from elspeth.web.composer.tools import execute_tool
from elspeth.web.config import WebSettings
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
)
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.sessions.models import (
    blobs_table,
    chat_messages_table,
    composition_proposals_table,
    composition_states_table,
)
from tests.integration.web.composer.test_freeform_proposal_prevalidation import (
    _count_rows,
    _harness,
    _incremental_base_state,
    _persisted_tool_content,
    _ScriptedLLM,
    _tool_turn,
)
from tests.unit.web.composer.test_planner_authoring_aids import (
    _catalog_with_llm_source,
    _compiler_manager_with_llm_source,
    _empty_state,
)


class _ProfileInventory:
    def has_server_ref(self, name: str) -> bool:
        return name == "OPENROUTER_API_KEY"

    def has_user_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return False

    def has_ref(self, principal: str, name: str) -> bool:
        del principal
        return self.has_server_ref(name)

    def server_generation(self, name: str) -> str | None:
        return "test-generation" if self.has_server_ref(name) else None

    def user_generation(self, principal: str, name: str) -> str | None:
        del principal, name
        return None


def _required_textract_policy(tmp_path: Path) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=SecretBytes(b"0" * 32),
        deployment_aws_region="ap-southeast-2",
        aws_textract_profiles=({"alias": "acceptance-docs", "bucket": "operator-owned-docs", "key_prefix": "org/acme"},),
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
        plugin_allowlist=(
            "transform:aws_textract_document_analysis",
            "transform:aws_bedrock_prompt_shield",
            "transform:aws_bedrock_content_safety",
        ),
        plugin_preferences={
            PluginCapability.PROMPT_SHIELD: ("transform:aws_bedrock_prompt_shield",),
            PluginCapability.CONTENT_SAFETY: ("transform:aws_bedrock_content_safety",),
        },
        plugin_control_modes={
            PluginCapability.PROMPT_SHIELD: ControlMode.REQUIRED,
            PluginCapability.CONTENT_SAFETY: ControlMode.REQUIRED,
        },
        bedrock_guardrail_profiles=(
            BedrockGuardrailProfileSettings(
                alias="prompt-approved",
                plugin="aws_bedrock_prompt_shield",
                guardrail_identifier="operatorpromptguardrail",
                guardrail_version="1",
                region="ap-southeast-2",
            ),
            BedrockGuardrailProfileSettings(
                alias="content-approved",
                plugin="aws_bedrock_content_safety",
                guardrail_identifier="operatorcontentguardrail",
                guardrail_version="1",
                region="ap-southeast-2",
            ),
        ),
        bedrock_guardrail_default_profiles={
            "aws_bedrock_prompt_shield": "prompt-approved",
            "aws_bedrock_content_safety": "content-approved",
        },
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=_compiler_manager_with_llm_source(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = _catalog_with_llm_source()
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:freeform-required-controls",
        secret_inventory=_ProfileInventory(),
        generation_key=b"freeform-required-controls-test-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _textract_llm_mapper_args(tmp_path: Path) -> dict[str, Any]:
    return {
        "source": {
            "plugin": "csv",
            "on_success": "manifest_rows",
            "options": {
                "schema": {
                    "mode": "fixed",
                    "fields": ["doc_key: str"],
                }
            },
            "on_validation_failure": "discard",
            "inline_blob": {
                "filename": "manifest.csv",
                "mime_type": "text/csv",
                "content": "doc_key\ninbox/document.pdf\n",
                "description": "One document manifest row",
            },
        },
        "nodes": [
            {
                "id": "extract_document",
                "node_type": "transform",
                "plugin": "aws_textract_document_analysis",
                "input": "manifest_rows",
                "on_success": "extracted_rows",
                "on_error": "discard",
                "options": {
                    "profile": "acceptance-docs",
                    "schema": {
                        "mode": "flexible",
                        "fields": [
                            {"name": "doc_key", "field_type": "str", "required": True},
                        ],
                    },
                    "required_input_fields": ["doc_key"],
                    "key_field": "doc_key",
                    "feature_types": ["TABLES", "FORMS"],
                    "text_field": "document_text",
                    "page_count_field": "page_count",
                },
            },
            {
                "id": "summarise_document",
                "node_type": "transform",
                "plugin": "llm",
                "input": "extracted_rows",
                "on_success": "summarised_rows",
                "on_error": "discard",
                "options": {
                    "profile": "sonnet",
                    "prompt_template": "Summarise this document: {{ row.document_text }}",
                    "required_input_fields": ["document_text"],
                    "response_field": "summary",
                    "schema": {"mode": "observed", "guaranteed_fields": ["summary"]},
                },
            },
            {
                "id": "shape_result",
                "node_type": "transform",
                "plugin": "field_mapper",
                "input": "summarised_rows",
                "on_success": "output_rows",
                "on_error": "discard",
                "options": {
                    "mapping": {"summary": "summary"},
                    "select_only": True,
                    "schema": {"mode": "observed", "guaranteed_fields": ["summary"]},
                },
            },
        ],
        "edges": [],
        "outputs": [
            {
                "sink_name": "output_rows",
                "plugin": "json",
                "options": {
                    "path": str(tmp_path / "outputs" / "00000000-0000-4000-8000-000000000001" / "summary.jsonl"),
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                "on_write_failure": "discard",
            }
        ],
        "metadata": {"name": "Textract summary", "description": "Extract, summarise, and shape one document."},
    }


def _node_plugins(candidate: dict[str, Any]) -> list[str]:
    return [str(node["plugin"]) for node in candidate["nodes"]]


async def _incremental_textract_state(
    tmp_path: Path,
    *,
    harness: Any,
    view: PolicyCatalogView,
    snapshot: PluginAvailabilitySnapshot,
) -> tuple[CompositionState, dict[str, Any]]:
    args = _textract_llm_mapper_args(tmp_path)
    blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
        UUID(harness.session_id),
        "manifest.csv",
        b"doc_key\ninbox/document.pdf\n",
        "text/csv",
    )
    args["source"] = {
        "plugin": "csv",
        "on_success": "manifest_rows",
        "blob_id": str(blob.id),
        "options": {
            "schema": {
                "mode": "fixed",
                "fields": ["doc_key: str"],
            },
        },
        "on_validation_failure": "discard",
    }
    output = args["outputs"][0]
    output["options"]["path"] = str(tmp_path / "outputs" / harness.session_id / "summary.jsonl")
    args["outputs"] = []
    result = execute_tool(
        "set_pipeline",
        args,
        _empty_state(),
        view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=harness.engine,
        session_id=harness.session_id,
        user_id="proposal-prevalidation-user",
        validate_arguments=True,
        require_data_dir_for_paths=True,
        raise_schema_argument_errors=True,
    )
    assert result.success is True
    assert result.validation.is_valid is False
    return result.updated_state, output


async def _incremental_named_blob_state(
    tmp_path: Path,
    *,
    harness: Any,
    view: PolicyCatalogView,
    snapshot: PluginAvailabilitySnapshot,
    source_count: Literal[1, 2],
) -> tuple[CompositionState, dict[str, Any]]:
    initial_state, output = await _incremental_textract_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
    )
    original_source = initial_state.sources["source"]
    sources = {"primary": original_source}
    outputs: tuple[OutputSpec, ...] = ()
    if source_count == 2:
        extra_blob = await BlobServiceImpl(harness.engine, tmp_path).create_blob(
            UUID(harness.session_id),
            "secondary.csv",
            b"doc_key\ninbox/secondary.pdf\n",
            "text/csv",
        )
        sources["secondary"] = SourceSpec(
            plugin=original_source.plugin,
            on_success="secondary_manifest_rows",
            options={
                **deep_thaw(original_source.options),
                "path": extra_blob.storage_path,
                "blob_ref": str(extra_blob.id),
                "mode": "bind_source",
            },
            on_validation_failure=original_source.on_validation_failure,
        )
        outputs = (
            OutputSpec(
                name="secondary_manifest_rows",
                plugin="json",
                options={
                    "path": str(tmp_path / "outputs" / harness.session_id / "secondary.jsonl"),
                    "schema": {"mode": "observed"},
                    "format": "jsonl",
                    "mode": "write",
                    "collision_policy": "auto_increment",
                },
                on_write_failure="discard",
            ),
        )
    named_source_state = CompositionState(
        sources=sources,
        nodes=initial_state.nodes,
        edges=initial_state.edges,
        outputs=outputs,
        metadata=initial_state.metadata,
        version=initial_state.version,
    )
    assert view.validate_composition_state(named_source_state).validation.is_valid is False
    return named_source_state, output


def _assert_required_control_disclosures(candidate: dict[str, Any]) -> None:
    controls = [node for node in candidate["nodes"] if node["plugin"] in {"aws_bedrock_prompt_shield", "aws_bedrock_content_safety"}]
    assert len(controls) == 2
    for control in controls:
        rows = control["options"][INTERPRETATION_REQUIREMENTS_KEY]
        assert len(rows) == 1
        assert rows[0]["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM


@pytest.mark.asyncio
async def test_explicit_approval_seals_auto_wired_textract_candidate_and_hash(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    original = _textract_llm_mapper_args(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_required_controls_explicit", "set_pipeline", original),
    )
    initial_state = _empty_state()

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Build a Textract to LLM pipeline and prepare it for review.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    authority = await harness.sessions.get_authoritative_composition_proposal(
        session_id=UUID(harness.session_id),
        proposal_id=proposals[0].id,
        reviewed_facts=None,
    )
    assert authority.pipeline is not None
    assert proposals[0].pipeline_metadata is not None
    assert authority.pipeline.proposal.draft_hash == proposals[0].pipeline_metadata.draft_hash
    from elspeth.web.composer.audit import BufferingRecorder
    from elspeth.web.composer.pipeline_commit import PipelineCommitConfig, prepare_pipeline_proposal_commit

    prepared = await prepare_pipeline_proposal_commit(
        authority=authority.pipeline,
        reviewed_facts={},
        current_state=initial_state,
        current_state_id=None,
        policy_catalog=view,
        plugin_snapshot=snapshot,
        config=PipelineCommitConfig(
            data_dir=str(tmp_path),
            session_engine=harness.engine,
            secret_service=None,
            user_id="proposal-prevalidation-user",
            user_message_content="Build a Textract to LLM pipeline and prepare it for review.",
            max_blob_storage_per_session_bytes=10_000_000,
            runtime_preflight=None,
            timeout_seconds=5.0,
        ),
        recorder=BufferingRecorder(),
        actor="user:proposal-prevalidation-user",
        settlement_surface="generic",
    )
    assert prepared.result.success is True
    assert prepared.result.validation.is_valid is True
    sealed = deep_thaw(proposals[0].arguments_json)
    assert _node_plugins(sealed) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ]
    _assert_required_control_disclosures(sealed)
    assert proposals[0].tool_arguments_hash == stable_hash(sealed)
    assert result.state is initial_state
    assert original == _textract_llm_mapper_args(tmp_path), "server finalization must not mutate provider arguments"


@pytest.mark.asyncio
async def test_auto_commit_persists_auto_wired_textract_state_and_disclosure(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    original = _textract_llm_mapper_args(tmp_path)
    llm = _ScriptedLLM(
        _tool_turn("call_required_controls_auto", "set_pipeline", original),
    )

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Build and apply a Textract to LLM pipeline.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    state_payload = result.state.to_dict()
    assert _node_plugins(state_payload) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ], (result.message, result.tool_invocations, harness.service._phase3_last_tool_outcomes)
    _assert_required_control_disclosures(state_payload)
    report = build_implicit_decisions_report(result.state)
    policy_entries = [entry for entry in report["entries"] if entry["category"] == "policy_control"]
    assert [entry["value"] for entry in policy_entries] == [
        "aws_bedrock_prompt_shield",
        "aws_bedrock_content_safety",
    ]
    assert _count_rows(harness.engine, composition_states_table) == 1
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []

    invocation = next(item for item in result.tool_invocations if item.tool_call_id == "call_required_controls_auto")
    with harness.engine.connect() as conn:
        assistant_calls = (
            conn.execute(
                chat_messages_table.select()
                .with_only_columns(chat_messages_table.c.tool_calls)
                .where(chat_messages_table.c.session_id == harness.session_id)
                .where(chat_messages_table.c.role == "assistant")
                .where(chat_messages_table.c.tool_calls.is_not(None))
            )
            .scalars()
            .all()
        )
    persisted_call = next(call for calls in assistant_calls for call in calls if call.get("id") == "call_required_controls_auto")
    persisted_args = json.loads(persisted_call["function"]["arguments"])
    assert _node_plugins(persisted_args) == _node_plugins(state_payload)
    audit_args = json.loads(invocation.arguments_canonical)
    assert _node_plugins(audit_args) == _node_plugins(state_payload)
    _assert_required_control_disclosures(audit_args)
    assert invocation.arguments_hash == stable_hash(audit_args)
    assert original == _textract_llm_mapper_args(tmp_path), "server finalization must not mutate provider arguments"


@pytest.mark.asyncio
async def test_auto_commit_does_not_wire_an_incomplete_set_pipeline_candidate(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    incomplete = _textract_llm_mapper_args(tmp_path)
    incomplete["outputs"] = []
    llm = _ScriptedLLM(_tool_turn("call_incomplete", "set_pipeline", incomplete))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch(
            "elspeth.web.composer.tool_batch.wire_required_controls",
            wraps=real_wire_required_controls,
        ) as finalizer,
    ):
        result = await harness.service.compose(
            "Build this pipeline incrementally; the sink will follow.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert finalizer.call_count == 0
    assert _node_plugins(result.state.to_dict()) == [
        "aws_textract_document_analysis",
        "llm",
        "field_mapper",
    ]
    assert not any(plugin.startswith("aws_bedrock_") for plugin in _node_plugins(result.state.to_dict()))


@pytest.mark.asyncio
async def test_auto_commit_does_not_duplicate_already_covered_controls(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    covered = deep_thaw(real_wire_required_controls(_textract_llm_mapper_args(tmp_path), snapshot, view))
    assert type(covered) is dict
    for node in covered["nodes"]:
        if node["plugin"] in {"aws_bedrock_prompt_shield", "aws_bedrock_content_safety"}:
            del node["options"][INTERPRETATION_REQUIREMENTS_KEY]
    llm = _ScriptedLLM(_tool_turn("call_already_covered", "set_pipeline", covered))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch(
            "elspeth.web.composer.tool_batch.wire_required_controls",
            wraps=real_wire_required_controls,
        ) as finalizer,
    ):
        result = await harness.service.compose(
            "Apply this already-covered pipeline.",
            [],
            _empty_state(),
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    plugins = _node_plugins(result.state.to_dict())
    assert plugins == _node_plugins(covered)
    assert plugins.count("aws_bedrock_prompt_shield") == 1
    assert plugins.count("aws_bedrock_content_safety") == 1
    assert finalizer.call_count == 1


@pytest.mark.asyncio
async def test_auto_commit_wires_controls_when_set_output_completes_incremental_pipeline(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    initial_state, output = await _incremental_textract_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
    )
    llm = _ScriptedLLM(_tool_turn("call_complete_with_output", "set_output", output))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Add the final JSON output.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    payload = result.state.to_dict()
    assert result.state.version == initial_state.version + 1
    assert _node_plugins(payload) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ]
    _assert_required_control_disclosures(payload)
    assert _count_rows(harness.engine, composition_states_table) == 1
    invocation = next(item for item in result.tool_invocations if item.tool_call_id == "call_complete_with_output")
    assert invocation.tool_name == "set_output"
    assert invocation.result_canonical is not None
    invocation_result = json.loads(invocation.result_canonical)
    assert invocation_result["validation"]["is_valid"] is True
    assert invocation_result["affected_nodes"] == [
        "output_rows",
        "prompt_shield_auto_1",
        "content_safety_auto_1",
    ]


@pytest.mark.asyncio
async def test_explicit_incremental_completion_stages_one_canonical_wired_pipeline_proposal(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    initial_state, output = await _incremental_textract_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
    )
    llm = _ScriptedLLM(_tool_turn("call_review_incremental_completion", "set_output", output))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Add the final JSON output and prepare the complete pipeline for review.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    proposals = await harness.sessions.list_composition_proposals(UUID(harness.session_id))
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.tool_name == "set_pipeline"
    assert proposal.pipeline_metadata is not None
    authority = await harness.sessions.get_authoritative_composition_proposal(
        session_id=UUID(harness.session_id),
        proposal_id=proposal.id,
        reviewed_facts=None,
    )
    assert authority.pipeline is not None
    sealed = deep_thaw(authority.pipeline.proposal.pipeline)
    assert _node_plugins(sealed) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ]
    _assert_required_control_disclosures(sealed)
    assert proposal.tool_arguments_hash == stable_hash(sealed)
    assert set(proposal.affects) >= {"graph", "validation"}
    assert result.state is initial_state
    invocation = next(item for item in result.tool_invocations if item.tool_call_id == "call_review_incremental_completion")
    assert invocation.tool_name == "set_output"
    assert json.loads(invocation.arguments_canonical) == output


@pytest.mark.asyncio
async def test_auto_commit_wires_incremental_named_blob_sources_without_public_round_trip(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    named_source_state, output = await _incremental_named_blob_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
        source_count=2,
    )
    llm = _ScriptedLLM(_tool_turn("call_complete_named_sources", "set_output", output))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
    ):
        result = await harness.service.compose(
            "Add the final JSON output.",
            [],
            named_source_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert result.state.sources.keys() == named_source_state.sources.keys()
    assert _node_plugins(result.state.to_dict()) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ]
    _assert_required_control_disclosures(result.state.to_dict())
    assert _count_rows(harness.engine, composition_states_table) == 1


@pytest.mark.parametrize("source_count", [1, 2], ids=("named", "multiple"))
@pytest.mark.asyncio
async def test_explicit_incremental_named_blob_completion_proposes_and_accepts_exact_owned_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_count: Literal[1, 2],
) -> None:
    from elspeth.web.composer.service import ComposerAvailability, ComposerServiceImpl
    from elspeth.web.composer.tools import execute_tool
    from elspeth.web.execution.schemas import ValidationResult
    from elspeth.web.sessions.converters import state_from_record
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web._sync_asgi_client import SyncASGITestClient
    from tests.unit.web.composer.conftest import _make_settings
    from tests.unit.web.sessions.test_routes import _async_return, _make_app

    app, sessions = _make_app(tmp_path)
    session = await sessions.create_session("alice", "Owned-state proposal", "local")
    await sessions.update_composer_preferences(
        session.id,
        trust_mode="explicit_approve",
        density_default="high",
        actor="user:alice",
    )
    user_message = await sessions.add_message(
        session.id,
        "user",
        "Add the final JSON output and prepare the complete pipeline for review.",
        writer_principal="route_user_message",
    )
    view, snapshot = _required_textract_policy(tmp_path)
    app.state.catalog_service = view._full
    app.state.operator_profile_registry = view._profiles
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    harness = SimpleNamespace(engine=sessions._engine, session_id=str(session.id))
    initial_state, output = await _incremental_named_blob_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
        source_count=source_count,
    )
    initial_payload = initial_state.to_dict()
    initial_record = await sessions.save_composition_state(
        session.id,
        CompositionStateData(
            sources=initial_payload["sources"],
            nodes=initial_payload["nodes"],
            edges=initial_payload["edges"],
            outputs=initial_payload["outputs"],
            metadata_=initial_payload["metadata"],
            is_valid=False,
            validation_errors=[entry.message for entry in initial_state.validate().errors],
        ),
        provenance="session_seed",
    )
    persisted_initial = state_from_record(initial_record)
    preview = execute_tool(
        "set_output",
        output,
        persisted_initial,
        view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=sessions._engine,
        session_id=str(session.id),
        user_id="alice",
        validate_arguments=True,
        require_data_dir_for_paths=True,
        raise_schema_argument_errors=True,
    )
    assert preview.success is True, preview.data
    assert preview.validation.is_valid is True, preview.validation
    expected = wire_required_controls_state(preview.updated_state, snapshot, view)
    assert expected is not preview.updated_state
    expected_plugins = _node_plugins(expected.to_dict())

    with patch.object(
        ComposerServiceImpl,
        "_compute_availability",
        return_value=ComposerAvailability(available=True, model="test-model", provider="test"),
    ):
        composer = ComposerServiceImpl.for_trained_operator(
            catalog=view._full,
            settings=_make_settings(tmp_path),
            sessions_service=sessions,
            session_engine=sessions._engine,
        )
    app.state.composer_service = composer
    llm = _ScriptedLLM(_tool_turn(f"call_review_{source_count}_blob_sources", "set_output", output))
    with (
        patch.object(composer, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(composer, "_call_llm", new=llm),
    ):
        result = await composer.compose(
            "Add the final JSON output and prepare the complete pipeline for review.",
            [],
            persisted_initial,
            session_id=str(session.id),
            current_state_id=str(initial_record.id),
            user_id="alice",
            user_message_id=str(user_message.id),
        )

    proposals = await sessions.list_composition_proposals(session.id)
    assert len(proposals) == 1
    proposal = proposals[0]
    assert proposal.pipeline_metadata is not None
    assert result.state is persisted_initial
    redacted_text = json.dumps(deep_thaw(proposal.arguments_redacted_json), sort_keys=True)
    for source in persisted_initial.sources.values():
        assert str(source.options["blob_ref"]) not in redacted_text
        assert str(source.options["path"]) not in redacted_text
    invocation = next(item for item in result.tool_invocations if item.tool_call_id == f"call_review_{source_count}_blob_sources")
    assert invocation.tool_name == "set_output"
    assert json.loads(invocation.arguments_canonical) == output

    monkeypatch.setattr(
        "elspeth.web.sessions.routes._helpers._runtime_preflight_for_state",
        _async_return(
            ValidationResult(
                is_valid=True,
                checks=[],
                errors=[],
                readiness={
                    "authoring_valid": True,
                    "execution_ready": True,
                    "completion_ready": True,
                    "blockers": [],
                },
            )
        ),
    )
    accepted = SyncASGITestClient(app).post(
        f"/api/sessions/{session.id}/proposals/{proposal.id}/accept",
        json={"draft_hash": proposal.pipeline_metadata.draft_hash},
    )
    assert accepted.status_code == 200, accepted.text
    committed = await sessions.get_current_state(session.id)
    assert committed is not None
    committed_state = state_from_record(committed)
    assert committed_state.sources.keys() == persisted_initial.sources.keys()
    assert _node_plugins(committed_state.to_dict()) == expected_plugins
    _assert_required_control_disclosures(committed_state.to_dict())
    for source_name, source in persisted_initial.sources.items():
        assert committed_state.sources[source_name].options["blob_ref"] == source.options["blob_ref"]


@pytest.mark.asyncio
async def test_auto_commit_does_not_wire_incremental_mutation_while_pipeline_is_invalid(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    initial_state, _output = await _incremental_textract_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
    )
    llm = _ScriptedLLM(_tool_turn("call_still_incomplete", "set_metadata", {"patch": {"name": "Still incomplete"}}))

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch(
            "elspeth.web.composer.tool_batch.wire_required_controls",
            wraps=real_wire_required_controls,
        ) as finalizer,
    ):
        result = await harness.service.compose(
            "Name this draft before I add the output.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert finalizer.call_count == 0
    assert result.state.metadata.name == "Still incomplete"
    assert _node_plugins(result.state.to_dict()) == [
        "aws_textract_document_analysis",
        "llm",
        "field_mapper",
    ]


@pytest.mark.asyncio
async def test_incremental_required_control_failure_does_not_publish_completed_graph(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    await harness.sessions.update_composer_preferences(
        UUID(harness.session_id),
        trust_mode="auto_commit",
        density_default="high",
        actor="user:proposal-prevalidation-user",
    )
    initial_state, output = await _incremental_textract_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
    )
    llm = _ScriptedLLM(_tool_turn("call_incremental_finalizer_failure", "set_output", output))
    failure = RuntimeError("private incremental finalizer failure detail")

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch("elspeth.web.composer.tool_batch.wire_required_controls_state", side_effect=failure) as finalizer,
        pytest.raises(ComposerPluginCrashError) as exc_info,
    ):
        await harness.service.compose(
            "Add the final JSON output.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert exc_info.value.original_exc is failure
    assert finalizer.call_count == 1
    assert _count_rows(harness.engine, blobs_table) == 1
    assert _count_rows(harness.engine, composition_proposals_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0
    outcome = harness.service._phase3_last_tool_outcomes[-1]
    assert outcome.call.id == "call_incremental_finalizer_failure"
    assert outcome.error_class == "RuntimeError"
    persisted_feedback = _persisted_tool_content(harness, "call_incremental_finalizer_failure")
    assert json.loads(persisted_feedback) == {
        "_redaction_status": "plugin_crash",
        "error_class": "RuntimeError",
        "error_message": "<redacted-failure-message>",
    }


@pytest.mark.asyncio
async def test_incremental_owned_state_projection_failure_does_not_publish_proposal(tmp_path: Path) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    initial_state, output = await _incremental_named_blob_state(
        tmp_path,
        harness=harness,
        view=view,
        snapshot=snapshot,
        source_count=2,
    )
    llm = _ScriptedLLM(_tool_turn("call_owned_state_projection_failure", "set_output", output))
    failure = RuntimeError("private owned-state projection failure detail")

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch("elspeth.web.composer.tool_batch.owned_composition_state_authority", side_effect=failure) as projector,
        pytest.raises(ComposerPluginCrashError) as exc_info,
    ):
        await harness.service.compose(
            "Add the final JSON output and prepare it for review.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert exc_info.value.original_exc is failure
    assert projector.call_count == 1
    assert _count_rows(harness.engine, blobs_table) == 2
    assert _count_rows(harness.engine, composition_proposals_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0
    outcome = harness.service._phase3_last_tool_outcomes[-1]
    assert outcome.call.id == "call_owned_state_projection_failure"
    assert outcome.error_class == "RuntimeError"
    persisted_feedback = _persisted_tool_content(harness, "call_owned_state_projection_failure")
    assert json.loads(persisted_feedback) == {
        "_redaction_status": "plugin_crash",
        "error_class": "RuntimeError",
        "error_message": "<redacted-failure-message>",
    }


@pytest.mark.parametrize("trust_mode", ["explicit_approve", "auto_commit"])
@pytest.mark.asyncio
async def test_required_control_finalizer_failure_is_audited_without_publication(
    tmp_path: Path,
    trust_mode: Literal["explicit_approve", "auto_commit"],
) -> None:
    harness = _harness(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    if trust_mode == "auto_commit":
        await harness.sessions.update_composer_preferences(
            UUID(harness.session_id),
            trust_mode=trust_mode,
            density_default="high",
            actor="user:proposal-prevalidation-user",
        )
    args = _textract_llm_mapper_args(tmp_path)
    llm = _ScriptedLLM(_tool_turn(f"call_finalizer_failure_{trust_mode}", "set_pipeline", args))
    failure = RuntimeError("private finalizer failure detail")
    initial_state = _incremental_base_state(tmp_path)

    with (
        patch.object(harness.service, "_plugin_policy_context", return_value=(snapshot, view)),
        patch.object(harness.service, "_call_llm", new=llm),
        patch("elspeth.web.composer.tool_batch.wire_required_controls", side_effect=failure) as finalizer,
        pytest.raises(ComposerPluginCrashError) as exc_info,
    ):
        await harness.service.compose(
            "Build a Textract to LLM pipeline.",
            [],
            initial_state,
            session_id=harness.session_id,
            user_id="proposal-prevalidation-user",
            user_message_id=harness.user_message_id,
        )

    assert exc_info.value.original_exc is failure
    assert finalizer.call_count == 1
    assert await harness.sessions.list_composition_proposals(UUID(harness.session_id)) == []
    assert _count_rows(harness.engine, blobs_table) == 0
    assert _count_rows(harness.engine, composition_proposals_table) == 0
    assert _count_rows(harness.engine, composition_states_table) == 0
    outcome = harness.service._phase3_last_tool_outcomes[-1]
    assert outcome.call.id == f"call_finalizer_failure_{trust_mode}"
    assert outcome.error_class == "RuntimeError"
    persisted_feedback = _persisted_tool_content(harness, f"call_finalizer_failure_{trust_mode}")
    assert json.loads(persisted_feedback) == {
        "_redaction_status": "plugin_crash",
        "error_class": "RuntimeError",
        "error_message": "<redacted-failure-message>",
    }
    assert "private finalizer failure detail" not in persisted_feedback


@pytest.mark.parametrize("finalizer_fails", [False, True])
def test_accept_incremental_proposal_wires_controls_before_state_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    finalizer_fails: bool,
) -> None:
    from elspeth.web.composer.redaction import redact_tool_call_arguments
    from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
    from elspeth.web.execution.schemas import ValidationResult
    from elspeth.web.sessions.protocol import CompositionStateData
    from tests.unit.web._sync_asgi_client import SyncASGITestClient
    from tests.unit.web.sessions.test_routes import _async_return, _make_app

    app, service = _make_app(tmp_path)
    view, snapshot = _required_textract_policy(tmp_path)
    app.state.catalog_service = view._full
    app.state.operator_profile_registry = view._profiles
    app.state.plugin_snapshot_factory = lambda _user: snapshot
    monkeypatch.setattr(
        "elspeth.web.sessions.routes._helpers._runtime_preflight_for_state",
        _async_return(
            ValidationResult(
                is_valid=True,
                checks=[],
                errors=[],
                readiness={
                    "authoring_valid": True,
                    "execution_ready": True,
                    "completion_ready": True,
                    "blockers": [],
                },
            )
        ),
    )

    async def _setup() -> tuple[UUID, UUID, UUID]:
        session = await service.create_session("alice", "Required-control settlement", "local")
        harness = SimpleNamespace(engine=service._engine, session_id=str(session.id))
        initial_state, output = await _incremental_textract_state(
            tmp_path,
            harness=harness,
            view=view,
            snapshot=snapshot,
        )
        output = deep_thaw(output)
        output["options"]["path"] = str(tmp_path / "outputs" / str(session.id) / "summary.jsonl")
        state_payload = initial_state.to_dict()
        state_record = await service.save_composition_state(
            session.id,
            CompositionStateData(
                sources=state_payload["sources"],
                nodes=state_payload["nodes"],
                edges=state_payload["edges"],
                outputs=state_payload["outputs"],
                metadata_=state_payload["metadata"],
                is_valid=False,
                validation_errors=[entry.message for entry in initial_state.validate().errors],
            ),
            provenance="session_seed",
        )
        proposal = await service.create_composition_proposal(
            session_id=session.id,
            tool_call_id="call_accept_incremental_output",
            tool_name="set_output",
            summary="Add the final JSON output.",
            rationale="Complete the reviewed pipeline.",
            affects=("outputs", "validation"),
            arguments_json=output,
            arguments_redacted_json=redact_tool_call_arguments(
                "set_output",
                output,
                telemetry=NoopRedactionTelemetry(),
            ),
            base_state_id=state_record.id,
            actor="composer-web:user:alice",
        )
        return session.id, proposal.id, state_record.id

    session_id, proposal_id, initial_state_id = asyncio.run(_setup())
    if finalizer_fails:

        def _fail_finalization(*_args: Any, **_kwargs: Any) -> CompositionState:
            raise RuntimeError("private settlement finalizer detail")

        monkeypatch.setattr(
            "elspeth.web.sessions.routes.composer.proposals.wire_required_controls_state",
            _fail_finalization,
        )
    response = SyncASGITestClient(app, raise_server_exceptions=not finalizer_fails).post(
        f"/api/sessions/{session_id}/proposals/{proposal_id}/accept"
    )

    if finalizer_fails:
        assert response.status_code == 500
        unchanged = asyncio.run(service.get_current_state(session_id))
        assert unchanged is not None
        assert unchanged.id == initial_state_id
        proposal = asyncio.run(
            service.get_authoritative_composition_proposal(
                session_id=session_id,
                proposal_id=proposal_id,
                reviewed_facts=None,
            )
        )
        assert proposal.row.status == "pending"
        assert "private settlement finalizer detail" not in response.text
        return
    assert response.status_code == 200, response.text
    persisted = asyncio.run(service.get_current_state(session_id))
    assert persisted is not None
    persisted_state = CompositionState.from_dict(
        {
            "sources": persisted.sources,
            "nodes": persisted.nodes,
            "edges": persisted.edges,
            "outputs": persisted.outputs,
            "metadata": persisted.metadata_,
            "version": persisted.version,
        }
    )
    assert _node_plugins(persisted_state.to_dict()) == [
        "aws_textract_document_analysis",
        "aws_bedrock_prompt_shield",
        "llm",
        "aws_bedrock_content_safety",
        "field_mapper",
    ]
    _assert_required_control_disclosures(persisted_state.to_dict())
