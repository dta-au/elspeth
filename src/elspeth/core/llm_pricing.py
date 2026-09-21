"""Shared data-only provider usage admission and catalogue pricing.

Routing aliases and pricing identities are supplied independently by callers.
Unknown prices and malformed metadata remain unavailable, never fabricated zero.
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping
from types import MemberDescriptorType
from typing import Any, cast

import structlog

from elspeth.contracts.composer_llm_audit import (
    PROVIDER_COST_SOURCE_COST_PER_TOKEN,
    PROVIDER_COST_SOURCE_HIDDEN_PARAMS_RESPONSE_COST,
    PROVIDER_COST_SOURCE_NOT_AVAILABLE,
    PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST,
    ComposerLLMProviderCostSource,
)
from elspeth.contracts.token_usage import TokenUsage
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.canonical import stable_hash
from elspeth.core.litellm_policy import configure_litellm_pricing

configure_litellm_pricing()

_log = structlog.get_logger()


def provider_cost_from_response(
    response: Any | None, *, pricing_model: str | None = None
) -> tuple[float | None, ComposerLLMProviderCostSource]:
    """Extract request cost, calculating only when both cost fields are absent or null.

    Prefer the public ``response.usage.cost`` field when the provider supplies
    it. LiteLLM stores Bedrock's calculated cost in the Pydantic private-data
    mapping at ``_hidden_params.response_cost``; consult that mapping only when
    ``usage.cost`` is absent. A present but malformed public value is evidence
    of malformed metadata and must not silently fall back to another source.
    If both fields are absent, the caller-selected pricing identity and
    reported usage may supply a catalog calculation with explicit provenance.

    Both sources are external provider metadata, so booleans, non-numbers,
    negative values, and non-finite values are treated as unavailable. The
    private-data read goes directly through ``__pydantic_private__`` and never
    invokes a provider-named property.
    """
    if response is None:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    usage = _provider_field(response, "usage")
    return provider_cost_from_captured_usage(response, usage, pricing_model=pricing_model)


@observation_boundary(
    tier=3,
    source="a LiteLLM response object's pydantic private-data mapping (__pydantic_private__ -> _hidden_params.response_cost)",
    source_param="response",
    suppresses=("R1", "R5"),
    invariant=(
        "preserves present non-null public or private cost values, rejects malformed metadata, and permits "
        "requested-model pricing only when both cost fields are absent or null; the private read goes through "
        "object.__getattribute__ so no provider-named property is invoked, and it never raises"
    ),
)
def provider_cost_from_captured_usage(
    response: Any,
    usage: Any | None,
    *,
    pricing_model: str | None = None,
) -> tuple[float | None, ComposerLLMProviderCostSource]:
    """Extract cost without resolving the response's usage field again."""

    usage_fields = _provider_field_map(usage)
    if usage_fields is not None and "cost" in usage_fields and usage_fields["cost"] is not None:
        return validated_provider_cost(usage_fields["cost"], PROVIDER_COST_SOURCE_RESPONSE_USAGE_COST)

    service_tier = _provider_field(response, "service_tier")

    try:
        private = object.__getattribute__(response, "__pydantic_private__")
    except AttributeError:
        return calculate_missing_provider_cost(usage, pricing_model=pricing_model, service_tier=service_tier)
    if private is None:
        return calculate_missing_provider_cost(usage, pricing_model=pricing_model, service_tier=service_tier)
    if not isinstance(private, Mapping):
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if "_hidden_params" not in private:
        return calculate_missing_provider_cost(usage, pricing_model=pricing_model, service_tier=service_tier)
    hidden_params = private["_hidden_params"]
    if not isinstance(hidden_params, Mapping):
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if "response_cost" not in hidden_params or hidden_params["response_cost"] is None:
        return calculate_missing_provider_cost(usage, pricing_model=pricing_model, service_tier=service_tier)
    return validated_provider_cost(
        hidden_params["response_cost"],
        PROVIDER_COST_SOURCE_HIDDEN_PARAMS_RESPONSE_COST,
    )


_PYDANTIC_EXTRA_SLOT = "__pydantic_extra__"


