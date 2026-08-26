"""Live-rendered planner authoring aids: exemplars must validate, byte-for-byte.

The aids exist because the 2026-07-22 pack stress test showed cold planners
fabricating ``blob_id`` values and missing the source options contract: the
pack had rules but no worked exemplars, and the ``no_deployment_plugin_facts``
gate (correctly) forbids plugin literals in the static prompts. These tests
enforce the self-verifying-teaching contract: the exact exemplar objects the
planner prompt carries are run through ``build_set_pipeline_candidate``
against the real catalog, so a drifting exemplar fails CI instead of teaching
planners an invalid shape.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace
from typing import Any, get_args
from uuid import uuid4

import pytest
from sqlalchemy import insert
from sqlalchemy.pool import StaticPool

from elspeth.web.catalog.policy_view import PolicyCatalogView
from elspeth.web.catalog.schemas import PluginSummary
from elspeth.web.composer import planner_authoring_aids
from elspeth.web.composer.planner_authoring_aids import (
    PLACEHOLDER_BLOB_ID,
    build_planner_authoring_aids,
    discovery_digest,
    fork_coalesce_exemplar_args,
    planner_expression_grammar,
    planner_model_catalog,
    source_custody_exemplar_args,
)
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolContext, build_set_pipeline_candidate
from elspeth.web.composer.tools import execute_tool as _dispatch_tool
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import chat_messages_table, sessions_table
from elspeth.web.sessions.schema import initialize_session_schema


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _trained_view() -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    catalog = create_catalog_service()
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    return PolicyCatalogView.for_trained_operator(catalog, snapshot), snapshot


def _compiler_manager_with_llm_source() -> Any:
    """Required-policy registry while production source discovery stays off."""
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sources.llm import LLMSource

    class _CompilerLLMSource(LLMSource):
        determinism = LLMSource.determinism
        source_file_hash = "sha256:0123456789abcdef"

    manager = PluginManager()
    manager.register_builtin_plugins()
    if all(source.name != "llm" for source in manager.get_sources()):
        manager.register(create_dynamic_hookimpl([_CompilerLLMSource], "elspeth_get_source"))
    return manager


def _catalog_with_llm_source() -> Any:
    from elspeth.web.catalog.service import CatalogServiceImpl

    return CatalogServiceImpl(_compiler_manager_with_llm_source())


def test_authoring_aids_memo_access_is_locked(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lookup, eviction, and insertion are locked while cold builds remain off-lock."""

    class LockCheckedMemo(dict[str, dict[str, Any]]):
        def _assert_locked(self) -> None:
            assert planner_authoring_aids._AIDS_MEMO_LOCK.locked()

        def get(self, key: str, default: Any = None) -> Any:
            self._assert_locked()
            return super().get(key, default)

        def __len__(self) -> int:
            self._assert_locked()
            return super().__len__()

        def __setitem__(self, key: str, value: dict[str, Any]) -> None:
            self._assert_locked()
            super().__setitem__(key, value)

        def __iter__(self) -> Iterator[str]:
            self._assert_locked()
            return super().__iter__()

        def pop(self, key: str, default: Any = None) -> Any:
            self._assert_locked()
            return super().pop(key, default)

    memo = LockCheckedMemo({"old-snapshot": {"key": "old-value"}})
    monkeypatch.setattr(planner_authoring_aids, "_AIDS_MEMO", memo)
    monkeypatch.setattr(planner_authoring_aids, "_AIDS_MEMO_MAX", 1)
    monkeypatch.setattr(planner_authoring_aids, "read_openrouter_catalog_snapshot_id", lambda: ("catalog-sha", "live"))

    def build(_catalog: object) -> dict[str, str]:
        assert not planner_authoring_aids._AIDS_MEMO_LOCK.locked()
        return {"key": "value"}

    monkeypatch.setattr(planner_authoring_aids, "_build_planner_authoring_aids", build)
    catalog: Any = SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash="snapshot"))

    first = planner_authoring_aids.build_planner_authoring_aids(catalog)
    second = planner_authoring_aids.build_planner_authoring_aids(catalog)

    assert first == second == {"key": "value"}
    assert first is not second
    assert dict(memo) == {"snapshot:live:catalog-sha": {"key": "value"}}


@pytest.mark.parametrize("snapshot_hashes", [["same"] * 4, ["one", "two", "three", "four"]])
def test_authoring_aids_concurrent_cold_builds_are_bounded_and_isolated(
    monkeypatch: pytest.MonkeyPatch,
    snapshot_hashes: list[str],
) -> None:
    """Concurrent misses cannot corrupt eviction or share mutable return values."""
    barrier = Barrier(len(snapshot_hashes))
    monkeypatch.setattr(planner_authoring_aids, "_AIDS_MEMO", {})
    monkeypatch.setattr(planner_authoring_aids, "_AIDS_MEMO_MAX", 2)

    def build(catalog: Any) -> dict[str, str]:
        assert not planner_authoring_aids._AIDS_MEMO_LOCK.locked()
        barrier.wait(timeout=5)
        return {"purpose": catalog.snapshot.snapshot_hash}

    monkeypatch.setattr(planner_authoring_aids, "_build_planner_authoring_aids", build)
    catalogs = [SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash=value)) for value in snapshot_hashes]

    with ThreadPoolExecutor(max_workers=len(catalogs)) as executor:
        results = list(executor.map(planner_authoring_aids.build_planner_authoring_aids, catalogs))

    assert [result["purpose"] for result in results] == snapshot_hashes
    assert len(planner_authoring_aids._AIDS_MEMO) <= 2
    assert len({id(result) for result in results}) == len(results)


