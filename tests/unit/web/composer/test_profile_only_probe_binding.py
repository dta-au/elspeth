"""A profile-bound non-llm node keeps its real output contract at authoring.

A web-authored ``aws_bedrock_prompt_shield``, ``aws_bedrock_content_safety``,
``aws_textract_document_analysis`` or ``azure_ai_search`` node carries only its
public ``profile`` alias and public options. The private binding is written by
the operator profile at lowering, after authoring validation. The authoring
probe constructed the authored shape, failed on the missing binding, and the
known-pass-through fail-closed arm made the node guarantee nothing: every
consumer requiring a field behind it was refused with ``guarantees: [(none)]``
and no source data-contract demand could cross it (deferred-contract audit,
finding 5 and section 5.2). The dispatched tool verdict hid this for a web
principal, because it validates the lowered state; the authored-state readers
(source demand, the planner's argument-rejection envelope, the completion
check) did not.

The probe now lowers the authored options through the plugin's own resolver
over an inert profile (``_validation_probe._inert_profile_bindings``), in the
detached projection only.
"""

from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import fields
from pathlib import Path
from typing import Any

import boto3
import pytest

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.llm_profiles import LLMProfileSettings, RuntimeLLMProfile
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
from elspeth.web.composer import yaml_generator
from elspeth.web.composer._validation_probe import prepare_validation_probe_options
from elspeth.web.composer.required_controls import wire_required_controls
from elspeth.web.composer.source_demand import backtraced_source_demand
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec, ValidationSummary
from elspeth.web.composer.tool_batch import _finalize_complete_set_pipeline_candidate, _finalize_completed_incremental_mutation
from elspeth.web.composer.tools._dispatch import execute_tool
from elspeth.web.composer.tools.sessions import build_set_pipeline_candidate
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginId
from elspeth.web.plugin_policy.profiles import (
    AWSS3SourceProfileSettings,
    AWSTextractProfileSettings,
    AzureSearchProfileSettings,
    OperatorProfileRegistry,
    RuntimeWebPluginConfig,
)
from tests.unit.web.composer.test_planner_authoring_aids import (
    _catalog_with_llm_source,
    _compiler_manager_with_llm_source,
    _custody_context,
    _empty_state,
    _guardrail_profile_view,
    _trained_view,
)
from tests.unit.web.composer.test_required_control_autowire import _INLINE_CONTENT, _bare_llm_candidate

# Every token the inert bindings write. None may appear on any surface but the probe's own dict.
_BINDING_TOKENS = ("validation-probe", "validationprobe", "VALIDATION_PROBE_UNSET")
_BINDING_KEYS = ("guardrail_identifier", "guardrail_version", "region", "bucket", "auth_mode", "endpoint", "api_key")

# The public, profile-authored option shape of each plugin, as a web author writes it.
_PUBLIC: dict[str, dict[str, Any]] = {
    "aws_bedrock_prompt_shield": {"fields": ["body"], "schema": {"mode": "observed"}},
    "aws_bedrock_content_safety": {"fields": ["body"], "schema": {"mode": "observed"}},
    "aws_textract_document_analysis": {
        "key_field": "document_key",
        "feature_types": ["TABLES"],
        "text_field": "doc_text",
        "schema": {"mode": "observed"},
    },
    "azure_ai_search": {"index": "kb", "query_field": "body", "output_prefix": "rag", "schema": {"mode": "observed"}},
}
_PROFILED = tuple(_PUBLIC)

# One operator alias per profile kind in the registry below.
_OPERATOR_ALIAS = {
    "aws_bedrock_prompt_shield": "prompt-approved",
    "aws_bedrock_content_safety": "content-approved",
    "aws_textract_document_analysis": "docs",
    "azure_ai_search": "kb",
    "aws_s3": "input",
    "llm": "sonnet",
}

# A private binding option beside the alias: refused by run-time lowering.
_PRIVATE_BESIDE_ALIAS = {
    "aws_bedrock_prompt_shield": {"region": "ap-southeast-2"},
    "aws_bedrock_content_safety": {"region": "ap-southeast-2"},
    "aws_textract_document_analysis": {"bucket": "author-bucket"},
    "azure_ai_search": {"endpoint": "https://author.search.windows.net"},
}

