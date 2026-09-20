"""Azure AI Search provider for RAG retrieval."""

from __future__ import annotations

import math
import re
import urllib.parse
from typing import TYPE_CHECKING, Any, Literal, Protocol, Self, cast

import httpx
from pydantic import BaseModel, ValidationInfo, field_validator, model_validator

from elspeth.contracts.coordination import CoordinationToken, WorkerMembershipToken
from elspeth.contracts.probes import CollectionReadinessResult
from elspeth.contracts.scheduler import TokenWorkItem
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.core.security.web import (
    NetworkError,
    SSRFBlockedError,
    validate_literal_ip_for_ssrf,
    validate_url_for_ssrf,
)
from elspeth.plugins.infrastructure.clients.retrieval.base import RetrievalError
from elspeth.plugins.infrastructure.clients.retrieval.types import RetrievalChunk

if TYPE_CHECKING:
    from elspeth.contracts.audit_protocols import CallRecorder
    from elspeth.contracts.contexts import LimiterProtocol
    from elspeth.plugins.infrastructure.clients.base import TelemetryEmitCallback


_AZURE_SEARCH_MANAGED_IDENTITY_SUFFIX = ".search.windows.net"
_AZURE_SEARCH_TOKEN_SCOPE = "https://search.azure.com/.default"
_AZURE_FIELD_NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_]*$")

AzureSearchAuthMode = Literal["api_key", "managed_identity"]


class _ManagedIdentityCredential(Protocol):
    def get_token(self, *scopes: str) -> Any:
        """Return an Azure access token for the requested scopes."""
        ...

    def close(self) -> None:
        """Release credential-owned resources."""
        ...


def _is_azure_search_managed_identity_hostname(hostname: str) -> bool:
    """Return True for Azure AI Search hostnames eligible for managed identity."""
    return hostname.lower().endswith(_AZURE_SEARCH_MANAGED_IDENTITY_SUFFIX)