def _profile_view(tmp_path: Path) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    """Live-deployment posture: one OpenRouter LLM operator profile.

    Mirrors ``_operator_profile_view`` in ``test_set_pipeline_candidate.py`` —
    the posture every failing planner surface (tutorial, guided, freeform web)
    actually runs under, where llm nodes are authored via a profile alias.
    """
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=_compiler_manager_with_llm_source(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = _catalog_with_llm_source()

    class _ServerKeyInventory:
        def has_server_ref(self, name: str) -> bool:
            return name == "OPENROUTER_API_KEY"

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return name == "OPENROUTER_API_KEY"

        def server_generation(self, name: str) -> str | None:
            return "gen-1" if name == "OPENROUTER_API_KEY" else None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:authoring-aids-profile",
        secret_inventory=_ServerKeyInventory(),
        generation_key=b"authoring-aids-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _source_only_profile_view(
    tmp_path: Path,
    *,
    content_safety_required: bool = False,
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    """Pre-discovery source profile posture with transform:llm hidden."""
    from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
    from elspeth.plugins.infrastructure.discovery import create_dynamic_hookimpl
    from elspeth.plugins.infrastructure.manager import PluginManager
    from elspeth.plugins.sources.llm import LLMSource
    from elspeth.web.catalog.service import CatalogServiceImpl
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    manager = PluginManager()
    manager.register_builtin_plugins()
    if all(source.name != "llm" for source in manager.get_sources()):
        manager.register(create_dynamic_hookimpl([LLMSource], "elspeth_get_source"))
    catalog = CatalogServiceImpl(manager)
    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = WebPluginPolicy.create(
        required=frozenset(),
        configured_optional=frozenset(),
        preferences=(),
        control_modes=(),
        plugin_code_identities=(),
    )
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    transform_llm = PluginId("transform", "llm")
    available = frozenset(
        {
            *(PluginId("source", item.name) for item in catalog.list_sources()),
            *(PluginId("transform", item.name) for item in catalog.list_transforms()),
            *(PluginId("sink", item.name) for item in catalog.list_sinks()),
        }
        - {transform_llm}
    )
    source_llm = PluginId("source", "llm")
    selected = tuple(
        (
            capability,
            (
                PluginId("transform", "aws_bedrock_content_safety")
                if capability is PluginCapability.CONTENT_SAFETY and content_safety_required
                else source_llm
                if capability is PluginCapability.LLM
                else None
            ),
        )
        for capability in PluginCapability
    )
    control_modes = ((PluginCapability.CONTENT_SAFETY, ControlMode.REQUIRED),) if content_safety_required else ()
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="source-only-policy",
        principal_scope="local:source-only",
        available=available,
        unavailable=(),
        selected=selected,
        usable_profile_aliases=((source_llm, ("sonnet",)),),
        selected_profile_aliases=((source_llm, "sonnet"),),
        binding_generation_fingerprint="source-only-bindings",
        control_modes=control_modes,
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _guardrail_profile_view(
    tmp_path: Path,
    *,
    control_mode: str = "required",
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    """Control-required posture: LLM profile plus both Bedrock Guardrail profiles.

    Mirrors the AWS scenario module, which authorizes the two Bedrock controls,
    sets both control modes to ``required``, and binds each to an operator-owned
    Guardrail behind an opaque alias.
    """
    from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
    from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
        plugin_allowlist=("transform:aws_bedrock_prompt_shield", "transform:aws_bedrock_content_safety"),
        plugin_preferences={
            PluginCapability.PROMPT_SHIELD: ("transform:aws_bedrock_prompt_shield",),
            PluginCapability.CONTENT_SAFETY: ("transform:aws_bedrock_content_safety",),
        },
        plugin_control_modes={
            PluginCapability.PROMPT_SHIELD: ControlMode(control_mode),
            PluginCapability.CONTENT_SAFETY: ControlMode(control_mode),
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

    class _ServerKeyInventory:
        def has_server_ref(self, name: str) -> bool:
            return name == "OPENROUTER_API_KEY"

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return name == "OPENROUTER_API_KEY"

        def server_generation(self, name: str) -> str | None:
            return "gen-1" if name == "OPENROUTER_API_KEY" else None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:authoring-aids-guardrail",
        secret_inventory=_ServerKeyInventory(),
        generation_key=b"authoring-aids-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _direct_control_view(
    tmp_path: Path,
    *,
    control_mode: str = "required",
) -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot]:
    """Control posture with NO operator profile aliases for the controls.

    The Azure safety plugins are USER_CONFIGURABLE_WITH_POLICY: selected and
    available (their credential is in the inventory) but carrying zero
    operator profile aliases — the direct-configuration deployment shape.
    """
    from elspeth.contracts.plugin_capabilities import ControlMode, PluginCapability
    from elspeth.web.config import WebSettings
    from elspeth.web.plugin_policy.availability import build_plugin_snapshot
    from elspeth.web.plugin_policy.compiler import compile_web_plugin_policy
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig

    settings = WebSettings(
        data_dir=tmp_path,
        composer_model="test/planner",
        composer_max_composition_turns=3,
        composer_max_discovery_turns=2,
        composer_timeout_seconds=20.0,
        composer_rate_limit_per_minute=10,
        shareable_link_signing_key=b"\x00" * 32,
        llm_profiles={
            "sonnet": {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "credential_scope": "server",
                "credential_ref": "OPENROUTER_API_KEY",
            }
        },
        default_llm_profile="sonnet",
        plugin_allowlist=("transform:azure_prompt_shield", "transform:azure_content_safety"),
        plugin_preferences={
            PluginCapability.PROMPT_SHIELD: ("transform:azure_prompt_shield",),
            PluginCapability.CONTENT_SAFETY: ("transform:azure_content_safety",),
        },
        plugin_control_modes={
            PluginCapability.PROMPT_SHIELD: ControlMode(control_mode),
            PluginCapability.CONTENT_SAFETY: ControlMode(control_mode),
        },
    )
    runtime = RuntimeWebPluginConfig.from_settings(settings)
    policy = compile_web_plugin_policy(registry=_compiler_manager_with_llm_source(), settings=runtime)
    profiles = OperatorProfileRegistry(policy=policy, settings=runtime)
    catalog = _catalog_with_llm_source()

    class _ServerKeyInventory:
        _names = frozenset({"OPENROUTER_API_KEY", "AZURE_CONTENT_SAFETY_KEY"})

        def has_server_ref(self, name: str) -> bool:
            return name in self._names

        def has_user_ref(self, principal: str, name: str) -> bool:
            return False

        def has_ref(self, principal: str, name: str) -> bool:
            return name in self._names

        def server_generation(self, name: str) -> str | None:
            return "gen-1" if name in self._names else None

        def user_generation(self, principal: str, name: str) -> str | None:
            return None

    snapshot = build_plugin_snapshot(
        policy=policy,
        catalog=catalog,
        profiles=profiles,
        principal_scope="local:authoring-aids-direct-control",
        secret_inventory=_ServerKeyInventory(),
        generation_key=b"authoring-aids-key",
    )
    return PolicyCatalogView(catalog, snapshot, profiles), snapshot


def _session_with_user_message(content: str) -> tuple[Any, str, str, str]:
    """Session + user chat message whose text contains ``content`` verbatim."""
    engine = create_session_engine(
        "sqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    initialize_session_schema(engine)
    session_id = str(uuid4())
    message_id = str(uuid4())
    message_content = f"Use this exact content:\n{content}"
    now = datetime.now(UTC)
    with engine.begin() as conn:
        conn.execute(
            insert(sessions_table).values(
                id=session_id,
                user_id="authoring-aids-user",
                auth_provider_type="local",
                title="authoring aids exemplar validation",
                created_at=now,
                updated_at=now,
            )
        )
        conn.execute(
            insert(chat_messages_table).values(
                id=message_id,
                session_id=session_id,
                role="user",
                content=message_content,
                raw_content=None,
                tool_calls=None,
                tool_call_id=None,
                sequence_no=1,
                writer_principal="route_user_message",
                created_at=now,
                composition_state_id=None,
                parent_assistant_id=None,
            )
        )
    return engine, session_id, message_id, message_content


def _custody_context(
    tmp_path: Path,
    content: str,
    *,
    view: PolicyCatalogView | None = None,
    snapshot: PluginAvailabilitySnapshot | None = None,
) -> ToolContext:
    if view is None or snapshot is None:
        view, snapshot = _trained_view()
    engine, session_id, message_id, message_content = _session_with_user_message(content)
    return ToolContext(
        catalog=view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content=message_content,
    )


def _create_real_blob(tmp_path: Path, *, filename: str, mime_type: str, content: str) -> tuple[str, ToolContext]:
    """Create a session blob through the real tool and return (blob_id, context)."""
    view, snapshot = _trained_view()
    engine, session_id, message_id, message_content = _session_with_user_message(content)
    (tmp_path / "blobs").mkdir(exist_ok=True)
    context = ToolContext(
        catalog=view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content=message_content,
    )
    result = _dispatch_tool(
        "create_blob",
        {"filename": filename, "mime_type": mime_type, "content": content},
        _empty_state(),
        view,
        plugin_snapshot=snapshot,
        data_dir=str(tmp_path),
        session_engine=engine,
        session_id=session_id,
        user_message_id=message_id,
        user_message_content=message_content,
    )
    assert result.success is True, result.to_dict()
    return result.data["blob_id"], context


class TestSourceCustodyExemplar:
    def test_inline_blob_exemplar_validates_through_the_real_candidate_builder(self, tmp_path: Path) -> None:
        """The exact inline-custody exemplar bytes the prompt carries must build."""
        view, _snapshot = _trained_view()
        args = source_custody_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"inline custody exemplar rejected: {rejection}"

    def test_existing_blob_exemplar_validates_with_a_real_session_blob(self, tmp_path: Path) -> None:
        """The blob_id binding exemplar must build once given a tool-returned id."""
        view, _snapshot = _trained_view()
        placeholder_args = source_custody_exemplar_args(view, blob_id=PLACEHOLDER_BLOB_ID)
        assert placeholder_args is not None
        inline_args = source_custody_exemplar_args(view)
        assert inline_args is not None
        blob_id, context = _create_real_blob(
            tmp_path,
            filename=inline_args["source"]["inline_blob"]["filename"],
            mime_type=inline_args["source"]["inline_blob"]["mime_type"],
            content=inline_args["source"]["inline_blob"]["content"],
        )
        args = source_custody_exemplar_args(view, blob_id=blob_id)
        assert args is not None

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"existing-blob exemplar rejected: {rejection}"
        # Single-source contract: only the binding differs between the two
        # variants the prompt shows; everything downstream is byte-identical.
        assert {key: value for key, value in args.items() if key != "source"} == {
            key: value for key, value in placeholder_args.items() if key != "source"
        }
        assert placeholder_args["source"]["blob_id"] == PLACEHOLDER_BLOB_ID

    def test_exemplar_variants_differ_only_in_the_custody_binding(self) -> None:
        view, _snapshot = _trained_view()
        inline_args = source_custody_exemplar_args(view)
        blob_args = source_custody_exemplar_args(view, blob_id=PLACEHOLDER_BLOB_ID)
        assert inline_args is not None and blob_args is not None

        assert "inline_blob" in inline_args["source"]
        assert "blob_id" not in inline_args["source"]
        assert "blob_id" in blob_args["source"]
        assert "inline_blob" not in blob_args["source"]
        # Neither variant authors custody-owned fields by hand.
        for variant in (inline_args, blob_args):
            assert "path" not in variant["source"]["options"]
            assert "blob_ref" not in variant["source"]["options"]
            assert variant["source"]["options"]["schema"]["mode"]
            assert variant["source"]["on_validation_failure"]


def _assert_operator_ruled_topology(args: dict[str, Any]) -> None:
    """Structural invariants BOTH exemplar variants must satisfy.

    Gate condition True / routes true,false -> fork / fork_to two branches;
    one branch transform per fork branch; coalesce branches keyed by FORK
    BRANCH NAME with values naming each branch's arriving connection;
    require_all / union; no on_success on the coalesce; downstream consumes
    the coalesce id and routes to the sink.
    """
    nodes = {node["id"]: node for node in args["nodes"]}
    gates = [node for node in nodes.values() if node["node_type"] == "gate"]
    coalesces = [node for node in nodes.values() if node["node_type"] == "coalesce"]
    assert len(gates) == 1 and len(coalesces) == 1
    gate, coalesce = gates[0], coalesces[0]
    assert gate["condition"] == "True"
    assert set(gate["routes"]) == {"true", "false"}
    assert set(gate["routes"].values()) == {"fork"}
    assert len(gate["fork_to"]) == 2
    branch_nodes = [node for node in nodes.values() if node.get("input") in gate["fork_to"]]
    assert len(branch_nodes) == 2
    assert {node["input"] for node in branch_nodes} == set(gate["fork_to"])
    assert all(node["node_type"] == "transform" for node in branch_nodes)
    assert set(coalesce["branches"]) == set(gate["fork_to"])
    assert set(coalesce["branches"].values()) == {node["on_success"] for node in branch_nodes}
    assert coalesce["policy"] == "require_all"
    assert coalesce["merge"] == "union"
    assert "on_success" not in coalesce
    # Downstream cleanup consumes the coalesce's own node id.
    cleanup = next(node for node in nodes.values() if node.get("input") == coalesce["id"])
    assert cleanup["node_type"] == "transform"
    assert cleanup["on_success"] == args["outputs"][0]["sink_name"]


class TestForkCoalesceExemplar:
    def test_fork_coalesce_exemplar_validates_under_the_live_profile_posture(self, tmp_path: Path) -> None:
        """The exact fork -> two-llm -> coalesce exemplar bytes must build."""
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content, view=view, snapshot=snapshot)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"fork/coalesce exemplar rejected: {rejection}"

    def test_fork_coalesce_exemplar_has_the_operator_ruled_topology(self, tmp_path: Path) -> None:
        """Two separate LLM transform nodes + coalesce merge — never a queries map."""
        view, _snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        _assert_operator_ruled_topology(args)
        nodes = {node["id"]: node for node in args["nodes"]}
        llms = [node for node in nodes.values() if node.get("plugin") == "llm"]
        gate = next(node for node in nodes.values() if node["node_type"] == "gate")
        assert len(llms) == 2
        # One llm per fork branch, each with its own prompt and output field.
        assert {llm["input"] for llm in llms} == set(gate["fork_to"])
        assert len({llm["options"]["prompt_template"] for llm in llms}) == 2
        assert len({llm["options"]["response_field"] for llm in llms}) == 2
        assert all("queries" not in llm["options"] for llm in llms)

    def test_forked_exemplar_still_builds_with_controls_inserted(self, tmp_path: Path) -> None:
        """Inserting the control nodes must not break the exemplar's topology.

        Scope: this proves the bytes still construct a valid pipeline with two
        extra nodes spliced into the stream chain. It does NOT prove control
        coverage — ``build_set_pipeline_candidate`` does not run the plugin-policy
        coverage check (an uncovered fork is accepted here and rejected later, at
        completion validation). The dominance relationships that satisfy coverage
        are asserted structurally in the next test.
        """
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _guardrail_profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content, view=view, snapshot=snapshot)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"control-required fork exemplar rejected: {rejection}"

    def test_selected_controls_are_wired_into_the_forked_llm_exemplar(self, tmp_path: Path) -> None:
        """A control-required deployment must not be taught two bare llm branches.

        ``control_coverage_findings`` proves coverage per LLM node: the shield
        must dominate the node's prompt inputs and content safety must dominate
        every one of its output streams. An exemplar modelling an uncovered fork
        would teach exactly the topology this deployment's validator rejects, so
        the controls are wired — one shield upstream of the fork covering both
        branches' prompt fields, one safety node downstream of the rejoin
        covering both branches' response fields.
        """
        view, _snapshot = _guardrail_profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        nodes = {node["id"]: node for node in args["nodes"]}
        llms = [node for node in nodes.values() if node.get("plugin") == "llm"]
        assert len(llms) == 2
        gate = next(node for node in nodes.values() if node["node_type"] == "gate")
        coalesce = next(node for node in nodes.values() if node["node_type"] == "coalesce")

        shield = next(node for node in nodes.values() if node.get("plugin") == "aws_bedrock_prompt_shield")
        safety = next(node for node in nodes.values() if node.get("plugin") == "aws_bedrock_content_safety")

        # The shield dominates the fork, so one node covers both branches, and it
        # covers exactly the row fields the branch prompts interpolate.
        assert shield["input"] == args["source"]["on_success"]
        assert gate["input"] == shield["on_success"]
        prompt_fields = {"ticket_id", "body"}
        assert prompt_fields <= set(shield["options"]["fields"])

        # Safety dominates the rejoin, covering both branches' response fields.
        assert safety["input"] == coalesce["id"]
        assert {llm["options"]["response_field"] for llm in llms} <= set(safety["options"]["fields"])
        cleanup = next(node for node in nodes.values() if node.get("plugin") == "field_mapper")
        assert cleanup["input"] == safety["on_success"]

        # Operator-owned bindings stay behind the alias — never modelled inline.
        for control in (shield, safety):
            assert control["options"]["profile"] in {"prompt-approved", "content-approved"}
            assert "guardrail_identifier" not in control["options"]
            assert "guardrail_version" not in control["options"]

    def test_forked_exemplar_passes_required_control_coverage_at_completion(self, tmp_path: Path) -> None:
        """The accepted exemplar must clear the completion-validation coverage gate.

        ``build_set_pipeline_candidate`` does not run required-control
        coverage — completion validation does. The exemplar's
        one-shield-above-the-fork placement is only teachable if that gate
        accepts a shield dominating both branches through the fan-out gate.
        """
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _guardrail_profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content, view=view, snapshot=snapshot)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)
        assert candidate.acceptable is True, (candidate.result.data or {}).get("error")

        result = view.validate_authored_state(candidate.result.updated_state)
        coverage = [finding for finding in result.findings if finding.stage == "required_control_coverage"]
        assert coverage == [], [finding.message for finding in coverage]

    def test_recommended_controls_remain_advisory_and_do_not_mutate_the_exemplar(self, tmp_path: Path) -> None:
        from elspeth.web.interpretation_state import PROMPT_SHIELD_AVAILABLE_DRAFT

        view, _snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        plugins = {node.get("plugin") for node in args["nodes"]}
        assert "aws_bedrock_prompt_shield" not in plugins
        assert "aws_bedrock_content_safety" not in plugins

        aids = build_planner_authoring_aids(view)
        rendered = "\n".join(aids["prompt_shield"]["rules"])
        assert PROMPT_SHIELD_AVAILABLE_DRAFT in rendered
        assert "required, not advisory" not in rendered
        assert "content_safety" not in aids

    def test_required_controls_without_profile_aliases_are_still_wired_into_the_exemplar(self, tmp_path: Path) -> None:
        """Zero profile aliases must not drop a REQUIRED control from the exemplar.

        A selected control can be usable without operator profile aliases
        (direct user-configurable plugins such as the Azure safety pair).
        Omitting the control nodes taught exactly the uncovered fork this
        deployment's required-control-coverage validator rejects. The
        alias-less form authors the control directly: no ``profile`` option,
        the credential wired as the supported inline ``secret_ref`` marker,
        and the remaining required service bindings as placeholders.
        """
        view, _snapshot = _direct_control_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        nodes = {node["id"]: node for node in args["nodes"]}
        shield = next(node for node in nodes.values() if node.get("plugin") == "azure_prompt_shield")
        safety = next(node for node in nodes.values() if node.get("plugin") == "azure_content_safety")
        gate = next(node for node in nodes.values() if node["node_type"] == "gate")
        coalesce = next(node for node in nodes.values() if node["node_type"] == "coalesce")

        # Same dominance wiring as the profiled variant.
        assert shield["input"] == args["source"]["on_success"]
        assert gate["input"] == shield["on_success"]
        assert safety["input"] == coalesce["id"]

        for control in (shield, safety):
            assert "profile" not in control["options"]
            assert control["options"]["fields"]
            assert control["options"]["api_key"] == {"secret_ref": "AZURE_CONTENT_SAFETY_KEY"}
        # Effective blocking posture — all-6 thresholds are a coverage no-op.
        thresholds = safety["options"]["thresholds"]
        assert any(value < 6 for value in thresholds.values())


