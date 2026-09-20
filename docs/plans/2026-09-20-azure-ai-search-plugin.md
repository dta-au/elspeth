# `azure_ai_search` Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** A web-authored pipeline retrieves RAG context from an operator-configured Azure AI Search service through a dedicated `azure_ai_search` transform, with several services per install and no author-chosen endpoint.

**Architecture:** The provider-neutral half of `rag_retrieval` moves into `RetrievalTransformBase`; `rag_retrieval` keeps Chroma and a new `azure_ai_search` transform owns Azure with typed options and a `runtime_preflight` readiness probe. A `_AzureSearchProfileResolver` registered on `transform/azure_ai_search` makes the web surface profile-only, which replaces the string-matching managed-identity refusal.

**Tech Stack:** Python 3.12, pydantic v2, httpx via `AuditedHTTPClient`, `azure-identity` (`ManagedIdentityCredential`), pytest + respx.

**Spec:** `docs/plans/2026-09-20-azure-search-retrieval-plugin-design.md` (approved 2026-09-20; rulings at its end).

## Global Constraints

- Worktree `.claude/worktrees/azure-ai-search-rag`, branch `feature/azure-ai-search-rag`. Never `cd` to the main checkout.
- Every pytest and lint run exports `PYTHONPATH=<wt>/src:<wt>/elspeth-lints/src`, passes `-o 'pythonpath=<wt>/src <wt>/elspeth-lints/src'` (whitespace separated), and first prints `elspeth.__file__` to prove it resolves inside the worktree. Results are read from a log file plus the exit code, never from a pipe.
- **The branch merges whole.** Between Task 3 and Task 5 the new plugin exists without its profile resolver; no intermediate commit is deployable to a web install.
- One path to Azure Search: after Task 6, `grep -rn azure_search src/elspeth/plugins/transforms/rag` returns nothing.
- The plugin never calls `ctx.record_readiness_check`. Chroma's `on_start` readiness path is not touched (Task 7 of the Document Intelligence plan owns it).
- `filter` is an operator- or author-authored literal string. Nothing builds an OData filter from row data.
- Managed identity means `ManagedIdentityCredential`, never `DefaultAzureCredential`.
- No `# noqa`, `# type: ignore`, lint suppression, or hand-edited `judge_metadata_signature`. Pins and signatures never reshape code: re-pin `source_file_hash` from the `plugin_contract.plugin_hashes` rule's "expected" value; leave trust-tier signature churn for the operator.
- Commit by pathspec (`git commit -- <paths>`); run `scripts/branch-safety-check.sh --intent commit --base release/0.8.1` first. End commit messages with `Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>`.
- No push, no merge, no move of `release/0.8.1`.

## Review Focus

1. **An authored node names a private binding option** (`endpoint`, `api_key`, `use_managed_identity`, `client_id`, `api_version`) alongside a valid `profile`. Expected: refused at composer tool, `validate_pipeline`, and execution, never silently overridden. Tests in Task 5 and Task 6.
2. **An `index` outside the profile's pin, or a profile whose `indexes` is missing, empty, or null.** Expected: the first is refused at lowering; the second fails web start-up. Tests in Task 4 and Task 5.
3. **Two profiles on different services in one pipeline.** Expected: each node lowers to its own endpoint and identity, and they do not share a rate-limit bucket. Tests in Task 5 and Task 3.
4. **The readiness probe meets 403, 404, a non-integer `$count`, and an empty index.** Expected: `RuntimePreflightFailedError` before any row loads, retryable only for transport and 5xx/429, with the probe recorded as an operation call. Tests in Task 2 and Task 3.
5. **A YAML run with a raw endpoint and no profile.** Expected: works unchanged, because profiles are a web concern. Test in Task 3.

---

## File Structure

| File | Responsibility |
| --- | --- |
| `src/elspeth/plugins/transforms/rag/core.py` (create) | `RetrievalOutputConfig`, `RetrievalTransformBase`: query build, search, zero-result handling, formatting, `__rag_*` fields, score telemetry |
| `src/elspeth/plugins/transforms/rag/transform.py` (modify) | `RAGRetrievalTransform`: Chroma provider selection and its `on_start` readiness only |
| `src/elspeth/plugins/transforms/rag/config.py` (modify) | `RAGRetrievalConfig` on top of `RetrievalOutputConfig`; Chroma-only registry |
| `src/elspeth/plugins/infrastructure/clients/retrieval/base.py` (modify) | `RetrievalSearcher` protocol (search/close/skip evidence); `RetrievalProvider` extends it with `check_readiness` |
| `src/elspeth/plugins/infrastructure/clients/retrieval/azure_search.py` (modify) | audited `runtime_preflight` probe replaces the raw-httpx `check_readiness` |
| `src/elspeth/plugins/transforms/azure/ai_search.py` (create) | `AzureAISearchConfig`, `AzureAISearchTransform` |
| `src/elspeth/contracts/azure_ai_search.py` (create) | `AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES`, shared by plugin and web policy |
| `src/elspeth/web/plugin_policy/profiles.py` (modify) | `AzureSearchProfileSettings`, `_AzureSearchProfileResolver`, registry arm, summary and assistance arms |
| `src/elspeth/web/config.py` (modify) | `azure_search_profiles` setting, validator, JSON-array and safe-path sets |
| `src/elspeth/web/provider_config_policy.py` and six call sites (modify) | delete the RAG managed-identity refusal |

---

### Task 1: Extract the provider-neutral retrieval core

Behaviour-preserving. The existing `tests/unit/plugins/transforms/rag/` suite is the regression net and passes unmodified.

**Files:**
- Create: `src/elspeth/plugins/transforms/rag/core.py`
- Modify: `src/elspeth/plugins/transforms/rag/transform.py`, `src/elspeth/plugins/transforms/rag/config.py`, `src/elspeth/plugins/infrastructure/clients/retrieval/base.py`
- Test: `tests/unit/plugins/transforms/rag/test_core.py` (create)

**Interfaces:**
- Produces: `RetrievalSearcher` (Protocol: `search(...)`, `close()`, `last_skipped_count: int`, `last_skipped_reasons: list[dict[str, Any]]`); `RetrievalProvider(RetrievalSearcher)` adds `check_readiness()`.
- Produces: `RetrievalOutputConfig(TransformDataConfig)` with `output_prefix`, `query_field`, `query_template`, `query_pattern`, `top_k`, `min_score`, `on_no_results`, `context_format`, `context_separator`, `max_context_length` and their validators, moved verbatim from `RAGRetrievalConfig`.
- Produces: `RetrievalTransformBase(BaseTransform)` with no `name` attribute. Subclass hooks: `_build_searcher(self, ctx: LifecycleContext) -> RetrievalSearcher` and class attribute `_provider_label: str` (the `provider` value written into error reasons and `RAGRetrievalStatistics`). Its constructor is `__init__(self, config: dict[str, Any], retrieval_config: RetrievalOutputConfig)`. It owns `process`, `on_complete`, `close`, `_update_score_stats`, and the `self._searcher` slot; `on_start` resets the accumulators and calls `self._searcher = self._build_searcher(ctx)`.

- [ ] **Step 1: Write the failing test**

