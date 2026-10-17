"""Dedicated Azure Prompt Shields transport for authoring admission."""

from __future__ import annotations

import asyncio

import httpx
from pydantic import BaseModel, ConfigDict, Field

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.prompt_guard import PromptGuardClient, PromptGuardDecision, PromptGuardRequest
from elspeth.core.canonical import stable_hash
from elspeth.web.composer.bounded_json import bounded_json_loads
from elspeth.web.prompt_guard_config import AZURE_PROMPT_SHIELDS_API_VERSION, PromptGuardSettings

_MAX_PROMPT_CHARACTERS = 10_000
_MAX_DOCUMENT_CHARACTERS = 10_000
_MAX_DOCUMENTS_PER_CALL = 5
_MAX_DOCUMENTS = 128
_MAX_INPUT_UTF8_BYTES = 262_144
_MAX_RESPONSE_BYTES = 65_536


class _AzureAnalysis(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    attack_detected: bool = Field(alias="attackDetected")


class _AzureResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)

    user_prompt_analysis: _AzureAnalysis | None = Field(default=None, alias="userPromptAnalysis")
    documents_analysis: list[_AzureAnalysis] | None = Field(default=None, alias="documentsAnalysis")


def _document_batches(request: PromptGuardRequest) -> tuple[tuple[str, ...], ...] | None:
    """Keep whole documents within the standalone API's count/character limits."""
    if len(request.user_prompt) > _MAX_PROMPT_CHARACTERS or len(request.documents) > _MAX_DOCUMENTS:
        return None
    if not request.user_prompt and not any(request.documents):
        return None
    try:
        size = len(request.user_prompt.encode("utf-8")) + sum(len(document.encode("utf-8")) for document in request.documents)
    except UnicodeEncodeError:
        return None
    if size > _MAX_INPUT_UTF8_BYTES or any(len(document) > _MAX_DOCUMENT_CHARACTERS for document in request.documents):
        return None
    batches: list[tuple[str, ...]] = []
    current: list[str] = []
    characters = 0
    for document in request.documents:
        if current and (len(current) == _MAX_DOCUMENTS_PER_CALL or characters + len(document) > _MAX_DOCUMENT_CHARACTERS):
            batches.append(tuple(current))
            current = []
            characters = 0
        current.append(document)
        characters += len(document)
    if current or not batches:
        batches.append(tuple(current))
    return tuple(batches)


def _unique_json_object(pairs: list[tuple[str, object]]) -> object:
    if len({key for key, _ in pairs}) != len(pairs):
        raise ValueError("Prompt guard response contains duplicate object keys")
    return dict(pairs)


def _admit_azure_response(body: bytes, *, document_count: int, has_user_prompt: bool) -> PromptGuardDecision:
    """Apply the row plugin's strict boolean/cardinality contract without row custody."""
    try:
        payload = bounded_json_loads(body.decode("utf-8"), label="Azure prompt guard response", object_pairs_hook=_unique_json_object)
        result = _AzureResponse.model_validate(payload)
    except ValueError:
        return PromptGuardDecision("unavailable", "invalid_response")
    user = result.user_prompt_analysis
    documents = result.documents_analysis
    if has_user_prompt and user is None:
        return PromptGuardDecision("unavailable", "invalid_response")
    if document_count and (documents is None or len(documents) != document_count):
        return PromptGuardDecision("unavailable", "invalid_response")
    if not document_count and documents:
        return PromptGuardDecision("unavailable", "invalid_response")
    if (user is not None and user.attack_detected) or (documents is not None and any(document.attack_detected for document in documents)):
        return PromptGuardDecision("blocked", "policy_blocked")
    return PromptGuardDecision("allow", "clean")


class AzurePromptGuardClient(PromptGuardClient):
    """One independent Azure endpoint; no runtime row/run identity or model keys."""

    def __init__(self, settings: PromptGuardSettings, *, http_transport: httpx.AsyncBaseTransport | None = None) -> None:
        if type(settings) is not PromptGuardSettings:
            raise AuditIntegrityError("Prompt guard factory requires owned Azure settings")
        self._settings = settings
        self._http_transport = http_transport
        self._policy_fingerprint = stable_hash(
            {
                "adapter": settings.adapter,
                "endpoint": settings.endpoint,
                "api_version": AZURE_PROMPT_SHIELDS_API_VERSION,
                "auth": "subscription_key",
                "timeout_seconds": settings.timeout_seconds,
                "max_prompt_characters": _MAX_PROMPT_CHARACTERS,
                "max_document_characters": _MAX_DOCUMENT_CHARACTERS,
                "max_documents_per_call": _MAX_DOCUMENTS_PER_CALL,
                "max_documents": _MAX_DOCUMENTS,
                "max_input_utf8_bytes": _MAX_INPUT_UTF8_BYTES,
                "max_response_bytes": _MAX_RESPONSE_BYTES,
            }
        )

    @property
    def adapter(self) -> str:
        return "azure_prompt_shields"

    @property
    def policy_fingerprint(self) -> str:
        return self._policy_fingerprint

    @property
    def timeout_seconds(self) -> float:
        return self._settings.timeout_seconds

    async def inspect(self, request: PromptGuardRequest) -> PromptGuardDecision:
        if type(request) is not PromptGuardRequest:
            raise AuditIntegrityError("Prompt guard requires an owned immutable request")
        batches = _document_batches(request)
        if batches is None:
            return PromptGuardDecision("unavailable", "unsupported_input")
        url = f"{self._settings.endpoint}/contentsafety/text:shieldPrompt?api-version={AZURE_PROMPT_SHIELDS_API_VERSION}"
        try:
            async with asyncio.timeout(self.timeout_seconds):
                async with httpx.AsyncClient(
                    transport=self._http_transport,
                    timeout=self.timeout_seconds,
                    follow_redirects=False,
                    trust_env=False,
                    headers={
                        "Ocp-Apim-Subscription-Key": self._settings.subscription_key.get_secret_value(),
                        "Accept-Encoding": "identity",
                    },
                ) as client:
                    for documents in batches:
                        async with client.stream(
                            "POST", url, json={"userPrompt": request.user_prompt, "documents": list(documents)}
                        ) as response:
                            if response.status_code != 200:
                                return PromptGuardDecision("unavailable", "service_error")
                            if response.headers.get("content-encoding", "identity").lower() != "identity":
                                return PromptGuardDecision("unavailable", "invalid_response")
                            body = bytearray()
                            async for chunk in response.aiter_bytes(chunk_size=4096):
                                if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                                    return PromptGuardDecision("unavailable", "invalid_response")
                                body.extend(chunk)
                            decision = _admit_azure_response(
                                bytes(body), document_count=len(documents), has_user_prompt=bool(request.user_prompt)
                            )
                            if decision.disposition != "allow":
                                return decision
        except TimeoutError:
            return PromptGuardDecision("unavailable", "timeout")
        except httpx.TimeoutException:
            return PromptGuardDecision("unavailable", "timeout")
        except httpx.HTTPError:
            return PromptGuardDecision("unavailable", "service_error")
        return PromptGuardDecision("allow", "clean")


def build_prompt_guard_client(settings: PromptGuardSettings) -> PromptGuardClient:
    return AzurePromptGuardClient(settings)
