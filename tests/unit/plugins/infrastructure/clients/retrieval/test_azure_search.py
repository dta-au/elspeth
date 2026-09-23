"""Tests for Azure AI Search provider."""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock, patch

import httpx
import pytest
import respx

from elspeth.contracts.call_mode import ArchivedCallRequestEvidence, ReplayCallEvidence, ReplaySSRFRequest
from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.enums import CallStatus, CallType, RunMode
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.plugins.infrastructure.clients.retrieval.azure_search import (
    AzureSearchProvider,
    AzureSearchProviderConfig,
)
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk
from tests.fixtures.mock_audit import mock_audit_authority, mock_item_audit_authority


@dataclass
class _FakeCall:
    call_id: str


@dataclass
class _FakeExecutionRecorder:
    call_indices: dict[str, int] = field(default_factory=dict)
    operation_call_indices: dict[str, int] = field(default_factory=dict)
    recorded_calls: list[dict[str, Any]] = field(default_factory=list)

    def allocate_call_index(self, state_id: str, *, member_token: WorkerMembershipToken, work_item: TokenWorkItem) -> int:
        index = self.call_indices.get(state_id, 0)
        self.call_indices[state_id] = index + 1
        return index

    def allocate_operation_call_index(self, operation_id: str, *, coordination_token: CoordinationToken) -> int:
        index = self.operation_call_indices.get(operation_id, 0)
        self.operation_call_indices[operation_id] = index + 1
        return index

    def record_call(self, **kwargs: Any) -> _FakeCall:
        self.recorded_calls.append(kwargs)
        return _FakeCall(call_id=f"call-{len(self.recorded_calls)}")

    def record_operation_call(self, **kwargs: Any) -> _FakeCall:
        return self.record_call(**kwargs)


@dataclass
class _TelemetrySink:
    events: list[Any] = field(default_factory=list)

    def __call__(self, event: Any) -> None:
        self.events.append(event)


@dataclass
class _FakeAzureCredential:
    token: str = "managed-identity-token-123"
    error: BaseException | None = None
    scopes: list[tuple[str, ...]] = field(default_factory=list)
    close_calls: int = 0

    def get_token(self, *scopes: str) -> SimpleNamespace:
        self.scopes.append(scopes)
        if self.error is not None:
            raise self.error
        return SimpleNamespace(token=self.token)

    def close(self) -> None:
        self.close_calls += 1


class _FakeLimiter:
    def acquire(self, weight: int = 1, timeout: float | None = None) -> None:
        pass


class TestAzureSearchProviderConfig:
    def test_requires_https(self):
        with pytest.raises(ValueError, match="HTTPS"):
            AzureSearchProviderConfig(
                endpoint="http://test.search.windows.net",
                index="test",
                api_key="key",
            )

    @pytest.mark.parametrize(
        "endpoint",
        [
            "https://10.0.0.1",
            "https://169.254.169.254",
        ],
    )
    def test_rejects_blocked_literal_ip_endpoints(self, endpoint: str) -> None:
        with pytest.raises(ValueError, match=r"blocked|metadata|private|loopback|link-local"):
            AzureSearchProviderConfig(
                endpoint=endpoint,
                index="test",
                api_key="key",
            )

    def test_auth_mutual_exclusion(self):
        with pytest.raises(ValueError, match="only one"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="test",
                api_key="key",
                use_managed_identity=True,
            )

    def test_auth_required(self):
        with pytest.raises(ValueError, match="either"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="test",
            )

    def test_managed_identity_rejects_non_azure_search_hostname(self) -> None:
        with pytest.raises(ValueError, match=r"managed identity.*search\.windows\.net"):
            AzureSearchProviderConfig(
                endpoint="https://attacker.example.com",
                index="test",
                use_managed_identity=True,
            )

    def test_api_key_auth_preserves_existing_public_hostname_support(self) -> None:
        config = AzureSearchProviderConfig(
            endpoint="https://search-proxy.example.com",
            index="test",
            api_key="key",
        )

        assert config.endpoint == "https://search-proxy.example.com"

    def test_semantic_requires_config(self):
        with pytest.raises(ValueError, match="semantic_config"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="test",
                api_key="key",
                search_mode="semantic",
            )

    def test_index_name_validation(self):
        with pytest.raises(ValueError, match="alphanumeric"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="bad/path/../traversal",
                api_key="key",
            )

    def test_valid_index_names(self):
        for name in ["my-index", "index_v2", "MyIndex123"]:
            config = AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index=name,
                api_key="key",
            )
            assert config.index == name


class TestAzureSearchProviderSearch:
    def _make_provider(self, search_mode: str = "hybrid") -> AzureSearchProvider:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
            search_mode=search_mode,
        )
        execution = _FakeExecutionRecorder()
        telemetry_emit = _TelemetrySink()
        return AzureSearchProvider(
            config=config,
            execution=execution,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
        )

    def test_returns_retrieval_chunks(self):
        provider = self._make_provider()
        mock_response = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
                {"@search.score": 3.0, "content": "Result 2", "id": "doc2"},
            ]
        }
        with patch.object(provider, "_execute_search", return_value=mock_response):
            chunks = provider.search(
                "test query",
                top_k=5,
                min_score=0.0,
                **mock_item_audit_authority(),
                state_id="state-1",
                token_id="token-1",
            )
        assert len(chunks) == 2
        assert all(isinstance(c, RetrievalChunk) for c in chunks)
        assert chunks[0].score >= chunks[1].score

    def test_min_score_filtering(self):
        # Use keyword mode: range (0, 50). Score 30.0 -> 0.6, score 2.0 -> 0.04.
        provider = self._make_provider("keyword")
        mock_response = {
            "value": [
                {"@search.score": 30.0, "content": "High", "id": "doc1"},
                {"@search.score": 2.0, "content": "Low", "id": "doc2"},
            ]
        }
        with patch.object(provider, "_execute_search", return_value=mock_response):
            chunks = provider.search(
                "test",
                top_k=5,
                min_score=0.5,
                **mock_item_audit_authority(),
                state_id="state-1",
                token_id=None,
            )
        assert len(chunks) == 1
        assert chunks[0].content == "High"

    def test_malformed_json_raises_retrieval_error(self):
        provider = self._make_provider()
        with patch.object(provider, "_execute_search", side_effect=RetrievalError("bad json", retryable=False)):
            with pytest.raises(RetrievalError) as exc_info:
                provider.search("test", top_k=5, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None)
            assert not exc_info.value.retryable

    def test_server_error_raises_retryable(self):
        provider = self._make_provider()
        with patch.object(
            provider,
            "_execute_search",
            side_effect=RetrievalError("server error", retryable=True, status_code=500),
        ):
            with pytest.raises(RetrievalError) as exc_info:
                provider.search("test", top_k=5, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None)
            assert exc_info.value.retryable
            assert exc_info.value.status_code == 500