```python
"""The retrieval core runs against any searcher, with no provider registry."""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig, RetrievalTransformBase


class _Searcher:
    def __init__(self, chunks: list[RetrievalChunk]) -> None:
        self._chunks = chunks
        self.last_skipped_count = 0
        self.last_skipped_reasons: list[dict[str, Any]] = []
        self.closed = False

    def search(self, query: str, top_k: int, min_score: float, **_audit: Any) -> list[RetrievalChunk]:
        return self._chunks

    def close(self) -> None:
        self.closed = True


class _Transform(RetrievalTransformBase):
    name = "core_probe"
    config_model = RetrievalOutputConfig
    _provider_label = "probe"

    def __init__(self, config: dict[str, Any], searcher: _Searcher) -> None:
        super().__init__(config, RetrievalOutputConfig.from_dict(config, plugin_name=self.name))
        self._probe_searcher = searcher

    def _build_searcher(self, ctx: Any) -> _Searcher:
        return self._probe_searcher


def test_base_class_is_not_a_discoverable_plugin() -> None:
    assert "name" not in RetrievalTransformBase.__dict__


def test_output_config_rejects_template_and_pattern_together() -> None:
    with pytest.raises(ValueError, match="mutually exclusive"):
        RetrievalOutputConfig.from_dict(
            {"output_prefix": "p", "query_field": "q", "query_template": "{{ row.q }}", "query_pattern": "x", "schema": {"mode": "observed"}},
            plugin_name="core_probe",
        )


def test_declared_output_fields_come_from_the_prefix() -> None:
    transform = _Transform({"output_prefix": "kb", "query_field": "q", "schema": {"mode": "observed"}}, _Searcher([]))
    assert transform.declared_output_fields == frozenset({"kb__rag_context", "kb__rag_score", "kb__rag_count", "kb__rag_sources"})
```

- [ ] **Step 2: Run it to verify it fails**

Run: `pytest tests/unit/plugins/transforms/rag/test_core.py -n 0 -q`
Expected: collection error, `ModuleNotFoundError: elspeth.plugins.transforms.rag.core`.

- [ ] **Step 3: Implement**

In `retrieval/base.py`, split the protocol. `RetrievalSearcher` takes the `search` and `close` members and the two `last_skipped_*` attributes from the current `RetrievalProvider`; `RetrievalProvider(RetrievalSearcher, Protocol)` keeps only `check_readiness`. Neither is `runtime_checkable` (ADR-032).

Create `core.py`. Move, without editing their bodies: from `config.py` the ten shared fields and the validators `validate_prefix`, `validate_query_field`, `declared_input_fields`, `validate_query_modes`, `validate_regex`, and `coerce_schema_config` into `RetrievalOutputConfig`; from `transform.py` the constructor body from `prefix = ...` down to the telemetry default, `process`, `on_complete`, `close`, `_update_score_stats`, into `RetrievalTransformBase`. Apply exactly these substitutions in the moved code: `self._rag_config` becomes `self._retrieval_config`; `self._provider` becomes `self._searcher`; every `self._rag_config.provider` becomes `self._provider_label`. `on_start` in the base is:

```python
    def on_start(self, ctx: LifecycleContext) -> None:
        """Capture lifecycle context and build the searcher."""
        super().on_start(ctx)
        self._run_id = ctx.run_id
        self._telemetry_emit = ctx.telemetry_emit
        self._total_queries = 0
        self._quarantine_count = 0
        self._total_chunks = 0
        self._score_count = 0
        self._score_mean = 0.0
        self._score_m2 = 0.0
        self._searcher = self._build_searcher(ctx)
```

`RAGRetrievalConfig` becomes `class RAGRetrievalConfig(RetrievalOutputConfig)` keeping `provider`, `provider_config`, and `validate_provider_config`. `RAGRetrievalTransform(RetrievalTransformBase)` keeps its class attributes, `probe_config`, the invariant-probe methods (which set `self.__dict__["_searcher"]`), `_configured_collection_name`, `_record_readiness_check`, `get_agent_assistance`, and:

```python
    def __init__(self, config: dict[str, Any]) -> None:
        self._rag_config = RAGRetrievalConfig.from_dict(config, plugin_name=self.name)
        super().__init__(config, self._rag_config)
        self._provider_label = self._rag_config.provider

    def _build_searcher(self, ctx: LifecycleContext) -> RetrievalSearcher:
        ...  # the existing on_start body from "Construct provider from registry" to the final readiness guard, returning the provider
```

The elided body above is the current `on_start` lines from `provider_name = self._rag_config.provider` through the last `raise RetrievalNotReadyError(...)`, with `self._provider = factory(...)` changed to a local `provider = factory(...)`, `self._provider.check_readiness()` to `provider.check_readiness()`, and a final `return provider`.

- [ ] **Step 4: Run the regression net**

Run: `pytest tests/unit/plugins/transforms/rag tests/integration/plugins/transforms/test_rag_pipeline.py -q`
Expected: exit 0 with no test file under `tests/unit/plugins/transforms/rag/` modified except the new `test_core.py`. If a test reached into `transform._provider`, that is the rename showing: change the attribute name in the test and nothing else.

- [ ] **Step 5: Re-pin and commit**

Run the `plugin_contract.plugin_hashes` rule, copy its "expected" hash into `RAGRetrievalTransform.source_file_hash`, re-run to exit 0, then:

```bash
git add -- src/elspeth/plugins/transforms/rag/core.py tests/unit/plugins/transforms/rag/test_core.py
git commit -m "refactor(rag): extract the provider-neutral retrieval core" -- src/elspeth/plugins/transforms/rag src/elspeth/plugins/infrastructure/clients/retrieval/base.py tests/unit/plugins/transforms/rag
```

---

### Task 2: Audited readiness probe on the Azure provider

**Files:**
- Modify: `src/elspeth/plugins/infrastructure/clients/retrieval/azure_search.py`
- Test: `tests/unit/plugins/infrastructure/clients/retrieval/test_azure_search.py`

**Interfaces:**
- Produces: `AzureSearchProvider.runtime_preflight(self, *, operation_id: str, coordination_token: CoordinationToken) -> CollectionReadinessResult`. Raises `RetrievalError` (`retryable=True` for transport errors, 429 and 5xx; `retryable=False` for 401/403, SSRF block, other 4xx). Returns a result for 200 (count parsed or `count=None` on a non-integer body) and 404 (`reachable=True, count=None`).
- `check_readiness` and `_readiness_get` stay until Task 6 removes their last caller.

- [ ] **Step 1: Write the failing tests** (append to `test_azure_search.py`; `COUNT_URL` is the pinned-IP `$count` URL, built the way `TestAzureSearchProviderReadiness` already builds it)

```python
class TestRuntimePreflightProbe:
    PINNED_COUNT_URL = "https://93.184.216.34/indexes/test-index/docs/$count?api-version=2024-07-01"

    def _probe(self, provider: AzureSearchProvider) -> Any:
        return provider.runtime_preflight(operation_id="op-1", coordination_token=mock_item_audit_authority()["member_token"].coordination_token)

    def test_probe_is_recorded_as_an_operation_call(self) -> None:
        recorder = _FakeExecutionRecorder()
        provider = AzureSearchProvider(
            config=AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="test-index", api_key="test-key"),
            execution=recorder, run_id="run-1", telemetry_emit=_TelemetrySink(),
        )
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), respx.mock:
            respx.get(self.PINNED_COUNT_URL).respond(status_code=200, text="42")
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, 42)
        assert recorder.operation_call_indices == {"op-1": 1}
        assert len(recorder.recorded_calls) == 1

    @pytest.mark.parametrize(("status", "retryable"), [(401, False), (403, False), (429, True), (503, True), (400, False)])
    def test_probe_error_statuses(self, status: int, retryable: bool) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), respx.mock:
            respx.get(self.PINNED_COUNT_URL).respond(status_code=status)
            with pytest.raises(RetrievalError) as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is retryable
        assert exc_info.value.status_code == status

    def test_probe_missing_index_is_reachable_with_unknown_count(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), respx.mock:
            respx.get(self.PINNED_COUNT_URL).respond(status_code=404)
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, None)
        assert "not found" in result.message

    def test_probe_non_integer_count_is_unknown_not_zero(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]), respx.mock:
            respx.get(self.PINNED_COUNT_URL).respond(status_code=200, text="<html>")
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, None)

    def test_probe_refuses_a_private_address_before_any_request(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]), respx.mock:
            route = respx.get(self.PINNED_COUNT_URL)
            with pytest.raises(RetrievalError, match="SSRF") as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is False
        assert not route.called
```