@observation_boundary(
    tier=3,
    source="a LiteLLM/provider response object whose pydantic v2 extra='allow' overflow slot holds undeclared provider fields",
    source_param="value",
    suppresses=("R1", "R5"),
    invariant=(
        "returns None unless __pydantic_extra__ resolves through the owning class's __mro__ to a genuine "
        "__slots__ member descriptor holding a non-empty dict; a provider-defined property is treated as "
        "absent rather than invoked, so no provider-controlled code runs and the read never raises"
    ),
)
def _pydantic_extra_fields(value: Any) -> Mapping[str, Any] | None:
    """Return a pydantic v2 ``extra="allow"`` overflow mapping, or ``None``.

    Pydantic v2 models with ``extra="allow"`` (LiteLLM response objects) store
    undeclared provider fields in the ``__pydantic_extra__`` slot rather than
    in ``__dict__``. ``usage`` on a real ``ModelResponse`` lives there, and a
    real ``litellm.types.utils.ChatCompletionMessageToolCall`` declares no
    model fields at all — its whole payload (``id``/``type``/``function``) is
    in that slot and its ``__dict__`` is empty. Any reader that consults
    ``__dict__`` alone therefore sees a real provider object as field-less
    (ADR-032; the defect class of elspeth-9ea866438b).

    The slot is resolved through the owning class's ``__mro__`` and read only
    when it is a genuine ``__slots__`` member descriptor. A provider object
    that defines ``__pydantic_extra__`` as its own property is treated as
    having no extras rather than having its descriptor invoked, which keeps
    this a data-only read: no provider-controlled code runs. That posture is
    pinned by ``test_provider_reasoning_does_not_invoke_provider_descriptors``.
    """
    for klass in type(value).__mro__:
        # Absence and a None-valued class attribute both mean "this class does
        # not carry the slot"; each keeps walking the MRO so a genuine
        # descriptor on a base class is still found.
        if _PYDANTIC_EXTRA_SLOT not in klass.__dict__:
            continue
        descriptor = klass.__dict__[_PYDANTIC_EXTRA_SLOT]
        if descriptor is None:
            continue
        if type(descriptor) is not MemberDescriptorType:
            return None
        try:
            extra = descriptor.__get__(value, type(value))
        except AttributeError:
            return None
        return extra if isinstance(extra, dict) and extra else None
    return None


def _merge_pydantic_extra(value: Any, fields: Mapping[str, Any]) -> Mapping[str, Any]:
    """Overlay an object's own ``__dict__`` fields onto its pydantic extras."""
    extra = _pydantic_extra_fields(value)
    if extra is None:
        return fields
    return {**extra, **fields}


@observation_boundary(
    tier=3,
    source="one provider response value: a Mapping payload, or an attribute-style provider/SDK response object",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns None for None and for any object whose vars() is unavailable or is not a Mapping; "
        "shape mismatch is absence, never a coercion, and the read never raises"
    ),
)
def _provider_field_map(value: Any) -> Mapping[str, Any] | None:
    if isinstance(value, Mapping):
        return value
    if value is None:
        return None
    try:
        fields = vars(value)
    except TypeError:
        return None
    if not isinstance(fields, Mapping):
        return None
    return _merge_pydantic_extra(value, fields)


def _provider_field(value: Any, field: str) -> Any:
    fields = _provider_field_map(value)
    if fields is None or field not in fields:
        return None
    return fields[field]


@observation_boundary(
    tier=3,
    source="an external provider token-usage detail object or mapping",
    source_param="value",
    suppresses=("R5",),
    invariant=(
        "returns mappings unchanged and projects only requested data fields from SDK objects; "
        "unreadable detail objects remain absent without invoking provider-named properties"
    ),
)
def _provider_details_payload(value: Any, *, fields: tuple[str, ...]) -> Mapping[str, Any] | None:
    value_fields = _provider_field_map(value)
    if value_fields is None:
        return None
    if isinstance(value, Mapping):
        return value
    return {field: value_fields[field] if field in value_fields else None for field in fields}