class TestScoreNormalization:
    def _make_provider(self, search_mode: str = "hybrid") -> AzureSearchProvider:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
            search_mode=search_mode,
        )
        return AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="run-1",
            telemetry_emit=_TelemetrySink(),
        )

    def test_keyword_mid_range(self):
        provider = self._make_provider("keyword")
        assert provider._normalize_score(25.0) == pytest.approx(0.5)

    def test_keyword_zero(self):
        provider = self._make_provider("keyword")
        assert provider._normalize_score(0.0) == 0.0

    def test_keyword_max(self):
        provider = self._make_provider("keyword")
        assert provider._normalize_score(50.0) == 1.0

    def test_keyword_exceeds_max_clamped(self):
        provider = self._make_provider("keyword")
        assert provider._normalize_score(200.0) == 1.0

    def test_negative_score_clamped_to_zero(self):
        provider = self._make_provider("keyword")
        assert provider._normalize_score(-5.0) == 0.0

    def test_vector_already_normalized(self):
        provider = self._make_provider("vector")
        assert provider._normalize_score(0.75) == pytest.approx(0.75)

    def test_semantic_range(self):
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
            search_mode="semantic",
            semantic_config="my-semantic-config",
        )
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="run-1",
            telemetry_emit=_TelemetrySink(),
        )
        assert provider._normalize_score(2.0) == pytest.approx(0.5)

    def test_nan_score_raises_retrieval_error(self):
        provider = self._make_provider("keyword")
        with pytest.raises(RetrievalError, match="non-finite"):
            provider._normalize_score(float("nan"))

    def test_infinity_score_raises_retrieval_error(self):
        provider = self._make_provider("keyword")
        with pytest.raises(RetrievalError, match="non-finite"):
            provider._normalize_score(float("inf"))

    def test_negative_infinity_raises_retrieval_error(self):
        provider = self._make_provider("keyword")
        with pytest.raises(RetrievalError, match="non-finite"):
            provider._normalize_score(float("-inf"))


class TestBuildRequestBody:
    def _make_provider(self, search_mode: str = "hybrid", **overrides: Any) -> AzureSearchProvider:
        config_data = {
            "endpoint": "https://test.search.windows.net",
            "index": "test-index",
            "api_key": "test-key",
            "search_mode": search_mode,
        }
        config_data.update(overrides)
        if search_mode == "semantic":
            config_data.setdefault("semantic_config", "my-semantic-config")
        config = AzureSearchProviderConfig(**config_data)
        return AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="run-1",
            telemetry_emit=_TelemetrySink(),
        )

    def test_keyword_body(self):
        provider = self._make_provider("keyword")
        body = provider._build_request_body("test query", top_k=5)
        assert body["search"] == "test query"
        assert body["top"] == 5
        assert "vectorQueries" not in body

    def test_vector_body(self):
        provider = self._make_provider("vector")
        body = provider._build_request_body("test query", top_k=3)
        assert "search" not in body
        assert body["vectorQueries"][0]["text"] == "test query"
        assert body["vectorQueries"][0]["k"] == 3

    def test_hybrid_body(self):
        provider = self._make_provider("hybrid")
        body = provider._build_request_body("test query", top_k=5)
        assert body["search"] == "test query"
        assert "vectorQueries" in body

    def test_semantic_body(self):
        provider = self._make_provider("semantic")
        body = provider._build_request_body("test query", top_k=5)
        assert body["queryType"] == "semantic"
        assert body["semanticConfiguration"] == "my-semantic-config"