# A genuinely bad public option: refused by the plugin's config model with or without a binding.
_BAD_PUBLIC = {
    "aws_bedrock_prompt_shield": {"fields": 42},
    "aws_bedrock_content_safety": {"fields": 42},
    "aws_textract_document_analysis": {"feature_types": ["NOT_A_FEATURE"]},
    "azure_ai_search": {"output_prefix": 42},
}


def _runtime_config() -> RuntimeWebPluginConfig:
    """Operator settings carrying one profile of EVERY kind the registry knows."""
    return RuntimeWebPluginConfig(
        plugin_allowlist=(),
        power_automate_allowed_origins=(),
        plugin_preferences=(),
        plugin_control_modes=(),
        llm_profiles=(
            (
                "sonnet",
                RuntimeLLMProfile.from_settings(
                    "sonnet",
                    LLMProfileSettings(
                        provider="openrouter",
                        model="anthropic/claude-sonnet-4.6",
                        credential_scope="server",
                        credential_ref="OPENROUTER_API_KEY",
                    ),
                ),
            ),
        ),
        default_llm_profile=None,
        bedrock_guardrail_profiles=(
            BedrockGuardrailProfileSettings(
                alias="prompt-approved",
                plugin="aws_bedrock_prompt_shield",
                guardrail_identifier="operatorpromptguardrail",
                guardrail_version="3",
                region="ap-southeast-2",
            ),
            BedrockGuardrailProfileSettings(
                alias="content-approved",
                plugin="aws_bedrock_content_safety",
                guardrail_identifier="operatorcontentguardrail",
                guardrail_version="3",
                region="ap-southeast-2",
            ),
        ),
        bedrock_guardrail_default_profiles=(),
        aws_s3_source_profiles=(AWSS3SourceProfileSettings(alias="input", bucket="operator-input"),),
        aws_textract_profiles=(AWSTextractProfileSettings(alias="docs", bucket="operator-docs"),),
        azure_search_profiles=(
            AzureSearchProfileSettings(
                alias="kb",
                endpoint="https://operator.search.windows.net",
                auth="api_key",
                credential_ref="AZURE_SEARCH_KEY",
                indexes="any",
            ),
        ),
        deployment_aws_region="ap-southeast-2",
    )


def _registry() -> OperatorProfileRegistry:
    runtime = _runtime_config()
    return OperatorProfileRegistry(
        policy=compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime), settings=runtime
    )


def _lowered_twin_options(plugin: str, public: dict[str, Any]) -> dict[str, Any]:
    """What the operator's own profile lowers the same public options to at run time."""
    lowered = _registry().lower_options(PluginId("transform", plugin), alias=_OPERATOR_ALIAS[plugin], safe_options=dict(public))
    return dict(deep_thaw(lowered.executable_options))


def _node(node_id: str, plugin: str, inp: str, out: str, options: dict[str, Any]) -> NodeSpec:
    return NodeSpec(
        id=node_id,
        node_type="transform",
        plugin=plugin,
        input=inp,
        on_success=out,
        on_error="discard",
        options=options,
        condition=None,
        routes=None,
        fork_to=None,
        branches=None,
        policy=None,
        merge=None,
    )