If `mock_item_audit_authority()` does not expose a coordination token under that path, read `tests/fixtures/mock_audit.py` and use the helper it provides for operation-scoped audit authority; do not construct a `CoordinationToken` by hand.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/plugins/infrastructure/clients/retrieval/test_azure_search.py -n 0 -q -k TestRuntimePreflightProbe`
Expected: FAIL, `AttributeError: 'AzureSearchProvider' object has no attribute 'runtime_preflight'`.

- [ ] **Step 3: Implement**

```python
    def runtime_preflight(self, *, operation_id: str, coordination_token: CoordinationToken) -> CollectionReadinessResult:
        """Count the index's documents as an audited call under an operation parent."""
        from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient

        index_name = self._config.index
        count_url = f"{self._config.endpoint.rstrip('/')}/indexes/{index_name}/docs/$count?api-version={self._config.api_version}"
        try:
            safe_request = validate_url_for_ssrf(count_url)
        except SSRFBlockedError as exc:
            raise RetrievalError(f"Azure AI Search endpoint blocked by SSRF validation: {exc}", retryable=False) from exc
        except NetworkError as exc:
            raise RetrievalError(f"Azure AI Search endpoint DNS validation failed: {exc}", retryable=True) from exc

        client = AuditedHTTPClient(
            execution=self._execution,
            state_id=None,
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            timeout=10.0,
            limiter=self._limiter,
            operation_id=operation_id,
            coordination_token=coordination_token,
        )
        try:
            response, _final_url, _call = client.get_ssrf_safe(safe_request, headers=self._auth_headers())
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise RetrievalError(f"Readiness probe failed: {exc}", retryable=True) from exc
        finally:
            client.close()

        status_code = response.status_code
        if status_code == 404:
            return CollectionReadinessResult(collection=index_name, reachable=True, count=None, message=f"Index '{index_name}' not found")
        if status_code in (401, 403):
            raise RetrievalError(
                f"Authentication failed for {self._config.endpoint} index {index_name!r}: HTTP {status_code}",
                retryable=False, status_code=status_code,
            )
        if status_code == 429 or status_code >= 500:
            raise RetrievalError(f"Azure AI Search readiness probe: HTTP {status_code}", retryable=True, status_code=status_code)
        if status_code >= 400:
            raise RetrievalError(f"Azure AI Search readiness probe: HTTP {status_code}", retryable=False, status_code=status_code)
        try:
            count = int(response.text.strip())
        except ValueError:
            return CollectionReadinessResult(
                collection=index_name, reachable=True, count=None,
                message=f"Index '{index_name}' returned a non-integer $count body",
            )
        return CollectionReadinessResult(
            collection=index_name, reachable=True, count=count,
            message=(f"Index '{index_name}' has {count} documents" if count > 0 else f"Index '{index_name}' is empty"),
        )
```

Import `CoordinationToken` from `elspeth.contracts.coordination`. The non-integer message deliberately omits `response.text`: an HTML error page does not belong in an audit message.

- [ ] **Step 4: Run to green, then mutate**

Run the class again: exit 0. Then change `retryable=True` on the 429/5xx branch to `False` and confirm `test_probe_error_statuses[429-True]` and `[503-True]` go red; restore.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(azure-search): audited readiness probe under an operation parent" -- src/elspeth/plugins/infrastructure/clients/retrieval/azure_search.py tests/unit/plugins/infrastructure/clients/retrieval/test_azure_search.py
```

---

### Task 3: The `azure_ai_search` transform

**Files:**
- Create: `src/elspeth/contracts/azure_ai_search.py`, `src/elspeth/plugins/transforms/azure/ai_search.py`
- Test: `tests/unit/plugins/transforms/azure/test_ai_search.py` (create)
- Modify (pins, found by running them): `tests/unit/plugins/test_discovery.py`, `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`, `tests/unit/plugins/test_state_engine_config_discriminators.py`, `tests/unit/telemetry/test_plugin_wiring.py`, `tests/golden/web/catalog/knob_schema/transform__azure_ai_search.json` (new golden), `src/elspeth/web/audit_readiness/boundary_expectations.py`, `src/elspeth/web/audit_readiness/explain.py`, `src/elspeth/web/frontend/src/components/catalog/pluginDisplayName.ts` and its test.

**Interfaces:**
- Consumes: `RetrievalOutputConfig`, `RetrievalTransformBase`, `RetrievalSearcher` (Task 1); `AzureSearchProvider.runtime_preflight` (Task 2).
- Produces: `AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES: frozenset[str] = frozenset({"endpoint", "api_key", "use_managed_identity", "client_id", "api_version"})` in `contracts/azure_ai_search.py`.
- Produces: `AzureAISearchConfig(RetrievalOutputConfig)` with top-level `endpoint`, `index`, `api_key`, `use_managed_identity`, `client_id`, `api_version`, `search_mode`, `vector_field`, `semantic_config`, `content_field`, `id_field`, `title_field`, `url_field`, `select`, `filter`, `request_timeout`; and `provider_config(self) -> AzureSearchProviderConfig`.
- Produces: `AzureAISearchTransform` with `name = "azure_ai_search"`, `requires_runtime_preflight = True`.

- [ ] **Step 1: Write the failing tests**

```python
"""azure_ai_search: typed options, RAG advertising, runtime_preflight readiness."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

from elspeth.contracts.errors import FrameworkBugError, RuntimePreflightFailedError
from elspeth.contracts.probes import CollectionReadinessResult
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
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
    searcher = MagicMock()
    searcher.runtime_preflight.side_effect = result if isinstance(result, Exception) else None
    searcher.runtime_preflight.return_value = None if isinstance(result, Exception) else result
    transform.__dict__["_searcher"] = searcher
    return searcher


def test_advertises_itself_as_rag() -> None:
    assert {"rag", "retrieval", "azure"} <= set(AzureAISearchTransform.capability_tags)
    assistance = AzureAISearchTransform.get_agent_assistance()
    assert assistance is not None
    assert "RAG" in assistance.summary
    assert "RAG" in AzureAISearchTransform.usage_when_to_use


def test_options_are_typed_and_flat() -> None:
    properties = AzureAISearchConfig.model_json_schema()["properties"]
    assert {"endpoint", "index", "search_mode", "content_field", "id_field", "select", "filter"} <= set(properties)
    assert "provider_config" not in properties
    assert "provider" not in properties


def test_provider_validators_are_reused_not_restated() -> None:
    with pytest.raises(ValueError, match="select must include"):
        AzureAISearchConfig.from_dict({**_BASE, "content_field": "chunk", "select": ["title"]}, plugin_name="azure_ai_search")
    with pytest.raises(ValueError, match="client_id requires use_managed_identity"):
        AzureAISearchConfig.from_dict({**_BASE, "client_id": "abc"}, plugin_name="azure_ai_search")


def test_yaml_run_with_a_raw_endpoint_needs_no_profile() -> None:
    config = AzureAISearchConfig.from_dict(_BASE, plugin_name="azure_ai_search")
    assert config.provider_config().endpoint == "https://svc-a.search.windows.net"


def test_limiter_is_keyed_by_search_service_host() -> None:
    assert AzureAISearchTransform(_BASE).limiter_service_name == "azure_ai_search:svc-a.search.windows.net"
    other = AzureAISearchTransform({**_BASE, "endpoint": "https://svc-b.search.windows.net"})
    assert other.limiter_service_name != AzureAISearchTransform(_BASE).limiter_service_name


def test_declares_runtime_preflight_and_never_records_a_readiness_check() -> None:
    assert AzureAISearchTransform.requires_runtime_preflight is True
    transform = AzureAISearchTransform(_BASE)
    ctx = _ctx(record_readiness_check=MagicMock())
    _ready(transform, CollectionReadinessResult(collection="approved-documents", reachable=True, count=3, message="ok"))
    transform.runtime_preflight(ctx)
    ctx.record_readiness_check.assert_not_called()


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
```

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/plugins/transforms/azure/test_ai_search.py -n 0 -q`
Expected: collection error, `ModuleNotFoundError: elspeth.plugins.transforms.azure.ai_search`.

- [ ] **Step 3: Implement**

`contracts/azure_ai_search.py` holds only the frozenset above with a one-line docstring.

`ai_search.py`:

```python
"""Azure AI Search RAG retrieval transform."""