class TestParseResponse:
    def _make_provider(self) -> AzureSearchProvider:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
        )
        return AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="run-1",
            telemetry_emit=_TelemetrySink(),
        )

    def test_missing_value_key_raises(self):
        provider = self._make_provider()
        with pytest.raises(RetrievalError, match="missing 'value'"):
            provider._parse_response({}, min_score=0.0)

    def test_skips_items_without_score(self):
        provider = self._make_provider()
        response = {"value": [{"content": "text", "id": "doc1"}]}
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert chunks == []
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "missing_score"

    def test_skips_items_without_content(self):
        provider = self._make_provider()
        response = {"value": [{"@search.score": 5.0, "id": "doc1"}]}
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert chunks == []
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "missing_content"

    def test_skips_items_without_id(self):
        """Items with no id are skipped — no fabricated "unknown" source_id."""
        provider = self._make_provider()
        response = {
            "value": [
                {"@search.score": 5.0, "content": "text", "id": "doc1"},
                {"@search.score": 5.0, "content": "text", "@search.documentId": "doc2"},
                {"@search.score": 5.0, "content": "text"},  # no id at all
            ]
        }
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert len(chunks) == 2
        assert {c.source_id for c in chunks} == {"doc1", "doc2"}
        assert len(skipped) == 1
        assert skipped[0]["reason"] == "missing_id"

    @pytest.mark.parametrize(
        "bad_score,desc",
        [
            ("high", "string"),
            (True, "bool_true"),
            (False, "bool_false"),
            ([1.0], "list"),
            ({"v": 1.0}, "dict"),
        ],
    )
    def test_non_numeric_score_skipped_at_tier3_boundary(self, bad_score, desc):
        """Tier 3 boundary: non-numeric @search.score must be skipped, not crash."""
        provider = self._make_provider()
        response = {
            "value": [
                {"@search.score": bad_score, "content": "text", "id": "doc1"},
                {"@search.score": 5.0, "content": "good", "id": "doc2"},
            ]
        }
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        # Bad score item skipped; good item still returned
        assert len(chunks) == 1
        assert chunks[0].source_id == "doc2"
        assert any(s["reason"] == "invalid_score_type" for s in skipped)

    def test_results_sorted_by_descending_score(self):
        provider = self._make_provider()
        response = {
            "value": [
                {"@search.score": 1.0, "content": "low", "id": "d1"},
                {"@search.score": 40.0, "content": "high", "id": "d2"},
                {"@search.score": 10.0, "content": "mid", "id": "d3"},
            ]
        }
        chunks, _ = provider._parse_response(response, min_score=0.0)
        assert chunks[0].score >= chunks[1].score >= chunks[2].score

    @pytest.mark.parametrize(
        "bad_value,desc",
        [
            ({"id": "doc1"}, "dict"),
            ("a string", "str"),
            (42, "int"),
        ],
    )
    def test_non_list_value_raises(self, bad_value, desc):
        """Tier 3: top-level 'value' present but not an array → RetrievalError, not AttributeError.

        Regression for elspeth-1b138841d3: a non-array 'value' (object/string)
        previously iterated into `item.get(...)` and raised raw AttributeError,
        bypassing the transform's RetrievalError handling.
        """
        provider = self._make_provider()
        with pytest.raises(RetrievalError, match="must be an array"):
            provider._parse_response({"value": bad_value}, min_score=0.0)

    def test_non_dict_array_members_skipped_at_tier3_boundary(self):
        """Tier 3: non-object array members are recorded skips, not AttributeError crashes.

        Regression for elspeth-1b138841d3. Skip-over-raise is deliberate: it
        matches the file's per-item skip pattern and survives the mixed case
        (one garbage member must not nuke the whole valid page).
        """
        provider = self._make_provider()
        response = {
            "value": [
                "not an object",
                {"@search.score": 5.0, "content": "good", "id": "doc1"},
                123,
            ]
        }
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert len(chunks) == 1
        assert chunks[0].source_id == "doc1"
        invalid_item_skips = [s for s in skipped if s["reason"] == "invalid_item_type"]
        assert len(invalid_item_skips) == 2
        assert {s["type"] for s in invalid_item_skips} == {"str", "int"}