def _chain(tmp_path: Path, plugin: str, options: dict[str, Any], *, sink_fields: tuple[str, ...] = ("widget_id",)) -> CompositionState:
    """csv (fixed) -> plugin -> json sink requiring ``sink_fields``."""
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={
                "path": str(tmp_path / "in.csv"),
                "schema": {"mode": "fixed", "fields": ["widget_id: str", "body: str", "document_key: str"]},
            },
            on_validation_failure="discard",
        ),
        nodes=(_node("guarded", plugin, "rows", "results", options),),
        edges=(),
        outputs=(
            OutputSpec(
                name="results",
                plugin="json",
                options={
                    "path": str(tmp_path / "out.jsonl"),
                    "format": "jsonl",
                    "schema": {"mode": "flexible", "fields": [f"{name}: any" for name in sink_fields]},
                },
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _codes(summary: ValidationSummary) -> tuple[list[str | None], list[str | None]]:
    return [entry.error_code for entry in summary.errors], [entry.error_code for entry in summary.warnings]


def _verdict(summary: ValidationSummary) -> tuple[object, ...]:
    """Everything a verdict says, apart from the node's own options."""
    return (
        summary.is_valid,
        sorted((entry.error_code, entry.message) for entry in summary.errors),
        sorted((entry.error_code, entry.message) for entry in summary.warnings),
        sorted(repr(contract) for contract in summary.edge_contracts),
    )


# ---------------------------------------------------------------------------
# Coverage: driven by the live profile registry, not a hand list
# ---------------------------------------------------------------------------


def _profile_field_values(config: RuntimeWebPluginConfig) -> dict[str, object]:
    """Each operator profile field by name, read as plain attributes, without reflection."""
    return {
        "llm_profiles": config.llm_profiles,
        "bedrock_guardrail_profiles": config.bedrock_guardrail_profiles,
        "bedrock_guardrail_default_profiles": config.bedrock_guardrail_default_profiles,
        "aws_s3_source_profiles": config.aws_s3_source_profiles,
        "aws_textract_profiles": config.aws_textract_profiles,
        "azure_search_profiles": config.azure_search_profiles,
    }


def test_every_profile_kind_in_the_operator_settings_is_populated_here() -> None:
    """A new profile kind adds a settings field; this fails until the coverage test below can see it."""
    profile_fields = {field.name for field in fields(RuntimeWebPluginConfig) if field.name.endswith("_profiles")}
    values = _profile_field_values(_runtime_config())

    assert profile_fields == set(values), (
        "a new operator profile kind: populate it in _runtime_config() and give its plugin a probe binding"
    )
    assert {name for name, value in values.items() if value} == profile_fields - {"bedrock_guardrail_default_profiles"}


def test_every_profile_bound_transform_has_an_inert_binding_and_no_row_is_stale() -> None:
    """Enumerate the registry over every registered plugin; each profile-bound transform needs a row.

    ``llm`` keeps its own gateway stub. ``aws_s3`` is a source: the source vote
    reads the raw schema block and never constructs the plugin, so a
    profile-only S3 source is already valid at authoring (measured in the
    builder's report). Both are named here so the enumeration's positive
    control is visible: the instrument does find profiled plugins.
    """
    registry = _registry()
    manager = get_shared_plugin_manager()
    profiled: set[tuple[str, str, str]] = set()
    for kind, names in (
        ("source", [plugin.name for plugin in manager.get_sources()]),
        ("transform", [plugin.name for plugin in manager.get_transforms()]),
    ):
        for name in names:
            for alias in _OPERATOR_ALIAS.values():
                detached = registry.detach_validation_lowering(PluginId(kind, name), alias=alias)
                if detached.kind != "unavailable":
                    profiled.add((kind, name, detached.kind))
                    break

    assert profiled == {
        ("source", "llm", "llm"),
        ("source", "aws_s3", "s3"),
        ("transform", "llm", "llm"),
        ("transform", "aws_bedrock_prompt_shield", "bedrock_guardrail"),
        ("transform", "aws_bedrock_content_safety", "bedrock_guardrail"),
        ("transform", "aws_textract_document_analysis", "textract"),
        ("transform", "azure_ai_search", "azure_search"),
    }
    needing_a_row = {(name, kind) for plugin_kind, name, kind in profiled if plugin_kind == "transform" and name != "llm"}
    from elspeth.web.composer._validation_probe import _inert_profile_bindings

    bindings = _inert_profile_bindings()
    assert {(name, binding.kind) for name, binding in bindings.items()} == needing_a_row
    assert set(bindings) == set(_PUBLIC)


# ---------------------------------------------------------------------------
# The projection is exactly what lowering writes, and only there
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plugin", _PROFILED)
def test_the_projection_has_the_lowered_shape_and_the_lowered_contract(plugin: str) -> None:
    authored = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}
    twin = _lowered_twin_options(plugin, _PUBLIC[plugin])

    prepared = prepare_validation_probe_options(authored, plugin=plugin)

    assert "profile" not in prepared
    assert set(prepared) == set(twin), "the probe writes exactly the keys run-time lowering writes"
    manager = get_shared_plugin_manager()
    probed = manager.create_transform(plugin, prepared)
    lowered = manager.create_transform(plugin, prepare_validation_probe_options(twin, plugin=plugin))
    for instance in (probed, lowered):
        assert instance._output_schema_config is not None
    assert probed.passes_through_input == lowered.passes_through_input
    assert probed.declared_input_fields == lowered.declared_input_fields
    assert probed.declared_output_fields == lowered.declared_output_fields
    assert probed._output_schema_config == lowered._output_schema_config


@pytest.mark.parametrize("plugin", _PROFILED)
def test_the_projection_never_writes_into_the_authored_options(plugin: str) -> None:
    authored = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}
    before = json.loads(json.dumps(authored))

    prepare_validation_probe_options(authored, plugin=plugin)

    assert authored == before