from __future__ import annotations

import urllib.parse
from typing import TYPE_CHECKING, Any, Literal

from pydantic import Field, model_validator

from elspeth.contracts import Determinism
from elspeth.contracts.errors import FrameworkBugError, RuntimePreflightFailedError
from elspeth.contracts.plugin_assistance import PluginAssistance
from elspeth.contracts.plugin_capabilities import ContentTrust
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import AzureSearchProvider, AzureSearchProviderConfig
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.transforms.rag.core import RetrievalOutputConfig, RetrievalTransformBase

if TYPE_CHECKING:
    from typing import Self

    from elspeth.contracts.contexts import LifecycleContext

_PROVIDER_FIELDS: tuple[str, ...] = (
    "endpoint", "index", "api_key", "use_managed_identity", "client_id", "api_version", "search_mode",
    "request_timeout", "vector_field", "semantic_config", "content_field", "id_field", "title_field",
    "url_field", "select", "filter",
)


class AzureAISearchConfig(RetrievalOutputConfig):
    """Options for the azure_ai_search transform."""

    endpoint: str = Field(description="Azure AI Search service URL, https://<service>.search.windows.net.")
    index: str = Field(description="Name of the existing index to query.")
    api_key: str | None = Field(default=None, description="Query key. Mutually exclusive with use_managed_identity.")
    use_managed_identity: bool = Field(default=False, description="Authenticate with the host's managed identity.")
    client_id: str | None = Field(default=None, description="Client id of a user-assigned managed identity.")
    api_version: str = Field(default="2024-07-01", description="Azure AI Search REST API version.")
    search_mode: Literal["vector", "keyword", "hybrid", "semantic"] = Field(
        default="hybrid", description="vector and hybrid need an integrated vectorizer on vector_field; semantic needs semantic_config."
    )
    request_timeout: float = Field(default=30.0, gt=0, description="Per-request timeout in seconds.")
    vector_field: str = Field(default="contentVector", description="Vector field queried by vector and hybrid modes.")
    semantic_config: str | None = Field(default=None, description="Semantic configuration name, required for semantic mode.")
    content_field: str = Field(default="content", description="Retrievable string field holding the chunk text.")
    id_field: str = Field(default="id", description="Retrievable key field.")
    title_field: str | None = Field(default=None, description="Optional field emitted as source_name citation metadata.")
    url_field: str | None = Field(default=None, description="Optional field emitted as source_link citation metadata.")
    select: tuple[str, ...] | None = Field(default=None, description="Fields to return; must include every mapped field.")
    filter: str | None = Field(default=None, description="Literal OData $filter sent with every query.")

    def provider_config(self) -> AzureSearchProviderConfig:
        return AzureSearchProviderConfig(**{name: getattr(self, name) for name in _PROVIDER_FIELDS})

    @model_validator(mode="after")
    def validate_provider_options(self) -> Self:
        self.provider_config()
        return self
```

`getattr(self, name)` over a fixed tuple is a dynamic-attribute site. Before writing it, read CONTRIBUTING.md § Whole-tree gates: if the dynamic-attribute gate pins the exact site set, write the sixteen keyword arguments out explicitly instead (`endpoint=self.endpoint, index=self.index, ...`) and delete `_PROVIDER_FIELDS`. Prefer the explicit form if in any doubt.

```python
class AzureAISearchTransform(RetrievalTransformBase):
    """RAG retrieval against an existing Azure AI Search index."""

    name = "azure_ai_search"
    plugin_version = "1.0.0"
    source_file_hash: str | None = None
    determinism: Determinism = Determinism.EXTERNAL_CALL
    config_model = AzureAISearchConfig
    passes_through_input = True
    content_trust = ContentTrust.UNTRUSTED
    requires_runtime_preflight = True
    capability_tags: tuple[str, ...] = ("rag", "retrieval", "vector-search", "azure")
    _provider_label = "azure_ai_search"

    usage_when_to_use = (
        "Use for RAG retrieval against an existing Azure AI Search index: it returns ranked, "
        "provenance-bearing context for a downstream llm transform. Retrieved text is untrusted "
        "before LLM consumption even when it comes from an approved index."
    )
    usage_when_not_to_use = (
        "Not for indexing documents or generating answers: populate the index with Azure's own indexer, "
        "and add an llm transform after retrieval when an answer is required. Use rag_retrieval for Chroma."
    )
    example_use = (
        "transform:\n"
        "  plugin: azure_ai_search\n"
        "  options:\n"
        "    output_prefix: policy\n"
        "    query_field: question\n"
        "    endpoint: https://catalogue-reference.search.windows.net\n"
        "    index: approved-documents\n"
        "    api_key: {secret_ref: AZURE_SEARCH_API_KEY}\n"
        "    search_mode: hybrid\n"
        "    top_k: 5\n"
        "    schema: {mode: observed}"
    )

    def __init__(self, config: dict[str, Any]) -> None:
        self._search_config = AzureAISearchConfig.from_dict(config, plugin_name=self.name)
        super().__init__(config, self._search_config)
        hostname = urllib.parse.urlparse(self._search_config.endpoint).hostname
        self.limiter_service_name = f"azure_ai_search:{hostname}"

    def _build_searcher(self, ctx: LifecycleContext) -> AzureSearchProvider:
        return AzureSearchProvider(
            self._search_config.provider_config(),
            execution=ctx.landscape,
            run_id=ctx.run_id,
            telemetry_emit=ctx.telemetry_emit,
            limiter=(ctx.rate_limit_registry.get_limiter(self.limiter_service_name) if ctx.rate_limit_registry is not None else None),
        )

    def runtime_preflight(self, ctx: LifecycleContext) -> None:
        """Refuse to load rows against a missing, empty or unreachable index."""
        if self._searcher is None:
            raise FrameworkBugError("AzureAISearchTransform runtime_preflight called before provider initialization")
        if ctx.operation_id is None:
            raise FrameworkBugError("AzureAISearchTransform runtime_preflight requires an operation audit parent")
        try:
            readiness = self._searcher.runtime_preflight(
                operation_id=ctx.operation_id, coordination_token=ctx.require_coordination_token()
            )
        except RetrievalError as exc:
            raise RuntimePreflightFailedError(plugin_name=self.name, provider=self._provider_label, cause=exc) from exc
        if readiness.count is None or readiness.count <= 0:
            raise RuntimePreflightFailedError(
                plugin_name=self.name,
                provider=self._provider_label,
                cause=RetrievalError(readiness.message, retryable=False),
            )
