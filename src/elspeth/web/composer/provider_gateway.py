"""Physical Composer provider transport and its request/response boundary."""

from __future__ import annotations

import asyncio
import sys
import time
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, datetime
from typing import Any, Final, cast

from openai import OpenAIError

from elspeth.contracts.composer_llm_audit import ComposerLLMCallStatus, ToolContractDialect
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.trust_boundary import trust_boundary
from elspeth.plugins.transforms.llm.model_catalog import OPENROUTER_LITELLM_PREFIX
from elspeth.web.composer._compose_loop_carriers import (
    _AdmittedAssistantMessage,
    _AdmittedLLMCompletion,
    _AdmittedLLMProviderMetadata,
)
from elspeth.web.composer.audit import BufferingRecorder
from elspeth.web.composer.llm_response_parsing import (
    admit_llm_provider_metadata,
    apply_anthropic_cache_markers,
    attach_llm_calls,
    build_llm_call_record,
    supports_anthropic_prompt_cache_markers,
)
from elspeth.web.composer.protocol import ComposerServiceError, ComposerSettings
from elspeth.web.composer.provider_errors import classify_provider_failure
from elspeth.web.composer.provider_quota import admit_provider_attempt, quota_provider_calls
from elspeth.web.composer.reasoning import apply_reasoning_kwargs
from elspeth.web.composer.tools.wire_projection import wire_tool_definitions
from elspeth.web.credential_guard import (
    CredentialMaterialRefused,
    require_no_credential_material,
    require_no_credential_material_in_tool_wire,
)

_COMPOSER_LLM_SEED_PARAM: Final[str] = "seed"


class _MalformedLLMResponseError(ComposerServiceError):
    """Malformed completion with only already-admitted provider facts."""

    def __init__(self, message: str, *, provider_metadata: _AdmittedLLMProviderMetadata, text_received: bool = False) -> None:
        super().__init__(message)
        self.provider_metadata = provider_metadata
        self.text_received = text_received


def advisor_provider_failure_types() -> tuple[type[Exception], ...]:
    """The Tier-3 failure surface an advisor call can raise.

    Single authority for the callers that degrade an advisor outage into
    structured tool feedback instead of failing the composer turn. It names
    the provider SDK, transport and malformed-response families only.

    Deliberately absent: ``TimeoutError`` and ``asyncio.CancelledError``,
    which callers route through their own deadline and lifecycle arms, and
    every first-party error. An ``AuditIntegrityError`` out of the recorder,
    a plugin crash, or an ordinary defect in controlled code is not a
    provider fault and must keep unwinding rather than be reported to the
    composer LLM as an advisor outage.
    """

    import httpx
    from litellm.exceptions import APIError as LiteLLMAPIError
    from litellm.exceptions import AuthenticationError as LiteLLMAuthError
    from litellm.exceptions import BadRequestError as LiteLLMBadRequestError
    from openai import OpenAIError as OpenAIProviderError

    return (
        LiteLLMAPIError,
        LiteLLMAuthError,
        LiteLLMBadRequestError,
        OpenAIProviderError,
        httpx.HTTPError,
        _MalformedLLMResponseError,
    )