@pytest.mark.parametrize("plugin", _PROFILED)
def test_a_lowering_refusal_is_a_typed_config_error_naming_no_value(plugin: str) -> None:
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.web.composer.state import _is_config_probe_exception

    authored = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin], **_PRIVATE_BESIDE_ALIAS[plugin]}

    with pytest.raises(PluginConfigError) as raised:
        prepare_validation_probe_options(authored, plugin=plugin)

    assert raised.value.cause == "private_profile_option"
    assert _is_config_probe_exception(raised.value)
    for value in _PRIVATE_BESIDE_ALIAS[plugin].values():
        assert value not in str(raised.value)


def test_a_plugin_without_a_profile_gains_no_binding() -> None:
    """``profile`` on a plugin that has no operator profile stays an extra input and fails."""
    authored = {"profile": "anything", "fields": ["body"], "schema": {"mode": "observed"}, "blocked_patterns": ["x"]}

    prepared = prepare_validation_probe_options(authored, plugin="keyword_filter")

    assert prepared["profile"] == "anything"
    assert not set(prepared) & set(_BINDING_KEYS)


# ---------------------------------------------------------------------------
# Authoring: the node validates like its lowered twin
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plugin", _PROFILED)
def test_a_profile_only_node_validates_like_its_lowered_twin(tmp_path: Path, plugin: str) -> None:
    profiled = _chain(tmp_path, plugin, {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]})
    twin = _chain(tmp_path, plugin, _lowered_twin_options(plugin, _PUBLIC[plugin]))

    summary = profiled.validate()

    assert summary.is_valid, [entry.message for entry in summary.errors]
    assert "contract_probe_failed" not in _codes(summary)[1]
    assert _verdict(summary) == _verdict(twin.validate())


@pytest.mark.parametrize("plugin", ["aws_bedrock_prompt_shield", "aws_bedrock_content_safety"])
def test_a_profile_only_shield_forwards_its_upstream_guarantees(tmp_path: Path, plugin: str) -> None:
    state = _chain(tmp_path, plugin, {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}, sink_fields=("widget_id", "body"))

    summary = state.validate()

    assert summary.is_valid, [entry.message for entry in summary.errors]
    [contract] = [contract for contract in summary.edge_contracts if "body" in contract.consumer_requires]
    assert contract.satisfied
    assert {"widget_id", "body"} <= set(contract.producer_guarantees)


@pytest.mark.parametrize("plugin", _PROFILED)
@pytest.mark.parametrize("defect", ["private-option-beside-alias", "bad-public-option", "non-string-profile"])
def test_a_real_config_error_on_a_profile_bound_node_still_fails(tmp_path: Path, plugin: str, defect: str) -> None:
    authored: dict[str, Any] = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}
    if defect == "private-option-beside-alias":
        authored.update(_PRIVATE_BESIDE_ALIAS[plugin])
    elif defect == "bad-public-option":
        authored.update(_BAD_PUBLIC[plugin])
    else:
        authored["profile"] = 7

    summary = _chain(tmp_path, plugin, authored).validate()

    assert not summary.is_valid
    assert "contract_probe_failed" in _codes(summary)[1]
    assert any("guarantees: [(none)]" in entry.message for entry in summary.errors)


@pytest.mark.parametrize("plugin", _PROFILED)
def test_the_lowered_twin_with_the_same_bad_public_option_fails_the_same_way(tmp_path: Path, plugin: str) -> None:
    twin_options = {**_lowered_twin_options(plugin, _PUBLIC[plugin]), **_BAD_PUBLIC[plugin]}
    profiled_options = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin], **_BAD_PUBLIC[plugin]}

    twin = _chain(tmp_path, plugin, twin_options).validate()
    profiled = _chain(tmp_path, plugin, profiled_options).validate()

    assert _codes(twin) == _codes(profiled)
    assert "contract_probe_failed" in _codes(twin)[1]