class AzureSearchProviderConfig(BaseModel):
    """Configuration for Azure AI Search provider."""

    model_config = {"extra": "forbid", "frozen": True}

    endpoint: str
    index: str

    auth_mode: AzureSearchAuthMode | None = None
    api_key: str | None = None
    use_managed_identity: bool = False
    api_version: str = "2024-07-01"

    search_mode: Literal["vector", "keyword", "hybrid", "semantic"] = "hybrid"
    request_timeout: float = 30.0

    vector_field: str = "contentVector"
    semantic_config: str | None = None

    # Index field mapping. Defaults match the historical hardcoded names; an
    # index built by the portal's import wizard uses chunk / chunk_id / title.
    content_field: str = "content"
    id_field: str = "id"
    title_field: str | None = None
    url_field: str | None = None
    select: tuple[str, ...] | None = None
    # Operator-authored OData $filter, sent verbatim. Never built from row data.
    filter: str | None = None

    # Client id of a user-assigned managed identity; unset selects the
    # system-assigned identity.
    client_id: str | None = None

    @field_validator("content_field", "id_field", "title_field", "url_field", "vector_field")
    @classmethod
    def validate_field_name(cls, v: str | None, info: ValidationInfo) -> str | None:
        if v is not None and not _AZURE_FIELD_NAME.match(v):
            raise ValueError(f"{info.field_name} must be an Azure AI Search field name (letters, digits, underscores), got {v!r}")
        return v

    @field_validator("select")
    @classmethod
    def validate_select(cls, v: tuple[str, ...] | None) -> tuple[str, ...] | None:
        if v is not None:
            if not v:
                raise ValueError("select must name at least one field when set")
            for name in v:
                if not _AZURE_FIELD_NAME.match(name):
                    raise ValueError(f"select entries must be Azure AI Search field names, got {name!r}")
        return v

    @field_validator("filter")
    @classmethod
    def validate_filter(cls, v: str | None) -> str | None:
        if v is not None:
            if not v.strip():
                raise ValueError("filter must not be blank when set")
            if any(c in v for c in "\r\n\x00"):
                raise ValueError("filter must not contain newlines or null bytes")
        return v

    @field_validator("endpoint")
    @classmethod
    def validate_endpoint(cls, v: str) -> str:
        parsed = urllib.parse.urlparse(v)
        if parsed.scheme != "https":
            raise ValueError(f"endpoint must use HTTPS scheme, got {parsed.scheme!r}")
        if not parsed.hostname:
            raise ValueError(f"endpoint must have a hostname, got {v!r}")
        if parsed.username or parsed.password:
            raise ValueError("endpoint must not include embedded credentials")
        if parsed.query or parsed.fragment:
            raise ValueError("endpoint must not include query strings or fragments")
        try:
            validate_literal_ip_for_ssrf(parsed.hostname)
        except SSRFBlockedError as exc:
            raise ValueError(f"endpoint literal IP is blocked by SSRF policy: {exc}") from exc
        return v

    @field_validator("index")
    @classmethod
    def validate_index_name(cls, v: str) -> str:
        if not re.match(r"^[a-zA-Z0-9][a-zA-Z0-9_-]*$", v):
            raise ValueError(
                f"index must contain only alphanumeric characters, hyphens, and underscores (and start with alphanumeric), got {v!r}."
            )
        return v

    @field_validator("api_version")
    @classmethod
    def validate_api_version(cls, v: str) -> str:
        if not re.match(r"^\d{4}-\d{2}-\d{2}(-preview)?$", v):
            raise ValueError(f"api_version must match YYYY-MM-DD or YYYY-MM-DD-preview format, got {v!r}")
        return v

    @field_validator("api_key")
    @classmethod
    def validate_api_key_format(cls, v: str | None) -> str | None:
        if v is not None:
            if any(c in v for c in "\r\n\x00"):
                raise ValueError("api_key must not contain newlines or null bytes")
            if len(v) > 256:
                raise ValueError(f"api_key exceeds maximum length of 256, got {len(v)}")
        return v

    @model_validator(mode="after")
    def validate_auth(self) -> Self:
        if not self.api_key and not self.use_managed_identity:
            raise ValueError("Specify either api_key or use_managed_identity=true")
        if self.api_key and self.use_managed_identity:
            raise ValueError("Specify only one of api_key or use_managed_identity")
        if self.use_managed_identity:
            hostname = urllib.parse.urlparse(self.endpoint).hostname
            if hostname is None or not _is_azure_search_managed_identity_hostname(hostname):
                raise ValueError(
                    "managed identity Azure Search endpoints must use a hostname ending in "
                    f"{_AZURE_SEARCH_MANAGED_IDENTITY_SUFFIX!r}; use api_key auth or add an operator-controlled "
                    "endpoint allowlist before enabling managed identity for other hosts"
                )
        inferred_mode: AzureSearchAuthMode = "managed_identity" if self.use_managed_identity else "api_key"
        if self.auth_mode is not None and self.auth_mode != inferred_mode:
            raise ValueError(f"auth_mode {self.auth_mode!r} does not match configured authentication method {inferred_mode!r}")
        if self.client_id is not None and not self.use_managed_identity:
            raise ValueError("client_id requires use_managed_identity=true")
        object.__setattr__(self, "auth_mode", inferred_mode)
        return self

    @model_validator(mode="after")
    def validate_semantic_config(self) -> Self:
        if self.search_mode == "semantic" and not self.semantic_config:
            raise ValueError("semantic search_mode requires semantic_config")
        return self

    @model_validator(mode="after")
    def validate_select_covers_mapped_fields(self) -> Self:
        if self.select is not None:
            mapped = [name for name in (self.content_field, self.id_field, self.title_field, self.url_field) if name is not None]
            missing = [name for name in mapped if name not in self.select]
            if missing:
                raise ValueError(f"select must include the mapped fields {missing!r}; Azure returns only selected fields")
        return self