class TestExecuteSearchHTTP:
    """HTTP-level tests for _execute_search using respx to mock httpx transport.

    These tests exercise the real HTTP call path through AuditedHTTPClient,
    unlike the other test classes which mock _execute_search directly.
    """

    SEARCH_URL = "https://test.search.windows.net/indexes/test-index/docs/search?api-version=2024-07-01"
    PINNED_SEARCH_URL = "https://93.184.216.34:443/indexes/test-index/docs/search?api-version=2024-07-01"

    @pytest.fixture(autouse=True)
    def _pin_search_dns(self):
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]):
            yield

    def _make_provider(self) -> AzureSearchProvider:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
        )
        execution = _FakeExecutionRecorder()
        telemetry_emit = _TelemetrySink()
        return AzureSearchProvider(
            config=config,
            execution=execution,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
        )

    def _make_managed_identity_provider(self) -> AzureSearchProvider:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            use_managed_identity=True,
        )
        execution = _FakeExecutionRecorder()
        telemetry_emit = _TelemetrySink()
        return AzureSearchProvider(
            config=config,
            execution=execution,
            run_id="run-1",
            telemetry_emit=telemetry_emit,
        )

    def test_constructor_wires_audited_http_client_context(self) -> None:
        config = AzureSearchProviderConfig(
            endpoint="https://test.search.windows.net",
            index="test-index",
            api_key="test-key",
            request_timeout=12.5,
        )
        execution = _FakeExecutionRecorder()
        telemetry_emit = _TelemetrySink()
        limiter = _FakeLimiter()

        with patch("elspeth.plugins.infrastructure.clients.http.AuditedHTTPClient", autospec=True) as client_cls:
            provider = AzureSearchProvider(
                config=config,
                execution=execution,
                run_id="run-1",
                telemetry_emit=telemetry_emit,
                limiter=limiter,
            )

        client_cls.assert_called_once_with(
            execution=execution,
            state_id="__init__",
            run_id="run-1",
            telemetry_emit=telemetry_emit,
            timeout=12.5,
            limiter=limiter,
            headers={"Content-Type": "application/json", "api-key": "test-key"},
            call_mode_session=None,
            archived_auth_for_replay=False,
            semantic_managed_identity_verify=False,
        )
        assert provider._http_client is client_cls.return_value

    @pytest.mark.parametrize("status_code", [401, 403])
    def test_auth_error_response_raises_non_retryable(self, status_code: int) -> None:
        """HTTP 401/403 both map to RetrievalError(retryable=False)."""
        provider = self._make_provider()

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).respond(status_code=status_code, json={"error": "Auth failed"})

            with pytest.raises(RetrievalError) as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

            assert not exc_info.value.retryable
            assert exc_info.value.status_code == status_code

    def test_429_response_raises_retryable(self) -> None:
        """HTTP 429 maps to RetrievalError(retryable=True)."""

        provider = self._make_provider()

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).respond(status_code=429, json={"error": "Rate limited"})

            with pytest.raises(RetrievalError) as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

            assert exc_info.value.retryable
            assert exc_info.value.status_code == 429

    def test_500_response_raises_retryable(self) -> None:
        """HTTP 500 maps to RetrievalError(retryable=True)."""

        provider = self._make_provider()

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).respond(status_code=500, json={"error": "Internal Server Error"})

            with pytest.raises(RetrievalError) as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

            assert exc_info.value.retryable
            assert exc_info.value.status_code == 500

    def test_200_valid_json_returns_parsed_dict(self) -> None:
        """HTTP 200 with valid JSON returns the parsed response dict."""

        provider = self._make_provider()
        response_body = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
            ]
        }

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).respond(status_code=200, json=response_body)

            result = provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id="t1")

        assert result == response_body

    def test_execute_search_managed_identity_sends_bearer_token(self) -> None:
        provider = self._make_managed_identity_provider()
        response_body = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
            ]
        }
        credential = _FakeAzureCredential()

        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential),
            respx.mock,
        ):
            route = respx.post(self.PINNED_SEARCH_URL).mock(return_value=httpx.Response(200, json=response_body))

            result = provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id="t1")

        assert result == response_body
        assert credential.scopes == [("https://search.azure.com/.default",)]
        assert route.calls.last is not None
        request_headers = route.calls.last.request.headers
        assert request_headers["Authorization"] == "Bearer managed-identity-token-123"
        assert "api-key" not in request_headers

    def test_execute_search_managed_identity_token_failure_raises_retrieval_error(self) -> None:
        from azure.core.exceptions import ClientAuthenticationError

        provider = self._make_managed_identity_provider()
        auth_error = ClientAuthenticationError("DefaultAzureCredential failed")
        credential = _FakeAzureCredential(error=auth_error)

        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential),
            respx.mock,
        ):
            route = respx.post(self.PINNED_SEARCH_URL).mock(return_value=httpx.Response(200, json={"value": []}))

            with pytest.raises(RetrievalError, match="Azure managed identity token acquisition failed") as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id="t1")

        assert not exc_info.value.retryable
        assert exc_info.value.__cause__ is auth_error
        assert not route.called

    def test_managed_identity_credential_is_cached_and_closed(self) -> None:
        provider = self._make_managed_identity_provider()
        response_body = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
            ]
        }
        credential = _FakeAzureCredential()

        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential) as credential_cls,
            respx.mock,
        ):
            respx.post(self.PINNED_SEARCH_URL).mock(return_value=httpx.Response(200, json=response_body))

            provider._execute_search("first query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id="t1")
            provider._execute_search("second query", top_k=5, **mock_item_audit_authority(), state_id="s2", token_id="t2")
            provider.close()

        credential_cls.assert_called_once_with()
        assert len(credential.scopes) == 2
        assert credential.close_calls == 1

    def test_managed_identity_credential_without_close_fails_loudly(self) -> None:
        provider = self._make_managed_identity_provider()
        provider._managed_identity_credential = object()

        with pytest.raises(AttributeError, match="close"):
            provider.close()

    def test_execute_search_updates_audit_context_with_token(self) -> None:
        provider = self._make_provider()
        authority = mock_item_audit_authority()
        response_body = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
            ]
        }

        with (
            patch.object(provider._http_client, "update_call_context", wraps=provider._http_client.update_call_context) as update_context,
            respx.mock,
        ):
            respx.post(self.PINNED_SEARCH_URL).respond(status_code=200, json=response_body)

            result = provider._execute_search("test query", top_k=5, **authority, state_id="state-99", token_id="token-99")

        assert result == response_body
        update_context.assert_called_once_with("state-99", "token-99", **authority)

    def test_execute_search_uses_ssrf_pinned_post_connection(self) -> None:
        provider = self._make_provider()
        response_body = {
            "value": [
                {"@search.score": 5.0, "content": "Result 1", "id": "doc1"},
            ]
        }

        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("93.184.216.34", 0))]),
            respx.mock,
        ):
            ip_route = respx.post(self.PINNED_SEARCH_URL).mock(return_value=httpx.Response(200, json=response_body))
            hostname_route = respx.post(self.SEARCH_URL).mock(return_value=httpx.Response(200, json={"value": []}))

            result = provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

        assert result == response_body
        assert ip_route.called, "Search POST must connect to the validated pinned IP"
        assert not hostname_route.called, "Search POST must not re-resolve/request the hostname URL"

    def test_execute_search_blocks_dns_to_private_before_request(self) -> None:
        provider = self._make_provider()

        with (
            patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]),
            patch.object(provider._http_client, "request_ssrf_safe") as mock_request,
            pytest.raises(RetrievalError, match="blocked by SSRF validation") as exc_info,
        ):
            provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

        assert not exc_info.value.retryable
        mock_request.assert_not_called()

    @pytest.mark.parametrize(
        "transport_error",
        [
            httpx.TimeoutException("timed out"),
            httpx.ConnectError("connection refused"),
        ],
    )
    def test_transport_errors_raise_retryable_retrieval_error(self, transport_error: httpx.HTTPError) -> None:
        """Connection and timeout failures from the HTTP transport are retryable."""
        provider = self._make_provider()

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).mock(side_effect=transport_error)

            with pytest.raises(RetrievalError, match="Search request failed") as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

        assert exc_info.value.retryable
        assert exc_info.value.__cause__ is transport_error

    @pytest.mark.parametrize(
        "response_body",
        ["null", "42", "1.5", "true", "false", '""', '"value"', "[]", '["value"]'],
        ids=["null", "integer", "float", "true", "false", "empty-string", "value-string", "empty-array", "value-array"],
    )
    def test_200_non_object_json_raises_non_retryable(self, response_body: str) -> None:
        provider = self._make_provider()
        try:
            with respx.mock:
                route = respx.post(self.PINNED_SEARCH_URL).respond(
                    status_code=200,
                    text=response_body,
                    headers={"Content-Type": "application/json"},
                )

                with pytest.raises(RetrievalError, match="response must be a JSON object") as exc_info:
                    provider.search("test query", top_k=5, min_score=0.0, **mock_item_audit_authority(), state_id="s1", token_id=None)

                assert exc_info.value.retryable is False
                assert route.call_count == 1
        finally:
            provider.close()

    def test_200_malformed_json_raises_non_retryable(self) -> None:
        """HTTP 200 with unparseable body maps to RetrievalError(retryable=False)."""

        provider = self._make_provider()

        with respx.mock:
            respx.post(self.PINNED_SEARCH_URL).respond(
                status_code=200,
                content=b"this is not json",
                headers={"Content-Type": "text/plain"},
            )

            with pytest.raises(RetrievalError, match="Malformed JSON") as exc_info:
                provider._execute_search("test query", top_k=5, **mock_item_audit_authority(), state_id="s1", token_id=None)

            assert not exc_info.value.retryable