# ---------------------------------------------------------------------------
# The binding never leaves the probe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("plugin", _PROFILED)
def test_the_binding_exists_only_in_the_detached_projection(tmp_path: Path, plugin: str) -> None:
    authored = {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}
    state = _chain(tmp_path, plugin, authored)

    summary = state.validate()

    assert summary.is_valid
    assert deep_thaw(state.nodes[0].options) == authored, "saved options keep only the public shape"
    surfaces = {
        "state.to_dict": json.dumps(state.to_dict(), default=str),
        "validation summary": json.dumps([entry.to_dict() for entry in (*summary.errors, *summary.warnings, *summary.suggestions)]),
        "generated pipeline": json.dumps(yaml_generator.generate_pipeline_dict(state), default=str),
        "generated yaml": yaml_generator.generate_yaml(state),
    }
    for surface, rendered in surfaces.items():
        for token in _BINDING_TOKENS:
            assert token not in rendered, (surface, token)


def test_the_tool_result_agrees_with_the_authored_verdict_and_carries_no_binding(tmp_path: Path) -> None:
    """Through the real set_pipeline candidate builder, for a web principal holding Bedrock profiles.

    The dispatched verdict validates the LOWERED state and was already valid;
    the authored state, which source demand and the planner's
    argument-rejection envelope read, was not. Both now agree.
    """
    (tmp_path / "outputs").mkdir(exist_ok=True)
    view, snapshot = _guardrail_profile_view(tmp_path)
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    candidate_args = _bare_llm_candidate()
    candidate_args["nodes"][0] = {
        "id": "shield",
        "node_type": "transform",
        "plugin": "aws_bedrock_prompt_shield",
        "input": "rows",
        "on_success": "assessed",
        "on_error": "discard",
        "options": {"profile": "prompt-approved", "fields": ["body"], "schema": {"mode": "observed"}},
    }
    candidate_args["outputs"][0]["options"]["schema"] = {"mode": "flexible", "fields": ["ticket_id: any"]}

    candidate = build_set_pipeline_candidate(candidate_args, _empty_state(), context)

    assert candidate.acceptable, candidate.result.to_dict()["validation"]["errors"]
    authored = candidate.result.updated_state.validate()
    assert authored.is_valid, [entry.message for entry in authored.errors]
    assert "contract_probe_failed" not in _codes(authored)[1]
    rendered = json.dumps(candidate.result.to_dict(), default=str)
    for token in _BINDING_TOKENS:
        assert token not in rendered, token
    [shield] = [node for node in candidate.result.updated_state.nodes if node.id == "shield"]
    assert not set(shield.options) & set(_BINDING_KEYS)


def test_a_trained_operator_still_cannot_write_a_profile_alias_on_these_plugins(tmp_path: Path) -> None:
    """The stub reaches the authoring probe only. Write-time prevalidation builds the
    principal's lowered options, and a trained operator's are never lowered, so
    ``profile`` stays an extra input there."""
    view, snapshot = _trained_view()
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    candidate_args = _bare_llm_candidate()
    candidate_args["nodes"][0] = {
        "id": "shield",
        "node_type": "transform",
        "plugin": "aws_bedrock_prompt_shield",
        "input": "rows",
        "on_success": "assessed",
        "on_error": "discard",
        "options": {"profile": "prompt-approved", "fields": ["body"], "schema": {"mode": "observed"}},
    }

    candidate = build_set_pipeline_candidate(candidate_args, _empty_state(), context)

    assert not candidate.acceptable
    [error] = candidate.result.validation.errors
    assert error.error_code == "plugin_options_invalid"
    assert "profile: Extra inputs are not permitted" in error.message