```

`self._searcher` is typed `RetrievalSearcher | None` in the base; narrow it for the `runtime_preflight` call with an `isinstance(self._searcher, AzureSearchProvider)` check that raises `FrameworkBugError` otherwise (ADR-032: nominal typing of a class we own). The test's `MagicMock` then needs `spec=AzureSearchProvider`; update `_ready` accordingly.

Add `probe_config`, `forward_invariant_probe_rows` and `execute_forward_invariant_probe` by copying the three methods from `RAGRetrievalTransform` and changing only the probe config to the flat shape (`endpoint`, `index`, `api_key` at top level). Add `get_agent_assistance` returning a `PluginAssistance` whose `summary` is `"RAG retrieval against an Azure AI Search index. Builds a query from row fields and returns ranked, cited chunks for downstream LLM grounding."` and whose `composer_hints` are the two Azure hints currently in `RAGRetrievalTransform.get_agent_assistance` plus `"This is the Azure RAG plugin: pair it with an llm transform that reads {output_prefix}__rag_context."`.

- [ ] **Step 4: Run to green, then the pins**

Run the new test file to exit 0. Then run, one at a time, and update each pin it names: `tests/unit/plugins/test_discovery.py`, `tests/unit/plugins/transforms/test_external_catalogue_metadata.py`, `tests/unit/plugins/test_state_engine_config_discriminators.py`, `tests/unit/telemetry/test_plugin_wiring.py`, `tests/unit/web/catalog`, `tests/unit/architecture`. Each update adds `azure_ai_search` beside `rag_retrieval`; none removes anything in this task. Generate the new knob-schema golden with the regeneration switch that test file documents, then diff it by eye: it must list `index` and `search_mode` and must not list `provider_config`. Add `"azure_ai_search": Determinism.EXTERNAL_CALL` to `boundary_expectations.py`, the matching arm in `explain.py`, and a display name `"Azure AI Search (RAG)"` in the frontend map with its test.

- [ ] **Step 5: Re-pin the source hash and commit**

```bash
git add -- src/elspeth/contracts/azure_ai_search.py src/elspeth/plugins/transforms/azure/ai_search.py tests/unit/plugins/transforms/azure/test_ai_search.py tests/golden/web/catalog/knob_schema/transform__azure_ai_search.json
git commit -m "feat(azure): azure_ai_search RAG retrieval transform on runtime_preflight" -- src tests
```

---

### Task 4: `azure_search_profiles` setting

**Files:**
- Modify: `src/elspeth/web/plugin_policy/profiles.py` (settings class, `RuntimeWebPluginConfig`), `src/elspeth/web/config.py`
- Test: `tests/unit/web/plugin_policy/test_profiles.py`, `tests/unit/web/test_config.py`

**Interfaces:**
- Produces: `AzureSearchProfileSettings(BaseModel)` with `alias: str`, `endpoint: str`, `auth: Literal["managed_identity", "api_key"]`, `client_id: str | None`, `credential_ref: str | None`, `indexes: tuple[str, ...] | Literal["any"]` (required), `api_version: str | None`; method `admits_index(self, index: str) -> bool`.
- Produces: `WebSettings.azure_search_profiles` and `RuntimeWebPluginConfig.azure_search_profiles`, both `tuple[AzureSearchProfileSettings, ...]`, runtime copy sorted by alias.

- [ ] **Step 1: Write the failing tests** (in `test_profiles.py`)

```python
_SEARCH_MI = {"alias": "policies", "endpoint": "https://svc-a.search.windows.net", "auth": "managed_identity", "client_id": "11111111-2222-3333-4444-555555555555", "indexes": ["approved-documents"]}
_SEARCH_KEY = {"alias": "contracts", "endpoint": "https://svc-b.search.windows.net", "auth": "api_key", "credential_ref": "SEARCH_B_KEY", "indexes": "any"}


def test_search_profile_index_pin_is_mandatory() -> None:
    for bad in ({}, {"indexes": []}, {"indexes": None}):
        payload = {k: v for k, v in _SEARCH_MI.items() if k != "indexes"} | bad
        with pytest.raises(ValidationError):
            AzureSearchProfileSettings(**payload)


def test_search_profile_any_is_an_explicit_opt_out() -> None:
    assert AzureSearchProfileSettings(**_SEARCH_KEY).admits_index("whatever") is True
    pinned = AzureSearchProfileSettings(**_SEARCH_MI)
    assert pinned.admits_index("approved-documents") is True
    assert pinned.admits_index("other") is False


