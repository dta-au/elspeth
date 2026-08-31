"""Shared option preparation for resolver-free plugin construction probes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from elspeth.contracts.freeze import deep_thaw
from elspeth.core.llm_profiles import LLM_PROFILE_PRIVATE_FIELDS
from elspeth.core.secrets import redact_secret_refs_for_validation


def prepare_validation_probe_options(options: Mapping[str, Any], *, plugin: str | None) -> dict[str, Any]:
    """Return detached runtime options safe for validation-only construction.

    ``plugin`` is required (``None`` for an unknown plugin) so a new probe
    call site cannot silently opt out of the deployment-injected stubs below —
    a forgotten argument is a TypeError, not a quietly zeroed contract.

    For a profile-authored ``plugin="llm"`` with no private binding fields, an
    inert gateway provider/model block is supplied for the probe. A
    profile-authored composer llm node (transform and source alike — both are
    named "llm") carries only its public ``profile`` alias: lowering removes
    that alias and injects the private ``provider``/``model``/credential binding
    (``plugin_policy/profiles.py::lower_options``), after Stage 1 has already
    run. The validation-only projection mirrors that executable shape by
    removing ``profile`` and supplying inert bindings. Probing construction on
    authored options alone otherwise fails on the missing private fields (and
    on ``profile`` being extra executable input), and
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
    if plugin == "llm" and "profile" in prepared and not set(prepared).intersection(LLM_PROFILE_PRIVATE_FIELDS):
        from elspeth.plugins.llm.config_validation import GATEWAY_SUPPORTED_CAPABILITIES

        # ``profile`` is public authoring input, not executable plugin config;
        # trusted lowering consumes it before writing the private provider and
        # model binding. The branch arms only when every private field is
        # absent: a malformed profile-plus-private-field draft stays malformed
        # and fails closed instead of borrowing this stub.
        del prepared["profile"]
        prepared["provider"] = "gateway"
        prepared["model"] = "validation-probe-model"
        prepared["endpoint"] = "https://validation-probe.invalid/v1"
        prepared["api_key"] = "validation-probe-placeholder"
        prepared["required_capabilities"] = tuple(sorted(GATEWAY_SUPPORTED_CAPABILITIES))
    return prepared