@trust_boundary(
    tier=3,
    source="raw LiteLLM/provider-SDK completion response object (untrusted model/provider output)",
    source_param="response",
    suppresses=("R5",),
    invariant=(
        "raises _MalformedLLMResponseError (carrying only already-admitted provider facts) on any "
        "malformed choices/message/content/tool_calls surface; never coerces or fabricates a field"
    ),
    test_ref="tests/unit/web/composer/test_capture_llm_completion_boundary.py::test_malformed_choices_raises_malformed_llm_response_error",
    test_fingerprint="bde1559884f55a4452526d639a4c77f46e2f20c2a98cc7884cc3ab2421105866",
)
def _capture_composer_llm_completion_fields(
    response: Any,
    *,
    pricing_model: str | None = None,
) -> tuple[_AdmittedAssistantMessage, tuple[Any, ...], _AdmittedLLMProviderMetadata]:
    """Read the response/message surface once, before validating its tool batch."""

    missing = object()
    choices = getattr(response, "choices", missing)
    choice = choices[0] if isinstance(choices, list | tuple) and choices else None
    message = getattr(choice, "message", missing) if choice is not None else missing
    provider_metadata = admit_llm_provider_metadata(
        response,
        choice=choice,
        message=None if message is missing else message,
        pricing_model=pricing_model,
    )
    _require_no_credential_material_in_completion_fields(
        content=None,
        tool_calls=(),
        provider_metadata=provider_metadata,
        surface="composer_provider_response",
    )
    if choices is missing or not isinstance(choices, list | tuple):
        raise _MalformedLLMResponseError(
            "LLM returned malformed choices — cannot continue composition",
            provider_metadata=provider_metadata,
        )
    if not choices:
        raise _MalformedLLMResponseError(
            "LLM returned empty choices array — cannot continue composition",
            provider_metadata=provider_metadata,
        )
    if message is missing:
        raise _MalformedLLMResponseError(
            "LLM response choice carries no message",
            provider_metadata=provider_metadata,
        )
    content = getattr(message, "content", missing)
    if content is missing or (content is not None and type(content) is not str):
        raise _MalformedLLMResponseError(
            "LLM message content is neither absent nor a string",
            provider_metadata=provider_metadata,
        )
    _require_no_credential_material_in_completion_fields(
        content=content,
        tool_calls=(),
        provider_metadata=provider_metadata,
        surface="composer_provider_response",
    )
    tool_calls = getattr(message, "tool_calls", missing)
    if tool_calls is missing or (tool_calls is not None and not isinstance(tool_calls, list | tuple)):
        raise _MalformedLLMResponseError(
            "LLM message tool_calls is neither absent nor a sequence",
            provider_metadata=provider_metadata,
        )
    return _AdmittedAssistantMessage(content=content), tuple(tool_calls or ()), provider_metadata


def _admit_composer_llm_completion(
    response: Any,
    *,
    wrap_tool_batch_error: bool = True,
    pricing_model: str | None = None,
) -> _AdmittedLLMCompletion:
    """Read one LiteLLM completion once and discard the provider objects."""

    message, tool_calls, provider_metadata = _capture_composer_llm_completion_fields(response, pricing_model=pricing_model)
    return _admit_captured_composer_llm_completion(
        message,
        tool_calls,
        provider_metadata,
        wrap_tool_batch_error=wrap_tool_batch_error,
    )


def _admit_captured_composer_llm_completion(
    message: _AdmittedAssistantMessage,
    tool_calls: tuple[Any, ...],
    provider_metadata: _AdmittedLLMProviderMetadata,
    *,
    wrap_tool_batch_error: bool,
) -> _AdmittedLLMCompletion:
    """Validate captured calls and finish the fully owned completion."""

    from elspeth.web.composer.tool_batch import _admit_tool_batch

    try:
        admitted_batch = _admit_tool_batch(tool_calls)
    except AuditIntegrityError as exc:
        if not wrap_tool_batch_error:
            raise
        raise _MalformedLLMResponseError(
            f"LLM tool batch failed admission: {exc}",
            provider_metadata=provider_metadata,
        ) from exc
    return _AdmittedLLMCompletion(
        message=message,
        tool_batch=admitted_batch,
        provider_metadata=provider_metadata,
    )


def _require_no_credential_material_in_completion(
    completion: _AdmittedLLMCompletion,
    *,
    surface: str,
) -> None:
    """Reject all persisted or replayed text metadata in an owned completion."""
    _require_no_credential_material_in_completion_fields(
        content=completion.message.content,
        tool_calls=tuple((call.id, call.function.name) for call in completion.tool_batch.calls),
        provider_metadata=completion.provider_metadata,
        surface=surface,
    )


