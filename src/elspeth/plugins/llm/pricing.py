"""Observe monetary evidence on HTTP LLM failures without replacing the failure."""

from elspeth.contracts.composer_llm_audit import ComposerLLMProviderCostSource
from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.core.llm_pricing import provider_cost_from_response
from elspeth.plugins.infrastructure.clients.json_utils import parse_json_strict


@observation_boundary(
    tier=3,
    source="the external HTTP LLM provider response body, decoded as strict JSON",
    source_param="body",
    suppresses=("R5",),
    invariant=(
        "admits only a JSON object to shared provider cost parsing; malformed JSON and non-object bodies "
        "remain explicitly unavailable without replacing the original provider failure"
    ),
)
def observe_http_provider_cost(body: bytes, *, model_requested: str) -> tuple[float | None, ComposerLLMProviderCostSource]:
    """Keep billable usage from valid JSON even when its completion is unusable."""
    try:
        response, error = parse_json_strict(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        return None, "not_available"
    if error is not None or not isinstance(response, dict):
        return None, "not_available"
    return provider_cost_from_response(response, pricing_model=model_requested)
