"""Shared option preparation for resolver-free plugin construction probes."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, cast

from elspeth.contracts.blobs_inline import is_widened_blob_ref
from elspeth.contracts.freeze import deep_thaw
from elspeth.core.llm_profiles import LLM_PROFILE_PRIVATE_FIELDS
from elspeth.core.secrets import redact_secret_refs_for_validation


class DeferredBlobContractProbe(Exception):
    """A constructor needs blob bytes unavailable to a resolver-free probe."""


def is_inline_content_reference(value: object) -> bool:
    """Recognize an unresolved value only when it is a valid inline marker."""
    marker = is_widened_blob_ref(value)
    return marker is not None and marker.mode == "inline_content"


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
    # Approval evidence is server-stamped after review and is retained by the
    # runtime config. It is not a private provider binding: its presence must
    # not disable the same contract probe that worked before approval.
    binding_fields = LLM_PROFILE_PRIVATE_FIELDS - {"approved_prompt_artifact_hash"}
    if plugin == "llm" and "profile" in prepared and not set(prepared).intersection(binding_fields):
        from elspeth.plugins.llm.config_validation import GATEWAY_SUPPORTED_CAPABILITIES

        # ``profile`` is public authoring input, not executable plugin config;
        # trusted lowering consumes it before writing the private provider and
        # model binding. The branch arms only when every private provider-binding
        # field is absent; approval evidence is retained and validated. A malformed
        # profile-plus-private-binding draft still fails closed.
        del prepared["profile"]
        prepared["provider"] = "gateway"
        prepared["model"] = "validation-probe-model"
        prepared["endpoint"] = "https://validation-probe.invalid/v1"
        prepared["api_key"] = "validation-probe-placeholder"
        prepared["required_capabilities"] = tuple(sorted(GATEWAY_SUPPORTED_CAPABILITIES))
    return prepared