def test_validation_opens_no_client_and_no_socket(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The probe constructs; it never runs. Every client factory and socket refuses here."""

    def refuse(*_args: object, **_kwargs: object) -> object:
        raise AssertionError("authoring validation opened a client or a socket")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket, "create_connection", refuse)
    monkeypatch.setattr(boto3, "client", refuse)
    monkeypatch.setattr(boto3.session.Session, "client", refuse)
    with pytest.raises(AssertionError, match="opened a client"):
        boto3.client("bedrock-runtime", region_name="us-east-1")

    for plugin in _PROFILED:
        summary = _chain(tmp_path, plugin, {"profile": _OPERATOR_ALIAS[plugin], **_PUBLIC[plugin]}).validate()
        assert summary.is_valid, (plugin, [entry.message for entry in summary.errors])


# ---------------------------------------------------------------------------
# What the user sees: source demand and required controls
# ---------------------------------------------------------------------------


def _observed_source_shield_llm(tmp_path: Path, shield: str | None) -> CompositionState:
    llm = {
        "profile": "sonnet",
        "system_prompt": "You assess support tickets.",
        "prompt_template": "{{ row.ticket_id }} {{ row.body }}",
        "required_input_fields": ["ticket_id", "body"],
        "response_field": "assessment",
        "schema": {"mode": "observed"},
    }
    nodes = []
    if shield is not None:
        nodes.append(_node("shield", shield, "rows", "shielded", {"profile": _OPERATOR_ALIAS[shield], **_PUBLIC[shield]}))
    nodes.append(_node("assess", "llm", "shielded" if shield is not None else "rows", "assessed", llm))
    return CompositionState(
        source=SourceSpec(
            plugin="csv",
            on_success="rows",
            options={"path": str(tmp_path / "in.csv"), "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=tuple(nodes),
        edges=(),
        outputs=(
            OutputSpec(
                name="assessed",
                plugin="json",
                options={"path": str(tmp_path / "out.json"), "format": "json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


@pytest.mark.parametrize("shield", ["aws_bedrock_prompt_shield", "aws_bedrock_content_safety"])
def test_source_data_contract_demand_crosses_a_profile_only_shield(tmp_path: Path, shield: str) -> None:
    """The source data-contract card is derived from the AUTHORED state; behind a
    profile-only shield it demanded nothing, so the card was never offered."""
    control = backtraced_source_demand(_observed_source_shield_llm(tmp_path, None), "source")
    shielded_state = _observed_source_shield_llm(tmp_path, shield)

    demand = backtraced_source_demand(shielded_state, "source")

    assert control == ("body", "ticket_id")
    assert demand == control
    assert "contract_probe_failed" not in _codes(shielded_state.validate())[1]


def _content_safety_before_llm_candidate() -> dict[str, Any]:
    """csv -> content_safety (profile only, user-authored) -> llm -> sink requiring ticket_id."""
    candidate = _bare_llm_candidate()
    llm = candidate["nodes"][0]
    candidate["nodes"] = [
        {
            "id": "screen_input",
            "node_type": "transform",
            "plugin": "aws_bedrock_content_safety",
            "input": "rows",
            "on_success": "screened",
            "on_error": "discard",
            "options": {"profile": "content-approved", "fields": ["body"], "schema": {"mode": "observed"}},
        },
        {**llm, "input": "screened"},
    ]
    candidate["outputs"][0]["options"]["schema"] = {"mode": "flexible", "fields": ["ticket_id: any"]}
    return candidate


def test_a_required_control_wired_after_a_profile_only_node_finalizes_without_an_integrity_error(tmp_path: Path) -> None:
    """set_pipeline finalization splices the prompt shield AFTER the user's profile-only
    content-safety node; no AuditIntegrityError, and the authored state agrees."""
    (tmp_path / "outputs").mkdir(exist_ok=True)
    view, snapshot = _guardrail_profile_view(tmp_path)
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    arguments = _content_safety_before_llm_candidate()
    wired_preview = wire_required_controls(arguments, snapshot, view)
    wired_nodes = {node["id"]: node for node in wired_preview["nodes"]}
    assert wired_nodes["prompt_shield_auto_1"]["input"] == "screened", "the control is spliced after the profile-only node"

    finalized = asyncio.run(
        _finalize_complete_set_pipeline_candidate(arguments, _empty_state(), context, plugin_snapshot=snapshot, policy_catalog=view)
    )

    assert finalized.candidate.acceptable, finalized.candidate.result.to_dict()["validation"]["errors"]
    state = finalized.candidate.result.updated_state
    assert {node.id for node in state.nodes} >= {"screen_input", "prompt_shield_auto_1", "assess_ticket", "content_safety_auto_1"}
    authored = state.validate()
    assert authored.is_valid, [entry.message for entry in authored.errors]


def test_an_incremental_write_over_a_profile_only_node_finalizes_without_an_integrity_error(tmp_path: Path) -> None:
    """The incremental finalizer re-validates the wired state and raises AuditIntegrityError
    if it is invalid. Over a user-authored profile-only node it must not raise."""
    (tmp_path / "outputs").mkdir(exist_ok=True)
    view, snapshot = _guardrail_profile_view(tmp_path)
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    unwired = build_set_pipeline_candidate(_content_safety_before_llm_candidate(), _empty_state(), context)
    assert unwired.acceptable, unwired.result.to_dict()["validation"]["errors"]
    state = unwired.result.updated_state

    write = execute_tool("set_metadata", {"patch": {"name": "screened tickets"}}, state, view, plugin_snapshot=snapshot)
    assert write.success, write.to_dict()
    finalized = asyncio.run(_finalize_completed_incremental_mutation(write, state, plugin_snapshot=snapshot, policy_catalog=view))

    ids = [node.id for node in finalized.updated_state.nodes]
    assert "prompt_shield_auto_1" in ids
    [shield] = [node for node in finalized.updated_state.nodes if node.id == "prompt_shield_auto_1"]
    assert shield.input == "screened"
    authored = finalized.updated_state.validate()
    assert authored.is_valid, [entry.message for entry in authored.errors]


def _azure_search_view(tmp_path: Path) -> tuple[Any, Any]:
    """A web principal allowed ``azure_ai_search`` under one profile, ``kb``, that reaches only index ``approved``."""
    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_context_max_prompt_tokens=200_000,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        plugin_allowlist=("transform:azure_ai_search",),
        azure_search_profiles=(
            AzureSearchProfileSettings(
                alias="kb",
                endpoint="https://operator.search.windows.net",
                auth="api_key",
                credential_ref="AZURE_SEARCH_KEY",
                indexes=("approved",),
            ),
        ),
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=_compiler_manager_with_llm_source(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = _catalog_with_llm_source()

    class _ServerKeyInventory:
        def has_server_ref(self, name: str) -> bool:
            return name == "AZURE_SEARCH_KEY"

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return name == "AZURE_SEARCH_KEY"

        def server_generation(self, name: str) -> str | None:
            return "gen-1" if name == "AZURE_SEARCH_KEY" else None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:profile-only-azure",
        secret_inventory=_ServerKeyInventory(),
        generation_key=b"profile-only-azure-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


@pytest.mark.parametrize(
    ("label", "profile", "index", "refused"),
    [
        ("allowed_alias_and_index", "kb", "approved", False),
        ("index_outside_the_profile_list", "kb", "forbidden", True),
        ("unknown_alias", "nope", "approved", True),
    ],
)
def test_an_alias_or_index_the_operator_did_not_grant_is_refused_at_the_tool_call(
    tmp_path: Path, label: str, profile: str, index: str, refused: bool
) -> None:
    """The inert binding ignores the alias and reaches ``indexes="any"``, so authoring alone would accept both
    defects. The web write path lowers through the live registry (``_prevalidate_transform_for_context``, shared by
    ``upsert_node``, option patches and ``set_pipeline``), so the refusal the user sees stays at the tool call."""
    view, snapshot = _azure_search_view(tmp_path)
    source = SourceSpec(
        plugin="csv",
        on_success="rows",
        options={"path": str(tmp_path / "in.csv"), "schema": {"mode": "fixed", "fields": ["body: str"]}},
        on_validation_failure="discard",
    )
    state = CompositionState(source=source, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    options = {"profile": profile, "index": index, "query_field": "body", "output_prefix": "rag", "schema": {"mode": "observed"}}
    arguments = {
        "id": "rag",
        "node_type": "transform",
        "plugin": "azure_ai_search",
        "input": "rows",
        "on_success": "out",
        "on_error": "discard",
        "options": options,
    }

    result = execute_tool("upsert_node", arguments, state, view, plugin_snapshot=snapshot)

    stored = any(node.id == "rag" for node in result.updated_state.nodes)
    codes = [entry["error_code"] for entry in result.to_dict()["validation"]["errors"]]
    if refused:
        assert (result.success, stored) == (False, False), label
        assert "plugin_options_invalid" in codes, codes
    else:
        assert (result.success, stored) == (True, True), codes