def _provider(**overrides: Any) -> AzureSearchProvider:
    config_data: dict[str, Any] = {
        "endpoint": "https://test.search.windows.net",
        "index": "test-index",
        "api_key": "test-key",
    }
    config_data.update(overrides)
    return AzureSearchProvider(
        config=AzureSearchProviderConfig(**config_data),
        execution=_FakeExecutionRecorder(),
        run_id="run-1",
        telemetry_emit=_TelemetrySink(),
    )


class TestModeScoring:
    """Score ranges per https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking.

    Semantic ranking reports ``@search.rerankerScore`` (0.00 - 4.00) separately from
    ``@search.score``; hybrid ``@search.score`` is an RRF sum where each fused query
    contributes at most 1/(1 + 60), so one text plus one vector query tops out at 2/61.
    """

    def test_semantic_mode_scores_by_reranker_score_not_bm25(self):
        provider = _provider(search_mode="semantic", semantic_config="cfg")
        response = {"value": [{"@search.score": 31.7, "@search.rerankerScore": 1.0, "content": "c", "id": "d1"}]}
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert skipped == []
        assert chunks[0].score == pytest.approx(0.25)

    def test_semantic_mode_orders_by_reranker_score(self):
        provider = _provider(search_mode="semantic", semantic_config="cfg")
        response = {
            "value": [
                {"@search.score": 40.0, "@search.rerankerScore": 0.4, "content": "low", "id": "d1"},
                {"@search.score": 2.0, "@search.rerankerScore": 3.6, "content": "high", "id": "d2"},
            ]
        }
        chunks, _ = provider._parse_response(response, min_score=0.0)
        assert [c.source_id for c in chunks] == ["d2", "d1"]

    def test_semantic_mode_without_reranker_score_is_recorded_skip(self):
        provider = _provider(search_mode="semantic", semantic_config="cfg")
        response = {"value": [{"@search.score": 31.7, "content": "c", "id": "d1"}]}
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert chunks == []
        assert skipped == [{"reason": "missing_score", "id": "d1"}]

    def test_hybrid_top_rrf_score_normalizes_to_one(self):
        provider = _provider(search_mode="hybrid")
        assert provider._normalize_score(2 / 61) == pytest.approx(1.0)

    def test_hybrid_realistic_score_survives_a_mid_threshold(self):
        provider = _provider(search_mode="hybrid")
        response = {"value": [{"@search.score": 1 / 61 + 1 / 63, "content": "c", "id": "d1"}]}
        chunks, _ = provider._parse_response(response, min_score=0.5)
        assert [c.source_id for c in chunks] == ["d1"]


class TestFieldMapping:
    def test_defaults_keep_content_and_id(self):
        config = AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="i", api_key="k")
        assert (config.content_field, config.id_field, config.title_field, config.url_field) == ("content", "id", None, None)
        assert config.select is None
        assert config.filter is None

    def test_custom_content_and_id_fields(self):
        provider = _provider(content_field="chunk", id_field="chunk_id")
        response = {"value": [{"@search.score": 1.0, "chunk": "body", "chunk_id": "c-7", "title": "Doc"}]}
        chunks, skipped = provider._parse_response(response, min_score=0.0)
        assert skipped == []
        assert chunks[0].content == "body"
        assert chunks[0].source_id == "c-7"
        assert dict(chunks[0].metadata) == {"title": "Doc"}

    def test_skip_evidence_names_the_configured_id_field(self):
        provider = _provider(content_field="chunk", id_field="chunk_id")
        response = {"value": [{"@search.score": 1.0, "chunk_id": "c-7"}]}
        _, skipped = provider._parse_response(response, min_score=0.0)
        assert skipped == [{"reason": "missing_content", "id": "c-7"}]

    def test_title_and_url_become_citation_metadata(self):
        provider = _provider(title_field="doc_title", url_field="doc_url")
        response = {"value": [{"@search.score": 1.0, "content": "c", "id": "d1", "doc_title": "Returns", "doc_url": "https://x.example/r"}]}
        chunks, _ = provider._parse_response(response, min_score=0.0)
        metadata = dict(chunks[0].metadata)
        assert metadata["source_name"] == "Returns"
        assert metadata["source_link"] == "https://x.example/r"

    def test_non_string_citation_value_is_omitted_not_fabricated(self):
        provider = _provider(title_field="doc_title")
        response = {"value": [{"@search.score": 1.0, "content": "c", "id": "d1", "doc_title": 7}]}
        chunks, _ = provider._parse_response(response, min_score=0.0)
        assert "source_name" not in dict(chunks[0].metadata)

    @pytest.mark.parametrize("field_name", ["content_field", "id_field", "title_field", "url_field", "vector_field"])
    def test_field_names_must_be_azure_identifiers(self, field_name: str):
        with pytest.raises(ValueError, match=field_name):
            AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="i", api_key="k", **{field_name: "bad name,x"})

    def test_select_and_filter_reach_the_request_body(self):
        provider = _provider(
            select=("chunk", "chunk_id", "title"),
            content_field="chunk",
            id_field="chunk_id",
            filter="category eq 'policy'",
        )
        body = provider._build_request_body("q", top_k=3)
        assert body["select"] == "chunk,chunk_id,title"
        assert body["filter"] == "category eq 'policy'"

    def test_select_and_filter_absent_by_default(self):
        body = _provider()._build_request_body("q", top_k=3)
        assert "select" not in body
        assert "filter" not in body

    def test_select_must_include_the_mapped_fields(self):
        with pytest.raises(ValueError, match="select must include"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="i",
                api_key="k",
                content_field="chunk",
                select=("title",),
            )