# Score normalization ranges per search mode
# (https://learn.microsoft.com/en-us/azure/search/hybrid-search-ranking).
# BM25 has no upper limit, so the keyword ceiling is a clamp, not a bound.
# Hybrid @search.score is an RRF sum: each fused query contributes at most
# 1/(1 + 60), and this provider fuses one text and one vector query.
_RRF_K = 60
_SCORE_RANGES: dict[str, tuple[float, float]] = {
    "keyword": (0.0, 50.0),
    "vector": (0.0, 1.0),
    "hybrid": (0.0, 2 / (1 + _RRF_K)),
    "semantic": (0.0, 4.0),
}

# Semantic ranking reports its 0-4 score separately; @search.score stays BM25.
_SCORE_KEYS: dict[str, str] = {
    "keyword": "@search.score",
    "vector": "@search.score",
    "hybrid": "@search.score",
    "semantic": "@search.rerankerScore",
}


class AzureSearchProvider:
    """Azure AI Search implementation of RetrievalSearcher.

    Uses a single AuditedHTTPClient for connection pooling across searches.
    The client's state_id and token_id are updated per-call for correct
    audit scoping. This is safe because row processing is serial within
    a transform — no concurrent calls to search().
    """

    def __init__(
        self,
        config: AzureSearchProviderConfig,
        *,
        execution: CallRecorder,
        run_id: str,
        telemetry_emit: TelemetryEmitCallback,
        limiter: LimiterProtocol | None = None,
    ) -> None:
        from elspeth.plugins.infrastructure.clients.http import AuditedHTTPClient

        self._config = config
        self._execution = execution
        self._run_id = run_id
        self._telemetry_emit = telemetry_emit
        self._limiter = limiter

        self._search_url = f"{config.endpoint.rstrip('/')}/indexes/{config.index}/docs/search?api-version={config.api_version}"
        self._score_range = _SCORE_RANGES[config.search_mode]
        self._score_key = _SCORE_KEYS[config.search_mode]

        # Per-search skipped item tracking — allows callers to include
        # skip counts in audit records without changing the protocol.
        self.last_skipped_count: int = 0
        self.last_skipped_reasons: list[dict[str, Any]] = []
        self._managed_identity_credential: _ManagedIdentityCredential | None = None

        # Shared HTTP client for connection pooling. Created once, reused
        # across all search() calls. state_id is updated per-call.
        headers = {"Content-Type": "application/json"}
        if self._config.api_key:
            headers["api-key"] = self._config.api_key
        self._http_client = AuditedHTTPClient(
            execution=self._execution,
            state_id="__init__",  # Updated per-call in _execute_search
            run_id=self._run_id,
            telemetry_emit=self._telemetry_emit,
            timeout=self._config.request_timeout,
            limiter=self._limiter,
            headers=headers,
        )

    def _auth_headers(self) -> dict[str, str]:
        if self._config.api_key:
            return {"api-key": self._config.api_key}
        if self._config.use_managed_identity:
            credential = self._get_managed_identity_credential()
            try:
                from azure.core.exceptions import AzureError

                token = credential.get_token(_AZURE_SEARCH_TOKEN_SCOPE)
            except AzureError as exc:
                raise RetrievalError(
                    f"Azure managed identity token acquisition failed for {self._config.endpoint} index {self._config.index!r}: {exc}",
                    retryable=False,
                ) from exc
            return {"Authorization": f"Bearer {token.token}"}
        return {}

    def _get_managed_identity_credential(self) -> _ManagedIdentityCredential:
        if self._managed_identity_credential is None:
            # ManagedIdentityCredential, never DefaultAzureCredential: the default
            # chain tries EnvironmentCredential first, so a host carrying service
            # principal variables would authenticate as that principal while the
            # audit trail records auth_mode=managed_identity.
            try:
                from azure.identity import ManagedIdentityCredential
            except ImportError as exc:
                raise RetrievalError(
                    "Azure managed identity token acquisition failed: azure-identity is not installed. "
                    "Install elspeth with the 'azure' extra or use api_key authentication.",
                    retryable=False,
                ) from exc
            client_id = self._config.client_id
            credential = ManagedIdentityCredential(client_id=client_id) if client_id is not None else ManagedIdentityCredential()
            self._managed_identity_credential = cast(_ManagedIdentityCredential, credential)
        return self._managed_identity_credential

    def search(
        self,
        query: str,
        top_k: int,
        min_score: float,
        *,
        state_id: str,
        token_id: str | None,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
    ) -> list[RetrievalChunk]:
        response_data = self._execute_search(
            query, top_k, state_id=state_id, token_id=token_id, member_token=member_token, work_item=work_item
        )
        chunks, skipped_items = self._parse_response(response_data, min_score)
        # "Record what we didn't get" — skipped items are audit evidence.
        # Store on instance so callers (RAGRetrievalTransform) can include
        # skip counts in their audit success_reason metadata, which flows
        # into the Landscape audit trail.
        # "Record what we didn't get" — skipped items are audit evidence.
        # Stored on instance for the caller (RAGRetrievalTransform) to include
        # in the Landscape audit trail via success_reason metadata.
        # No logger.debug — per logging policy, pipeline activity belongs
        # in the Landscape, not in logs.
        self.last_skipped_count = len(skipped_items)
        self.last_skipped_reasons = skipped_items
        return chunks

    def _execute_search(
        self,
        query: str,
        top_k: int,
        *,
        state_id: str,
        token_id: str | None,
        member_token: WorkerMembershipToken,
        work_item: TokenWorkItem,
    ) -> dict[str, Any]:
        body = self._build_request_body(query, top_k)

        # Update per-call audit scoping on the shared client.
        self._http_client.update_call_context(state_id, token_id, member_token=member_token, work_item=work_item)

        try:
            try:
                safe_request = validate_url_for_ssrf(self._search_url)
            except SSRFBlockedError as exc:
                raise RetrievalError(f"Azure AI Search endpoint blocked by SSRF validation: {exc}", retryable=False) from exc
            except NetworkError as exc:
                raise RetrievalError(f"Azure AI Search endpoint DNS validation failed: {exc}", retryable=True) from exc

            response, _final_hostname_url, _call = self._http_client.request_ssrf_safe(
                "POST",
                safe_request,
                headers=self._auth_headers(),
                json=body,
            )

            status_code = response.status_code
            if status_code in (401, 403):
                raise RetrievalError(
                    f"Authentication failed for {self._config.endpoint} index {self._config.index!r}: HTTP {status_code}",
                    retryable=False,
                    status_code=status_code,
                )
            if status_code == 429:
                raise RetrievalError("Rate limited by Azure AI Search", retryable=True, status_code=429)
            if status_code >= 500:
                raise RetrievalError(f"Azure AI Search server error: HTTP {status_code}", retryable=True, status_code=status_code)
            if status_code >= 400:
                raise RetrievalError(f"Azure AI Search client error: HTTP {status_code}", retryable=False, status_code=status_code)

            from elspeth.plugins.infrastructure.clients.json_utils import parse_json_strict

            parsed, error = parse_json_strict(response.text)
            if error is not None:
                raise RetrievalError(
                    f"Malformed JSON response from Azure AI Search: {error}",
                    retryable=False,
                )
            if not isinstance(parsed, dict):
                raise RetrievalError(
                    f"Azure AI Search response must be a JSON object, got {type(parsed).__name__}",
                    retryable=False,
                )
            return parsed
        except RetrievalError:
            raise
        except (httpx.TimeoutException, httpx.ConnectError, httpx.NetworkError) as exc:
            raise RetrievalError(f"Search request failed: {exc}", retryable=True) from exc
        except httpx.HTTPError as exc:
            raise RetrievalError(f"HTTP error during search: {exc}", retryable=True) from exc
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise RetrievalError(f"Search request failed: {exc}", retryable=True) from exc

    def _build_request_body(self, query: str, top_k: int) -> dict[str, Any]:
        body: dict[str, Any] = {"top": top_k}
        mode = self._config.search_mode

        if mode in ("keyword", "hybrid"):
            body["search"] = query
        if mode in ("vector", "hybrid"):
            body["vectorQueries"] = [
                {
                    "kind": "text",
                    "text": query,
                    "fields": self._config.vector_field,
                    "k": top_k,
                }
            ]
        if mode == "semantic":
            body["search"] = query
            body["queryType"] = "semantic"
            body["semanticConfiguration"] = self._config.semantic_config

        if self._config.select is not None:
            body["select"] = ",".join(self._config.select)
        if self._config.filter is not None:
            body["filter"] = self._config.filter

        return body

    @trust_boundary(
        tier=3,
        source="Azure AI Search /docs/search JSON response body (the result items and their per-item fields)",
        source_param="response_data",
        suppresses=("R1",),
        invariant="raises RetrievalError when the top-level 'value' array is absent or not a list; per-item non-dict members and missing/invalid fields are coerced to a recorded skip (absence captured in skipped_items), never fabricated",
        test_ref="tests/unit/plugins/infrastructure/clients/retrieval/test_azure_search.py::TestParseResponse::test_missing_value_key_raises",
        test_fingerprint="af5342f173e6c43afedff0b28a3de9acf5a0fc990f0325f9dc5ff61aaa581d98",
    )
    def _parse_response(self, response_data: dict[str, Any], min_score: float) -> tuple[list[RetrievalChunk], list[dict[str, Any]]]:
        if "value" not in response_data:
            raise RetrievalError("Azure AI Search response missing 'value' array", retryable=False)

        results = response_data["value"]
        # Tier 3 boundary: 'value' may be present but not an array (object/string).
        # Iterating a non-list would reach `item.get(...)` and raise raw
        # AttributeError — a structural violation of the response, so raise
        # RetrievalError here, parallel to the missing-'value' case above.
        if not isinstance(results, list):
            raise RetrievalError(
                f"Azure AI Search response 'value' must be an array, got {type(results).__name__}",
                retryable=False,
            )
        chunks: list[RetrievalChunk] = []
        # Track items skipped at Tier 3 boundary — "record what we didn't get"
        skipped_items: list[dict[str, Any]] = []

        for item in results:
            # Tier 3 boundary: a non-object array member (string, number, null)
            # cannot be probed for fields. Record it as a skip rather than
            # crash the whole page on AttributeError — one garbage member must
            # not discard the valid results alongside it.
            if not isinstance(item, dict):
                skipped_items.append({"reason": "invalid_item_type", "type": type(item).__name__})
                continue

            content_field = self._config.content_field
            id_field = self._config.id_field
            item_id = item.get(id_field)

            # Semantic mode reads @search.rerankerScore; an item the ranker did
            # not score is a recorded skip, never ranked on its BM25 score.
            raw_score = item.get(self._score_key)
            if raw_score is None:
                skipped_items.append({"reason": "missing_score", "id": item_id})
                continue

            # Tier 3 boundary: validate score type before arithmetic.
            # Azure returns JSON — score could be string, bool, list, etc.
            # bool check required because isinstance(True, int) is True in Python.
            if isinstance(raw_score, bool) or not isinstance(raw_score, (int, float)):
                skipped_items.append({"reason": "invalid_score_type", "id": item_id, "type": type(raw_score).__name__})
                continue

            normalized_score = self._normalize_score(raw_score)
            if normalized_score < min_score:
                continue

            content = item.get(content_field)
            if content is None:
                skipped_items.append({"reason": "missing_content", "id": item_id})
                continue
            if not isinstance(content, str):
                skipped_items.append({"reason": "invalid_content_type", "id": item_id, "type": type(content).__name__})
                continue
            if not content:
                skipped_items.append({"reason": "empty_content", "id": item_id})
                continue

            source_id = item_id or item.get("@search.documentId")
            if source_id is None:
                # No identifier available — skip rather than fabricate "unknown".
                # "record what we didn't get": absence is captured by the count
                # gap between results returned and chunks emitted.
                skipped_items.append({"reason": "missing_id", "keys": list(item.keys())})
                continue

            metadata: dict[str, Any] = {
                k: str(v) if not isinstance(v, (str, int, float, bool, type(None), list, dict)) else v
                for k, v in item.items()
                if k not in (self._score_key, content_field, id_field)
            }
            # Citation fields under provider-neutral names. A missing or
            # non-string value is omitted, never fabricated.
            for citation_key, field_name in (("source_name", self._config.title_field), ("source_link", self._config.url_field)):
                if field_name is not None:
                    citation_value = item.get(field_name)
                    if isinstance(citation_value, str) and citation_value:
                        metadata[citation_key] = citation_value

            try:
                chunks.append(
                    RetrievalChunk(
                        content=content,
                        score=normalized_score,
                        source_id=str(source_id),
                        metadata=metadata,
                    )
                )
            except ValueError as exc:
                raise RetrievalError(f"Provider returned invalid data: {exc}", retryable=False) from exc

        chunks.sort(key=lambda c: c.score, reverse=True)
        return chunks, skipped_items

    def _normalize_score(self, raw_score: float) -> float:
        if not math.isfinite(raw_score):
            raise RetrievalError(
                f"Azure AI Search returned non-finite score: {raw_score!r}. "
                f"This indicates a malformed API response (Tier 3 boundary violation).",
                retryable=False,
            )
        min_val, max_val = self._score_range
        if max_val <= min_val:
            return 0.0
        normalized = (raw_score - min_val) / (max_val - min_val)
        return max(0.0, min(1.0, normalized))

    def runtime_preflight(self, *, operation_id: str, coordination_token: CoordinationToken) -> CollectionReadinessResult:
        """Count the index's documents as an audited call under an operation parent.

        Returns a result for 200 and 404 (the caller decides whether a missing
        or empty index is fatal); raises RetrievalError for everything else,
        retryable only for transport failures, 429 and 5xx.
        """
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
        except httpx.HTTPError as exc:
            raise RetrievalError(f"HTTP error during readiness probe: {exc}", retryable=True) from exc
        finally:
            client.close()

        status_code = response.status_code
        if status_code == 404:
            return CollectionReadinessResult(collection=index_name, reachable=True, count=None, message=f"Index '{index_name}' not found")
        if status_code in (401, 403):
            raise RetrievalError(
                f"Authentication failed for {self._config.endpoint} index {index_name!r}: HTTP {status_code}",
                retryable=False,
                status_code=status_code,
            )
        if status_code == 429 or status_code >= 500:
            raise RetrievalError(f"Azure AI Search readiness probe: HTTP {status_code}", retryable=True, status_code=status_code)
        if status_code >= 400:
            raise RetrievalError(f"Azure AI Search readiness probe: HTTP {status_code}", retryable=False, status_code=status_code)
        try:
            count = int(response.text.strip())
        except ValueError:
            # The body is not echoed: an HTML error page does not belong in an audit message.
            return CollectionReadinessResult(
                collection=index_name,
                reachable=True,
                count=None,
                message=f"Index '{index_name}' returned a non-integer $count body",
            )
        return CollectionReadinessResult(
            collection=index_name,
            reachable=True,
            count=count,
            message=(f"Index '{index_name}' has {count} documents" if count > 0 else f"Index '{index_name}' is empty"),
        )

    def close(self) -> None:
        """Release the shared HTTP client and its connection pool."""
        try:
            self._http_client.close()
        finally:
            credential = self._managed_identity_credential
            self._managed_identity_credential = None
            if credential is not None:
                credential.close()