@observation_boundary(
    tier=3,
    source="one provider-reported usage payload (Mapping or attribute-style usage object) carried on a LiteLLM response",
    source_param="usage",
    suppresses=("R1", "R5"),
    invariant=(
        "returns TokenUsage.unknown() for an absent usage payload and records every missing or "
        "non-Mapping counter as None (absence) rather than a fabricated zero; never raises"
    ),
)
def _token_usage_from_usage(usage: Any | None) -> TokenUsage:
    """Normalize one already-captured provider usage value."""

    if usage is None:
        return TokenUsage.unknown()
    if isinstance(usage, Mapping):
        details = usage.get("prompt_tokens_details")
        completion_details = usage.get("completion_tokens_details")
        output_details = usage.get("output_tokens_details")
        usage_data = {
            "prompt_tokens": usage.get("prompt_tokens"),
            "completion_tokens": usage.get("completion_tokens"),
            "total_tokens": usage.get("total_tokens"),
            "prompt_tokens_details": details if isinstance(details, Mapping) else None,
            "completion_tokens_details": completion_details if isinstance(completion_details, Mapping) else None,
            "output_tokens_details": output_details if isinstance(output_details, Mapping) else None,
            "cache_creation_input_tokens": usage.get("cache_creation_input_tokens"),
            "cache_read_input_tokens": usage.get("cache_read_input_tokens"),
            "reasoning_tokens": usage.get("reasoning_tokens"),
        }
    else:
        details_attr = _provider_field(usage, "prompt_tokens_details")
        details_payload = _provider_details_payload(details_attr, fields=("cached_tokens",))
        completion_details_payload = _provider_details_payload(
            _provider_field(usage, "completion_tokens_details"),
            fields=("reasoning_tokens",),
        )
        output_details_payload = _provider_details_payload(
            _provider_field(usage, "output_tokens_details"),
            fields=("reasoning_tokens",),
        )
        usage_data = {
            "prompt_tokens": _provider_field(usage, "prompt_tokens"),
            "completion_tokens": _provider_field(usage, "completion_tokens"),
            "total_tokens": _provider_field(usage, "total_tokens"),
            "prompt_tokens_details": details_payload,
            "completion_tokens_details": completion_details_payload,
            "output_tokens_details": output_details_payload,
            "cache_creation_input_tokens": _provider_field(usage, "cache_creation_input_tokens"),
            "cache_read_input_tokens": _provider_field(usage, "cache_read_input_tokens"),
            "reasoning_tokens": _provider_field(usage, "reasoning_tokens"),
        }
    if usage_data["cache_read_input_tokens"] is not None or usage_data["cache_creation_input_tokens"] is not None:
        usage_data["prompt_tokens_details"] = None
    return TokenUsage.from_dict(usage_data)


@observation_boundary(
    tier=3,
    source="optional cache and reasoning counters from an external LiteLLM usage payload",
    source_param="usage",
    suppresses=("R1",),
    invariant=(
        "rejects present non-null malformed optional counters and detail containers before pricing; "
        "None stays absent, and only nonnegative exact integer counters are accepted through data-only field reads"
    ),
)
def _pricing_usage_optional_fields_valid(usage: Any | None) -> bool:
    """Reject malformed pricing counters before token admission erases their shape.

    Explicit None remains absent. Read data fields only, using the same SDK
    mapping boundary as token admission, without resolving provider properties.
    """
    fields = _provider_field_map(usage)
    if fields is None:
        return False
    for name in ("cache_creation_input_tokens", "cache_read_input_tokens", "reasoning_tokens"):
        value = fields.get(name)
        if value is not None and (type(value) is not int or value < 0):
            return False
    for details_name, counter_name in (
        ("prompt_tokens_details", "cached_tokens"),
        ("completion_tokens_details", "reasoning_tokens"),
        ("output_tokens_details", "reasoning_tokens"),
    ):
        details = fields.get(details_name)
        if details is None:
            continue
        detail_fields = _provider_field_map(details)
        if detail_fields is None:
            return False
        value = detail_fields.get(counter_name)
        if value is not None and (type(value) is not int or value < 0):
            return False
        if details_name == "prompt_tokens_details":
            creation_details = detail_fields.get("cache_creation_token_details")
            if creation_details is not None:
                ttl_fields = _provider_field_map(creation_details)
                if ttl_fields is None:
                    return False
                if set(ttl_fields) != {"ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"}:
                    return False
                short = ttl_fields.get("ephemeral_5m_input_tokens")
                long = ttl_fields.get("ephemeral_1h_input_tokens")
                if type(short) is not int or type(long) is not int or short < 0 or long < 0:
                    return False
                creation = fields.get("cache_creation_input_tokens")
                nested_creation = detail_fields.get("cache_creation_tokens")
                if type(creation) is not int or short + long != creation:
                    return False
                if nested_creation is not None and (type(nested_creation) is not int or nested_creation != creation):
                    return False
    return True


