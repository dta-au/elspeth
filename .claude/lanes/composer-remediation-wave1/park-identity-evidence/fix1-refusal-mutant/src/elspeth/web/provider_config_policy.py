"""Web-authored provider configuration policy helpers."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Final

from pydantic import TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from elspeth.contracts.trust_boundary import observation_boundary
from elspeth.plugins.transforms.llm.providers.openrouter import (
    OPENROUTER_BASE_URL,
    normalize_openrouter_base_url,
)

WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS: Final[int] = 30

LLM_BASE_URL_POLICY_ERROR: Final[str] = (
    "Web-authored OpenRouter LLM nodes may not override base_url. The api_key is "
    "resolved server-side, so a custom base_url — a loopback/private address or any "
    "non-canonical host — would direct the server-held bearer credential to an "
    "author-chosen destination (a credential-egress / SSRF path). Omit base_url to use "
    f"the canonical OpenRouter endpoint ({OPENROUTER_BASE_URL}); a private OpenAI-compatible "
    "gateway requires an operator-controlled runtime outside the web composer."
)
LLM_TRACING_POLICY_ERROR: Final[str] = (
    "Web-authored LLM nodes may not configure tracing. Tracing can send server-held "
    "credentials and pipeline inputs or outputs to a configured destination; use "
    "operator-controlled runtime configuration instead."
)
LLM_RETRY_BUDGET_POLICY_ERROR: Final[str] = (
    "Web-authored sequential multi-query LLM nodes must explicitly set "
    f"max_capacity_retry_seconds <= {WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS}. "
    "The LLM transform default is one hour, which can monopolize the web execution worker. "
    "Set max_capacity_retry_seconds to a small positive value or use pool_size > 1 for pooled retry handling."
)
AWS_S3_SOURCE_POLICY_ERROR: Final[str] = (
    "Web-authored aws_s3 sources require an available operator profile that fixes the bucket, "
    "optional prefix, deployment region, and default-chain authentication. Ask an operator to "
    "configure that profile, or use a batch/CLI runtime for raw S3 source options."
)
AWS_S3_ENDPOINT_URL_POLICY_ERROR: Final[str] = (
    "Web-authored aws_s3 source and sink options may not set endpoint_url. "
    "Custom storage endpoints can redirect server-side requests to an author-chosen "
    "destination; omit endpoint_url and use operator-controlled AWS configuration."
)

_INT_ADAPTER: Final[TypeAdapter[int]] = TypeAdapter(int)


@observation_boundary(
    tier=3,
    source="web-authored aws_s3 source plugin (untrusted author-supplied selection)",
    source_param="plugin",
    suppresses=("R1", "R5"),
    invariant=(
        "non-aws_s3 source plugins are ignored; an aws_s3 source with an available operator profile "
        "is admitted; otherwise returns one static profile-required policy error; never raises"
    ),
)
def web_aws_s3_source_policy_error(
    plugin: str | None,
    *,
    operator_profile_available: bool = False,
) -> str | None:
    """Require profile-derived authority for a Web-authored AWS S3 source."""
    if plugin != "aws_s3":
        return None
    if operator_profile_available:
        return None
    return AWS_S3_SOURCE_POLICY_ERROR


@observation_boundary(
    tier=3,
    source="web-authored aws_s3 source/sink options (untrusted author-supplied mapping)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "non-aws_s3 plugins are ignored; omitted or explicit-null endpoint_url is allowed; "
        "every non-null aws_s3 endpoint_url returns the static policy error; never raises"
    ),
)
def web_aws_s3_endpoint_url_policy_error(
    plugin: str | None,
    options: Mapping[str, Any],
) -> str | None:
    """Reject non-null endpoint overrides in web-authored AWS S3 options."""
    if plugin != "aws_s3":
        return None
    if options.get("endpoint_url") is None:
        return None
    return AWS_S3_ENDPOINT_URL_POLICY_ERROR


def _positive_int_or_none(value: object) -> int | None:
    try:
        parsed = _INT_ADAPTER.validate_python(value)
    except PydanticValidationError:
        return None
    if parsed > 0:
        return parsed
    return None


@observation_boundary(
    tier=3,
    source="web/composer-authored LLM transform options (untrusted author-supplied mapping)",
    source_param="options",
    suppresses=("R1",),
    invariant=(
        "absent 'queries' means no multi-query retry-budget policy applies (None); a "
        "malformed or unbounded retry budget returns LLM_RETRY_BUDGET_POLICY_ERROR; "
        "never raises on malformed options"
    ),
)
def web_llm_retry_budget_policy_error(options: Mapping[str, Any]) -> str | None:
    """Reject known-LLM sequential multi-query configs with unbounded local retries."""
    if options.get("queries") is None:
        return None

    pool_size = _positive_int_or_none(options.get("pool_size", 1))
    if pool_size is None:
        return LLM_RETRY_BUDGET_POLICY_ERROR
    if pool_size > 1:
        return None

    max_retry_seconds = _positive_int_or_none(options.get("max_capacity_retry_seconds"))
    if max_retry_seconds is None or max_retry_seconds > WEB_LLM_SEQUENTIAL_MULTI_QUERY_MAX_RETRY_SECONDS:
        return LLM_RETRY_BUDGET_POLICY_ERROR

    return None


@observation_boundary(
    tier=3,
    source="web-authored pipeline LLM provider config (untrusted author-supplied options mapping)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "omitted base_url falls back to the canonical OpenRouter endpoint (None); an "
        "explicit base_url is rejected with LLM_BASE_URL_POLICY_ERROR unless it "
        "normalises to the canonical endpoint; never raises on malformed options"
    ),
)
def web_llm_base_url_policy_error(options: Mapping[str, Any]) -> str | None:
    """Reject known-LLM web-authored configs that override OpenRouter base_url.

    The OpenRouter provider sends ``Authorization: Bearer <api_key>`` to whatever
    ``base_url`` names. In a web-authored pipeline the ``api_key`` is resolved
    server-side (a ``{"secret_ref": ...}`` against the deployment secret
    inventory, which may be a *server*-scoped credential the author cannot read),
    while ``base_url`` is set by the untrusted pipeline author. That asymmetry
    turns a custom base_url into a credential-egress / SSRF vector — the author
    can direct the server's bearer to a loopback service, a private host, or an
    attacker-controlled public host.

    The plugin config-validator deliberately tolerates HTTP loopback so the
    *CLI* dev examples (local ChaosLLM at ``http://127.0.0.1:8199/v1``) run; that
    single-machine threat model does not hold for a hosted server, so the web
    execution boundary pins base_url here. An unset base_url falls back to the
    canonical OpenRouter endpoint and is always allowed; an explicit base_url is
    allowed only when it normalises to that same canonical endpoint. Private
    OpenAI-compatible gateways are an operator-controlled runtime concern, not a
    web-author option — mirroring the managed-identity and web_scrape network
    policies.
    """
    base_url = options.get("base_url")
    if base_url is None:
        return None
    if not isinstance(base_url, str):
        # Non-string base_url is rejected at config construction (pydantic); the
        # network-policy gate only adjudicates author-chosen string endpoints.
        return None
    try:
        normalized_base_url = normalize_openrouter_base_url(base_url.strip())
    except (TypeError, ValueError):
        # Malformed author-controlled URLs are unsafe overrides. Keep the
        # diagnostic static so the submitted value is never reflected.
        return LLM_BASE_URL_POLICY_ERROR
    if normalized_base_url == normalize_openrouter_base_url(OPENROUTER_BASE_URL):
        return None
    return LLM_BASE_URL_POLICY_ERROR


@observation_boundary(
    tier=3,
    source="web-authored LLM tracing options (untrusted author-supplied mapping)",
    source_param="options",
    suppresses=("R1", "R5"),
    invariant=(
        "absent/null tracing returns None; every non-null LLM tracing "
        "value returns a static policy error without inspecting or echoing nested values; never raises"
    ),
)
def web_llm_tracing_policy_error(options: Mapping[str, Any]) -> str | None:
    """Reject every known-LLM author-supplied tracing configuration."""
    if options.get("tracing") is None:
        return None
    return LLM_TRACING_POLICY_ERROR