@pytest.mark.parametrize(
    "override",
    [
        {"auth": "managed_identity", "credential_ref": "X", "client_id": None},
        {"auth": "api_key", "client_id": "abc", "credential_ref": "SEARCH_B_KEY"},
        {"auth": "api_key", "credential_ref": None},
        {"endpoint": "http://svc-a.search.windows.net"},
        {"endpoint": "https://evil.example.com"},
        {"indexes": ["bad name"]},
    ],
)
def test_search_profile_rejects_inconsistent_bindings(override: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        AzureSearchProfileSettings(**(_SEARCH_MI | override))


def test_search_profile_aliases_must_be_unique() -> None:
    with pytest.raises(ValidationError, match="aliases must be unique"):
        _settings(azure_search_profiles=(_SEARCH_MI, _SEARCH_MI))


def test_runtime_config_carries_search_profiles_sorted() -> None:
    runtime = RuntimeWebPluginConfig.from_settings(_settings(azure_search_profiles=(_SEARCH_MI, _SEARCH_KEY)))
    assert [p.alias for p in runtime.azure_search_profiles] == ["contracts", "policies"]
```

The `https://evil.example.com` case is managed identity only; an `api_key` profile may name any HTTPS host, as the provider allows today. Extend the existing settings/runtime field-parity test near line 430 with `"azure_search_profiles"`. In `test_config.py`, add a case beside the Textract one proving `ELSPETH_WEB__AZURE_SEARCH_PROFILES` decodes from a JSON array and that a malformed value fails start-up without echoing the value.

- [ ] **Step 2: Run to verify they fail**

Run: `pytest tests/unit/web/plugin_policy/test_profiles.py -n 0 -q -k search_profile`
Expected: `ImportError: cannot import name 'AzureSearchProfileSettings'`.

- [ ] **Step 3: Implement**

```python
class AzureSearchProfileSettings(BaseModel):
    """Operator-owned binding for one Web-authorable Azure AI Search service."""

    model_config = ConfigDict(frozen=True, extra="forbid", hide_input_in_errors=True)

    alias: str
    endpoint: str = Field(repr=False)
    auth: Literal["managed_identity", "api_key"]
    client_id: str | None = Field(default=None, repr=False)
    credential_ref: str | None = Field(default=None, repr=False)
    indexes: tuple[str, ...] | Literal["any"]
    api_version: str | None = None

    @field_validator("alias")
    @classmethod
    def _validate_alias(cls, value: str) -> str:
        validate_profile_alias(value)
        return value

    @field_validator("indexes")
    @classmethod
    def _validate_indexes(cls, value: tuple[str, ...] | str) -> tuple[str, ...] | str:
        if value == "any":
            return value
        if not value:
            raise ValueError('indexes must list at least one index, or be the literal "any"')
        for name in value:
            if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", name):
                raise ValueError("indexes entries must be Azure AI Search index names")
        return value

    @model_validator(mode="after")
    def _validate_binding(self) -> Self:
        if self.auth == "managed_identity" and self.credential_ref is not None:
            raise ValueError("managed_identity profiles must not set credential_ref")
        if self.auth == "api_key" and (self.credential_ref is None or self.client_id is not None):
            raise ValueError("api_key profiles require credential_ref and must not set client_id")
        if self.credential_ref is not None:
            validate_secret_name(self.credential_ref, field_name="azure_search_profiles credential_ref")
        AzureSearchProviderConfig(
            endpoint=self.endpoint,
            index="profile-validation",
            api_key=None if self.auth == "managed_identity" else "profile-validation",
            use_managed_identity=self.auth == "managed_identity",
            client_id=self.client_id,
            **({} if self.api_version is None else {"api_version": self.api_version}),
        )
        return self

    def admits_index(self, index: str) -> bool:
        return self.indexes == "any" or index in self.indexes
```

Constructing `AzureSearchProviderConfig` reuses the endpoint, managed-identity-suffix and `api_version` rules instead of restating them; `hide_input_in_errors` keeps the endpoint out of start-up errors. Check the import direction first: `profiles.py` already imports from `elspeth.plugins.transforms.aws`, so importing the provider config from `elspeth.plugins.infrastructure` is within the existing layering; confirm with `tests/unit/architecture` after the change.

Then in `RuntimeWebPluginConfig` add the field and its sorted `from_settings` line; in `web/config.py` add the field beside `aws_textract_profiles`, a uniqueness validator with message `"Azure Search profile aliases must be unique"`, and the name in both `_JSON_ARRAY_FIELDS` (near line 1354) and the safe-error-path set (near line 1501).

- [ ] **Step 4: Run to green**

Run: `pytest tests/unit/web/plugin_policy tests/unit/web/test_config.py -q`
Expected: exit 0.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(web): azure_search_profiles operator setting with a mandatory index pin" -- src/elspeth/web/plugin_policy/profiles.py src/elspeth/web/config.py tests/unit/web/plugin_policy/test_profiles.py tests/unit/web/test_config.py
```

---

### Task 5: The profile resolver

**Files:**
- Modify: `src/elspeth/web/plugin_policy/profiles.py`
- Test: `tests/unit/web/plugin_policy/test_profiles.py`, `tests/unit/web/plugin_policy/test_availability.py`, `tests/unit/web/plugin_policy/test_validation.py`, `tests/unit/web/catalog/test_policy_view.py`

**Interfaces:**
- Consumes: `AzureSearchProfileSettings` (Task 4), `AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES` and the `azure_ai_search` schema (Task 3).
- Produces: `_AzureSearchProfileResolver` implementing `OperatorProfileResolver`, registered on `PluginId("transform", "azure_ai_search")` when `settings.azure_search_profiles` is non-empty.

- [ ] **Step 1: Write the failing tests**

```python
def _search_registry(**overrides: object) -> tuple[OperatorProfileRegistry, PluginId]:
    defaults: dict[str, object] = {
        "plugin_allowlist": ("transform:azure_ai_search",),
        "azure_search_profiles": (_SEARCH_MI, _SEARCH_KEY),
        "server_secret_allowlist": ("SEARCH_B_KEY",),
    }
    defaults.update(overrides)
    runtime = RuntimeWebPluginConfig.from_settings(_settings(**defaults))
    registry = OperatorProfileRegistry(
        policy=compile_web_plugin_policy(registry=_isolated_manager_with_llm_source(), settings=runtime), settings=runtime
    )
    return registry, PluginId("transform", "azure_ai_search")


_SEARCH_AUTHORED: dict[str, object] = {"output_prefix": "policy", "query_field": "question", "index": "approved-documents", "schema": {"mode": "observed"}}


def test_search_public_schema_hides_every_private_binding() -> None:
    registry, plugin_id = _search_registry()
    public = registry.public_schema(plugin_id, create_catalog_service().get_schema("transform", "azure_ai_search"), available_aliases=("contracts", "policies"))
    properties = public.json_schema["properties"]
    assert properties["profile"]["enum"] == ["contracts", "policies"]
    assert not set(properties) & AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES
    assert {"index", "search_mode", "content_field", "select", "filter", "output_prefix", "query_field"} <= set(properties)
    assert public.json_schema["additionalProperties"] is False
    assert "profile" in public.json_schema["required"]
    assert "approved-documents" in properties["profile"]["description"]
    assert any("RAG" in hint for hint in public.composer_hints)


def test_search_lowering_injects_managed_identity_binding() -> None:
    registry, plugin_id = _search_registry()
    lowered = registry.lower_options(plugin_id, alias="policies", safe_options=dict(_SEARCH_AUTHORED))
    executable = deep_thaw(lowered.executable_options)
    assert executable["endpoint"] == "https://svc-a.search.windows.net"
    assert executable["use_managed_identity"] is True
    assert executable["client_id"] == "11111111-2222-3333-4444-555555555555"
    assert "api_key" not in executable
    assert deep_thaw(lowered.audit_safe_options) == {"profile": "policies", **_SEARCH_AUTHORED}


def test_search_lowering_injects_a_server_secret_reference_not_a_value() -> None:
    registry, plugin_id = _search_registry()
    lowered = registry.lower_options(plugin_id, alias="contracts", safe_options={**_SEARCH_AUTHORED, "index": "anything"})
    executable = deep_thaw(lowered.executable_options)
    assert executable["api_key"] == {"secret_ref": "SEARCH_B_KEY", "secret_scope": "server"}
    assert "use_managed_identity" not in executable


def test_two_profiles_lower_to_two_services() -> None:
    registry, plugin_id = _search_registry()
    a = deep_thaw(registry.lower_options(plugin_id, alias="policies", safe_options=dict(_SEARCH_AUTHORED)).executable_options)
    b = deep_thaw(registry.lower_options(plugin_id, alias="contracts", safe_options=dict(_SEARCH_AUTHORED)).executable_options)
    assert a["endpoint"] != b["endpoint"]


@pytest.mark.parametrize("option", sorted(AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES))
def test_search_lowering_refuses_an_authored_private_option(option: str) -> None:
    registry, plugin_id = _search_registry()
    with pytest.raises(ValueError, match="private_profile_option"):
        registry.lower_options(plugin_id, alias="policies", safe_options={**_SEARCH_AUTHORED, option: "x"})


def test_search_lowering_refuses_an_index_outside_the_pin() -> None:
    registry, plugin_id = _search_registry()
    with pytest.raises(ValueError, match="profile_index_not_admitted"):
        registry.lower_options(plugin_id, alias="policies", safe_options={**_SEARCH_AUTHORED, "index": "hr-records"})


def test_search_unknown_alias_is_unavailable() -> None:
    registry, plugin_id = _search_registry()
    with pytest.raises(ValueError, match="profile_unavailable"):
        registry.lower_options(plugin_id, alias="nope", safe_options=dict(_SEARCH_AUTHORED))


def test_search_has_no_silent_default_among_several_sources() -> None:
    registry, plugin_id = _search_registry()
    assert registry.selected_alias(plugin_id, ("contracts", "policies")) is None
    assert registry.selected_alias(plugin_id, ("policies",)) == "policies"


def test_no_profiles_means_no_resolver_and_no_web_availability() -> None:
    registry, plugin_id = _search_registry(azure_search_profiles=())
    assert registry.profile_availability(plugin_id, principal="local:alice", inventory=cast(Any, object())) == ()
```

Match `registry.selected_alias`'s real signature to the Textract test at `test_textract_profile_selection_promotes_only_a_sole_usable_alias` before running. In `test_availability.py`, mirror the Textract availability cases: an `api_key` profile is unusable with `CREDENTIAL_MISSING` when the inventory lacks `SEARCH_B_KEY`; a `managed_identity` profile is unusable with `LOCAL_REQUIREMENT_MISSING` when `importlib.util.find_spec("azure.identity")` is patched to `None`. In `test_validation.py`, add one `validate_plugin_policy` case: a state whose `azure_ai_search` node has no `profile` yields a `profile_unavailable` finding, and one with `profile: policies` plus `endpoint` yields a finding and no lowered state. **Also add a case to that file proving whether `secret_wiring_allowlist` is consulted for a profile-injected `secret_ref`**: build the policy with an empty allowlist and assert the outcome the LLM profile path produces today for its own injected `api_key`. Whichever it is, record it in the runbook in Task 7 (an operator either needs a wiring rule for `SEARCH_B_KEY` → `transform` / `azure_ai_search` / `api_key`, or does not).

- [ ] **Step 2: Run to verify they fail**

Expected: `AssertionError` in the schema test (no resolver, so the full schema with `endpoint` is returned) and `KeyError`/`ValueError` in the lowering tests.

- [ ] **Step 3: Implement**

Model `_AzureSearchProfileResolver` on `_TextractProfileResolver` method for method. The differences, exactly:

- Author option names are computed, not listed: `tuple(name for name in full_schema.json_schema["properties"] if name not in AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES)`, after the same `isinstance(raw_properties, dict)` guard that raises `malformed_profile_schema`. A new author option added to the plugin later is then web-visible without touching this file, and a new private option must be added to the frozenset to be hidden, which the `test_search_public_schema_hides_every_private_binding` test pins.
- The `profile` property description lists each alias with its indexes: `"policies: approved-documents; contracts: any index"`. When exactly one alias is available and its `indexes` is a tuple, the `index` property also gets `"enum": list(profile.indexes)`.
- `composer_hints` are `("This is the Azure RAG plugin: select an operator-approved Azure AI Search profile; the server supplies the endpoint and credential.", "Name an index the chosen profile admits.", "Pair it with an llm transform that reads {output_prefix}__rag_context.")`.
- `lower_options`:

```python
    def lower_options(self, alias: str, safe_options: dict[str, object]) -> LoweredPluginConfig:
        try:
            profile = self._profiles[alias]
        except KeyError:
            raise ValueError("profile_unavailable") from None
        if set(safe_options) & AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES:
            raise ValueError("private_profile_option")
        index = safe_options.get("index")
        if type(index) is not str or not profile.admits_index(index):
            raise ValueError("profile_index_not_admitted")
        executable: dict[str, object] = {**safe_options, "endpoint": profile.endpoint}
        if profile.api_version is not None:
            executable["api_version"] = profile.api_version
        if profile.auth == "managed_identity":
            executable["use_managed_identity"] = True
            if profile.client_id is not None:
                executable["client_id"] = profile.client_id
        else:
            executable["api_key"] = {"secret_ref": profile.credential_ref, "secret_scope": "server"}
        return LoweredPluginConfig(
            executable_options=MappingProxyType(executable),
            audit_safe_options=MappingProxyType({"profile": alias, **safe_options}),
        )
```

- `profile_availability`: for `api_key` profiles, `usable = inventory.has_server_ref(profile.credential_ref)`, `credential_scope="server"`, `reason=CREDENTIAL_MISSING` when false, `generation` = sha256 over `{endpoint, auth, credential_ref, indexes, api_version, server_generation}`; for `managed_identity` profiles, `usable=True`, `credential_scope=None`, `generation` over `{endpoint, auth, client_id, indexes, api_version}`. `check_local_requirements` returns unavailable with `LOCAL_REQUIREMENT_MISSING` for a managed-identity alias when `importlib.util.find_spec("azure.identity") is None`.
- `selected_alias`: `usable_aliases[0] if len(usable_aliases) == 1 else None`.

`"profile_index_not_admitted"` is a new lowering error code. Find where `validation.py` turns `private_profile_option` into a finding message and add the new code beside it with the message `"Index is not admitted by the selected Azure AI Search profile; choose an index the profile lists."` Do not echo the index or the profile's list.

Register it in `OperatorProfileRegistry.__init__`:

```python
        if settings.azure_search_profiles:
            self._resolvers[PluginId("transform", "azure_ai_search")] = _AzureSearchProfileResolver(settings.azure_search_profiles)
```

Add an exact-type arm for `_AzureSearchProfileResolver` in `public_summary` (usage text and a profile-shaped `example_use` with `profile: <alias>` and `index:`) and in the assistance method beside the Textract arm near line 1308, both describing the plugin as Azure RAG.

- [ ] **Step 4: Run to green, then mutate**

Run: `pytest tests/unit/web/plugin_policy tests/unit/web/catalog -q` to exit 0. Mutants that must each go red: delete the `private_profile_option` check; delete the `admits_index` check; change `"secret_scope": "server"` to `"user"`; drop `endpoint` from the private frozenset.

- [ ] **Step 5: Commit**

```bash
git commit -m "feat(web): Azure AI Search operator profiles make the web surface profile-only" -- src/elspeth/web/plugin_policy tests/unit/web/plugin_policy tests/unit/web/catalog
```

---

### Task 6: One path to Azure Search

Remove `azure_search` from `rag_retrieval` and delete the refusal that guarded it. Review this task hardest.

**Files:**
- Modify: `src/elspeth/plugins/transforms/rag/config.py`, `src/elspeth/plugins/transforms/rag/transform.py`, `src/elspeth/plugins/infrastructure/clients/retrieval/azure_search.py`, `src/elspeth/web/provider_config_policy.py`, `src/elspeth/web/composer/tools/_common.py`, `src/elspeth/web/composer/tools/sessions.py`, `src/elspeth/web/composer/tools/transforms.py`, `src/elspeth/web/execution/_validation_materialization.py`, `src/elspeth/web/execution/validation.py`, `src/elspeth/web/execution/service.py`
- Test: `tests/unit/web/test_provider_config_policy.py`, `tests/unit/web/composer/test_tools.py`, `tests/unit/web/execution/test_validation.py`, `tests/unit/web/execution/test_validation_materialization.py`, `tests/unit/web/execution/test_service.py`, `tests/unit/plugins/transforms/rag/test_config.py`, `tests/unit/plugins/transforms/rag/test_transform.py`, `tests/integration/plugins/test_rag_retrieval_providers_live.py`, `tests/unit/core/rate_limit/test_registry.py`, `tests/golden/web/catalog/knob_schema/transform__rag_retrieval.json`

- [ ] **Step 1: Write the replacement guarantees first**

Before deleting anything, add the three layer proofs the design requires, each driving a web-authored `azure_ai_search` node with `_SEARCH_MI` configured:

1. `tests/unit/web/composer/test_tools.py`: `upsert_node` with `{"profile": "policies", "index": "approved-documents", "endpoint": "https://evil.example.com", ...}` returns a failure result and leaves state unchanged; the same call without `endpoint` succeeds. Parametrize the refused key over `AZURE_AI_SEARCH_PRIVATE_BINDING_OPTION_NAMES`.
2. `tests/unit/web/execution/test_validation.py`: `validate_pipeline` on a state carrying such a node returns a failed report whose failing check is the plugin-policy check, and no node reaches runtime loading.
3. `tests/unit/web/execution/test_service.py`: `execute` on such a state raises before provider construction; assert `AzureSearchProvider` was never instantiated by patching the class in `elspeth.plugins.transforms.azure.ai_search` and asserting `assert_not_called()`.

Add one more to each of 2 and 3: a node with **no** `profile` is refused the same way. Use the fixtures those files already use for Textract-profiled nodes; find them with `grep -n "aws_textract" <file>`.

Run them: all must pass **now**, before the deletion, because Task 5 already made the surface profile-only. If any fails, the profile machinery does not cover that layer and the deletion below must not proceed: stop and report.

- [ ] **Step 2: Measure the trained-operator path**

`PluginAvailabilitySnapshot.for_trained_operator` short-circuits several profile checks. Write a test in `tests/unit/web/plugin_policy/test_validation.py` that runs `validate_plugin_policy` with a trained-operator snapshot on an `azure_ai_search` node with raw `endpoint` and `use_managed_identity: true`, and assert what it does. Record the result in the commit message. The local MCP is the operator's own trust domain (the snapshot's docstring says web requests must never reach it), so admitting raw options there is the expected outcome; the test exists so that is a measured fact and not an assumption.

- [ ] **Step 3: Remove `azure_search` from `rag_retrieval`**

In `config.py`: `RetrievalProviderName = Literal["chroma"]`; delete the `azure_search` import and registry entry from `_get_providers` and its comment. In `transform.py`: delete the `azure_search` arm of `_configured_collection_name`; change `usage_when_to_use` to name Chroma only and add `"Use azure_ai_search for Azure AI Search."`; replace `example_use` and `probe_config` with the Chroma form:

```python
    @classmethod
    def probe_config(cls) -> dict[str, Any]:
        """Minimal no-network config for the ADR-009 forward invariant."""
        return {
            "output_prefix": "policy",
            "query_field": "rag_probe_query",
            "provider": "chroma",
            "provider_config": {"collection": "invariant-probe", "mode": "ephemeral"},
            "schema": {"mode": "observed"},
        }
```

Confirm `mode: ephemeral` is a real `ChromaSearchProviderConfig` mode by reading that class; if the no-disk mode has another name, use it. Remove the two Azure lines from `get_agent_assistance`. In `azure_search.py` delete `check_readiness` and `_readiness_get` (their last caller is gone) and the `TestAzureSearchProviderReadiness` class that tested them; `TestRuntimePreflightProbe` from Task 2 covers the same ground.

Move the Azure cases in `test_config.py` and `test_transform.py` to `test_ai_search.py` where they test behaviour not already covered; delete the rest. `test_rag_retrieval_providers_live.py`: switch its Azure case to the `azure_ai_search` plugin.

- [ ] **Step 4: Delete the refusal**

Delete from `provider_config_policy.py`: `MANAGED_IDENTITY_POLICY_ERROR`, `_provider_config_enables_managed_identity`, `_FALSE_LITERALS`, `_TRUE_LITERALS` (if no other user), `web_rag_provider_config_policy_error`. Then at the six sites:

- `_common.py:2194`: `_validate_transform_provider_config_policy` keeps only the LLM retry-budget arm.
- `sessions.py:1510` and `transforms.py:754,1732,1944`: call sites are unchanged (they call the helper above); fix the comment at `sessions.py:1510` that names the deleted function.
- `_validation_materialization.py`: delete `validate_managed_identity_policy`, its `__all__` entry, and `CHECK_MANAGED_IDENTITY_POLICY` where it is defined; `validation.py:84,619`: delete the import and the phase call.
- `service.py:2007`: delete the `provider_policy_error` block and the import.

Delete `tests/unit/web/test_provider_config_policy.py` cases for the removed function and the materialization tests for the removed phase. Then prove nothing references the deleted names:

```bash
grep -rn "web_rag_provider_config_policy_error\|MANAGED_IDENTITY_POLICY_ERROR\|CHECK_MANAGED_IDENTITY_POLICY\|validate_managed_identity_policy" src tests docs
```

Expected: no output. Control the instrument: the same grep for `web_llm_base_url_policy_error` must still print matches.

- [ ] **Step 5: Gates and commit**

Regenerate `transform__rag_retrieval.json`; update `fingerprint_baseline.json`, the rate-limit registry test (its Azure case now uses the `azure_ai_search:<host>` key), and `tests/unit/architecture`. Run `pytest tests/unit/web tests/unit/plugins tests/unit/architecture tests/unit/core/rate_limit -q` to exit 0. Re-pin `source_file_hash` for both transforms.

```bash
git commit -m "refactor(rag): one path to Azure Search; profiles replace the managed-identity refusal" -- src tests
```

---

### Task 7: Example, runbook, reference docs

**Files:**
- Modify: `examples/azure_search_rag/settings.yaml`, `examples/azure_search_rag/README.md`, `tests/unit/core/dag/canonical_hash_corpus.json`, `docs/runbooks/azure-container-apps-cold-install.md`, `docs/reference/environment-variables.md`, `CHANGELOG.md`, and the composer teaching skill that lists `rag_retrieval`

- [ ] **Step 1: Switch the example to the new plugin**

Replace the `rag_0` transform's `plugin`, `provider` and `provider_config` with flat options:

```yaml
- name: rag_0
  plugin: azure_ai_search
  input: rag_in
  on_success: output
  on_error: quarantine
  options:
    query_field: question
    output_prefix: policy
    endpoint: ${AZURE_SEARCH_ENDPOINT}
    index: ${AZURE_SEARCH_INDEX}
    api_key: ${AZURE_SEARCH_API_KEY}
    search_mode: hybrid
    content_field: chunk
    id_field: chunk_id
    title_field: title
    vector_field: text_vector
    select: [chunk, chunk_id, title]
    top_k: 3
    min_score: 0.0
    on_no_results: continue
    context_format: numbered
    schema:
      mode: flexible
      fields:
      - 'id: int'
      - 'question: str'
```

Run `elspeth validate --settings examples/azure_search_rag/settings.yaml` with placeholder environment values: exit 0. Negative control: `select: [title]` must exit 1. Re-record the hash corpus pin with `ELSPETH_CANONICAL_CORPUS_RECORD=1`, confirm the diff changes exactly one line, and re-run without the variable to exit 0. In the README replace the `provider_config` table heading with "Options", the managed-identity YAML with the flat form, and the last paragraph with: web-authored pipelines select an operator profile and never name an endpoint.

- [ ] **Step 2: Rewrite runbook section 10**

Keep the role-grant block. Replace the pipeline YAML and the "Web-authored pipelines cannot use it yet" limit with the profile step:

````markdown
Declare the service to the web app as an operator profile. It is not a secret,
so it travels in `extraEnvironment` in the operator-local workload parameter
file; `indexes` is mandatory, and `"any"` is the written decision to open every
index on the service to web authors:

```json
{"name": "ELSPETH_WEB__AZURE_SEARCH_PROFILES",
 "value": "[{\"alias\":\"policies\",\"endpoint\":\"https://<service>.search.windows.net\",\"auth\":\"managed_identity\",\"client_id\":\"<identityClientId>\",\"indexes\":[\"<index>\"]}]"}
```

Add `transform:azure_ai_search` to `ELSPETH_WEB__PLUGIN_ALLOWLIST`. A web author
then selects `profile: policies` and an index the profile lists; the endpoint
and identity never appear in an authored pipeline. Several services are several
entries in the array.
````

Add the secret-wiring sentence that Task 5's measurement dictates. Keep the private-endpoint and 403 limits; the 403 text now reads `pre_flight_failed` (the `RuntimePreflightFailedError` class) instead of `RetrievalNotReadyError`. Run `pytest tests/unit/web/test_azure_container_apps_runbook_contract.py tests/unit/docs -q` to exit 0: the `bash -n` gate does not parse the `json` fence, so validate that fence with `python -c "import json,sys; json.loads(sys.stdin.read())"`.

- [ ] **Step 3: Reference docs and changelog**

`docs/reference/environment-variables.md`: add `ELSPETH_WEB__AZURE_SEARCH_PROFILES` beside the Textract profiles entry with the field list from Task 4 and the mandatory-pin rule. `CHANGELOG.md`: confirm the target section with John before editing (release 0.8.1 is the working assumption), then add: new `azure_ai_search` transform; `azure_search` provider removed from `rag_retrieval` (breaking for YAML that used it); `azure_search_profiles` setting. Update the composer teaching skill's plugin list so the planner is told `azure_ai_search` is the Azure RAG plugin; find it with `grep -rln rag_retrieval .agents src/elspeth/web/composer`.

- [ ] **Step 4: Commit**

```bash
git commit -m "docs: azure_ai_search example, ACA profile step and environment reference" -- examples docs CHANGELOG.md tests/unit/core/dag/canonical_hash_corpus.json
```

---

### Task 8: Pre-merge gate

- [ ] **Step 1:** `scripts/branch-safety-check.sh --intent merge --base release/0.8.1`: FAIL=0.
- [ ] **Step 2:** Compare the lint corpus against the base commit the way the first commit on this branch did (normalize `path:line:col`, `comm -13`). No new finding outside `R_TB_SUPPRESSED` lines for functions this plan edited; report any that appear.
- [ ] **Step 3:** Check host load and that no other suite is running, then `scripts/full-suite-gate.sh --execute --detach`; poll the printed `.done` path and read `summary.txt`. `frozen=NO` means the run is not evidence. For a red in `e2e/recovery`, `integration/pipeline` or `unit/engine/orchestrator`, re-run the id with `-n 0` and diff against the base commit before attributing it.
- [ ] **Step 4:** Frontend: run the unit suite for `pluginDisplayName`.
- [ ] **Step 5:** Report to John with the summary file's contents. Do not merge, push, or stage signatures.