@observation_boundary(
    tier=3,
    source="LiteLLM get_model_info pricing metadata for the requested model",
    source_param="model_info",
    suppresses=("R1", "R5"),
    invariant="requires an explicit finite nonnegative numeric rate for each charged cache-write duration; absent rates are never free",
)
def _cache_write_prices_available(
    model_info: Any, *, short_writes: bool, long_writes: bool, prompt_tokens: int, service_tier: str | None
) -> bool:
    """Validate the effective cache rates selected by LiteLLM's flat catalog.

    Match its strict-above input threshold and service-tier/base fallback.
    Structured tier tables need a separate admission contract; do not infer
    rates from those tables or substitute zero for a missing duration price.
    """
    if not isinstance(model_info, Mapping):
        return False
    if model_info.get("tiered_pricing") is not None:
        return False
    tier_suffix = f"_{service_tier}" if service_tier in ("priority", "flex") else ""
    active_threshold = 0.0
    threshold_suffix = ""
    for key, value in model_info.items():
        if type(key) is not str or value is None:
            continue
        match = re.fullmatch(r"input_cost_per_token_above_(\d+(?:\.\d+)?)(k?)_tokens", key)
        if match is None:
            continue
        threshold = float(match[1]) * (1000 if match[2] else 1)
        if active_threshold < threshold < prompt_tokens:
            active_threshold = threshold
            threshold_suffix = key.removeprefix("input_cost_per_token")

    for required, key in (
        (short_writes, "cache_creation_input_token_cost"),
        (long_writes, "cache_creation_input_token_cost_above_1hr"),
    ):
        if required:
            # The SDK applies service tiers to short-write base rates, and
            # to both durations' threshold rates; one-hour base is untiered.
            base_key = key + tier_suffix if key == "cache_creation_input_token_cost" else key
            selected = model_info.get(base_key)
            if selected is None:
                selected = model_info.get(key)
            if threshold_suffix:
                threshold_key = key + threshold_suffix
                threshold_rate = model_info.get(threshold_key + tier_suffix)
                if threshold_rate is None:
                    threshold_rate = model_info.get(threshold_key)
                if threshold_rate is not None:
                    selected = threshold_rate
            rate, _ = validated_provider_cost(selected, PROVIDER_COST_SOURCE_COST_PER_TOKEN)
            if rate is None:
                return False
    return True


@observation_boundary(
    tier=3,
    source="LiteLLM's raw model pricing catalog and SDK-resolved model identity",
    source_param="catalog",
    suppresses=("R1", "R5"),
    invariant="requires a real catalog entry with explicit finite nonnegative input and output token rates; unknown models are never free",
)
def _catalog_token_prices_available(catalog: Any, *, model_info: Any) -> bool:
    """Reject the zero prices LiteLLM synthesizes for unknown deployments.

    Use the SDK's resolved key so supported provider prefixes and aliases
    retain their catalog identity. Check the raw entry because get_model_info
    fills absent rates with zero, including for entirely unknown models.
    """
    key = _provider_field(model_info, "key")
    if type(key) is not str or not isinstance(catalog, Mapping) or key not in catalog:
        return False
    entry = catalog[key]
    if not isinstance(entry, Mapping):
        return False
    for name in ("input_cost_per_token", "output_cost_per_token"):
        rate, _ = validated_provider_cost(entry.get(name), PROVIDER_COST_SOURCE_COST_PER_TOKEN)
        if rate is None:
            return False
    return True