class TestManagedIdentityCredentialClass:
    """Managed identity means ManagedIdentityCredential, never the DefaultAzureCredential chain."""

    def test_uses_managed_identity_credential_with_client_id(self):
        provider = _provider(
            api_key=None,
            use_managed_identity=True,
            client_id="11111111-2222-3333-4444-555555555555",
        )
        credential = _FakeAzureCredential()
        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential) as mi_cls,
            patch("azure.identity.DefaultAzureCredential") as default_cls,
        ):
            assert provider._auth_headers() == {"Authorization": "Bearer managed-identity-token-123"}
        mi_cls.assert_called_once_with(client_id="11111111-2222-3333-4444-555555555555")
        default_cls.assert_not_called()

    def test_system_assigned_passes_no_client_id(self):
        provider = _provider(api_key=None, use_managed_identity=True)
        with patch("azure.identity.ManagedIdentityCredential", return_value=_FakeAzureCredential()) as mi_cls:
            provider._auth_headers()
        mi_cls.assert_called_once_with()

    def test_client_id_requires_managed_identity(self):
        with pytest.raises(ValueError, match="client_id requires use_managed_identity"):
            AzureSearchProviderConfig(
                endpoint="https://test.search.windows.net",
                index="i",
                api_key="k",
                client_id="abc",
            )


_PUBLIC_ADDRINFO = ((2, 1, 6, "", ("93.184.216.34", 0)),)


class TestRuntimePreflightProbe:
    """The $count readiness probe as an audited call under an operation parent."""

    _PUBLIC = _PUBLIC_ADDRINFO

    def _probe(self, provider: AzureSearchProvider) -> Any:
        return provider.runtime_preflight(operation_id="op-1", coordination_token=mock_audit_authority()["coordination_token"])

    def test_probe_is_recorded_as_an_operation_call(self) -> None:
        recorder = _FakeExecutionRecorder()
        provider = AzureSearchProvider(
            config=AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="test-index", api_key="test-key"),
            execution=recorder,
            run_id="run-1",
            telemetry_emit=_TelemetrySink(),
        )
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            route = respx.get(host="93.184.216.34").respond(status_code=200, text="42")
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, 42)
        assert recorder.operation_call_indices == {"op-1": 1}
        assert len(recorder.recorded_calls) == 1
        sent = route.calls.last.request
        assert sent.url.path.endswith("/indexes/test-index/docs/$count")
        assert sent.headers["api-key"] == "test-key"

    @pytest.mark.parametrize(("status", "retryable"), [(401, False), (403, False), (429, True), (503, True), (400, False)])
    def test_probe_error_statuses(self, status: int, retryable: bool) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            respx.get(host="93.184.216.34").respond(status_code=status)
            with pytest.raises(RetrievalError) as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is retryable
        assert exc_info.value.status_code == status

    def test_probe_missing_index_is_reachable_with_unknown_count(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            respx.get(host="93.184.216.34").respond(status_code=404)
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, None)
        assert "not found" in result.message

    def test_probe_non_integer_count_is_unknown_not_zero(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            respx.get(host="93.184.216.34").respond(status_code=200, text="<html>secret page</html>")
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, None)
        assert "secret page" not in result.message

    def test_probe_empty_index_reports_zero(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            respx.get(host="93.184.216.34").respond(status_code=200, text="0")
            result = self._probe(provider)
        assert (result.reachable, result.count) == (True, 0)

    def test_probe_refuses_a_private_address_before_any_request(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=[(2, 1, 6, "", ("10.0.0.1", 0))]), respx.mock:
            route = respx.get(host="10.0.0.1")
            with pytest.raises(RetrievalError, match="SSRF") as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is False
        assert not route.called

    def test_probe_sends_the_managed_identity_bearer_token(self) -> None:
        provider = _provider(api_key=None, use_managed_identity=True)
        credential = _FakeAzureCredential()
        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=credential),
            patch("socket.getaddrinfo", return_value=self._PUBLIC),
            respx.mock,
        ):
            route = respx.get(host="93.184.216.34").respond(status_code=200, text="7")
            result = self._probe(provider)
        assert result.count == 7
        assert credential.scopes == [("https://search.azure.com/.default",)]
        sent = route.calls.last.request.headers
        assert sent["Authorization"] == "Bearer managed-identity-token-123"
        assert "api-key" not in sent

    def test_probe_token_failure_is_permanent_and_sends_nothing(self) -> None:
        from azure.core.exceptions import ClientAuthenticationError

        provider = _provider(api_key=None, use_managed_identity=True)
        auth_error = ClientAuthenticationError("no identity on this host")
        with (
            patch("azure.identity.ManagedIdentityCredential", return_value=_FakeAzureCredential(error=auth_error)),
            patch("socket.getaddrinfo", return_value=self._PUBLIC),
            respx.mock,
        ):
            route = respx.get(host="93.184.216.34")
            with pytest.raises(RetrievalError, match="Azure managed identity token acquisition failed") as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is False
        assert exc_info.value.__cause__ is auth_error
        assert not route.called

    def test_probe_transport_failure_is_retryable(self) -> None:
        provider = _provider()
        with patch("socket.getaddrinfo", return_value=self._PUBLIC), respx.mock:
            respx.get(host="93.184.216.34").mock(side_effect=httpx.ConnectError("refused"))
            with pytest.raises(RetrievalError) as exc_info:
                self._probe(provider)
        assert exc_info.value.retryable is True


