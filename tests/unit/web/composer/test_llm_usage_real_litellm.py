"""``token_usage_from_response`` driven by real litellm objects.

Why this file exists
--------------------
``token_usage_from_response`` reads provider token counts off a litellm
``ModelResponse``. Every other test of that path used hand-written dataclass
fakes, and those fakes were *more conventional than reality*: they declared
``usage`` on the response and ``cache_creation_input_tokens`` /
``cache_read_input_tokens`` on the usage object as ordinary attributes. Real
litellm declares none of them. A regression that broke the extras read would
have left every fake-based test green (elspeth-6664a00cb0; the same shape as
the P0 elspeth-9ea866438b, where a typed fake satisfied a guard that real
litellm objects failed).

Measured facts about litellm 1.85.0 that this file pins
-------------------------------------------------------
``ModelResponse.model_fields`` is ``{choices, created, id, model, object,
system_fingerprint}``. ``usage`` is **not** a declared field:
``hasattr(ModelResponse(), "usage")`` is ``False``, and both construction
paths — the ``ModelResponse(..., usage=...)`` kwarg that litellm's own
``completion()`` uses, and a later ``response.usage = ...`` — deposit it in
``__pydantic_extra__`` (the model sets ``extra="allow"``). It never appears
in ``vars(response)``.

``Usage.model_fields`` is ``{completion_tokens, completion_tokens_details,
cost, prompt_tokens, prompt_tokens_details, server_tool_use, total_tokens}``.
The Anthropic sibling counters ``cache_creation_input_tokens`` and
``cache_read_input_tokens`` are **not** declared either; they too land in
``__pydantic_extra__`` and are absent from ``vars(usage)``.

So production's ``_provider_field_map`` merging ``__pydantic_extra__`` over
``vars()`` is not defensive belt-and-braces — it is the *only* reason any of
these fields are readable at all. The fakes could not express that, because a
dataclass has no extras.

``Usage.__init__`` also synthesizes the nested OpenAI shape from the
Anthropic siblings (``cache_read_input_tokens=N`` yields
``prompt_tokens_details.cached_tokens=N``). That synthesis is the reason
``token_usage_from_response`` suppresses the nested shape when a sibling is
present, so it is asserted here directly as a canary: if litellm stops
synthesizing, the dedup rule would begin discarding a genuine independent
signal, and ``test_litellm_synthesizes_nested_cached_tokens_from_sibling``
fails loudly rather than the data silently disappearing.

Scope: this file drives ``token_usage_from_response`` directly with real
provider objects and no mocks. Mapping-shaped responses are a real provider
shape with their own production branch and stay covered by the fakes in
``test_service.py``.
"""

from __future__ import annotations

from litellm.types.utils import (
    Choices,
    CompletionTokensDetailsWrapper,
    Message,
    ModelResponse,
    PromptTokensDetailsWrapper,
    Usage,
)

from elspeth.web.composer.llm_response_parsing import token_usage_from_response


def _choices() -> list[Choices]:
    return [Choices(message=Message(content="Done.", role="assistant"), finish_reason="stop")]


def _response(usage: Usage | None) -> ModelResponse:
    """Build a real ``ModelResponse``, attaching usage the way litellm does.

    litellm's ``completion()`` passes ``usage`` as a constructor kwarg. It is
    not a declared field, so it lands in ``__pydantic_extra__`` — verified
    identical to the ``response.usage = ...`` setattr form.
    """
    if usage is None:
        return ModelResponse(choices=_choices(), model="anthropic/claude-sonnet-4.6", id="msg_test")
    return ModelResponse(
        choices=_choices(),
        model="anthropic/claude-sonnet-4.6",
        id="msg_test",
        usage=usage,
    )


# ---------------------------------------------------------------------------
# The reality being reconciled against
# ---------------------------------------------------------------------------


