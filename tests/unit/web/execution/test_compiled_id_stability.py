"""Compiled node-id stability across preflight compiles and the run path.

Regression suite for elspeth-ba01834a57: the compiled node-id suffix is
sha256(rfc8785-canonical JSON of ``plugin.config``)[:12], so node IDENTITY is
whatever config bytes feed the build. The execution service builds under
``_audit_safe_plugin_configs`` (authored options for every operator-profiled
plugin), while the composer dry-run used to build bare (profile-lowered,
secret-resolved config) — the same node minted a different id on every
preflight vs every run (seam A), and the tolerant/strict interpretation
materializers minted different ids across adjacent turns (seam B: strict adds
``resolved_prompt_template_hash`` and the rendered prompt; tolerant does not).

None of these ids was pinned anywhere: ``tests/unit/core/test_dag.py``'s
"deterministic ids" test reuses one settings object and one build path, so it
never crossed either seam.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from types import SimpleNamespace
from typing import Any, cast

import pytest

from elspeth.contracts.hashing import stable_hash
from elspeth.contracts.secrets import ResolvedSecret, SecretInventoryItem, SecretScope
from elspeth.core.canonical import canonical_json
from elspeth.core.config import load_bounded_pipeline_yaml, load_settings_from_config_dict
from elspeth.core.dag.graph import ExecutionGraph
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.web.composer import yaml_generator as composer_yaml_generator
from elspeth.web.composer.state import CompositionState, NodeSpec, OutputSpec, PipelineMetadata, SourceSpec
from elspeth.web.config import WebSettings
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution import validation as validation_module
from elspeth.web.execution.preflight import build_runtime_graph, build_validated_runtime_graph, resolve_runtime_yaml_paths
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    InterpretationReviewPending,
    materialize_state_for_execution,
)
from elspeth.web.plugin_policy.availability import build_plugin_snapshot
from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.plugin_policy.validation import validate_plugin_policy
from elspeth.web.secrets.service import ScopedSecretResolver

_SESSION_ID = "test-session"
_PROMPT = "Summarise: {{ row.text }}"


class _AllSecretsInventory:
    """Snapshot-side inventory: every referenced credential is available."""

    def has_server_ref(self, name: str) -> bool:
        del name
        return True

    def has_user_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def has_ref(self, principal: str, name: str) -> bool:
        del principal, name
        return True

    def server_generation(self, name: str) -> str:
        del name
        return "generation"

    def user_generation(self, principal: str, name: str) -> str:
        del principal, name
        return "generation"


class _ScopedResolver(ScopedSecretResolver):
    """Preflight-side resolver: resolves every named secret to a fixed value.

    Subclasses the web ``ScopedSecretResolver`` because scoped-marker
    validation admits it NOMINALLY (ADR-032) — a structural lookalike is
    rejected with TypeError, so the fake must model the owned contract.
    """

    def __init__(self, secrets: dict[str, str]) -> None:
        # Deliberately does not call super().__init__ — the fake carries its
        # own store instead of a WebSecretService/auth-provider pair.
        self._secrets = secrets

    def list_refs(self, user_id: str) -> list[SecretInventoryItem]:
        del user_id
        return [SecretInventoryItem(name=name, scope="server", available=True) for name in self._secrets]

    def has_ref(self, user_id: str, name: str) -> bool:
        del user_id
        return name in self._secrets

    def resolve(self, user_id: str, name: str) -> ResolvedSecret | None:
        del user_id
        if name not in self._secrets:
            return None
        return ResolvedSecret(name=name, value=self._secrets[name], scope="server", fingerprint="a" * 64)

    def resolve_scoped(self, user_id: str, name: str, scope: SecretScope) -> ResolvedSecret | None:
        resolved = self.resolve(user_id, name)
        if resolved is None:
            return None
        return ResolvedSecret(name=resolved.name, value=resolved.value, scope=scope, fingerprint=resolved.fingerprint)


def _llm_profile_policy_context(
    *,
    llm_profile: dict[str, object],
) -> tuple[OperatorProfileRegistry, PluginAvailabilitySnapshot]:
    """Real registry + web snapshot for one operator LLM profile."""
    settings = WebSettings.model_validate(
        {
            "composer_max_composition_turns": 4,
            "composer_max_discovery_turns": 4,
            "composer_timeout_seconds": 60,
            "composer_rate_limit_per_minute": 20,
            "shareable_link_signing_key": b"0123456789abcdef0123456789abcdef",
            "plugin_allowlist": ["source:text", "transform:llm", "sink:json"],
            "llm_profiles": {"llm-default": llm_profile},
            "default_llm_profile": "llm-default",
        }
    )
    runtime_config = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=get_shared_plugin_manager(), settings=runtime_config)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime_config)
    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=create_catalog_service(),
        profiles=profiles,
        principal_scope="web:alice",
        secret_inventory=_AllSecretsInventory(),
        generation_key=b"compiled-id-stability-generation",
    )
    return profiles, snapshot


def _bedrock_profile() -> dict[str, object]:
    return {
        "provider": "bedrock",
        "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        "region_name": "us-east-1",
    }


def _profiled_llm_state(tmp_path: Path, *, prompt_requirement_status: str = "resolved") -> CompositionState:
    """Text source -> operator-profiled LLM -> JSON sink, review requirement included.

    The resolved ``llm_prompt_template`` requirement is what arms the seam-B
    delta: the strict materializer renders the prompt and stamps
    ``resolved_prompt_template_hash`` into the node options; the tolerant
    (authoring) materializer does neither, so lane-local identity hashing
    minted two ids for this one node.
    """
    blobs_dir = tmp_path / "blobs" / _SESSION_ID
    blobs_dir.mkdir(parents=True, exist_ok=True)
    text_path = blobs_dir / "input.txt"
    text_path.write_text("hello world\n", encoding="utf-8")
    interpretation_requirements = [
        {
            "id": "prompt_template_review:llm",
            "kind": "llm_prompt_template",
            "user_term": "llm_prompt_template:llm",
            "status": prompt_requirement_status,
            "draft": _PROMPT,
            "event_id": "evt-prompt",
            "accepted_value": _PROMPT if prompt_requirement_status == "resolved" else None,
            "accepted_artifact_hash": None,
            "resolved_prompt_template_hash": (stable_hash(_PROMPT) if prompt_requirement_status == "resolved" else None),
        },
    ]
    return CompositionState(
        source=SourceSpec(
            plugin="text",
            on_success="llm_in",
            options={"path": str(text_path), "column": "text", "schema": {"mode": "observed"}},
            on_validation_failure="discard",
        ),
        nodes=(
            NodeSpec(
                id="llm",
                node_type="transform",
                plugin="llm",
                input="llm_in",
                on_success="main",
                on_error="discard",
                options={
                    "profile": "llm-default",
                    "prompt_template": _PROMPT,
                    "response_field": "summary",
                    "required_input_fields": ["text"],
                    "schema": {"mode": "observed", "guaranteed_fields": ["summary"]},
                    INTERPRETATION_REQUIREMENTS_KEY: interpretation_requirements,
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
        outputs=(
            OutputSpec(
                name="main",
                plugin="json",
                options={"path": "outputs/out.json", "schema": {"mode": "observed"}},
                on_write_failure="discard",
            ),
        ),
        metadata=PipelineMetadata(),
        version=1,
    )


def _preflight_node_ids(
    state: CompositionState,
    tmp_path: Path,
    profiles: OperatorProfileRegistry,
    snapshot: PluginAvailabilitySnapshot,
    monkeypatch: pytest.MonkeyPatch,
    *,
    allow_pending: bool = False,
    secret_service: object | None = None,
    user_id: str | None = None,
) -> tuple[str, ...]:
    """Run the real composer dry-run and return the built graph's node ids."""
    graphs: list[ExecutionGraph] = []

    def _capturing_build(runtime_settings: Any, bundle: Any) -> ExecutionGraph:
        graph = build_runtime_graph(runtime_settings, bundle)
        graphs.append(graph)
        return graph

    with monkeypatch.context() as patcher:
        patcher.setattr(validation_module, "build_runtime_graph", _capturing_build)
        result = validation_module.validate_pipeline(
            state,
            cast(Any, SimpleNamespace(data_dir=tmp_path)),
            composer_yaml_generator,
            plugin_snapshot=snapshot,
            profile_registry=profiles,
            catalog=create_catalog_service(),
            secret_service=cast(Any, secret_service),
            user_id=user_id,
            allow_pending_interpretation_placeholders=allow_pending,
            session_id=_SESSION_ID,
        )
    assert result.is_valid is True, [error.message for error in result.errors]
    assert len(graphs) == 1
    return tuple(sorted(str(info.node_id) for info in graphs[0].get_nodes()))