class TestForkRowUnionExemplar:
    def test_exemplar_validates_and_teaches_the_row_union_contract(self, tmp_path: Path) -> None:
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _trained_view()
        args = planner_authoring_aids.fork_row_union_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content, view=view, snapshot=snapshot)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"fork/row_union exemplar rejected: {rejection}"
        nodes = {node["id"]: node for node in args["nodes"]}
        gate = next(node for node in nodes.values() if node["node_type"] == "gate")
        union = next(node for node in nodes.values() if node["node_type"] == "row_union")
        branch_nodes = [node for node in nodes.values() if node.get("input") in gate["fork_to"]]
        assert set(union["branches"]) == set(gate["fork_to"])
        assert set(union["branches"].values()) == {node["on_success"] for node in branch_nodes}
        assert union["input"] == next(iter(union["branches"].values()))
        assert union["on_success"] == "unioned_rows"
        assert union["timeout_seconds"] > 0
        assert "plugin" not in union
        assert "policy" not in union
        assert "merge" not in union

    def test_authoring_aids_publish_row_union_rules_and_exemplar(self) -> None:
        view, _snapshot = _trained_view()
        payload = build_planner_authoring_aids(view)

        assert payload["fork_row_union"]["set_pipeline_exemplar"] == planner_authoring_aids.fork_row_union_exemplar_args(view)
        rendered = " ".join(payload["fork_row_union"]["rules"])
        assert "require_all" in rendered
        assert "N-to-N" in rendered
        assert "first branch" in rendered

    def test_select_only_exemplars_cover_distinct_fixed_sink_contracts(self) -> None:
        """Every taught whitelist visibly preserves its downstream required fields."""
        trained_view, _trained_snapshot = _trained_view()
        fork_args = fork_coalesce_exemplar_args(trained_view)
        union_args = planner_authoring_aids.fork_row_union_exemplar_args(trained_view)
        assert fork_args is not None and union_args is not None

        sink_contracts: list[set[str]] = []
        for args in (fork_args, union_args):
            cleanup = next(node for node in args["nodes"] if node.get("plugin") == "field_mapper")
            sink_schema = args["outputs"][0]["options"]["schema"]
            assert sink_schema["mode"] == "fixed"
            required_fields = {field.partition(":")[0].strip() for field in sink_schema["fields"]}
            assert required_fields
            assert required_fields <= set(cleanup["options"]["mapping"].values())
            assert set(cleanup["options"]["mapping"]) <= set(cleanup["options"]["schema"]["guaranteed_fields"])
            sink_contracts.append(required_fields)

        assert sink_contracts[0] != sink_contracts[1]

    def test_alias_less_control_exemplar_validates_through_the_real_candidate_builder(self, tmp_path: Path) -> None:
        """The exact alias-less control exemplar bytes must build."""
        (tmp_path / "outputs").mkdir(exist_ok=True)
        view, snapshot = _direct_control_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content, view=view, snapshot=snapshot)

        candidate = build_set_pipeline_candidate(args, _empty_state(), context)

        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"alias-less control exemplar rejected: {rejection}"

    def test_recommended_alias_less_controls_do_not_mutate_the_exemplar(self, tmp_path: Path) -> None:
        view, _snapshot = _direct_control_view(tmp_path, control_mode="recommend")
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        plugins = {node.get("plugin") for node in args["nodes"]}
        assert "azure_prompt_shield" not in plugins
        assert "azure_content_safety" not in plugins

    def test_topology_exemplar_renders_and_validates_without_a_usable_llm_profile(self, tmp_path: Path) -> None:
        """No usable llm profile -> the SAME topology with non-LLM branches.

        Fork/gate/coalesce WIRING is pure topology, independent of LLMs — only
        the branch contents need a profile. Gating the whole section on an llm
        profile alias (as the module originally did) meant a deployment with no
        visible LLM profile lost the topology teaching entirely, which is
        exactly backwards. Branch transforms are drawn deterministically from
        the policy-visible catalog: ``passthrough`` when available, otherwise
        the first alphabetic candidate whose only required option is
        ``schema``. The same plugin/configuration on both branches guarantees
        the union coalesce does not mix runtime schema modes.
        """
        view, _snapshot = _trained_view()
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        _assert_operator_ruled_topology(args)
        nodes = args["nodes"]
        assert all(node.get("plugin") != "llm" for node in nodes)
        rendered = json.dumps(args)
        # Nothing llm-flavored may leak into the profile-less variant: no
        # profile alias, no interpretation review rows (llm-node concern).
        assert '"profile"' not in rendered
        assert "interpretation_requirements" not in rendered
        # Deterministic branch pick, recomputed here from the same posture:
        # first two alphabetically that are non-LLM, non-batch-aware, and
        # authorable with only the universal schema option.
        from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager

        batch_aware = {cls.name for cls in get_shared_plugin_manager().get_transforms() if cls.is_batch_aware}
        renderable = sorted(
            plugin.name
            for plugin in view.list_transforms()
            if plugin.name != "llm"
            and plugin.name not in batch_aware
            and {field.name for field in plugin.config_fields if field.required} <= {"schema"}
        )
        gate = next(node for node in nodes if node["node_type"] == "gate")
        branch_plugins = [node["plugin"] for node in nodes if node.get("input") in gate["fork_to"]]
        expected_plugin = "passthrough" if "passthrough" in renderable else renderable[0]
        assert branch_plugins == [expected_plugin, expected_plugin]
        # The exact bytes the prompt carries must build under this posture.
        (tmp_path / "outputs").mkdir(exist_ok=True)
        content = args["source"]["inline_blob"]["content"]
        context = _custody_context(tmp_path, content)
        candidate = build_set_pipeline_candidate(args, _empty_state(), context)
        rejection = None if candidate.acceptable else (candidate.result.data or {}).get("error")
        assert candidate.acceptable is True, f"topology exemplar rejected: {rejection}"

    def test_trained_posture_payload_carries_the_topology_exemplar(self) -> None:
        """The aids payload renders fork_coalesce even with no llm profile."""
        view, _snapshot = _trained_view()

        payload = build_planner_authoring_aids(view)

        section = payload["fork_coalesce"]
        assert section["set_pipeline_exemplar"] == fork_coalesce_exemplar_args(view)


class TestSelectedControlProfile:
    """Selection contract for the exemplar's required-control wiring."""

    def test_required_with_aliases_returns_plugin_and_alias(self, tmp_path: Path) -> None:
        from elspeth.contracts.plugin_capabilities import PluginCapability
        from elspeth.web.composer.planner_authoring_aids import _selected_control_profile

        view, _snapshot = _guardrail_profile_view(tmp_path)

        assert _selected_control_profile(view, PluginCapability.PROMPT_SHIELD) == (
            "aws_bedrock_prompt_shield",
            "prompt-approved",
        )
        assert _selected_control_profile(view, PluginCapability.CONTENT_SAFETY) == (
            "aws_bedrock_content_safety",
            "content-approved",
        )

    def test_required_without_aliases_returns_plugin_with_no_alias(self, tmp_path: Path) -> None:
        from elspeth.contracts.plugin_capabilities import PluginCapability
        from elspeth.web.composer.planner_authoring_aids import _selected_control_profile

        view, snapshot = _direct_control_view(tmp_path)
        # Precondition: the controls really are selected with zero aliases.
        aliases_by_plugin = dict(snapshot.usable_profile_aliases)
        for capability in (PluginCapability.PROMPT_SHIELD, PluginCapability.CONTENT_SAFETY):
            selected = dict(snapshot.selected)[capability]
            assert selected is not None
            assert not aliases_by_plugin.get(selected, ())

        assert _selected_control_profile(view, PluginCapability.PROMPT_SHIELD) == ("azure_prompt_shield", None)
        assert _selected_control_profile(view, PluginCapability.CONTENT_SAFETY) == ("azure_content_safety", None)

    def test_recommend_mode_returns_none_even_with_a_selection(self, tmp_path: Path) -> None:
        from elspeth.contracts.plugin_capabilities import PluginCapability
        from elspeth.web.composer.planner_authoring_aids import _selected_control_profile

        for view, _snapshot in (
            _guardrail_profile_view(tmp_path, control_mode="recommend"),
            _direct_control_view(tmp_path, control_mode="recommend"),
        ):
            assert _selected_control_profile(view, PluginCapability.PROMPT_SHIELD) is None
            assert _selected_control_profile(view, PluginCapability.CONTENT_SAFETY) is None


class TestExemplarDomainDisjointness:
    """Exemplars teach STRUCTURE, never a live acceptance test's solution.

    The fork/coalesce exemplar previously mirrored the live colour-A/B
    acceptance test (color_name/hex source fields, tone/usage branches,
    emotional-tone prompts) — effectively the test's answer key. When a live
    test's domain vocabulary appears in an exemplar, the test measures
    pack-lookup, not planner capability, and its pass signal is contaminated.
    These tests pin the neutral domains and the structural elements that must
    survive any future domain change.
    """

    # The live colour A/B acceptance vocabulary. json.dumps + lowercase scan.
    _COLOUR_ACCEPTANCE_TOKENS = ("color_name", "hex", "cerulean", "saffron", "colour", "color")

    def test_fork_coalesce_exemplar_is_disjoint_from_the_colour_acceptance_domain(self, tmp_path: Path) -> None:
        trained_view, _ = _trained_view()
        profile_view, _ = _profile_view(tmp_path)
        for view in (trained_view, profile_view):
            args = fork_coalesce_exemplar_args(view)
            assert args is not None
            rendered = json.dumps(args).lower()
            for token in self._COLOUR_ACCEPTANCE_TOKENS:
                assert token not in rendered, f"acceptance-domain token {token!r} in fork/coalesce exemplar"
            # The neutral domain is present and carried end to end: source
            # schema fields reappear in the cleanup mapping.
            assert "ticket_id" in rendered and "body" in rendered

    def test_source_custody_exemplar_is_domain_neutral(self) -> None:
        view, _snapshot = _trained_view()
        args = source_custody_exemplar_args(view)
        assert args is not None
        rendered = json.dumps(args).lower()
        for token in self._COLOUR_ACCEPTANCE_TOKENS:
            assert token not in rendered, f"acceptance-domain token {token!r} in custody exemplar"
        # The old quarterly-totals domain is retired with the rewrite.
        assert "quarterly" not in rendered
        assert "sku" in rendered

    def test_llm_branch_prompts_interpolate_the_row_namespace_over_guaranteed_fields(self, tmp_path: Path) -> None:
        """Prompts use {{ row.<field> }} and only name source-guaranteed fields.

        Candidate-level CI does not validate prompt interpolation (the run-19
        readiness rejection came from planners copying a {{ field }} prompt),
        so the exemplar's prompts are pinned here: every placeholder uses the
        row namespace and resolves to a field the source guarantees.
        """
        import re

        view, _snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        guaranteed = set(args["source"]["options"]["schema"]["guaranteed_fields"])
        llms = [node for node in args["nodes"] if node.get("plugin") == "llm"]
        assert len(llms) == 2
        for llm in llms:
            prompt = llm["options"]["prompt_template"]
            placeholders = re.findall(r"\{\{\s*([^}]+?)\s*\}\}", prompt)
            assert placeholders, "each branch prompt must interpolate the row"
            for placeholder in placeholders:
                assert placeholder.startswith("row."), f"placeholder {{{{ {placeholder} }}}} must use the row namespace"
                assert placeholder.removeprefix("row.") in guaranteed
            # Ownership doctrine (G3 + run-3 E6): backend-owned reviews are
            # never hand-authored. The sentiment branch authors no judgment
            # semantics and stages nothing; the urgency branch authors a
            # category rubric and stages exactly its own wired vague_term row.
            if llm["id"] == "assess_urgency":
                rows = llm["options"]["interpretation_requirements"]
                assert [row["kind"] for row in rows] == ["vague_term"]
            else:
                assert "interpretation_requirements" not in llm["options"]

    def test_payload_states_the_per_branch_shape_rule(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)

        payload = build_planner_authoring_aids(view)

        section = payload["fork_coalesce"]
        assert section["set_pipeline_exemplar"] == fork_coalesce_exemplar_args(view)
        rules = " ".join(section["rules"])
        assert "SHAPE SELECTION" in rules  # run-3 E1: selection rule, not preference
        assert "queries map (multi_query)" in rules  # run-3 E1: both shapes taught, selection by input variance