def test_usage_is_not_a_declared_field_on_model_response() -> None:
    """``usage`` is dynamically attached, never declared.

    The fakes declared it. If a future litellm promotes it to a real field
    this test fails and the module docstring's premise needs rewriting — but
    production keeps working either way, because ``_provider_field_map``
    reads ``vars()`` as well as the extras.
    """
    assert "usage" not in ModelResponse.model_fields
    assert not hasattr(ModelResponse(), "usage")

    response = _response(Usage(prompt_tokens=1, completion_tokens=2, total_tokens=3))
    assert "usage" not in vars(response)
    assert "usage" in (response.__pydantic_extra__ or {})


def test_anthropic_cache_counters_live_in_pydantic_extra_not_declared_fields() -> None:
    """The Anthropic sibling counters are extras on the real ``Usage``."""
    assert "cache_creation_input_tokens" not in Usage.model_fields
    assert "cache_read_input_tokens" not in Usage.model_fields

    usage = Usage(
        prompt_tokens=8200,
        completion_tokens=120,
        total_tokens=8320,
        cache_creation_input_tokens=7000,
        cache_read_input_tokens=1100,
    )
    assert "cache_creation_input_tokens" not in vars(usage)
    assert "cache_read_input_tokens" not in vars(usage)
    assert (usage.__pydantic_extra__ or {}) == {
        "cache_creation_input_tokens": 7000,
        "cache_read_input_tokens": 1100,
    }


def test_litellm_synthesizes_nested_cached_tokens_from_sibling() -> None:
    """Constructing ``Usage`` with a sibling populates the nested OpenAI shape.

    This is the behaviour the dedup rule in ``token_usage_from_response``
    exists to neutralise. Asserting it here means a change in litellm surfaces
    as a failure rather than as silently discarded cache data.
    """
    usage = Usage(
        prompt_tokens=8200,
        completion_tokens=120,
        total_tokens=8320,
        cache_read_input_tokens=1100,
    )
    details = usage.prompt_tokens_details
    assert isinstance(details, PromptTokensDetailsWrapper)
    assert details.cached_tokens == 1100


# ---------------------------------------------------------------------------
# token_usage_from_response against real objects
# ---------------------------------------------------------------------------


def test_all_counts_present_land_verbatim() -> None:
    result = token_usage_from_response(_response(Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)))

    assert result.prompt_tokens == 100
    assert result.completion_tokens == 20
    assert result.reported_total == 120
    assert result.cached_prompt_tokens is None
    assert result.cache_creation_input_tokens is None
    assert result.cache_read_input_tokens is None
    assert result.reasoning_tokens is None


def test_openai_nested_cached_tokens_land_without_siblings() -> None:
    result = token_usage_from_response(
        _response(
            Usage(
                prompt_tokens=1200,
                completion_tokens=80,
                total_tokens=1280,
                prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=1024),  # type: ignore[no-untyped-call]
            )
        )
    )

    assert result.prompt_tokens == 1200
    assert result.cached_prompt_tokens == 1024
    assert result.cache_creation_input_tokens is None
    assert result.cache_read_input_tokens is None


def test_anthropic_sibling_cache_counters_are_read_from_extras() -> None:
    """The load-bearing case: both values live only in ``__pydantic_extra__``."""
    result = token_usage_from_response(
        _response(
            Usage(
                prompt_tokens=8200,
                completion_tokens=120,
                total_tokens=8320,
                cache_creation_input_tokens=7000,
                cache_read_input_tokens=1100,
            )
        )
    )

    assert result.cache_creation_input_tokens == 7000
    assert result.cache_read_input_tokens == 1100
    assert result.cached_prompt_tokens is None


def test_dual_shape_response_records_only_the_anthropic_signal() -> None:
    """Nested and sibling shapes carry the same counter; record it once."""
    result = token_usage_from_response(
        _response(
            Usage(
                prompt_tokens=8200,
                completion_tokens=120,
                total_tokens=8320,
                prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=1100),  # type: ignore[no-untyped-call]
                cache_creation_input_tokens=7000,
                cache_read_input_tokens=1100,
            )
        )
    )

    assert result.cache_creation_input_tokens == 7000
    assert result.cache_read_input_tokens == 1100
    assert result.cached_prompt_tokens is None