def _run_path_node_ids(
    state: CompositionState,
    tmp_path: Path,
    profiles: OperatorProfileRegistry,
    snapshot: PluginAvailabilitySnapshot,
) -> tuple[tuple[str, ...], dict[str, Any]]:
    """Mint run-path ids exactly as the execution service does.

    Mirrors ``ExecutionServiceImpl``'s runtime preparation: strict
    materialization of the authored state, policy lowering, YAML generation
    for BOTH the authored (audit-safe) and executable states, runtime path
    resolution, then ``build_validated_runtime_graph`` under the audit-safe
    swap. Returns the ids plus the audit-safe config used, so callers can pin
    the identity bytes themselves.
    """
    materialized = materialize_state_for_execution(state)
    assert not isinstance(materialized, InterpretationReviewPending)
    policy_result = validate_plugin_policy(
        materialized,
        snapshot=snapshot,
        profile_registry=profiles,
        catalog=create_catalog_service(),
    )
    assert policy_result.findings == ()
    pipeline_yaml = resolve_runtime_yaml_paths(
        composer_yaml_generator.generate_yaml(materialized),
        str(tmp_path),
        session_id=_SESSION_ID,
    )
    executable_yaml = resolve_runtime_yaml_paths(
        composer_yaml_generator.generate_yaml(policy_result.executable_state),
        str(tmp_path),
        session_id=_SESSION_ID,
    )
    audit_safe_config = load_bounded_pipeline_yaml(pipeline_yaml)
    executable_config = load_bounded_pipeline_yaml(executable_yaml)
    assert type(audit_safe_config) is dict
    assert type(executable_config) is dict
    runtime_settings = load_settings_from_config_dict(cast(dict[str, Any], executable_config), expand_env_vars=False)
    runtime_graph = build_validated_runtime_graph(
        runtime_settings,
        plugin_snapshot=snapshot,
        audit_safe_settings=cast(dict[str, Any], audit_safe_config),
    )
    ids = tuple(sorted(str(info.node_id) for info in runtime_graph.graph.get_nodes()))
    return ids, cast(dict[str, Any], audit_safe_config)


