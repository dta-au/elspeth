"""azure_ai_search: typed options, RAG advertising, runtime_preflight readiness."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.azure_ai_search import AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
from elspeth.contracts.errors import FrameworkBugError, RuntimePreflightFailedError
from elspeth.contracts.plugin_capabilities import WebConfigAuthority
from elspeth.contracts.plugin_context import PluginContext
from elspeth.contracts.probes import CollectionReadinessResult
from elspeth.contracts.schema_contract import PipelineRow, SchemaContract
from elspeth.core.landscape.plugin_audit_writer import PluginAuditWriterAdapter
from elspeth.core.rate_limit.registry import RateLimitRegistry
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProvider
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.transforms.azure.ai_search import AzureAISearchConfig, AzureAISearchTransform

_BASE: dict[str, Any] = {
    "output_prefix": "policy",
    "query_field": "question",
    "endpoint": "https://svc-a.search.windows.net",
    "index": "approved-documents",
    "api_key": "test-key",
    "schema": {"mode": "observed"},
}


def _ctx(**overrides: Any) -> Any:
    values = {"operation_id": "op-1", "require_coordination_token": lambda: "coord", "landscape": None}
    values.update(overrides)
    return SimpleNamespace(**values)


def _ready(transform: AzureAISearchTransform, result: CollectionReadinessResult | Exception) -> MagicMock:
    searcher = MagicMock(spec=AzureSearchProvider)
    if isinstance(result, Exception):
        searcher.runtime_preflight.side_effect = result
    else:
        searcher.runtime_preflight.return_value = result
    transform.__dict__["_searcher"] = searcher
    return searcher


def test_advertises_itself_as_rag() -> None:
    assert {"rag", "retrieval", "azure"} <= set(AzureAISearchTransform.capability_tags)
    assistance = AzureAISearchTransform.get_agent_assistance()
    assert assistance is not None
    assert "RAG" in assistance.summary
    assert any("Azure RAG plugin" in hint for hint in assistance.composer_hints)
    assert "RAG" in AzureAISearchTransform.usage_when_to_use


def test_web_authoring_requires_an_operator_profile() -> None:
    # The web validator refuses an operator_profiled plugin without a usable profile
    # alias, whether or not a profile resolver is registered for it.
    assert AzureAISearchTransform.web_config_authority is WebConfigAuthority.OPERATOR_PROFILED


def test_index_field_options_are_not_read_as_row_columns() -> None:
    # The base class reads every ``*_field`` option as the name of a ROW column the
    # transform consumes. These options name Azure INDEX fields, so under that spelling
    # every row would be required to carry columns called ``content`` and ``id``.
    transform = AzureAISearchTransform(
        {**AzureAISearchTransform.probe_config(), "field_content": "chunk", "field_id": "chunk_id", "field_vector": "text_vector"}
    )
    assert transform.consumed_input_fields == frozenset({"rag_probe_query"})


def test_options_are_typed_and_flat() -> None:
    properties = AzureAISearchConfig.model_json_schema()["properties"]
    assert {"endpoint", "index", "search_mode", "field_content", "field_id", "select", "filter"} <= set(properties)
    assert "provider_config" not in properties
    assert "provider" not in properties


def test_every_private_binding_name_is_a_real_option() -> None:
    # A private name that is not an option would hide nothing from a web author.
    assert set(AzureAISearchConfig.model_fields) >= AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
    assert (
        frozenset({"endpoint", "api_key", "use_managed_identity", "client_id", "api_version"})
        == AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
    )


def test_provider_validators_are_reused_not_restated() -> None:
    with pytest.raises(PluginConfigError, match="select must include"):
        AzureAISearchConfig.from_dict({**_BASE, "field_content": "chunk", "select": ["title"]}, plugin_name="azure_ai_search")
    with pytest.raises(PluginConfigError, match="client_id requires use_managed_identity"):
        AzureAISearchConfig.from_dict({**_BASE, "client_id": "abc"}, plugin_name="azure_ai_search")


def test_every_provider_option_reaches_the_provider_config() -> None:
    config = AzureAISearchConfig.from_dict(
        {
            **_BASE,
            "api_version": "2024-05-01-preview",
            "search_mode": "semantic",
            "semantic_config": "sem",
            "request_timeout": 7.5,
            "field_vector": "text_vector",
            "field_content": "chunk",
            "field_id": "chunk_id",
            "field_title": "title",
            "field_url": "link",
            "select": ["chunk", "chunk_id", "title", "link"],
            "filter": "category eq 'policy'",
        },
        plugin_name="azure_ai_search",
    )
    provider_config = config.provider_config()
    # auth_mode is derived by the provider from the credentials, so it is not an option here.
    assert provider_config.auth_mode == "api_key"
    # The provider spells index fields ``*_field``; the plugin spells them ``field_*``.
    option_for = {f"{stem}_field": f"field_{stem}" for stem in ("vector", "content", "id", "title", "url")}
    for name in set(type(provider_config).model_fields) - {"auth_mode"}:
        assert provider_config.model_dump()[name] == config.model_dump()[option_for.get(name, name)], name


def test_endpoint_rules_are_enforced_at_config_load_not_first_row() -> None:
    with pytest.raises(PluginConfigError):
        AzureAISearchConfig.from_dict({**_BASE, "endpoint": "http://no-https.example.com"}, plugin_name="azure_ai_search")
    managed = {k: v for k, v in _BASE.items() if k != "api_key"} | {"use_managed_identity": True}
    with pytest.raises(PluginConfigError, match=r"managed identity.*search\.windows\.net"):
        AzureAISearchConfig.from_dict({**managed, "endpoint": "https://attacker.example.com"}, plugin_name="azure_ai_search")


def test_a_permanent_search_failure_is_a_row_error_labelled_with_this_plugin() -> None:
    transform = AzureAISearchTransform(_BASE)
    searcher = MagicMock(spec=AzureSearchProvider)
    searcher.search.side_effect = RetrievalError("Azure managed identity token acquisition failed", retryable=False)
    transform.__dict__["_searcher"] = searcher
    transform._on_start_called = True
    ctx = SimpleNamespace(
        state_id="state-1",
        token=SimpleNamespace(token_id="token-1"),
        require_member_token=lambda: "member",
        require_work_item=lambda: "work",
    )
    result = transform.process(PipelineRow({"question": "q"}, SchemaContract(mode="OBSERVED", fields=())), ctx)
    assert result.status == "error"
    assert result.reason["reason"] == "retrieval_failed"
    assert result.reason["provider"] == "azure_ai_search"


def test_yaml_run_with_a_raw_endpoint_needs_no_profile() -> None:
    config = AzureAISearchConfig.from_dict(_BASE, plugin_name="azure_ai_search")
    assert config.provider_config().endpoint == "https://svc-a.search.windows.net"


def test_limiter_is_keyed_by_search_service_host() -> None:
    assert AzureAISearchTransform(_BASE).limiter_service_name == "azure_ai_search:svc-a.search.windows.net"
    other = AzureAISearchTransform({**_BASE, "endpoint": "https://svc-b.search.windows.net"})
    assert other.limiter_service_name != AzureAISearchTransform(_BASE).limiter_service_name


def test_build_searcher_asks_the_registry_for_the_host_keyed_limiter() -> None:
    registry = MagicMock(spec=RateLimitRegistry)
    ctx = SimpleNamespace(
        landscape=MagicMock(spec=PluginAuditWriterAdapter),
        run_id="run-1",
        call_mode_session=None,
        telemetry_emit=lambda event: None,
        rate_limit_registry=registry,
    )
    searcher = AzureAISearchTransform(_BASE)._build_searcher(ctx)
    try:
        assert isinstance(searcher, AzureSearchProvider)
        registry.get_limiter.assert_called_once_with("azure_ai_search:svc-a.search.windows.net")
    finally:
        searcher.close()


def test_declares_runtime_preflight_and_never_records_a_readiness_check() -> None:
    assert AzureAISearchTransform.requires_runtime_preflight is True
    transform = AzureAISearchTransform(_BASE)
    ctx = _ctx(record_readiness_check=MagicMock(spec=PluginContext.record_readiness_check))
    searcher = _ready(transform, CollectionReadinessResult(collection="approved-documents", reachable=True, count=3, message="ok"))
    transform.runtime_preflight(ctx)
    ctx.record_readiness_check.assert_not_called()
    searcher.runtime_preflight.assert_called_once_with(operation_id="op-1", coordination_token="coord")


@pytest.mark.parametrize("count", [None, 0])
def test_missing_or_empty_index_fails_preflight(count: int | None) -> None:
    transform = AzureAISearchTransform(_BASE)
    _ready(transform, CollectionReadinessResult(collection="approved-documents", reachable=True, count=count, message="Index is empty"))
    with pytest.raises(RuntimePreflightFailedError, match="Index is empty") as exc_info:
        transform.runtime_preflight(_ctx())
    assert exc_info.value.retryable is False


@pytest.mark.parametrize("retryable", [True, False])
def test_probe_error_keeps_its_retryability(retryable: bool) -> None:
    transform = AzureAISearchTransform(_BASE)
    _ready(transform, RetrievalError("HTTP 503", retryable=retryable, status_code=503))
    with pytest.raises(RuntimePreflightFailedError) as exc_info:
        transform.runtime_preflight(_ctx())
    assert exc_info.value.retryable is retryable


def test_preflight_without_an_operation_parent_is_a_framework_bug() -> None:
    transform = AzureAISearchTransform(_BASE)
    _ready(transform, CollectionReadinessResult(collection="x", reachable=True, count=1, message="ok"))
    with pytest.raises(FrameworkBugError, match="operation audit parent"):
        transform.runtime_preflight(_ctx(operation_id=None))


def test_preflight_before_on_start_is_a_framework_bug() -> None:
    with pytest.raises(FrameworkBugError, match="before provider initialization"):
        AzureAISearchTransform(_BASE).runtime_preflight(_ctx())