def test_completion_reasoning_tokens_land_from_the_nested_wrapper() -> None:
    result = token_usage_from_response(
        _response(
            Usage(
                prompt_tokens=5,
                completion_tokens=6,
                total_tokens=11,
                completion_tokens_details=CompletionTokensDetailsWrapper(reasoning_tokens=4),
            )
        )
    )

    assert result.reasoning_tokens == 4


# ---------------------------------------------------------------------------
# Absence vs zero — the fabrication policy
# ---------------------------------------------------------------------------


def test_cache_counters_absent_stay_none_never_zero() -> None:
    """A response with no cache metadata must not fabricate zeros."""
    result = token_usage_from_response(_response(Usage(prompt_tokens=100, completion_tokens=20, total_tokens=120)))

    assert result.cached_prompt_tokens is None
    assert result.cache_creation_input_tokens is None
    assert result.cache_read_input_tokens is None


def test_reported_zero_cache_read_is_preserved_as_zero() -> None:
    """A creation-only call reports a real ``0`` read; that zero is evidence.

    ``cache_read_input_tokens=0`` was asserted by the provider and survives.
    ``cached_prompt_tokens`` was never asserted — litellm synthesized it from
    the sibling — so it must stay ``None``. An auditor reading the row can
    therefore tell "reported zero" from "not reported", which is the whole
    reason ``TokenUsage`` is all-optional.
    """
    usage = Usage(
        prompt_tokens=7000,
        completion_tokens=80,
        total_tokens=7080,
        cache_creation_input_tokens=7000,
        cache_read_input_tokens=0,
    )
    # litellm did synthesize a nested zero — this is what must be suppressed.
    assert usage.prompt_tokens_details is not None
    assert usage.prompt_tokens_details.cached_tokens == 0

    result = token_usage_from_response(_response(usage))

    assert result.cache_creation_input_tokens == 7000
    assert result.cache_read_input_tokens == 0
    assert result.cached_prompt_tokens is None


def test_nested_zero_without_siblings_is_a_provider_assertion_and_survives() -> None:
    """Without an Anthropic sibling there is no synthesis to suppress.

    An OpenAI-family provider reporting ``cached_tokens=0`` asserted a
    zero-hit cache read; recording ``0`` is faithful, not fabricated.
    """
    result = token_usage_from_response(
        _response(
            Usage(
                prompt_tokens=10,
                completion_tokens=2,
                total_tokens=12,
                prompt_tokens_details=PromptTokensDetailsWrapper(cached_tokens=0),  # type: ignore[no-untyped-call]
            )
        )
    )

    assert result.cached_prompt_tokens == 0


def test_usage_absent_entirely_yields_all_unknown() -> None:
    """No ``usage`` attached at all: every counter is unknown, none is zero."""
    result = token_usage_from_response(_response(None))

    assert result.prompt_tokens is None
    assert result.completion_tokens is None
    assert result.reported_total is None
    assert result.cached_prompt_tokens is None
    assert result.cache_creation_input_tokens is None
    assert result.cache_read_input_tokens is None
    assert result.reasoning_tokens is None
    assert not result.has_data


def test_empty_usage_object_is_distinguishable_from_absent_usage() -> None:
    """A present-but-empty ``Usage`` is *not* the same as no usage.

    litellm defaults the three base counters to ``0`` on ``Usage()``, so a
    response carrying an empty usage object reports zeros. Production records
    what litellm asserted; the zeros originate upstream, not here. Contrast
    with ``test_usage_absent_entirely_yields_all_unknown`` — that distinction
    is exactly what an auditor needs, so pin both.
    """
    assert Usage().prompt_tokens == 0

    result = token_usage_from_response(_response(Usage()))

    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.reported_total == 0
    assert result.has_data
    # Cache counters are genuinely absent from an empty Usage.
    assert result.cached_prompt_tokens is None
    assert result.cache_creation_input_tokens is None
    assert result.cache_read_input_tokens is None


def test_response_is_none_yields_unknown() -> None:
    assert token_usage_from_response(None).has_data is False