def _llm_id(node_ids: tuple[str, ...]) -> str:
    llm_ids = [node_id for node_id in node_ids if node_id.startswith("transform_llm_")]
    assert len(llm_ids) == 1, node_ids
    return llm_ids[0]


def test_same_state_compiled_twice_mints_identical_node_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    profiles, snapshot = _llm_profile_policy_context(llm_profile=_bedrock_profile())
    state = _profiled_llm_state(tmp_path)
    first = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch)
    second = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch)
    assert first == second


def test_tolerant_and_strict_preflight_mint_identical_node_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seam B: the two interpretation lanes must hash the same identity bytes.

    The state carries a RESOLVED prompt-template review (a pending one cannot
    pass the strict lane at all — ``interpretation_review`` fails closed
    before any graph exists), which is exactly the adjacent-turn shape whose
    ids churned: strict materialization stamps the rendered prompt and
    ``resolved_prompt_template_hash`` into the llm node options, tolerant
    materialization does not.
    """
    profiles, snapshot = _llm_profile_policy_context(llm_profile=_bedrock_profile())
    state = _profiled_llm_state(tmp_path)
    strict = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch, allow_pending=False)
    tolerant = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch, allow_pending=True)
    assert _llm_id(strict) == _llm_id(tolerant)
    assert strict == tolerant


def test_preflight_ids_equal_run_path_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Seam A: the dry-run and ``build_validated_runtime_graph`` agree per node.

    Also pins the mechanism byte-for-byte: the profiled llm node's suffix is
    sha256(canonical_json(<AUTHORED options from the audit-safe config>))[:12]
    — the lowered config (provider/model injected, profile popped) must not
    feed identity on either path.
    """
    profiles, snapshot = _llm_profile_policy_context(llm_profile=_bedrock_profile())
    state = _profiled_llm_state(tmp_path)
    preflight_ids = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch)
    run_ids, audit_safe_config = _run_path_node_ids(state, tmp_path, profiles, snapshot)
    assert preflight_ids == run_ids

    transforms = cast(list[dict[str, Any]], audit_safe_config["transforms"])
    llm_component = next(component for component in transforms if component["name"] == "llm")
    authored_options = llm_component["options"]
    assert authored_options["profile"] == "llm-default"
    assert "provider" not in authored_options
    expected_suffix = hashlib.sha256(canonical_json(authored_options).encode()).hexdigest()[:12]
    assert _llm_id(preflight_ids) == f"transform_llm_{expected_suffix}"


def test_secret_resolver_presence_does_not_move_node_ids(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A credentialed profile resolves (or redacts) its api_key per lane; the
    resolved and redacted configs differ byte-for-byte, but identity derives
    from the authored options, which never carry the credential at all."""
    profiles, snapshot = _llm_profile_policy_context(
        llm_profile={
            "provider": "openrouter",
            "model": "anthropic/claude-3-haiku",
            "credential_scope": "server",
            "credential_ref": "OPENROUTER_API_KEY",
        }
    )
    state = _profiled_llm_state(tmp_path)
    without_resolver = _preflight_node_ids(state, tmp_path, profiles, snapshot, monkeypatch, secret_service=None, user_id=None)
    with_resolver = _preflight_node_ids(
        state,
        tmp_path,
        profiles,
        snapshot,
        monkeypatch,
        secret_service=_ScopedResolver({"OPENROUTER_API_KEY": "sk-test-value"}),
        user_id="alice",
    )
    assert without_resolver == with_resolver