class TestDiscoveryDigest:
    def test_digest_carries_bounded_prohibited_plugin_facts_without_inventory_call(self) -> None:
        view, _snapshot = _trained_view()
        prohibited = PluginSummary(
            name="named_but_prohibited",
            description="Categorically unavailable on the web surface.",
            plugin_type="source",
            config_fields=[],
        )
        view.list_prohibited_sources = lambda: [prohibited]  # type: ignore[method-assign]

        digest = discovery_digest(view)

        assert digest["prohibited"]["sources"] == [
            {
                "name": "named_but_prohibited",
                "reason": "plugin_not_allowed_on_web",
                "explanation": (
                    "the plugin is installed and authorized for this deployment's runtime but prohibited on the "
                    "web authoring surface by security policy"
                ),
            }
        ]
        assert len(planner_authoring_aids.canonical_json(digest).encode("utf-8")) <= 24 * 1024

    def test_digest_covers_every_policy_visible_plugin(self) -> None:
        """DISCOVERY_CYCLE churn re-derives the catalog; the digest IS the catalog."""
        view, _snapshot = _trained_view()

        digest = discovery_digest(view)

        assert {entry["name"] for entry in digest["sources"]} == {plugin.name for plugin in view.list_sources()}
        assert {entry["name"] for entry in digest["transforms"]} == {plugin.name for plugin in view.list_transforms()}
        assert {entry["name"] for entry in digest["sinks"]} == {plugin.name for plugin in view.list_sinks()}

    def test_digest_entries_carry_bounded_selection_facts_without_contract_hints(self) -> None:
        view, _snapshot = _trained_view()

        digest = discovery_digest(view)

        csv_source = next(entry for entry in digest["sources"] if entry["name"] == "csv")
        assert csv_source["purpose"] == next(plugin.description for plugin in view.list_sources() if plugin.name == "csv")
        assert {"schema", "path", "on_validation_failure"} <= set(csv_source["required_options"])
        json_sink = next(entry for entry in digest["sinks"] if entry["name"] == "json")
        assert "composer_hints" not in json_sink
        assert json_sink["purpose"]

    def test_every_declared_prohibition_and_tag_set_reaches_the_digest_verbatim(self) -> None:
        """The always-present tier states what a plugin must NOT be used for.

        g11 (elspeth-afdf55a17c): a planner authored multiline LLM text into
        the ``text`` sink, which diverts any value bearing CR or LF. The sink
        had said so all along in ``usage_when_not_to_use`` — but only in a
        ``list_sinks`` result the digest guidance tells the planner it rarely
        needs. A selection rule carried solely by a tool the planner is
        steered away from is a rule it will usually not read.
        """
        view, _snapshot = _trained_view()

        digest = discovery_digest(view)

        for kind, plugins in (("sources", view.list_sources()), ("transforms", view.list_transforms()), ("sinks", view.list_sinks())):
            entries = {entry["name"]: entry for entry in digest[kind]}
            for plugin in plugins:
                entry = entries[plugin.name]
                if plugin.usage_when_not_to_use is None:
                    assert "not_for" not in entry
                else:
                    # Verbatim, never summarized: a shortened prohibition
                    # reads as a narrower rule than the one declared.
                    assert entry["not_for"] == plugin.usage_when_not_to_use
                assert entry.get("capability_tags", []) == list(plugin.capability_tags)

    def test_digest_fits_the_canonical_utf8_selection_budget(self) -> None:
        from elspeth.core.canonical import canonical_json

        view, _snapshot = _trained_view()

        rendered = canonical_json(discovery_digest(view)).encode("utf-8")

        assert len(rendered) <= 24 * 1024

    def test_digest_bounds_synthetic_public_prose_without_losing_plugin_identities(self) -> None:
        from elspeth.core.canonical import canonical_json

        view, _snapshot = _trained_view()
        summaries: dict[str, list[PluginSummary]] = {"source": [], "transform": [], "sink": []}
        for kind in summaries:
            summaries[kind] = [
                PluginSummary(
                    name=f"synthetic_{kind}_{index:02d}",
                    description=f"purpose-{index}-" + ("p" * 12_000),
                    plugin_type=kind,
                    config_fields=[],
                    usage_when_not_to_use=f"prohibition-{index}-" + ("n" * 12_000),
                    capability_tags=("synthetic",),
                )
                for index in range(12)
            ]

        digest = discovery_digest(view, summaries=summaries)

        assert len(canonical_json(digest).encode("utf-8")) <= 24 * 1024
        assert digest["budget"]["max_canonical_bytes"] == 24 * 1024
        assert digest["budget"]["canonical_bytes_used"] <= digest["budget"]["max_canonical_bytes"]
        assert digest["budget"]["omitted_public_text_count"] > 0
        for kind, entries in (("source", digest["sources"]), ("transform", digest["transforms"]), ("sink", digest["sinks"])):
            assert [entry["name"] for entry in entries] == [plugin.name for plugin in summaries[kind]]
            for entry in entries:
                assert entry["required_options"] == []
                assert "purpose" not in entry
                assert entry["purpose_omitted"]["sha256"]
                assert entry["purpose_omitted"]["details_via"] == f"list_{kind}s"
                assert "not_for" not in entry
                assert entry["not_for_omitted"]["sha256"]
                assert entry["not_for_omitted"]["details_via"] == f"list_{kind}s"

    def test_digest_omission_hash_is_literal_utf8_sha256(self) -> None:
        view, _snapshot = _trained_view()
        purpose = "literal-purpose-" + ("p" * 2_000)
        summaries = {
            "source": [PluginSummary(name="hashed", description=purpose, plugin_type="source", config_fields=[])],
            "transform": [],
            "sink": [],
        }

        entry = discovery_digest(view, summaries=summaries)["sources"][0]

        assert entry["purpose_omitted"]["sha256"] == hashlib.sha256(purpose.encode("utf-8")).hexdigest()

    def test_digest_budgeting_uses_bounded_envelope_serializations(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        view, _snapshot = _trained_view()
        summaries = {
            "source": [
                PluginSummary(
                    name=f"bounded_{index:02d}",
                    description=f"purpose-{index}-" + ("p" * 700),
                    plugin_type="source",
                    config_fields=[],
                    usage_when_not_to_use=f"prohibition-{index}-" + ("n" * 700),
                )
                for index in range(40)
            ],
            "transform": [],
            "sink": [],
        }
        original = planner_authoring_aids.canonical_json
        calls = 0

        def counted(value: object) -> str:
            nonlocal calls
            calls += 1
            return original(value)

        monkeypatch.setattr(planner_authoring_aids, "canonical_json", counted)

        digest = discovery_digest(view, summaries=summaries)

        assert len(original(digest).encode("utf-8")) <= 24 * 1024
        assert calls <= 8

    def test_digest_fails_closed_when_identity_facts_alone_exceed_budget(self) -> None:
        view, _snapshot = _trained_view()
        summaries = {
            "source": [
                PluginSummary(
                    name=f"identity_{index:04d}_" + ("x" * 96),
                    description="",
                    plugin_type="source",
                    config_fields=[],
                )
                for index in range(400)
            ],
            "transform": [],
            "sink": [],
        }

        with pytest.raises(RuntimeError, match="discovery_digest_budget_invariant"):
            discovery_digest(view, summaries=summaries)

    def test_three_selected_plugin_contracts_fit_the_canonical_utf8_budget(self) -> None:
        """The budget is read from the runtime constant, never restated here.

        A hard-coded ``48 * 1024`` has teeth in both directions: raise the
        constant to unblock the planner and this test still fails, so it reads
        as a broken fix; lower it and this test passes while the planner
        refuses real contracts. Importing it means this test tracks whatever
        the planner actually enforces.

        Currently RED and expected to stay red until the ``llm`` contract
        shrinks (elspeth-623c69c59f): the three contracts AGGREGATE to 50,365
        bytes against a 49,152-byte budget, so the planner refuses the third
        ``get_plugin_schema`` call with ``schema_contract_budget_exceeded``.
        No single contract exceeds the budget — ``llm`` alone is 39,095 bytes,
        79.5% of it — so this is an aggregate overflow, not one oversized
        contract. That red IS this test's signal, not a defect in it.

        Do NOT green it by raising ``_SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES``.
        That constant is a ratchet: loosening it silently buys room for the
        next option pair to overflow again, and it is the developer's call,
        not a lane's. Shrink the contract instead.
        """
        from elspeth.core.canonical import canonical_json
        from elspeth.web.composer.pipeline_planner import _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES

        view, _snapshot = _trained_view()
        contracts = [
            planner_authoring_aids.planner_plugin_contract(view.get_schema("transform", name)).to_dict()
            for name in ("web_scrape", "llm", "field_mapper")
        ]

        assert len(canonical_json(contracts).encode("utf-8")) <= _SELECTED_SCHEMA_CONTRACTS_BUDGET_BYTES

    def test_text_sink_digest_entry_states_the_multiline_prohibition(self) -> None:
        """The exact g11 regression anchor, held on the stable token only.

        Phase 1 of the fix plan extends this sentence with a ``line_explode``
        remedy, so pinning the whole string would pin prose that is expected
        to change. What must not regress is that the digest — not only a
        ``list_sinks`` result — tells the planner the sink refuses multiline.
        """
        view, _snapshot = _trained_view()

        text_sink = next(entry for entry in discovery_digest(view)["sinks"] if entry["name"] == "text")

        assert "multiline" in text_sink["not_for"]

    def test_digest_carries_no_worked_example_and_says_so(self) -> None:
        """``example_use`` is YAML and unvalidated; the omission is disclosed.

        Every worked shape this module renders is run through the real
        candidate builder by these tests, so a drifting exemplar fails CI.
        ``example_use`` has no such gate and is written in YAML, while this
        surface authors through ``set_pipeline``. Carrying it in the
        always-present tier would teach an unverified shape in the wrong
        language — so it is omitted, and the guidance states the omission
        instead of leaving the planner to infer the digest is complete.
        """
        view, _snapshot = _trained_view()

        digest = discovery_digest(view)

        assert all("example_use" not in entry for kind in ("sources", "transforms", "sinks") for entry in digest[kind])
        assert "carry no worked example" in planner_authoring_aids._DISCOVERY_DIGEST_GUIDANCE

    def test_prohibition_is_absent_rather_than_null_when_a_plugin_declares_none(self) -> None:
        """A third-party plugin may omit reference content; nulls are not carried."""
        summary = PluginSummary(
            name="third_party",
            description="External plugin with no reference content.",
            plugin_type="sink",
            config_fields=[],
        )

        entry = planner_authoring_aids._digest_entries([summary])[0]

        assert "not_for" not in entry
        assert "capability_tags" not in entry
        assert entry["name"] == "third_party"

    def test_capability_core_discovery_order_speaks_with_the_digest_voice(self) -> None:
        """The static core and the live digest guidance must agree, zero daylight.

        The campaign's central finding: in-context-every-turn contradictions
        dominate planner behavior. The core's discovery-order steps and the
        digest's short-circuit guidance ride in the same context every turn,
        so where they overlap they must share exact sentences — a core that
        says "read the inventories with list_*" every turn undercuts the
        digest on the DISCOVERY_CYCLE failure mode it exists to fix.
        """
        from elspeth.web.composer.capability_skill import load_pipeline_capability_core
        from elspeth.web.composer.planner_authoring_aids import _DISCOVERY_DIGEST_GUIDANCE

        # Word-for-word agreement is about words: collapse the markdown's
        # hard line wrapping before comparing.
        core = load_pipeline_capability_core()
        core_flat = " ".join(core.split())

        # The old unconditional re-discovery instruction must be gone.
        assert "Read the policy-visible inventories" not in core_flat
        assert "Read every selected plugin's authoritative options" not in core_flat
        # Digest-first scoping, shared word-for-word between both texts.
        shared_phrases = (
            "rendered from the live policy-visible catalog at prompt build and is current for this deployment",
            "plan directly from it",
            "for structured repair when a proposal is rejected",
        )
        for phrase in shared_phrases:
            assert phrase in core_flat, f"core lacks shared phrase: {phrase!r}"
            assert phrase in _DISCOVERY_DIGEST_GUIDANCE, f"digest guidance lacks shared phrase: {phrase!r}"
        # The narrowing must not disturb the closed provenance rules the core
        # keeps: model ids from the request-supplied catalog or list_models
        # and nothing else, secret discovery intact. The provenance set is
        # closed either way — a supplied catalog is the same policy-projected
        # fact list_models returns, so pre-supplying it removes a discovery
        # turn without widening where an identifier may come from.
        assert (
            "Model identifiers come from the supplied model catalog where the request provides one, and otherwise only from `list_models`"
        ) in core_flat
        assert "list_secret_refs" in core

    def test_payload_carries_digest_with_discovery_short_circuit_guidance(self) -> None:
        view, _snapshot = _trained_view()

        payload = build_planner_authoring_aids(view)

        section = payload["discovery_digest"]
        assert section["plugins"] == discovery_digest(view)
        guidance = section["guidance"]
        assert "rarely need" in guidance
        assert "get_plugin_schema" in guidance
        assert "current" in guidance
        assert "omitted_public_text_count" in guidance
        assert "details_via" in guidance
        # The digest short-circuits catalog re-discovery only: model ids still
        # come from a served catalog and nothing else, and structured-repair
        # tooling (get_plugin_assistance) keeps its role.
        assert "list_models" in guidance
        assert "get_plugin_assistance" in guidance


def _llmless_view() -> tuple[PolicyCatalogView, PluginAvailabilitySnapshot, Any]:
    """Trained catalog with both llm surfaces closed by policy.

    Returns the profile registry too: this helper can only prove the registry
    was untouched by its OWN probe, and the build under test runs later. Each
    consuming test re-asserts ``mock_calls == []`` AFTER
    ``build_planner_authoring_aids``, which is where a registry touch would
    actually put a mock repr into a digest entry.
    """
    from unittest.mock import MagicMock

    from elspeth.web.plugin_policy.models import PluginId
    from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

    catalog = create_catalog_service()
    trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
    snapshot = PluginAvailabilitySnapshot.create(
        policy_hash="model-catalog-llmless",
        principal_scope="local:model-catalog",
        available=frozenset(plugin_id for plugin_id in trained.available if plugin_id.name != "llm"),
        unavailable=(),
        selected=(),
        usable_profile_aliases=(),
        selected_profile_aliases=(),
        binding_generation_fingerprint="model-catalog-llmless-generation",
    )
    assert PluginId("transform", "llm") not in snapshot.available
    profiles = MagicMock(spec=OperatorProfileRegistry)
    view = PolicyCatalogView(catalog, snapshot, profiles)
    # No plugin has a usable alias here, so the projection must short-circuit
    # before the registry: a MagicMock summary reaching the digest would render
    # a mock repr as a plugin purpose and still leave the grammar assertions
    # green. Prove the view is real, not merely non-crashing.
    assert [plugin.name for plugin in view.list_transforms()] == [
        plugin.name for plugin in catalog.list_transforms() if PluginId("transform", plugin.name) in snapshot.available
    ]
    assert profiles.mock_calls == []
    return view, snapshot, profiles


class TestModelCatalogAid:
    """F3: every llm-node intent bought a deployment-static catalog with a turn.

    ``list_models`` is two-mode — unfiltered it returns provider names and
    counts plus a hint to call again with a provider, so an author that did not
    already know the provider paid TWO turns for a catalog that is fixed for
    the process. The aid restates both modes from the same accessors, so the
    tests below are parity tests against the real tool rather than restatements
    of its filter rule.
    """

    def test_provider_summary_matches_the_models_listing_unfiltered_mode(self) -> None:
        view, snapshot = _trained_view()

        catalog = planner_model_catalog()
        result = _dispatch_tool("list_models", {}, _empty_state(), view, plugin_snapshot=snapshot)

        assert result.success is True, result.to_dict()
        assert catalog["provider_model_counts"] == result.data["providers"]
        assert catalog["total_models"] == result.data["total_models"]

    def test_every_carried_list_matches_the_models_listing_filtered_mode(self) -> None:
        """The identifiers are the tool's own, in the tool's own form.

        A trailing-slash provider argument is an exact segment match in the
        tool, which is the grouping the unfiltered mode reports counts for —
        so each carried list must equal the tool's answer for its provider,
        OpenRouter's live-catalog substitution included.
        """
        view, snapshot = _trained_view()

        catalog = planner_model_catalog()

        assert catalog["models_by_provider"]
        for provider, identifiers in catalog["models_by_provider"].items():
            result = _dispatch_tool(
                "list_models",
                {"provider": f"{provider}/", "limit": 10_000},
                _empty_state(),
                view,
                plugin_snapshot=snapshot,
            )
            assert result.success is True, result.to_dict()
            assert result.data["truncated"] is False
            # The tool result is deep-frozen on the way out; only the sequence
            # type differs from the aid's mutable list.
            assert identifiers == list(result.data["models"]), provider
            assert len(identifiers) == catalog["provider_model_counts"][provider]

    def test_carried_providers_are_the_live_llm_provider_vocabulary(self) -> None:
        """The carried set is derived from the two live ``provider`` literals.

        The subset direction alone is not enough. It permits an authorable
        provider with a non-zero count to be missing from BOTH
        ``models_by_provider`` and ``models_omitted`` while
        ``authorable_providers`` still names it — identifiers that vanish with
        no disclosure and no marker to follow, which is exactly the silent
        under-supply the aid exists to prevent. The positive direction is
        pinned below: every authorable provider the catalog knows models for is
        either carried or explicitly deferred.
        """
        from elspeth.plugins.sources.llm.config import LLMSourceConfig
        from elspeth.plugins.transforms.llm.base import LLMConfig

        catalog = planner_model_catalog()

        declared = {*get_args(LLMConfig.model_fields["provider"].annotation)} | {
            *get_args(LLMSourceConfig.model_fields["provider"].annotation)
        }
        assert catalog["authorable_providers"] == sorted(declared)
        assert set(catalog["models_by_provider"]) <= declared
        for provider in catalog["authorable_providers"]:
            if provider in catalog["provider_model_counts"] and catalog["provider_model_counts"][provider]:
                assert provider in catalog["models_by_provider"] or any(
                    omission["provider"] == provider for omission in catalog["models_omitted"]
                ), provider

    def test_identifiers_are_not_carried_for_providers_an_llm_node_cannot_name(self) -> None:
        """Counts stay complete; only the unspendable identifier lists drop out."""
        catalog = planner_model_catalog()

        unauthorable = set(catalog["provider_model_counts"]) - set(catalog["authorable_providers"])
        assert unauthorable, "the litellm catalog should know providers an llm node cannot name"
        assert not unauthorable & set(catalog["models_by_provider"])
        assert all(catalog["provider_model_counts"][provider] > 0 for provider in unauthorable)

    def test_an_authorable_provider_the_catalog_knows_nothing_about_carries_no_key(self) -> None:
        """An empty list would claim the deployment serves no model for it."""
        catalog = planner_model_catalog()

        for provider in catalog["authorable_providers"]:
            if provider not in catalog["provider_model_counts"]:
                assert provider not in catalog["models_by_provider"]
        assert all(identifiers for identifiers in catalog["models_by_provider"].values())

    def test_catalog_fits_its_canonical_utf8_budget(self) -> None:
        from elspeth.core.canonical import canonical_json

        catalog = planner_model_catalog()

        rendered = canonical_json(catalog).encode("utf-8")
        assert len(rendered) <= 32 * 1024
        assert catalog["budget"]["canonical_bytes_used"] == len(rendered)
        assert catalog["budget"]["max_canonical_bytes"] == 32 * 1024
        assert catalog["models_omitted"] == []
        assert catalog["budget"]["omitted_provider_count"] == 0

    def test_over_budget_defers_whole_lists_and_keeps_the_counts(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Whole lists or none: a sliced list reads as a complete one."""
        from elspeth.core.canonical import canonical_json

        full = planner_model_catalog()
        monkeypatch.setattr(planner_authoring_aids, "_MODEL_CATALOG_MAX_CANONICAL_BYTES", 4 * 1024)

        catalog = planner_model_catalog()

        assert catalog["models_by_provider"] == {}
        assert catalog["provider_model_counts"] == full["provider_model_counts"]
        assert catalog["total_models"] == full["total_models"]
        assert catalog["models_omitted"] == [
            {"provider": provider, "model_count": len(identifiers), "details_via": "list_models"}
            for provider, identifiers in sorted(full["models_by_provider"].items())
        ]
        assert catalog["budget"]["omitted_provider_count"] == len(full["models_by_provider"])
        assert len(canonical_json(catalog).encode("utf-8")) <= 4 * 1024

    def test_catalog_fails_closed_when_the_counts_alone_exceed_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """The raise is the SECOND gate, reached only after deferral still failed."""
        settled: list[dict[str, Any]] = []
        measure = planner_authoring_aids._model_catalog_size

        def spy(catalog: Any) -> int:
            settled.append(json.loads(json.dumps(catalog)))
            return measure(catalog)

        monkeypatch.setattr(planner_authoring_aids, "_model_catalog_size", spy)
        monkeypatch.setattr(planner_authoring_aids, "_MODEL_CATALOG_MAX_CANONICAL_BYTES", 64)

        with pytest.raises(RuntimeError, match=r"model_catalog_budget_invariant"):
            planner_model_catalog()

        # A no-op deferral arm would raise here too, and this is what tells the
        # two apart: the lists were dropped and disclosed before the raise.
        assert settled[-1]["models_by_provider"] == {}
        assert settled[-1]["models_omitted"]
        assert settled[0]["models_by_provider"]

    def test_payload_carries_the_catalog_only_where_an_llm_surface_is_visible(self) -> None:
        view, _snapshot = _trained_view()
        llmless, _llmless_snapshot, profiles = _llmless_view()

        assert build_planner_authoring_aids(view)["model_catalog"]["catalog"] == planner_model_catalog()
        assert "model_catalog" not in build_planner_authoring_aids(llmless)
        # After the build, not only after the helper's own probe: a registry
        # touch during the sweep is what would put a mock repr in a digest.
        assert profiles.mock_calls == []

    def test_guidance_names_the_closed_provenance_and_the_deferral_marker(self) -> None:
        view, _snapshot = _trained_view()

        guidance = build_planner_authoring_aids(view)["model_catalog"]["guidance"]

        assert "no discovery call" in guidance
        assert "authorable_providers" in guidance
        assert "models_omitted" in guidance
        assert "details_via" in guidance
        assert "Never invent a slug" in guidance

    def test_a_profile_bound_slug_reaches_the_prompt_only_as_public_inventory(self, tmp_path: Path) -> None:
        """The operator's binding stays private; the public catalog stays public.

        ``_source_only_profile_view`` binds ``anthropic/claude-sonnet-4.6``
        privately through the ``sonnet`` alias, and the public profile
        projection deliberately hides ``model``. That slug is also an ordinary
        member of OpenRouter's public catalog, which ``list_models`` already
        returns in full to this same planner — so carrying the catalog
        discloses nothing about which member the operator chose. What proves it
        is that the carried list is the public reader's whole content, never a
        deployment-selected subset.
        """
        from elspeth.contracts.value_source import get_catalog_values
        from elspeth.plugins.transforms.llm.model_catalog import MODEL_CATALOG_OPENROUTER

        view, _snapshot = _source_only_profile_view(tmp_path)

        aids = build_planner_authoring_aids(view)
        carried = aids["model_catalog"]["catalog"]["models_by_provider"]["openrouter"]

        # The profile view's catalog is the profile-free one, key for key: a
        # leak that landed under some OTHER provider key could never reach the
        # prompt either, whatever it was.
        assert aids["model_catalog"]["catalog"] == planner_model_catalog()
        assert carried == sorted(get_catalog_values(MODEL_CATALOG_OPENROUTER))
        assert "anthropic/claude-sonnet-4.6" in carried
        without_public_inventory = json.loads(json.dumps(aids))
        without_public_inventory["model_catalog"]["catalog"]["models_by_provider"] = {}
        assert "anthropic/claude-sonnet-4.6" not in json.dumps(without_public_inventory)
        assert "OPENROUTER_API_KEY" not in json.dumps(aids)

    def test_memo_keys_on_the_model_catalog_identity_not_the_snapshot_alone(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """A catalog primed after the first build must not serve the old aids.

        The OpenRouter catalog is a process-global the boot lifespan primes,
        and no plugin-policy snapshot covers it — so a memo keyed on
        ``snapshot_hash`` alone would pin the pre-prime catalog for the
        process lifetime. Reverting the key to the snapshot hash makes the
        second build below return the first build's payload.
        """
        builds: list[str] = []
        identity = ["bundled-sha", "bundled"]

        def build(_catalog: object) -> dict[str, str]:
            builds.append(identity[0])
            return {"purpose": identity[0]}

        monkeypatch.setattr(planner_authoring_aids, "_build_planner_authoring_aids", build)
        monkeypatch.setattr(
            planner_authoring_aids,
            "read_openrouter_catalog_snapshot_id",
            lambda: (identity[0], identity[1]),
        )
        catalog: Any = SimpleNamespace(snapshot=SimpleNamespace(snapshot_hash="one-policy-snapshot"))

        first = planner_authoring_aids.build_planner_authoring_aids(catalog)
        repeat = planner_authoring_aids.build_planner_authoring_aids(catalog)
        identity[0], identity[1] = "live-sha", "live"
        after_prime = planner_authoring_aids.build_planner_authoring_aids(catalog)

        assert first == repeat == {"purpose": "bundled-sha"}
        assert after_prime == {"purpose": "live-sha"}
        assert builds == ["bundled-sha", "live-sha"]
        assert sorted(planner_authoring_aids._AIDS_MEMO) == [
            "one-policy-snapshot:bundled:bundled-sha",
            "one-policy-snapshot:live:live-sha",
        ]


class TestExpressionGrammarAid:
    """F5: a deployment-static public reference cost one turn per gate shape."""

    def test_grammar_is_the_tools_own_reference_verbatim(self) -> None:
        from elspeth.web.composer.tools.generation import get_expression_grammar

        view, _snapshot = _trained_view()

        section = build_planner_authoring_aids(view)["expression_grammar"]

        assert section["grammar"] == get_expression_grammar()
        assert "grammar_omitted" not in section

    def test_grammar_is_carried_on_every_deployment(self) -> None:
        """Gate conditions are structural, so no plugin visibility gates it."""
        llmless, _snapshot, profiles = _llmless_view()

        assert build_planner_authoring_aids(llmless)["expression_grammar"] == planner_expression_grammar()
        # After the build: a registry touch during the sweep would put a mock
        # repr into a digest entry and leave this assertion the only witness.
        assert profiles.mock_calls == []

    def test_grammar_fits_its_canonical_utf8_budget(self) -> None:
        from elspeth.core.canonical import canonical_json

        section = planner_expression_grammar()

        rendered = canonical_json(section).encode("utf-8")
        assert len(rendered) <= 8 * 1024
        assert section["budget"]["canonical_bytes_used"] == len(rendered)
        assert section["budget"]["max_canonical_bytes"] == 8 * 1024

    def test_over_budget_defers_the_whole_reference_with_its_hash(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from elspeth.core.canonical import canonical_json
        from elspeth.web.composer.tools.generation import get_expression_grammar

        monkeypatch.setattr(planner_authoring_aids, "_EXPRESSION_GRAMMAR_MAX_CANONICAL_BYTES", 2 * 1024)

        section = planner_expression_grammar()

        assert "grammar" not in section
        assert section["grammar_omitted"] == {
            "sha256": hashlib.sha256(get_expression_grammar().encode("utf-8")).hexdigest(),
            "details_via": "get_expression_grammar",
        }
        assert len(canonical_json(section).encode("utf-8")) <= 2 * 1024

    def test_grammar_fails_closed_when_the_marker_alone_exceeds_budget(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(planner_authoring_aids, "_EXPRESSION_GRAMMAR_MAX_CANONICAL_BYTES", 32)

        with pytest.raises(RuntimeError, match=r"expression_grammar_budget_invariant"):
            planner_expression_grammar()

    def test_guidance_names_the_static_reference_and_its_deferral_marker(self) -> None:
        view, _snapshot = _trained_view()

        guidance = build_planner_authoring_aids(view)["expression_grammar"]["guidance"]

        assert "no discovery call" in guidance
        assert "grammar_omitted" in guidance
        assert "details_via" in guidance


class TestAuthoringAidsPayload:
    def test_payload_carries_the_custody_exemplar_and_the_closed_provenance_rule(self) -> None:
        view, _snapshot = _trained_view()

        payload = build_planner_authoring_aids(view)

        custody = payload["source_custody"]
        assert custody["set_pipeline_exemplar_inline_blob"] == source_custody_exemplar_args(view)
        blob_variant = source_custody_exemplar_args(view, blob_id=PLACEHOLDER_BLOB_ID)
        assert blob_variant is not None
        assert custody["existing_blob_source_binding"] == blob_variant["source"]
        rules = " ".join(custody["rules"])
        assert "ONLY from blob-tool output" in rules
        assert "inline_blob" in rules
        assert "Never fabricate" in rules

    def test_payload_never_carries_a_fabricated_blob_identifier(self) -> None:
        """The prompt teaches provenance; it must not model a fake UUID itself."""
        view, _snapshot = _trained_view()

        rendered = json.dumps(build_planner_authoring_aids(view))

        assert PLACEHOLDER_BLOB_ID in rendered
        assert "-4000-" not in rendered  # no synthetic UUID4-shaped identifiers
        assert rendered.count("blob_id") >= 1

    def test_payload_is_canonical_json_compatible(self) -> None:
        from elspeth.core.canonical import canonical_json

        view, _snapshot = _trained_view()

        canonical_json(build_planner_authoring_aids(view))


class TestPromptShieldRules:
    """The aids teach shield-staging with the registered constants verbatim.

    Tutorial finalizer battery (dim_c under-flag): the replan planner
    non-deterministically omitted the ADVISORY prompt_injection_shield
    review row on the scrape→summarize llm node. The omission carries no
    rejection code (the shield review is advisory — warnings only, excluded
    from the blocking contract), so the authoring aids are the ONLY teaching
    lever. Following the 52322ebe1 discipline, the rule must quote the
    registered user_term and the deployment-honest draft constant verbatim —
    imported constants, so the aids can never drift from the contract.
    """

    def test_selected_recommended_shield_stays_an_advisory(self) -> None:
        from elspeth.contracts.plugin_capabilities import PluginCapability
        from elspeth.web.interpretation_state import PROMPT_SHIELD_AVAILABLE_DRAFT

        view, snapshot = _trained_view()
        selected = dict(snapshot.selected).get(PluginCapability.PROMPT_SHIELD)
        assert selected is not None

        rendered = "\n".join(build_planner_authoring_aids(view)["prompt_shield"]["rules"])
        assert selected.name in rendered
        assert PROMPT_SHIELD_AVAILABLE_DRAFT in rendered
        assert "required, not advisory" not in rendered

    def test_shieldless_view_quotes_the_warning_draft_verbatim(self) -> None:
        from unittest.mock import MagicMock

        from elspeth.contracts.plugin_capabilities import PluginCapability
        from elspeth.web.interpretation_state import (
            PROMPT_SHIELD_AVAILABLE_DRAFT,
            PROMPT_SHIELD_USER_TERM,
            PROMPT_SHIELD_WARNING_DRAFT,
        )
        from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry

        catalog = create_catalog_service()
        trained = PluginAvailabilitySnapshot.for_trained_operator(catalog)
        shieldless = PluginAvailabilitySnapshot.create(
            policy_hash="prompt-shield-rules-shieldless",
            principal_scope="local:prompt-shield-rules",
            available=trained.available,
            unavailable=(),
            selected=tuple(
                (capability, plugin)
                for capability, plugin in dict(trained.selected).items()
                if capability is not PluginCapability.PROMPT_SHIELD
            ),
            usable_profile_aliases=(),
            selected_profile_aliases=(),
            binding_generation_fingerprint="prompt-shield-rules-generation",
        )
        view = PolicyCatalogView(catalog, shieldless, MagicMock(spec=OperatorProfileRegistry))

        aids = build_planner_authoring_aids(view)
        rules = aids["prompt_shield"]["rules"]
        rendered = "\n".join(rules)
        assert PROMPT_SHIELD_USER_TERM in rendered
        assert PROMPT_SHIELD_WARNING_DRAFT in rendered
        assert PROMPT_SHIELD_AVAILABLE_DRAFT not in rendered

    def test_rules_name_the_untrusted_producer_and_the_llm_attachment_point(self) -> None:
        """Both regimes name the untrusted producer and the llm attachment point.

        The shielded regime attaches a wired transform between them; the
        shieldless regime attaches the review row to the llm node's
        ``interpretation_requirements``.
        """
        view, _snapshot = _trained_view()

        rendered = "\n".join(build_planner_authoring_aids(view)["prompt_shield"]["rules"])
        assert "web_scrape" in rendered
        assert "llm" in rendered
        # Shielded deployment: the attachment point is the wiring, not a card.
        # Producer-neutral wording — "the fetch step" misdescribed a document
        # extraction producer once aws_textract_document_analysis joined the
        # untrusted set.
        assert "between that producer node and" in rendered

        from elspeth.web.composer.planner_authoring_aids import _prompt_shield_rules

        shieldless = "\n".join(_prompt_shield_rules(shield_plugin=None, untrusted_producers=("web_scrape",)))
        assert "web_scrape" in shieldless
        assert "llm" in shieldless
        assert "interpretation_requirements" in shieldless

    def test_rules_teach_every_untrusted_producer_in_the_contract_set(self) -> None:
        """The taught producers come from the contract set, so a new one is taught automatically.

        The aids intersect the contract's untrusted-producer set with the
        policy-visible transforms, so membership is the only thing that decides
        whether a producer is taught. Document extraction is in that set:
        Textract returns whatever text the uploaded document carried.
        """
        from elspeth.web.composer.planner_authoring_aids import _prompt_shield_rules
        from elspeth.web.interpretation_state import _UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS

        assert "aws_textract_document_analysis" in _UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS

        rendered = "\n".join(
            _prompt_shield_rules(
                shield_plugin="aws_bedrock_prompt_shield",
                untrusted_producers=tuple(sorted(_UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS)),
            )
        )
        for producer in _UNTRUSTED_REMOTE_CONTENT_PRODUCER_PLUGINS:
            assert producer in rendered

    def test_textract_is_shielded_but_never_taught_web_scrape_cleanup(self) -> None:
        """Document text is untrusted input, not raw HTML with a fingerprint field."""
        view, _snapshot = _trained_view()

        aids = build_planner_authoring_aids(view)
        shield_rules = "\n".join(aids["prompt_shield"]["rules"])
        cleanup_rules = "\n".join(aids["raw_html_cleanup"]["rules"])

        assert "aws_textract_document_analysis" in shield_rules
        assert "web_scrape" in cleanup_rules
        assert "aws_textract_document_analysis" not in cleanup_rules

    def test_recommend_mode_draft_names_the_selected_shield_not_azure(self) -> None:
        """A selected non-Azure shield's audit card must not claim azure_prompt_shield.

        ``PROMPT_SHIELD_AVAILABLE_DRAFT`` used to hardcode "azure_prompt_shield"
        in its text, and the planner is told to copy that draft verbatim into
        the authored review row — so a deployment that selected a different
        shield implementation got a review card recording the wrong control.
        """
        from elspeth.web.composer.planner_authoring_aids import _prompt_shield_rules

        rendered = "\n".join(_prompt_shield_rules(shield_plugin="aws_bedrock_prompt_shield", untrusted_producers=("web_scrape",)))
        assert "azure_prompt_shield" not in rendered
        assert "aws_bedrock_prompt_shield" in rendered

    def test_section_renders_under_the_live_profile_posture(self, tmp_path: Path) -> None:
        # The failing surface is the tutorial/guided walk under the operator-
        # profile posture — pin that the section actually reaches it (web_scrape
        # and llm are policy-visible there), not just the trained fixture.
        from elspeth.web.interpretation_state import PROMPT_SHIELD_USER_TERM

        view, _snapshot = _profile_view(tmp_path)

        rendered = "\n".join(build_planner_authoring_aids(view)["prompt_shield"]["rules"])
        assert PROMPT_SHIELD_USER_TERM in rendered


class TestRequiredModeAutoWireAids:
    """R2-F10: REQUIRED mode is auto-wired server-side — the aids teach the
    guarantee (and drop the shield-recommendation row) instead of demanding
    manual wiring. RECOMMEND mode keeps the advisory regime, pinned by the
    existing advisory tests."""

    def test_required_shield_rules_teach_auto_wiring_not_manual_wiring(self, tmp_path: Path) -> None:
        from elspeth.web.interpretation_state import (
            PROMPT_SHIELD_USER_TERM,
            REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
        )

        view, _snapshot = _guardrail_profile_view(tmp_path)

        rendered = "\n".join(build_planner_authoring_aids(view)["prompt_shield"]["rules"])
        assert "automatically splices" in rendered
        assert REQUIRED_CONTROL_AUTO_WIRED_USER_TERM in rendered
        assert "aws_bedrock_prompt_shield" in rendered
        # The manual mandate is retired; the drop-the-recommendation-row rule stays.
        assert "WIRE a" not in rendered
        assert f"Do NOT stage the {PROMPT_SHIELD_USER_TERM} review row" in rendered

    def test_required_content_safety_rules_teach_auto_wiring_and_keep_on_error_discipline(self, tmp_path: Path) -> None:
        from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM

        view, _snapshot = _guardrail_profile_view(tmp_path)

        rendered = "\n".join(build_planner_authoring_aids(view)["content_safety"]["rules"])
        assert "automatically splices" in rendered
        assert REQUIRED_CONTROL_AUTO_WIRED_USER_TERM in rendered
        assert "WIRE a" not in rendered
        # Auto-wiring cannot repair an error route — the on_error discipline stays taught.
        assert "on_error" in rendered
        assert "'discard'" in rendered
        assert "operator decision" in rendered

    def test_placeholder_dependent_direct_required_posture_keeps_the_manual_wiring_mandate(self, tmp_path: Path) -> None:
        """Review finding 1 counterpart: an alias-less selection whose required
        bindings only exist as placeholder exemplars is NOT auto-wired, so the
        aids must keep the manual WIRE mandate and never claim the guarantee."""
        view, _snapshot = _direct_control_view(tmp_path)

        aids = build_planner_authoring_aids(view)
        shield_rules = "\n".join(aids["prompt_shield"]["rules"])
        safety_rules = "\n".join(aids["content_safety"]["rules"])
        for rendered in (shield_rules, safety_rules):
            assert "automatically splices" not in rendered
            assert "WIRE a" in rendered
            assert "required, not advisory" in rendered

    def test_review_registry_forbids_hand_authoring_the_auto_wire_disclosure(self) -> None:
        """Minor 2: the disclosure row is server-staged only — a planner-authored
        row would forge a policy_required audit entry."""
        from elspeth.web.interpretation_state import REQUIRED_CONTROL_AUTO_WIRED_USER_TERM

        view, _snapshot = _trained_view()

        rules = "\n".join(build_planner_authoring_aids(view)["review_registry"]["rules"])
        assert "NEVER author a pipeline_decision row" in rules
        assert REQUIRED_CONTROL_AUTO_WIRED_USER_TERM in rules
        assert "forges" in rules

    def test_required_but_unselected_shield_keeps_the_warning_advisory(self, tmp_path: Path) -> None:
        """REQUIRED with no selected implementation is the operator-problem
        posture: nothing can be auto-wired, so the aids must not claim it."""
        from elspeth.web.composer.planner_authoring_aids import _prompt_shield_rules
        from elspeth.web.interpretation_state import PROMPT_SHIELD_WARNING_DRAFT

        rendered = "\n".join(
            _prompt_shield_rules(
                shield_plugin=None,
                shield_required=True,
                untrusted_producers=("web_scrape",),
            )
        )
        assert "automatically splices" not in rendered
        assert PROMPT_SHIELD_WARNING_DRAFT in rendered


class TestModelCustody:
    """Suite run 1 G2 (8/8): never-invent had no sanctioned alternative."""

    def test_model_custody_renders_the_live_profile_alias(self, tmp_path: Path) -> None:
        from elspeth.web.composer.planner_authoring_aids import _usable_llm_profile_alias

        view, _snapshot = _profile_view(tmp_path)
        alias = _usable_llm_profile_alias(view)
        assert alias is not None

        rules = " ".join(build_planner_authoring_aids(view)["model_custody"]["rules"])
        assert f"'{alias}'" in rules
        assert "options.profile" in rules
        assert "llm_model_choice" in rules
        assert "list_models" in rules

    def test_model_custody_without_a_profile_teaches_discovery_only(self) -> None:
        # The trained fixture serves no llm operator profile: the section must
        # still render and steer to list_models rather than invented slugs.
        view, _snapshot = _trained_view()

        rules = " ".join(build_planner_authoring_aids(view)["model_custody"]["rules"])
        assert "No llm operator profile" in rules
        assert "list_models" in rules

    def test_llm_output_contract_blesses_one_string_and_multi_query_only(self) -> None:
        view, _snapshot = _trained_view()

        rules = " ".join(build_planner_authoring_aids(view)["llm_output_contract"]["rules"])
        assert "response_field" in rules
        assert "llm_response" in rules
        assert "multi_query" in rules
        assert "does NOT create row fields" in rules


class TestLlmSourceGenerationAid:
    def test_source_only_generation_uses_llm_source_without_transform_advice(self, tmp_path: Path) -> None:
        view, _snapshot = _source_only_profile_view(tmp_path)

        aids = build_planner_authoring_aids(view)
        rendered = " ".join(aids["llm_source_generation"]["rules"])

        assert "source:llm" in rendered
        assert "generation-first" in rendered
        assert "transform:llm" not in rendered
        assert "seed" in rendered
        assert "exactly one" in rendered
        assert "incoming row" in rendered

    def test_source_generation_names_output_and_value_source_contract(self, tmp_path: Path) -> None:
        view, _snapshot = _source_only_profile_view(tmp_path)

        rendered = " ".join(build_planner_authoring_aids(view)["llm_source_generation"]["rules"])

        assert "response_field" in rendered
        assert "_usage" in rendered
        assert "_model" in rendered
        assert "lookup" in rendered
        assert "{{ row" in rendered
        assert "sonnet" in rendered
        assert "provider/model/credential" in rendered

    def test_source_generation_marks_model_catalog_as_refreshable_session_snapshot(self, tmp_path: Path) -> None:
        view, _snapshot = _source_only_profile_view(tmp_path)

        aids = build_planner_authoring_aids(view)
        rendered = " ".join(aids["llm_source_generation"]["rules"])
        guidance = aids["discovery_digest"]["guidance"]

        assert "list_models" in rendered
        assert "session snapshot" in rendered
        assert "stale" in rendered
        assert "list_models" in guidance
        assert "session snapshot" in guidance
        assert "stale" in guidance
        # The operator's profile binds a concrete model privately and the
        # public profile projection hides it. That slug is separately an
        # ordinary member of OpenRouter's public catalog, which the served
        # model_catalog section carries whole — so the canary blanks the public
        # identifier lists and still holds over every other section, including
        # that catalog's own counts, guidance and omission markers. The
        # discrimination itself is pinned by
        # TestModelCatalogAid.test_a_profile_bound_slug_reaches_the_prompt_only_as_public_inventory.
        without_public_inventory = json.loads(json.dumps(aids))
        without_public_inventory["model_catalog"]["catalog"]["models_by_provider"] = {}
        assert "anthropic/claude-sonnet-4.6" not in json.dumps(without_public_inventory)

    def test_source_generation_requires_discard_when_content_safety_is_required(self, tmp_path: Path) -> None:
        view, _snapshot = _source_only_profile_view(tmp_path, content_safety_required=True)

        rendered = " ".join(build_planner_authoring_aids(view)["llm_source_generation"]["rules"])

        assert "on_validation_failure" in rendered
        assert "discard" in rendered
        assert "REQUIRES" in rendered
        assert "Prompt Shield" in rendered
        assert "not applicable" in rendered

    def test_source_digest_has_source_profile_alias_and_public_required_options(self, tmp_path: Path) -> None:
        view, _snapshot = _source_only_profile_view(tmp_path)

        digest = discovery_digest(view)
        source = next(entry for entry in digest["sources"] if entry["name"] == "llm")

        assert source["profile_aliases"] == ["sonnet"]
        assert set(source["required_options"]) == {
            "profile",
            "schema",
            "prompt_template",
            "on_validation_failure",
        }
        assert not set(source["required_options"]) & {
            "provider",
            "model",
            "api_key",
            "queries",
            "required_input_fields",
        }


class TestReviewRegistry:
    """Suite run 1 G5: the closed pipeline_decision registry never surfaced at authoring time."""

    def test_registry_quotes_the_registered_terms_verbatim(self) -> None:
        from elspeth.web.interpretation_state import REGISTERED_PIPELINE_DECISION_USER_TERMS

        view, _snapshot = _trained_view()

        section = build_planner_authoring_aids(view)["review_registry"]
        assert section["registered_pipeline_decision_user_terms"] == sorted(REGISTERED_PIPELINE_DECISION_USER_TERMS)

    def test_registry_rules_carry_ownership_economy_override_and_off_vocab_route(self) -> None:
        view, _snapshot = _trained_view()

        rules = " ".join(build_planner_authoring_aids(view)["review_registry"]["rules"])
        # Closed-vocabulary + off-vocab route (G5).
        assert "metadata.description" in rules
        # Economy-override clause: an explicit user instruction does not waive
        # a registered demanded review — the row RECORDS the decision.
        assert "NEVER waived" in rules
        assert "RECORDS" in rules
        # Ownership: backend-owned kinds are never hand-authored (G3).
        assert "auto-stage" in rules
        assert "llm_prompt_template" in rules
        assert "llm_model_choice" in rules

    def test_gate_decision_rule_scopes_its_call_half_to_the_actual_palette(self) -> None:
        """F8: one aid payload, two surfaces, two different palettes.

        These aids ride in the planner request — whose palette carries no
        ``request_interpretation_review``, so obeying an unconditional
        instruction to call it lands on the DISCOVERY_ONLY terminal guard — and
        in the compose-loop catalog context, whose palette does carry it. The
        STAGING half is unconditional on both surfaces and stays verbatim; only
        the CALL half is scoped. The conditional clause is shared word-for-word
        with the capability core because both texts ride in the same context,
        and where they overlap they must not disagree.
        """
        from elspeth.web.composer.capability_skill import load_pipeline_capability_core

        view, _snapshot = _trained_view()

        rules = " ".join(build_planner_authoring_aids(view)["review_registry"]["rules"])
        # Word-for-word agreement is about words: collapse the markdown's hard
        # line wrapping before comparing.
        core_flat = " ".join(load_pipeline_capability_core().split())

        # The staging half survives, and the unconditional call is gone.
        assert "user_term gate_condition_authored ON THAT GATE NODE." in rules
        assert "ON THAT GATE NODE and call request_interpretation_review" not in rules
        assert "The row is valid only on a gate node" in rules
        for phrase in (
            "Where your palette carries",
            (
                "the staged requirement rides in that gate node's own options inside the terminal "
                "proposal and its review card is surfaced from the sealed proposal"
            ),
        ):
            assert phrase in rules, f"review-registry rule lacks shared phrase: {phrase!r}"
            assert phrase in core_flat, f"capability core lacks shared phrase: {phrase!r}"


class TestOwnershipAlignedExemplars:
    """G3: exemplars must not hand-author backend-auto-staged review rows."""

    def test_exemplar_authors_no_backend_owned_review_rows(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        for node in args["nodes"]:
            requirements = node.get("options", {}).get("interpretation_requirements", [])
            assert all(row["kind"] not in {"llm_prompt_template", "llm_model_choice"} for row in requirements), (
                f"node {node['id']} hand-authors a backend-owned review row"
            )


class TestCoalesceVocabulary:
    """Suite run 1 G7: policy/merge vocab + the inert-input convention."""

    def test_rules_state_the_closed_policy_and_merge_vocabularies(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)

        rules = " ".join(build_planner_authoring_aids(view)["fork_coalesce"]["rules"])
        assert "require_all, quorum, best_effort, first" in rules
        assert "union, nested, select" in rules
        assert "best_effort" in rules

    def test_exemplar_sets_coalesce_input_to_a_branch_connection(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None

        coalesce = next(node for node in args["nodes"] if node["node_type"] == "coalesce")
        assert coalesce["input"] in set(coalesce["branches"].values())


class TestNamedButMissingFile:
    """Suite run 1 singleton: honesty must not lose to fabrication."""

    def test_custody_rules_teach_the_discover_ask_decline_sequence(self) -> None:
        view, _snapshot = _trained_view()

        rules = " ".join(build_planner_authoring_aids(view)["source_custody"]["rules"])
        assert "never uploaded" in rules
        assert "list_blobs" in rules
        assert "named gap" in rules


class TestRun2PackEdits:
    """Pack pressure-suite run-2 gap closures (G2/G3/G4/G6/G9), pinned.

    Same discipline as the shield rules: product constants imported so the
    taught text can never drift; teaching co-located with the mandate it
    completes (the aids bless multi_query as the only multi-field shape, so
    the aids must also carry that shape's contract).
    """

    def test_llm_output_rules_teach_the_query_definition_contract(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["llm_output_contract"]["rules"])
        assert "input_fields" in rendered  # G2: REQUIRED per-query mapping
        assert "'template'" in rendered or '"template"' in rendered  # G2: per-query key is template
        assert "prompt_template" in rendered  # G2: top-level still required
        assert "max_capacity_retry_seconds" in rendered  # G9: web retry cap
        from elspeth.web.provider_config_policy import WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS

        assert str(WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS) in rendered

    def test_custody_rules_name_the_discovered_path_binding_slot(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["source_custody"]["rules"])
        assert "options.path" in rendered  # G4: where a discovered blobs/ path goes
        assert "blob_id" in rendered

    def test_raw_html_cleanup_rules_quote_the_constants_verbatim(self) -> None:
        # G6: the cleanup draft ships verbatim next to the shield draft — the
        # aids are the verbatim-constants channel (imported, drift-proof).
        from elspeth.web.interpretation_state import (
            RAW_HTML_CLEANUP_REVIEW_DRAFT,
            RAW_HTML_CLEANUP_USER_TERM,
        )

        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["raw_html_cleanup"]["rules"])
        assert RAW_HTML_CLEANUP_USER_TERM in rendered
        assert RAW_HTML_CLEANUP_REVIEW_DRAFT in rendered
        assert "field_mapper" in rendered  # attachment point

    def test_detailed_llm_hints_are_not_copied_into_the_selection_digest(self) -> None:
        view, _snapshot = _trained_view()
        aids = build_planner_authoring_aids(view)
        llm_entry = next(e for e in aids["discovery_digest"]["plugins"]["transforms"] if e["name"] == "llm")
        assert "composer_hints" not in llm_entry

    def test_skill_authoring_exemplar_carries_no_server_review_fields(self) -> None:
        # G5: an exemplar demonstrated the heavy server-side review row
        # (event_id/accepted_value/...) where the authored contract is the
        # short form (canonicalize_authored_node_review_requirements) —
        # three run-2 sims tripled the heavy row identically.
        from elspeth.web.composer.skills import load_skill

        skill = load_skill("pipeline_composer")
        assert '"event_id": null' not in skill
        assert '"accepted_artifact_hash": null' not in skill


class TestRun3PackEdits:
    """Pack pressure-suite run-3 closures (P2-rule, E1/E2/E3/E4/E6), pinned."""

    def test_retry_rule_is_profile_form_conditional(self) -> None:
        # run-3 P2: the unconditional retry rule steered profile-bound
        # authors into operator-private options; profile lowering injects
        # the bound itself.
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["llm_output_contract"]["rules"])
        assert "PROFILE-bound llm node" in rendered
        assert "operator-private" in rendered
        assert "Only a provider-form llm node" in rendered

    def test_shape_selection_rule_replaces_the_fork_preference(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["fork_coalesce"]["rules"])
        assert "SHAPE SELECTION" in rendered
        assert "SAME input field" in rendered
        assert "INDEPENDENT inputs" in rendered

    def test_output_prefix_contract_is_stated(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["llm_output_contract"]["rules"])
        assert "<query_key>_<response_field>" in rendered
        assert "<query_key>_<suffix>" in rendered

    def test_http_identity_rules_bind_or_omit_never_invent(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["web_scrape_http_identity"]["rules"])
        assert "OMIT the http block" in rendered
        assert "reserved-list-only" in rendered
        assert "NEVER invent" in rendered

    def test_llm_digest_entry_carries_the_live_profile_aliases(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)
        aids = build_planner_authoring_aids(view)
        llm_entry = next(e for e in aids["discovery_digest"]["plugins"]["transforms"] if e["name"] == "llm")
        assert llm_entry.get("profile_aliases") == ["sonnet"]

    def test_fork_exemplar_stages_its_own_classification_review(self, tmp_path: Path) -> None:
        # run-3 E6: the exemplar's urgency branch authors category semantics;
        # demonstrating it UN-reviewed was imported as precedent to skip
        # review-staging (the run's one leakage incident).
        view, _snapshot = _profile_view(tmp_path)
        args = fork_coalesce_exemplar_args(view)
        assert args is not None
        urgency = next(n for n in args["nodes"] if n["id"] == "assess_urgency")
        rows = urgency["options"]["interpretation_requirements"]
        assert len(rows) == 1
        assert rows[0]["kind"] == "vague_term"
        assert rows[0]["user_term"] == "urgency"
        assert set(rows[0]) == {"kind", "user_term", "draft"}
        parts = urgency["options"]["prompt_template_parts"]
        assert any(p.get("kind") == "interpretation_ref" and p.get("requirement_id") == "urgency:assess_urgency" for p in parts)


class TestRun5PackEdits:
    """Pack pressure-suite run-5 closures — planner-shielding review findings.

    Finding A: the run-4 CLOSED per-query key set (E1) omitted
    ``response_format``, ``max_tokens``, and list-form ``name`` — all valid
    ``QueryDefinition`` keys (``src/elspeth/plugins/transforms/llm/
    multi_query.py``) — so the rule steered planners to omit valid
    configuration or author a list entry validation rejects for a missing
    name.

    Finding B: the digest advertised an active profile-bound llm node's
    operator-private fields (``provider`` et al.) as *required* — the raw
    catalog schema's required set, never projected through the operator
    profile's public schema — steering profile-bound authors toward a
    ``profile_unavailable``/``plugin_unavailable`` rejection guaranteed by
    ``_LLMProfileResolver`` (``src/elspeth/web/plugin_policy/profiles.py``).
    """

    def test_query_definition_rule_permits_the_full_supported_key_set(self) -> None:
        view, _snapshot = _trained_view()
        rendered = "\n".join(build_planner_authoring_aids(view)["llm_output_contract"]["rules"])
        # response_format and max_tokens are real QueryDefinition fields the
        # run-4 closed-set rule forbade by omission.
        assert "response_format" in rendered
        assert "max_tokens" in rendered

    def test_llm_digest_required_options_are_profile_projected_under_an_active_profile(self, tmp_path: Path) -> None:
        view, _snapshot = _profile_view(tmp_path)
        aids = build_planner_authoring_aids(view)
        llm_entry = next(e for e in aids["discovery_digest"]["plugins"]["transforms"] if e["name"] == "llm")
        required = set(llm_entry["required_options"])
        # Operator profile validation rejects these on a profile-bound node —
        # the digest must never advertise them as required.
        assert not required & {"provider", "model", "credential_ref", "api_key", "api_key_secret"}
        assert "profile" in required

    def test_trained_operator_llm_digest_required_options_are_unaffected(self) -> None:
        """The trained-operator (profile-bypassed) view keeps the raw required set."""
        view, _snapshot = _trained_view()
        digest = discovery_digest(view)
        llm_entry = next(e for e in digest["transforms"] if e["name"] == "llm")
        assert set(llm_entry["required_options"]) == {"schema", "provider", "prompt_template"}


class TestLlmOnErrorRuleGating:
    """The on_error advice must match what the deployment's controls permit.

    Taught unconditionally, "route on_error to a dedicated quarantine sink"
    contradicted required_control_coverage: an llm node's on_error edge is an
    independent output path, so a quarantine sink on it is rejected, and no
    control transform can be interposed there (on_error may only name a sink
    or 'discard'). Only the GATING is asserted here — which of the two module
    constants is served — never the prose, per project convention.
    """

    def test_recommend_mode_keeps_the_quarantine_sink_advice(self, tmp_path: Path) -> None:
        view, _snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")

        rules = build_planner_authoring_aids(view)["llm_output_contract"]["rules"]

        assert planner_authoring_aids._LLM_ON_ERROR_QUARANTINE_RULE in rules
        assert not any(rule.startswith("This deployment REQUIRES the") for rule in rules)

    def test_required_mode_serves_the_discard_only_rule(self, tmp_path: Path) -> None:
        view, _snapshot = _guardrail_profile_view(tmp_path, control_mode="required")

        rules = build_planner_authoring_aids(view)["llm_output_contract"]["rules"]

        assert planner_authoring_aids._LLM_ON_ERROR_QUARANTINE_RULE not in rules
        assert planner_authoring_aids._LLM_ON_ERROR_CONTROLLED_RULE_TEMPLATE.format(control="aws_bedrock_content_safety") in rules
        assert planner_authoring_aids._LLM_ON_ERROR_CONTROLLED_TRADEOFF_TEMPLATE.format(control="aws_bedrock_content_safety") in rules

    def test_recommend_mode_carries_no_operator_escalation_rule(self, tmp_path: Path) -> None:
        """The operator escalation only makes sense when the gate is required."""
        view, _snapshot = _guardrail_profile_view(tmp_path, control_mode="recommend")

        rules = build_planner_authoring_aids(view)["llm_output_contract"]["rules"]

        assert not any("operator decision" in rule for rule in rules)

    def test_alias_less_required_control_also_gates_the_rule(self, tmp_path: Path) -> None:
        """Gating follows the control MODE, not how the control is configured."""
        view, _snapshot = _direct_control_view(tmp_path, control_mode="required")

        rules = build_planner_authoring_aids(view)["llm_output_contract"]["rules"]

        assert planner_authoring_aids._LLM_ON_ERROR_QUARANTINE_RULE not in rules
        assert planner_authoring_aids._LLM_ON_ERROR_CONTROLLED_RULE_TEMPLATE.format(control="azure_content_safety") in rules

    def test_deployment_without_a_selected_control_keeps_the_quarantine_advice(self) -> None:
        view, _snapshot = _trained_view()

        rules = build_planner_authoring_aids(view)["llm_output_contract"]["rules"]

        assert planner_authoring_aids._LLM_ON_ERROR_QUARANTINE_RULE in rules