def calculate_missing_provider_cost(
    usage: Any | None, *, pricing_model: str | None, service_tier: Any | None = None
) -> tuple[float | None, ComposerLLMProviderCostSource]:
    """Price reported usage against the caller-selected catalogue identity.

    LiteLLM can omit cost metadata when Azure returns an unpriceable deployment
    name. The independently configured pricing model can have a catalog price. Missing
    counters must not trigger LiteLLM's text-based token estimation; an unknown
    price or malformed calculation remains unavailable to the planner cap.
    """
    if not _pricing_usage_optional_fields_valid(usage):
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if service_tier is not None and (type(service_tier) is not str or service_tier not in ("default", "standard", "priority", "flex")):
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    reported = _token_usage_from_usage(usage)
    if pricing_model is None or reported.prompt_tokens is None or reported.completion_tokens is None:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if reported.cached_prompt_tokens is not None and reported.cached_prompt_tokens > reported.prompt_tokens:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if reported.reasoning_tokens is not None and reported.reasoning_tokens > reported.completion_tokens:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE

    try:
        import litellm
    except ModuleNotFoundError as exc:
        # The AWS provider pack can run without the optional LLM pricing
        # dependency. A broken installed SDK is a different failure: retain
        # missing transitive modules instead of misreporting absent pricing.
        if exc.name != "litellm":
            raise
        _log.debug("planner_cost_recovery", reason="pricing_dependency_unavailable")
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE

    try:
        model_info = litellm.get_model_info(model=pricing_model)
        if not _catalog_token_prices_available(litellm.model_cost, model_info=model_info):
            return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
        # Reconstruct only admitted counters. Passing the original response
        # to completion_cost permits LiteLLM to select Azure's returned alias
        # instead of the supplied pricing identity, losing known pricing.
        prompt_details: dict[str, int | dict[str, int] | None] = {}
        if reported.cached_prompt_tokens is not None:
            prompt_details["cached_tokens"] = reported.cached_prompt_tokens
        source_details = _provider_field(usage, "prompt_tokens_details")
        creation_details = _provider_field(source_details, "cache_creation_token_details")
        if reported.cache_creation_input_tokens:
            short_writes = creation_details is None or _provider_field(creation_details, "ephemeral_5m_input_tokens") > 0
            long_writes = creation_details is not None and _provider_field(creation_details, "ephemeral_1h_input_tokens") > 0
            if not _cache_write_prices_available(
                model_info,
                short_writes=short_writes,
                long_writes=long_writes,
                prompt_tokens=reported.prompt_tokens,
                service_tier=service_tier,
            ):
                return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
        if creation_details is not None:
            prompt_details["cache_creation_tokens"] = reported.cache_creation_input_tokens
            prompt_details["cache_creation_token_details"] = {
                "ephemeral_5m_input_tokens": _provider_field(creation_details, "ephemeral_5m_input_tokens"),
                "ephemeral_1h_input_tokens": _provider_field(creation_details, "ephemeral_1h_input_tokens"),
            }
        pricing_usage = litellm.Usage(
            prompt_tokens=reported.prompt_tokens,
            completion_tokens=reported.completion_tokens,
            total_tokens=reported.prompt_tokens + reported.completion_tokens,
            prompt_tokens_details=prompt_details or None,
            completion_tokens_details={"reasoning_tokens": reported.reasoning_tokens} if reported.reasoning_tokens is not None else None,
            cache_creation_input_tokens=reported.cache_creation_input_tokens,
            cache_read_input_tokens=reported.cache_read_input_tokens,
        )
        costs = litellm.cost_per_token(
            model=pricing_model,
            prompt_tokens=reported.prompt_tokens,
            completion_tokens=reported.completion_tokens,
            usage_object=pricing_usage,
            service_tier=service_tier,
        )
    except Exception as exc:
        # The external SDK raises several exception types for unsupported
        # models and malformed usage. Preserve unavailable cost, never zero.
        # DEBUG-only process diagnostics: arbitrary SDK exception prose may
        # contain request content or credentials and must never reach logs.
        _log.debug(
            "planner_cost_recovery",
            reason="calculator_exception",
            error_class=type(exc).__name__,
            model_identity_hash=stable_hash({"pricing_model": pricing_model}),
            prompt_tokens=reported.prompt_tokens,
            completion_tokens=reported.completion_tokens,
            cached_prompt_tokens=reported.cached_prompt_tokens,
            reasoning_tokens=reported.reasoning_tokens,
            cache_creation_input_tokens=reported.cache_creation_input_tokens,
            cache_read_input_tokens=reported.cache_read_input_tokens,
        )
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    return _validated_calculated_cost(costs)


@observation_boundary(
    tier=3,
    source="the external LiteLLM cost_per_token calculator result",
    source_param="costs",
    suppresses=("R5",),
    invariant="admits only a two-element tuple of finite nonnegative costs with a finite sum; malformed results remain unavailable",
)
def _validated_calculated_cost(costs: Any) -> tuple[float | None, ComposerLLMProviderCostSource]:
    if not isinstance(costs, tuple) or len(costs) != 2:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    prompt_cost, _ = validated_provider_cost(costs[0], PROVIDER_COST_SOURCE_COST_PER_TOKEN)
    completion_cost, _ = validated_provider_cost(costs[1], PROVIDER_COST_SOURCE_COST_PER_TOKEN)
    if prompt_cost is None or completion_cost is None:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    return validated_provider_cost(prompt_cost + completion_cost, PROVIDER_COST_SOURCE_COST_PER_TOKEN)


def validated_provider_cost(
    raw_cost: Any,
    source: ComposerLLMProviderCostSource,
) -> tuple[float | None, ComposerLLMProviderCostSource]:
    if type(raw_cost) is bool or type(raw_cost) not in (int, float):
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    try:
        cost = float(cast(int | float, raw_cost))
    except OverflowError:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    if not math.isfinite(cost) or cost < 0:
        return None, PROVIDER_COST_SOURCE_NOT_AVAILABLE
    return cost, source