def test_replay_search_uses_archived_dns_pin_without_live_dns() -> None:
    session = MagicMock()
    session.mode = RunMode.REPLAY
    config = AzureSearchProviderConfig(
        endpoint="https://test.search.windows.net",
        index="test-index",
        api_key="test-key",
    )
    url = "https://test.search.windows.net/indexes/test-index/docs/search?api-version=2024-07-01"
    session.replay_ssrf_request.return_value = ReplaySSRFRequest(
        original_url=url,
        resolved_ip="93.184.216.34",
        host_header="test.search.windows.net",
        port=443,
        path="/indexes/test-index/docs/search?api-version=2024-07-01",
        scheme="https",
        bare_hostname="test.search.windows.net",
    )
    with (
        patch("elspeth.plugins.infrastructure.clients.http.AuditedHTTPClient"),
        patch("socket.getaddrinfo", side_effect=AssertionError("replay resolved live DNS")),
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="replay-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        safe_request = provider._safe_request(url, state_id="state-1")
        headers = provider._auth_headers()
    assert safe_request.resolved_ip == "93.184.216.34"
    assert headers == {"api-key": "test-key"}
    session.replay_ssrf_request.assert_called_once_with(
        original_url=url,
        call_type=CallType.HTTP,
        current_state_id="state-1",
        current_operation_id=None,
    )


def test_replay_managed_identity_defers_credential_to_archived_http_identity() -> None:
    session = MagicMock()
    session.mode = RunMode.REPLAY
    config = AzureSearchProviderConfig(
        endpoint="https://test.search.windows.net",
        index="test-index",
        use_managed_identity=True,
    )
    with (
        patch("elspeth.plugins.infrastructure.clients.http.AuditedHTTPClient") as client,
        patch("azure.identity.ManagedIdentityCredential") as credential,
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="replay-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        assert provider._auth_headers() == {}
    assert client.call_args.kwargs["archived_auth_for_replay"] is True
    credential.assert_not_called()


def test_replay_search_restores_chunks_without_dns_or_http_client() -> None:
    session = MagicMock()
    session.mode = RunMode.REPLAY
    config = AzureSearchProviderConfig(
        endpoint="https://test.search.windows.net",
        index="test-index",
        api_key="test-key",
    )
    url = "https://test.search.windows.net/indexes/test-index/docs/search?api-version=2024-07-01"
    path = "/indexes/test-index/docs/search?api-version=2024-07-01"
    session.replay_ssrf_request.return_value = ReplaySSRFRequest(
        original_url=url,
        resolved_ip="93.184.216.34",
        host_header="test.search.windows.net",
        port=443,
        path=path,
        scheme="https",
        bare_hostname="test.search.windows.net",
    )
    body = json.dumps({"value": [{"id": "doc-1", "content": "archived content", "@search.score": 0.75}]}).encode()
    session.replay_call.return_value = ReplayCallEvidence(
        source_call_id="source-azure-call",
        status=CallStatus.SUCCESS,
        response_data={
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body_size": len(body),
            "body": {"value": [{"id": "doc-1", "content": "archived content", "@search.score": 0.75}]},
            "transport": {
                "body_b64": base64.b64encode(body).decode("ascii"),
                "headers": [["content-type", "application/json"]],
                "request_url": f"https://93.184.216.34{path}",
                "logical_url": url,
            },
        },
        error_data=None,
        latency_ms=1,
    )
    recorder = _FakeExecutionRecorder()
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("replay resolved live DNS")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("HTTP client constructed")),
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=recorder,
            run_id="replay-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        chunks = provider.search("query", 5, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)
    assert [(chunk.source_id, chunk.content) for chunk in chunks] == [("doc-1", "archived content")]
    assert recorder.recorded_calls[0]["source_call_id"] == "source-azure-call"


def test_replay_managed_identity_uses_archived_fingerprint_without_token_or_dns() -> None:
    session = MagicMock()
    session.mode = RunMode.REPLAY
    session.source_run_id = "source-run"
    config = AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="test-index", use_managed_identity=True)
    url = "https://test.search.windows.net/indexes/test-index/docs/search?api-version=2024-07-01"
    path = "/indexes/test-index/docs/search?api-version=2024-07-01"
    session.replay_ssrf_request.return_value = ReplaySSRFRequest(
        original_url=url,
        resolved_ip="93.184.216.34",
        host_header="test.search.windows.net",
        port=443,
        path=path,
        scheme="https",
        bare_hostname="test.search.windows.net",
    )
    session.archived_call_request.return_value = ArchivedCallRequestEvidence(
        source_call_id="source-mi-call",
        request_data={
            "method": "POST",
            "url": url,
            "resolved_ip": "93.184.216.34",
            "headers": {
                "Content-Type": "application/json",
                "Host": "test.search.windows.net",
                "Authorization": f"<fingerprint:{'a' * 64}>",
            },
            "json": {
                "top": 5,
                "search": "query",
                "vectorQueries": [{"kind": "text", "text": "query", "fields": "contentVector", "k": 5}],
            },
        },
    )
    body = json.dumps({"value": [{"id": "doc-1", "content": "archived content", "@search.score": 0.75}]}).encode()
    session.replay_call.return_value = ReplayCallEvidence(
        source_call_id="source-mi-call",
        status=CallStatus.SUCCESS,
        response_data={
            "status_code": 200,
            "headers": {"content-type": "application/json"},
            "body_size": len(body),
            "body": {"value": [{"id": "doc-1", "content": "archived content", "@search.score": 0.75}]},
            "transport": {
                "body_b64": base64.b64encode(body).decode("ascii"),
                "headers": [["content-type", "application/json"]],
                "request_url": f"https://93.184.216.34{path}",
                "logical_url": url,
            },
        },
        error_data=None,
        latency_ms=1,
    )
    recorder = _FakeExecutionRecorder()
    with (
        patch("socket.getaddrinfo", side_effect=AssertionError("replay resolved live DNS")),
        patch("azure.identity.ManagedIdentityCredential", side_effect=AssertionError("replay constructed a credential")),
        patch("elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("HTTP client constructed")),
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=recorder,
            run_id="replay-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        chunks = provider.search("query", 5, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)
    assert [(chunk.source_id, chunk.content) for chunk in chunks] == [("doc-1", "archived content")]
    assert recorder.recorded_calls[0]["source_call_id"] == "source-mi-call"
    request_data = recorder.recorded_calls[0]["request_data"].to_dict()
    assert request_data["replay_credential_origin"] == {
        "source_run_id": "source-run",
        "source_call_id": "source-mi-call",
        "identity": "archived_fingerprint",
    }


@pytest.mark.parametrize("managed_identity", [False, True])
@pytest.mark.parametrize("readiness", [False, True])
def test_verify_missing_source_refuses_before_dns_token_or_http(managed_identity: bool, readiness: bool) -> None:
    session = MagicMock()
    session.mode = RunMode.VERIFY
    session.preflight_verify_http_request.side_effect = RuntimeError("missing source request")
    session.preflight_verify_http_managed_identity.side_effect = RuntimeError("missing source request")
    config = AzureSearchProviderConfig(
        endpoint="https://test.search.windows.net",
        index="test-index",
        api_key=None if managed_identity else "test-key",
        use_managed_identity=managed_identity,
    )
    with (
        patch(
            "elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("HTTP client constructed")
        ) as client_cls,
        patch("socket.getaddrinfo", side_effect=AssertionError("verify resolved DNS before admission")),
        patch("azure.identity.ManagedIdentityCredential", side_effect=AssertionError("verify acquired token before admission")),
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="verify-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        with pytest.raises(RuntimeError, match="missing source request"):
            if readiness:
                provider.runtime_preflight(operation_id="operation-1", coordination_token=MagicMock())
            else:
                provider.search("query", 5, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)
    client_cls.assert_not_called()


@pytest.mark.parametrize("managed_identity", [False, True])
def test_verify_blocked_archived_pin_refuses_before_dns_or_token(managed_identity: bool) -> None:
    session = MagicMock()
    session.mode = RunMode.VERIFY
    source = ArchivedCallRequestEvidence(source_call_id="source-call", request_data={"resolved_ip": "127.0.0.1"})
    session.preflight_verify_http_request.return_value = source
    session.preflight_verify_http_managed_identity.return_value = source
    config = AzureSearchProviderConfig(
        endpoint="https://test.search.windows.net",
        index="test-index",
        api_key=None if managed_identity else "test-key",
        use_managed_identity=managed_identity,
    )
    with (
        patch(
            "elspeth.plugins.infrastructure.clients.http.httpx.Client", side_effect=AssertionError("HTTP client constructed")
        ) as client_cls,
        patch("socket.getaddrinfo", side_effect=AssertionError("verify resolved DNS after blocked archive")),
        patch("azure.identity.ManagedIdentityCredential", side_effect=AssertionError("verify acquired token after blocked archive")),
    ):
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="verify-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        with pytest.raises(RetrievalError, match="source DNS pin blocked"):
            provider.search("query", 5, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)
    client_cls.assert_not_called()


def test_verify_managed_identity_search_admits_before_token_and_dispatch() -> None:
    events: list[str] = []
    session = MagicMock()
    session.mode = RunMode.VERIFY
    source = ArchivedCallRequestEvidence(source_call_id="source-mi-call", request_data={"resolved_ip": "93.184.216.34"})

    def preflight(**_kwargs: Any) -> ArchivedCallRequestEvidence:
        events.append("preflight")
        return source

    def admit(**_kwargs: Any) -> str:
        events.append("admit")
        return "source-mi-call"

    session.preflight_verify_http_managed_identity.side_effect = preflight
    session.admit_verify_http_managed_identity.side_effect = admit
    credential = MagicMock()

    def get_token(*_scopes: str) -> SimpleNamespace:
        events.append("token")
        return SimpleNamespace(token="current-verify-token")

    credential.get_token.side_effect = get_token
    config = AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="test-index", use_managed_identity=True)
    recorder = _FakeExecutionRecorder()
    with (
        patch("azure.identity.ManagedIdentityCredential", return_value=credential),
        patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]),
        respx.mock,
    ):
        route = respx.post(host="93.184.216.34").respond(
            status_code=200,
            json={"value": [{"id": "doc-1", "content": "verified content", "@search.score": 0.75}]},
        )
        provider = AzureSearchProvider(
            config=config,
            execution=recorder,
            run_id="verify-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        chunks = provider.search("query", 5, 0.0, **mock_item_audit_authority(), state_id="state-1", token_id=None)
    assert [(chunk.source_id, chunk.content) for chunk in chunks] == [("doc-1", "verified content")]
    assert events == ["preflight", "token", "admit"]
    assert route.called
    session.verify_call.assert_called_once()
    assert session.verify_call.call_args.kwargs["current_call_id"] == "call-1"


def test_verify_managed_identity_readiness_admits_before_token_and_dispatch() -> None:
    events: list[str] = []
    session = MagicMock()
    session.mode = RunMode.VERIFY
    source = ArchivedCallRequestEvidence(source_call_id="source-readiness", request_data={"resolved_ip": "93.184.216.34"})

    def preflight(**_kwargs: Any) -> ArchivedCallRequestEvidence:
        events.append("preflight")
        return source

    def admit(**_kwargs: Any) -> str:
        events.append("admit")
        return "source-readiness"

    session.preflight_verify_http_managed_identity.side_effect = preflight
    session.admit_verify_http_managed_identity.side_effect = admit
    credential = MagicMock()

    def get_token(*_scopes: str) -> SimpleNamespace:
        events.append("token")
        return SimpleNamespace(token="current-verify-token")

    credential.get_token.side_effect = get_token
    config = AzureSearchProviderConfig(endpoint="https://test.search.windows.net", index="test-index", use_managed_identity=True)
    with (
        patch("azure.identity.ManagedIdentityCredential", return_value=credential),
        patch("socket.getaddrinfo", return_value=[(0, 0, 0, "", ("93.184.216.34", 0))]),
        respx.mock,
    ):
        route = respx.get(host="93.184.216.34").respond(status_code=200, text="7")
        provider = AzureSearchProvider(
            config=config,
            execution=_FakeExecutionRecorder(),
            run_id="verify-run",
            telemetry_emit=_TelemetrySink(),
            call_mode_session=session,
        )
        readiness = provider.runtime_preflight(operation_id="operation-1", coordination_token=mock_audit_authority()["coordination_token"])
    assert readiness.count == 7
    assert events == ["preflight", "token", "admit"]
    assert route.called
    session.verify_call.assert_called_once()
    assert session.verify_call.call_args.kwargs["current_operation_id"] == "operation-1"
