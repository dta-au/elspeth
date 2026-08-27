"""Shared option preparation for resolver-free plugin construction probes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.secrets import redact_secret_refs_for_validation


def prepare_validation_probe_options(options: Mapping[str, Any], *, plugin: str | None) -> dict[str, Any]:
    """Return detached runtime options safe for validation-only construction.

    ``plugin`` is required (``None`` for an unknown plugin) so a new probe
    call site cannot silently opt out of the deployment-injected stubs below —
    a forgotten argument is a TypeError, not a quietly zeroed contract.

    For ``plugin="llm"`` with no authored ``provider``, an inert gateway
    provider block is supplied for the probe. A composer llm node (transform
    and source alike — both are named "llm") never carries its provider block:
    the operator profile injects ``provider``/``endpoint``/``api_key`` at
    lowering (``plugin_policy/profiles.py::lower_options``), after Stage 1 has
    already run. Probing construction on the authored options alone therefore
    failed on ``provider: Field required`` for EVERY composer llm node, and
    ``_effective_producer_vote``'s known-pass-through fail-closed arm turned
    that permanent condition into "participates with zero guarantees" — a
    false ``guarantees: [(none)]`` reject for any required-fields consumer
    downstream of any llm (elspeth-d4ae04b374).

    The llm output contract is a pure function of the provider-INDEPENDENT
    config (schema block, ``response_field``, ``queries``/``output_fields``) —
    the provider instance is not even built until ``on_start()`` — so a stub
    provider yields the same contract math as the lowered runtime build.
    Gateway is the stub because its ``model`` is a logical alias with no local
    catalog to validate against — exactly the shape of composer-authored model
    names. ``required_capabilities`` derives from the closed vocabulary rather
    than restating it, so a structured-output node (which demands
    ``json_schema``) still constructs. An authored ``provider`` — a YAML
    import that carries its own — is left to stand on its own config.
    """
    from elspeth.web.interpretation_state import strip_authoring_options

    thawed = cast(dict[str, Any], deep_thaw(options))
    runtime_options = strip_authoring_options(thawed)
    prepared = redact_secret_refs_for_validation(runtime_options)
    if plugin == "llm" and "provider" not in prepared:
        from elspeth.plugins.llm.config_validation import GATEWAY_SUPPORTED_CAPABILITIES

        # Plain assignment, not setdefault: with no authored provider the
        # whole provider block is the stub's to own — a stray endpoint or
        # api_key without a provider is not a configuration the runtime can
        # ever see (lowering writes the full block), so preserving one here
        # would probe a config that exists on no surface.
        prepared["provider"] = "gateway"
        prepared["endpoint"] = "https://validation-probe.invalid/v1"
        prepared["api_key"] = "validation-probe-placeholder"
        prepared["required_capabilities"] = tuple(sorted(GATEWAY_SUPPORTED_CAPABILITIES))
    return prepared