def _require_no_credential_material_in_completion_fields(
    *,
    content: str | None,
    tool_calls: Sequence[tuple[str, str]],
    provider_metadata: _AdmittedLLMProviderMetadata,
    surface: str,
) -> None:
    """Guard the normalized provider fields that can be audited or replayed."""
    require_no_credential_material(
        {
            "content": content,
            "tool_calls": [{"id": call_id, "name": name} for call_id, name in tool_calls],
            "reasoning_content": provider_metadata.reasoning_content,
            "reasoning_details": provider_metadata.reasoning_details,
            "thinking_blocks": provider_metadata.thinking_blocks,
            "model_returned": provider_metadata.model_returned,
            "provider_request_id": provider_metadata.provider_request_id,
            "finish_reason": provider_metadata.finish_reason,
        },
        surface=surface,
    )


class _BadRequestLLMError(ComposerServiceError):
    """Internal carrier for provider bad-request failures.

    Carries the raw provider message and HTTP status code on dedicated
    attributes so the route layer can surface them under
    ``expose_provider_error=True`` without having to re-parse the wrapped
    LiteLLM exception. ``str(self)`` is unchanged from the parent class —
    only the wrap message is rendered there.
    """

    def __init__(
        self,
        message: str,
        *,
        provider_detail: str | None = None,
        provider_status_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.provider_detail = provider_detail
        self.provider_status_code = provider_status_code


def _apply_openrouter_app_identity(kwargs: dict[str, Any]) -> None:
    """Brand OpenRouter-routed composer calls as ELSPETH, not LiteLLM.

    LiteLLM injects its own OpenRouter attribution headers on every request
    unless the caller overrides them — ``HTTP-Referer: https://litellm.ai`` and
    ``X-Title: liteLLM`` (litellm/main.py). Without this the OpenRouter
    dashboard attributes all composer ("orchestrator") traffic to LiteLLM. The
    LLM transform plugins speak raw HTTP and set the same identity directly
    (``OPENROUTER_APP_REFERER`` / ``OPENROUTER_APP_TITLE`` in
    ``plugins/transforms/llm/providers/openrouter.py``); this brings the
    composer's LiteLLM-routed calls to parity using the one canonical source.

    Scoped to OpenRouter by the ``openrouter/`` routing prefix so the headers
    are never sent to other providers. ``HTTP-Referer`` is OpenRouter's primary
    ranking identifier; ``X-OpenRouter-Title`` is its current display-name
    header (what the plugins send) and ``X-Title`` is the legacy spelling
    LiteLLM defaults to ``liteLLM`` — we override both so no LiteLLM branding
    survives whichever one OpenRouter honours. Caller-supplied headers win: we
    only fill the identity keys we own (``setdefault``).
    """
    model = kwargs["model"] if "model" in kwargs else None
    if model is None or not model.startswith(OPENROUTER_LITELLM_PREFIX):
        return

    # Lazy import: providers/openrouter.py pulls httpx and the provider stack,
    # and the composer keeps that off the app-startup path.
    from elspeth.plugins.transforms.llm.providers.openrouter import (
        OPENROUTER_APP_REFERER,
        OPENROUTER_APP_TITLE,
    )

    existing = kwargs["extra_headers"] if "extra_headers" in kwargs else None
    # Caller-supplied headers win: our three attribution identity keys are laid
    # down first, then any caller headers overlay them (a key the caller already
    # set survives the merge). This expresses the precedence explicitly instead
    # of relying on setdefault's "fill-if-absent" side effect.
    headers: dict[str, str] = {
        "HTTP-Referer": OPENROUTER_APP_REFERER,
        "X-OpenRouter-Title": OPENROUTER_APP_TITLE,
        "X-Title": OPENROUTER_APP_TITLE,
        **(dict(existing) if existing else {}),
    }
    kwargs["extra_headers"] = headers


def _apply_openrouter_usage_accounting(kwargs: dict[str, Any]) -> None:
    """Pin OpenRouter's usage-accounting opt-in on the request, explicitly.

    The audit's provider cost (``response_usage.cost``) and in-band cache
    detail (``prompt_tokens_details.cached_tokens``) exist on the response
    only under OpenRouter's ``usage: {"include": true}`` opt-in. litellm
    1.85.0 happens to inject that opt-in unconditionally
    (``OpenrouterConfig.transform_request``), but that is undocumented
    internal behaviour and the dependency range admits any 1.x — this makes
    the opt-in an ELSPETH-owned contract rather than a litellm-version
    accident. Survival through litellm's param shaping is pinned by
    ``tests/unit/web/composer/test_openrouter_usage_accounting.py``.

    ``usage`` is an OpenRouter-proprietary request field, so non-openrouter/
    models are never touched. A caller-supplied ``usage`` value wins.
    """
    model = kwargs["model"] if "model" in kwargs else None
    if model is None or not model.startswith(OPENROUTER_LITELLM_PREFIX):
        return
    if "usage" not in kwargs:
        kwargs["usage"] = {"include": True}


def _apply_endpoint_kwargs(kwargs: dict[str, Any], *, base_url: str | None, api_key: str | None) -> None:
    """Add ``api_base``/``api_key`` to a LiteLLM kwargs dict, role-scoped.

    Both are omitted entirely when unset (the no-regression guarantee: an
    unconfigured deployment sends the exact same kwargs as before this
    affordance existed). Configuration surface only — no client boundary,
    no model-string rewriting. Callers pick which role's (base_url, api_key)
    pair to pass; this function has no opinion about roles.
    """
    if base_url is not None:
        kwargs["api_base"] = base_url
    if api_key is not None:
        kwargs["api_key"] = api_key


def composer_loop_tool_definitions(dialect: ToolContractDialect) -> list[dict[str, Any]]:
    """Return the tool list the freeform compose loop sends, in LiteLLM function format.

    The compose loop and the boot probe both call this, so the probe sends
    exactly the list production sends. The list is the static wire
    projection W of the registry for ``dialect``
    (:func:`elspeth.web.composer.tools.wire_projection.wire_tool_definitions`),
    which also owns the web ``set_pipeline`` envelope;
    :mod:`elspeth.web.composer.tool_batch` unwraps that envelope before
    custody, audit, redaction, or dispatch.

    Advisor is mandatory, so ``request_advisor_hint`` is always present
    in the LLM-visible list. The CLI MCP server (composer_mcp/) is not
    affected; advisor is web-composer only by design (the tool is not
    registered in the CLI dispatch tables).
    """
    return wire_tool_definitions(dialect)


def build_composer_loop_request_kwargs(
    *,
    model: str,
    messages: Sequence[Mapping[str, Any]],
    tools: Sequence[Mapping[str, Any]],
    settings: ComposerSettings,
    api_base: str | None,
    api_key: str | None,
) -> dict[str, Any]:
    """Build the LiteLLM kwargs of one freeform compose-loop or prose call.

    ``_call_llm``, ``_call_text_llm`` and the boot probe's loop-list request
    all build their request here, so temperature, seed, reasoning and
    endpoint kwargs cannot drift between the probe and production. An empty
    ``tools`` sequence omits the key (the prose call).
    """
    kwargs: dict[str, Any] = {
        "model": model,
        "messages": messages,
        # The Composer loop owns retries and records each provider attempt.
        # Disable LiteLLM and SDK retries inside one audited call.
        "num_retries": 0,
        "max_retries": 0,
    }
    if tools:
        kwargs["tools"] = tools
    if settings.composer_temperature is not None:
        kwargs["temperature"] = settings.composer_temperature
    if settings.composer_seed is not None:
        kwargs[_COMPOSER_LLM_SEED_PARAM] = settings.composer_seed
    # Freeform tool-loop and prose calls are interactive tool
    # choreography — discovery class (elspeth-dc459d438e).
    apply_reasoning_kwargs(kwargs, model=model, effort=settings.composer_discovery_reasoning_effort)
    _apply_endpoint_kwargs(kwargs, base_url=api_base, api_key=api_key)
    return kwargs


async def _litellm_acompletion(*, on_provider_dispatch: Callable[[], None] | None = None, **kwargs: Any) -> Any:
    """Call LiteLLM lazily so app startup never imports provider machinery.

    Brands OpenRouter-routed calls with ELSPETH's app-attribution headers (see
    :func:`_apply_openrouter_app_identity`) so the OpenRouter dashboard credits
    composer traffic to ELSPETH rather than LiteLLM's defaults, and pins
    OpenRouter's usage-accounting opt-in (see
    :func:`_apply_openrouter_usage_accounting`) so provider cost and cache
    detail arrive in-band for the call audit.
    """
    import litellm

    # Endpoint credentials are purpose-specific transport configuration and
    # intentionally excluded.  The dynamic messages are the Web/Composer
    # control content that crosses the provider boundary.
    require_no_credential_material(kwargs["messages"], surface="composer_provider_request")
    _apply_openrouter_app_identity(kwargs)
    _apply_openrouter_usage_accounting(kwargs)
    await admit_provider_attempt(model=kwargs["model"])
    if on_provider_dispatch is not None:
        on_provider_dispatch()
    return await litellm.acompletion(**kwargs)


class ProviderGateway:
    """Primary-role request, admission, and audit owner for one deployment.

    Endpoint credentials are held on a plain instance with the default object
    representation. They are never copied into repr-bearing carriers.
    """

    def __init__(
        self,
        *,
        model: str,
        settings: ComposerSettings,
        endpoint_base_url: str | None,
        endpoint_api_key: str | None,
    ) -> None:
        self._model = model
        self._settings = settings
        self._endpoint_base_url = endpoint_base_url
        self._endpoint_api_key = endpoint_api_key

    async def _call_llm(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> _AdmittedLLMCompletion:
        """Call LiteLLM and return only the admitted, owned completion."""
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        try:
            kwargs = build_composer_loop_request_kwargs(
                model=self._model,
                messages=messages,
                tools=tools,
                settings=self._settings,
                api_base=self._endpoint_base_url,
                api_key=self._endpoint_api_key,
            )
            response = await _litellm_acompletion(**kwargs)
        except LiteLLMBadRequestError as exc:
            raise _BadRequestLLMError(
                f"LLM request rejected ({type(exc).__name__})",
                provider_detail=str(exc) or None,
                provider_status_code=exc.status_code,
            ) from exc
        completion = _admit_composer_llm_completion(
            response,
            pricing_model=self._settings.composer_pricing_model or self._model,
        )
        for call in completion.tool_batch.calls:
            require_no_credential_material_in_tool_wire(
                call.function.name,
                call.function.arguments,
                surface="composer_provider_response",
            )
        _require_no_credential_material_in_completion(
            completion,
            surface="composer_provider_response",
        )
        return completion

    async def _call_text_llm(self, messages: list[dict[str, str]]) -> Any:
        """Call the LLM for non-tool text generation."""
        from litellm.exceptions import BadRequestError as LiteLLMBadRequestError

        try:
            kwargs = build_composer_loop_request_kwargs(
                model=self._model,
                messages=messages,
                tools=(),
                settings=self._settings,
                api_base=self._endpoint_base_url,
                api_key=self._endpoint_api_key,
            )
            response = await _litellm_acompletion(**kwargs)
        except LiteLLMBadRequestError as exc:
            raise _BadRequestLLMError(
                f"LLM request rejected ({type(exc).__name__})",
                provider_detail=str(exc) or None,
                provider_status_code=exc.status_code,
            ) from exc
        if not response.choices:
            raise _MalformedLLMResponseError(
                "LLM returned empty choices array — cannot explain run diagnostics",
                provider_metadata=admit_llm_provider_metadata(
                    response, choice=None, message=None, pricing_model=self._settings.composer_pricing_model or self._model
                ),
            )
        response_metadata = admit_llm_provider_metadata(
            response,
            choice=response.choices[0],
            message=response.choices[0].message,
            pricing_model=self._settings.composer_pricing_model or self._model,
        )
        require_no_credential_material(
            {
                "content": response.choices[0].message.content,
                "reasoning_content": response_metadata.reasoning_content,
                "reasoning_details": response_metadata.reasoning_details,
                "thinking_blocks": response_metadata.thinking_blocks,
                "model_returned": response_metadata.model_returned,
                "provider_request_id": response_metadata.provider_request_id,
                "finish_reason": response_metadata.finish_reason,
            },
            surface="composer_provider_response",
        )
        return response

    @quota_provider_calls
    async def _call_llm_with_audit(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
        *,
        timeout: float,
        recorder: BufferingRecorder | None,
    ) -> _AdmittedLLMCompletion:
        """Call the primary model once and record the exact marked request."""
        if supports_anthropic_prompt_cache_markers(self._model):
            messages, tools_or_none = apply_anthropic_cache_markers(messages, tools, mark_history_tail=True)
            tools = tools_or_none if tools_or_none is not None else tools

        started_at = datetime.now(UTC)
        started_ns = time.monotonic_ns()
        status: ComposerLLMCallStatus | None = None
        response_metadata: _AdmittedLLMProviderMetadata | None = None
        error_class: str | None = None
        error_message: str | None = None
        try:
            completion = await asyncio.wait_for(self._call_llm(messages, tools), timeout=timeout)
            _require_no_credential_material_in_completion(
                completion,
                surface="composer_provider_response",
            )
            response_metadata = completion.provider_metadata
            if not tools and (completion.tool_batch.calls or not (completion.message.content or "").strip()):
                raise _MalformedLLMResponseError(
                    "Reply-only completion must contain text and no tool calls",
                    provider_metadata=completion.provider_metadata,
                )
            status = ComposerLLMCallStatus.SUCCESS
            return completion
        except TimeoutError:
            status = ComposerLLMCallStatus.TIMEOUT
            error_class = "TimeoutError"
            error_message = "TimeoutError"
            raise
        except asyncio.CancelledError as exc:
            status = ComposerLLMCallStatus.CANCELLED
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        except OpenAIError as exc:
            failure = classify_provider_failure(exc)
            status = failure.audit_status if failure is not None else ComposerLLMCallStatus.API_ERROR
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        except _MalformedLLMResponseError as exc:
            status = ComposerLLMCallStatus.MALFORMED_RESPONSE
            response_metadata = exc.provider_metadata
            error_class = type(exc).__name__
            error_message = "malformed_response"
            attach_llm_calls(exc, recorder)
            raise
        except _BadRequestLLMError as exc:
            cause = exc.__cause__
            status = ComposerLLMCallStatus.BAD_REQUEST_ERROR
            error_class = type(cause).__name__ if cause is not None else type(exc).__name__
            error_message = error_class
            attach_llm_calls(exc, recorder)
            raise
        except CredentialMaterialRefused as exc:
            status = ComposerLLMCallStatus.MALFORMED_RESPONSE
            error_class = type(exc).__name__
            error_message = "credential_material_rejected"
            attach_llm_calls(exc, recorder)
            raise
        except Exception as exc:
            status = ComposerLLMCallStatus.API_ERROR
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        finally:
            if recorder is not None and status is not None:
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested=self._model,
                        pricing_model=self._settings.composer_pricing_model,
                        messages=messages,
                        tools=tools or None,
                        status=status,
                        started_at=started_at,
                        started_ns=started_ns,
                        temperature=self._settings.composer_temperature,
                        seed=self._settings.composer_seed,
                        response_metadata=response_metadata,
                        error_class=error_class,
                        error_message=error_message,
                    )
                )
                current_exc = sys.exc_info()[1]
                if current_exc is not None:
                    attach_llm_calls(current_exc, recorder)

    @quota_provider_calls
    async def _call_text_llm_with_audit(
        self,
        messages: list[dict[str, str]],
        *,
        timeout: float,
        recorder: BufferingRecorder | None,
    ) -> str:
        """Call the diagnostics text model and record one redacted audit row."""
        started_at = datetime.now(UTC)
        started_ns = time.monotonic_ns()
        status: ComposerLLMCallStatus | None = None
        response_metadata: _AdmittedLLMProviderMetadata | None = None
        error_class: str | None = None
        error_message: str | None = None
        try:
            provider_response = await asyncio.wait_for(self._call_text_llm(messages), timeout=timeout)
            choice = provider_response.choices[0] if provider_response.choices else None
            message = choice.message if choice is not None else None
            admitted_metadata = admit_llm_provider_metadata(
                provider_response,
                choice=choice,
                message=message,
                pricing_model=self._settings.composer_pricing_model or self._model,
            )
            try:
                content = provider_response.choices[0].message.content
            except (AttributeError, IndexError, TypeError):
                raise _MalformedLLMResponseError(
                    "LLM returned a malformed diagnostics explanation",
                    provider_metadata=admitted_metadata,
                ) from None
            _require_no_credential_material_in_completion_fields(
                content=content if type(content) is str else None,
                tool_calls=(),
                provider_metadata=admitted_metadata,
                surface="composer_provider_response",
            )
            if type(content) is not str or not content.strip():
                raise _MalformedLLMResponseError(
                    "LLM returned an empty diagnostics explanation",
                    provider_metadata=admitted_metadata,
                )
            response_metadata = admitted_metadata
            status = ComposerLLMCallStatus.SUCCESS
            return content.strip()
        except TimeoutError:
            status = ComposerLLMCallStatus.TIMEOUT
            error_class = "TimeoutError"
            error_message = "TimeoutError"
            raise
        except asyncio.CancelledError as exc:
            status = ComposerLLMCallStatus.CANCELLED
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        except OpenAIError as exc:
            failure = classify_provider_failure(exc)
            status = failure.audit_status if failure is not None else ComposerLLMCallStatus.API_ERROR
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        except _MalformedLLMResponseError as exc:
            status = ComposerLLMCallStatus.MALFORMED_RESPONSE
            response_metadata = exc.provider_metadata
            error_class = type(exc).__name__
            error_message = "malformed_response"
            attach_llm_calls(exc, recorder)
            raise
        except _BadRequestLLMError as exc:
            cause = exc.__cause__
            status = ComposerLLMCallStatus.BAD_REQUEST_ERROR
            error_class = type(cause).__name__ if cause is not None else type(exc).__name__
            error_message = error_class
            attach_llm_calls(exc, recorder)
            raise
        except CredentialMaterialRefused as exc:
            status = ComposerLLMCallStatus.MALFORMED_RESPONSE
            error_class = type(exc).__name__
            error_message = "credential_material_rejected"
            attach_llm_calls(exc, recorder)
            raise
        except Exception as exc:
            status = ComposerLLMCallStatus.API_ERROR
            error_class = type(exc).__name__
            error_message = type(exc).__name__
            attach_llm_calls(exc, recorder)
            raise
        finally:
            if recorder is not None and status is not None:
                recorder.record_llm_call(
                    build_llm_call_record(
                        model_requested=self._model,
                        pricing_model=self._settings.composer_pricing_model,
                        messages=cast(list[dict[str, Any]], messages),
                        tools=None,
                        status=status,
                        started_at=started_at,
                        started_ns=started_ns,
                        temperature=self._settings.composer_temperature,
                        seed=self._settings.composer_seed,
                        response_metadata=response_metadata,
                        error_class=error_class,
                        error_message=error_message,
                    )
                )
                current_exc = sys.exc_info()[1]
                if current_exc is not None:
                    attach_llm_calls(current_exc, recorder)
