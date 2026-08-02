"""Tests for the operator-owned LLM profile catalog on ElspethSettings.

Phase 2 Task 5 of the LLM gateway integration plan gives batch/CLI runs the
same operator profile catalog the web plugin-policy seam already has
(``elspeth.web.config.WebSettings.llm_profiles``): a top-level
``ElspethSettings.llm_profiles`` mapping plus ``default_llm_profile``, and an
``llm`` transform node that may select a profile alias (``options:
{"profile": "alias", ...}``) instead of carrying explicit provider config.

The design's acceptance criterion is that web and batch lower the SAME
profile to identical private executable options and identical audit-safe
projections — both surfaces call the single shared
``elspeth.core.llm_profiles.lower_llm_profile_options``. These tests prove:

- the catalog + default-alias parse and validate (unknown alias, bad alias
  syntax, unsupported provider/contract_major/capability all fail closed —
  the last three for free, by reusing ``LLMProfileSettings``'s existing
  validators);
- an `llm` node selecting a profile fails closed at config-load time for an
  unknown alias, an ambiguous `profile`+`provider` combination, a missing
  server secret, and a `user`-scoped profile (batch has no per-user secret
  store);
- a profile-selecting node lowers to the same private executable options
  (materialized end to end) and the shared lowering step's output is
  BYTE-IDENTICAL to what the web resolver produces for the same profile;
- existing explicit provider (azure/openrouter/bedrock) `llm` node configs
  are unaffected by a populated `llm_profiles` catalog;
- a `materialize=False` pass (the trusted-host-env-expansion gate closed)
  leaves a valid-alias node completely un-lowered rather than partially
  rewritten — pinned so it stays an explicit, tested contract rather than an
  incidental side effect of the gate; and
- a profile-selecting BATCH node's alias is recoverable from the run's own
  audit trail (`resolve_config()`'s settings_json snapshot), matching what
  `run_web_plugin_policy.selected_profile_aliases_json` already gives web —
  and that the alias value is never the profile's endpoint or credential_ref
  (retained under the distinct `profile_alias` key, never the authored
  `profile` selector key itself); and
- lowering is idempotent over the same owned in-memory output, while YAML/JSON
  replay loses that ownership proof and fails closed; a genuinely ambiguous
  AUTHORED node (`profile` and `provider` both written by hand) also still
  fails closed.
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
import yaml
from pydantic import ValidationError

from elspeth.core.config import (
    ElspethSettings,
    _lower_llm_profile_node_options,
    _lower_llm_profile_nodes,
    load_settings_from_config_dict,
    load_settings_from_yaml_string,
    resolve_config,
)
from elspeth.core.llm_profiles import LLMProfileSettings, LoweredLLMProfileAlias, RuntimeLLMProfile
from elspeth.plugins.sources.llm import LLMSource
from elspeth.plugins.transforms.llm.transform import LLMTransform
from elspeth.web.plugin_policy.profiles import _LLMProfileResolver

_GATEWAY_PROFILE: dict[str, object] = {
    "provider": "gateway",
    "model": "standard",
    "credential_scope": "server",
    "credential_ref": "LLM_GATEWAY_BEARER_TOKEN",
    "endpoint": "http://127.0.0.1:8787/v1",
    "contract_major": 1,
    "required_capabilities": ["text", "tools", "usage"],
    "timeout_seconds": 60,
}


def _llm_node(options: dict[str, object]) -> dict[str, object]:
    return {
        "name": "llm_node",
        "plugin": "llm",
        "input": "primary",
        "on_success": "output",
        "on_error": "discard",
        "options": options,
    }


def _config(
    *, transforms: list[dict[str, object]], llm_profiles: dict[str, object] | None = None, default_llm_profile: str | None = None
) -> dict[str, object]:
    config: dict[str, object] = {
        "sources": {"primary": {"plugin": "csv", "on_success": "llm_node", "options": {"path": "in.csv"}}},
        "sinks": {"output": {"plugin": "csv", "on_write_failure": "discard", "options": {"path": "out.csv"}}},
        "transforms": transforms,
    }
    if llm_profiles is not None:
        config["llm_profiles"] = llm_profiles
    if default_llm_profile is not None:
        config["default_llm_profile"] = default_llm_profile
    return config


# ---------------------------------------------------------------------------
# Catalog parsing / validation
# ---------------------------------------------------------------------------


class TestLLMProfileCatalogParsing:
    def test_empty_catalog_is_the_default(self) -> None:
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
        )
        assert settings.llm_profiles == {}
        assert settings.default_llm_profile is None

    def test_catalog_parses_a_gateway_profile(self) -> None:
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        assert "gw-primary" in settings.llm_profiles
        assert settings.llm_profiles["gw-primary"].provider == "gateway"

    def test_rejects_malformed_alias(self) -> None:
        with pytest.raises(ValidationError, match="lowercase opaque identifier"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                llm_profiles={"Not-Valid": _GATEWAY_PROFILE},
            )

    def test_default_llm_profile_must_name_a_configured_alias(self) -> None:
        with pytest.raises(ValidationError, match="must name a configured llm profile"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                llm_profiles={"gw-primary": _GATEWAY_PROFILE},
                default_llm_profile="not-defined",
            )

    def test_default_llm_profile_absent_is_a_supported_degraded_state(self) -> None:
        settings = ElspethSettings(
            sources={"primary": {"plugin": "csv", "on_success": "output"}},
            sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        assert settings.default_llm_profile is None

    def test_rejects_unsupported_provider(self) -> None:
        with pytest.raises(ValidationError, match="not registered"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                llm_profiles={"bad": {**_GATEWAY_PROFILE, "provider": "not_a_real_provider"}},
            )

    def test_rejects_unsupported_contract_major(self) -> None:
        with pytest.raises(ValidationError, match="not supported"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                llm_profiles={"gw-primary": {**_GATEWAY_PROFILE, "contract_major": 2}},
            )

    def test_rejects_unknown_capability(self) -> None:
        with pytest.raises(ValidationError, match="unknown gateway capability"):
            ElspethSettings(
                sources={"primary": {"plugin": "csv", "on_success": "output"}},
                sinks={"output": {"plugin": "csv", "on_write_failure": "discard"}},
                llm_profiles={"gw-primary": {**_GATEWAY_PROFILE, "required_capabilities": ["streaming"]}},
            )


# ---------------------------------------------------------------------------
# Batch/CLI node lowering — materialized end to end
# ---------------------------------------------------------------------------


class TestBatchProfileNodeLowering:
    def test_profile_node_lowers_to_executable_provider_config(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
            default_llm_profile="gw-primary",
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        options = settings.transforms[0].options
        assert options["provider"] == "gateway"
        assert options["model"] == "standard"
        assert options["endpoint"] == "http://127.0.0.1:8787/v1"
        assert options["contract_major"] == 1
        assert options["required_capabilities"] == ("text", "tools", "usage")
        assert options["api_key"] == "sk-test-value"
        # The alias is RETAINED (not stripped) in the executable options,
        # under a key DISTINCT from the authored `profile` selector — a
        # provenance-only marker so it is recoverable from the run's own
        # audit trail (see TestBatchRunAuditRecordsAlias below) without ever
        # putting a lowered node back into the exact shape the ambiguity
        # check rejects (`profile` + `provider` both present — see
        # TestLoweringIsRoundTripSafe). LLMTransform.__init__ excludes
        # `profile_alias` before provider construction.
        assert options["profile_alias"] == "gw-primary"
        assert type(options["profile_alias"]) is LoweredLLMProfileAlias
        assert "profile" not in options
        assert options["prompt_template"] == "{{ row }}"

    def test_unknown_alias_fails_closed(self) -> None:
        cfg = _config(
            transforms=[_llm_node({"profile": "nope", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        with pytest.raises(ValueError, match="unknown llm profile"):
            load_settings_from_config_dict(cfg, expand_env_vars=True)

    def test_profile_and_provider_together_is_rejected(self) -> None:
        cfg = _config(
            transforms=[
                _llm_node(
                    {
                        "profile": "gw-primary",
                        "provider": "gateway",
                        "prompt_template": "{{ row }}",
                        "schema": {"mode": "observed"},
                    }
                )
            ],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        with pytest.raises(ValueError, match="both 'profile' and 'provider'"):
            load_settings_from_config_dict(cfg, expand_env_vars=True)

    def test_missing_server_secret_fails_closed(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("LLM_GATEWAY_BEARER_TOKEN", raising=False)
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        with pytest.raises(ValueError, match="LLM_GATEWAY_BEARER_TOKEN"):
            load_settings_from_config_dict(cfg, expand_env_vars=True)

    def test_user_scoped_profile_rejected_for_batch(self) -> None:
        # openrouter supports credential_scope "user"; batch has no per-user
        # secret store, so a batch node referencing such a profile must fail
        # closed rather than silently reading a shared-process env var as if
        # it were a scoped-per-user secret.
        cfg = _config(
            transforms=[_llm_node({"profile": "or-user", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={
                "or-user": {
                    "provider": "openrouter",
                    "model": "some-model",
                    "credential_scope": "user",
                    "credential_ref": "OPENROUTER_KEY",
                }
            },
        )
        with pytest.raises(ValueError, match="credential_scope"):
            load_settings_from_config_dict(cfg, expand_env_vars=True)

    def test_structural_checks_run_even_without_materialization(self) -> None:
        """Unknown-alias/ambiguity fail closed even when expand_env_vars=False.

        Only the credential-materializing rewrite is gated on trusted host
        environment expansion; the purely-structural checks require no
        secret material and always run.
        """
        cfg = _config(
            transforms=[_llm_node({"profile": "nope", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        with pytest.raises(ValueError, match="unknown llm profile"):
            load_settings_from_config_dict(cfg, expand_env_vars=False)

    def test_valid_alias_is_left_completely_un_lowered_when_materialize_is_false(self) -> None:
        """Pin the `materialize=False` pass-through contract explicitly.

        Only the credential-materializing REWRITE is gated on `materialize`;
        a node naming a VALID, unambiguous alias is not partially rewritten
        when the gate is closed — it is left exactly as authored (`options`
        still has `profile`, no `provider`). This state is unreachable in
        production (both real callers below only ever pass
        `materialize=True` for a trusted-env-expansion caller), but pin it
        so the pass-through is a tested contract rather than incidental
        behavior a future edit could quietly change.
        """
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=False)
        options = settings.transforms[0].options
        assert options["profile"] == "gw-primary"
        assert "provider" not in options
        assert "endpoint" not in options
        assert "api_key" not in options


# ---------------------------------------------------------------------------
# Existing explicit provider configs are unaffected by a populated catalog
# ---------------------------------------------------------------------------


class TestExplicitProviderNodesUnaffected:
    @pytest.mark.parametrize(
        "options",
        [
            {
                "provider": "azure",
                "model": "gpt-4",
                "endpoint": "https://x.openai.azure.com",
                "deployment_name": "gpt-4",
                "api_version": "2024-02-01",
                "api_key": "${AZURE_KEY}",
                "prompt_template": "{{ row }}",
                "schema": {"mode": "observed"},
            },
            {
                "provider": "openrouter",
                "model": "anthropic/claude-sonnet-4.6",
                "api_key": "${OPENROUTER_KEY}",
                "prompt_template": "{{ row }}",
                "schema": {"mode": "observed"},
            },
            {
                "provider": "bedrock",
                "model": "anthropic.claude-3",
                "region_name": "us-east-1",
                "prompt_template": "{{ row }}",
                "schema": {"mode": "observed"},
            },
        ],
    )
    def test_explicit_provider_node_passes_through_unchanged(self, options: dict[str, Any], monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("AZURE_KEY", "azure-secret")
        monkeypatch.setenv("OPENROUTER_KEY", "openrouter-secret")
        cfg = _config(
            transforms=[_llm_node(dict(options))],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
            default_llm_profile="gw-primary",
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        lowered = settings.transforms[0].options
        assert lowered["provider"] == options["provider"]
        assert "profile" not in lowered
        assert "profile_alias" not in lowered


# ---------------------------------------------------------------------------
# THE critical property: web and batch lower the SAME profile identically
# ---------------------------------------------------------------------------


class TestWebBatchLoweringEquality:
    """Web and batch profile aliases lower to identical private gateway
    bindings and audit-safe projections (design acceptance criterion).

    Both sides go through the actual code each authoring surface calls in
    production — the web resolver's ``lower_options`` and the batch loader's
    ``_lower_llm_profile_node_options`` — rather than calling the shared
    ``lower_llm_profile_options`` twice directly, which would only prove the
    shared function equals itself.

    ``_SAFE_OPTIONS`` deliberately has no ``"queries"`` key, so web's
    post-delegation ``max_capacity_retry_seconds`` default (applied AFTER
    the shared lowering step, in ``_LLMProfileResolver.lower_options`` only —
    a web-execution-worker safety policy with no batch equivalent, see that
    method's own comment) never fires here. Equality is demonstrated for the
    shared lowering step itself, not for multi-query profile nodes with that
    web-only default applied — a future reader should not assume the latter
    from this test.
    """

    _SAFE_OPTIONS: ClassVar[dict[str, object]] = {"prompt_template": "{{ row }}", "schema": {"mode": "observed"}}

    def _profile_settings(self) -> LLMProfileSettings:
        return LLMProfileSettings(**_GATEWAY_PROFILE)

    def test_pre_materialization_lowering_is_byte_identical(self) -> None:
        profile_settings = self._profile_settings()
        runtime_profile = RuntimeLLMProfile.from_settings("gw-primary", profile_settings)

        web_resolver = _LLMProfileResolver((("gw-primary", runtime_profile),), preferred_alias=None)
        web_lowered = web_resolver.lower_options("gw-primary", dict(self._SAFE_OPTIONS))

        batch_executable, batch_audit_safe = _lower_llm_profile_node_options(
            "gw-primary", profile_settings, {"profile": "gw-primary", **self._SAFE_OPTIONS}
        )

        assert dict(web_lowered.executable_options) == batch_executable
        assert dict(web_lowered.audit_safe_options) == batch_audit_safe
        # The secret is a REFERENCE at this stage on both sides, never a value.
        assert batch_executable["api_key"] == {"secret_ref": "LLM_GATEWAY_BEARER_TOKEN", "secret_scope": "server"}

    def test_materialized_batch_api_key_matches_the_shared_secret_reference(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """After each surface's OWN (pre-existing, unchanged) credential
        materialization step, the same underlying secret value resolves to
        the same literal ``api_key`` — proving the divergence is confined to
        HOW each surface resolves a reference, never WHAT reference is
        injected by the shared lowering step (asserted above).
        """
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", **self._SAFE_OPTIONS})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        assert settings.transforms[0].options["api_key"] == "sk-test-value"


# ---------------------------------------------------------------------------
# Fix round 1: the alias must be recoverable from the batch RUN's own audit
# trail — not just present in the in-memory lowered options.
# ---------------------------------------------------------------------------


class TestBatchRunAuditRecordsAlias:
    """A profile-selecting batch node's alias is recoverable from the run record.

    Web already answers "which operator profile did this node use?" via
    ``run_web_plugin_policy.selected_profile_aliases_json`` (a web-only
    Landscape table — its NOT-NULL web-policy-hash columns make it unusable
    for batch/CLI, which has no operator-profile-policy/HMAC-binding
    machinery to fill them honestly). Batch has no equivalent table, so the
    alias must survive into ``ElspethSettings`` itself: it travels inside the
    node's own (already free-form) ``options`` dict, which is exactly what
    ``resolve_config()`` dumps into the run's ``settings_json`` audit
    snapshot (``core/landscape/run_lifecycle_repository.py``) — no new
    column or table. ``LLMTransform.__init__`` excludes the marker before
    provider construction (no provider config model declares it), so real
    execution is unaffected.

    The alias rides under ``profile_alias``, NOT the authored ``profile``
    selector key — see ``TestLoweringIsRoundTripSafe`` for why reusing
    ``profile`` would be unsafe.
    """

    _SAFE_OPTIONS: ClassVar[dict[str, object]] = {"prompt_template": "{{ row }}", "schema": {"mode": "observed"}}

    def test_resolved_run_config_contains_the_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", **self._SAFE_OPTIONS})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        resolved = resolve_config(settings)
        node_options = resolved["transforms"][0]["options"]

        assert node_options["profile_alias"] == "gw-primary"
        # ALIAS ONLY: the recorded value is the opaque alias, never the
        # profile's private endpoint or its credential reference name.
        assert node_options["profile_alias"] != _GATEWAY_PROFILE["endpoint"]
        assert node_options["profile_alias"] != _GATEWAY_PROFILE["credential_ref"]
        assert _GATEWAY_PROFILE["endpoint"] not in node_options["profile_alias"]
        assert _GATEWAY_PROFILE["credential_ref"] not in node_options["profile_alias"]

    def test_llm_transform_constructs_and_retains_the_alias_for_audit(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Prove the DAG's per-node audit path (which reads the live plugin's
        ``.config`` — the same raw dict, per ``BaseTransform.__init__`` —
        rather than ``resolve_config()``'s settings-level dump) also carries
        the alias, and that provider construction still succeeds despite the
        extra key (``LLMTransform.__init__`` excludes it before
        ``config_cls.from_dict()``, which forbids unknown fields).
        """
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", **self._SAFE_OPTIONS})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        transform = LLMTransform(dict(settings.transforms[0].options))

        assert transform.config["profile_alias"] == "gw-primary"
        assert type(transform.config["profile_alias"]) is str
        assert transform._config.provider == "gateway"
        assert transform._config.model == "standard"

    def test_transform_rejects_plain_forged_profile_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        settings = load_settings_from_config_dict(
            _config(
                transforms=[
                    _llm_node(
                        {
                            "profile": "gw-primary",
                            "prompt_template": "{{ row }}",
                            "schema": {"mode": "observed"},
                        }
                    )
                ],
                llm_profiles={"gw-primary": _GATEWAY_PROFILE},
            ),
            expand_env_vars=True,
        )
        forged = dict(settings.transforms[0].options)
        forged["profile_alias"] = str(forged["profile_alias"])

        with pytest.raises(ValueError, match="reserved for trusted LLM profile lowering"):
            LLMTransform(forged)


# ---------------------------------------------------------------------------
# Fix round 2: in-memory idempotence must retain provenance, while serialized
# replay must not be silently re-trusted.
# ---------------------------------------------------------------------------


class TestLoweringIsRoundTripSafe:
    """The lowering pass trusts only its own still-owned in-memory output.

    A second pass over the same nominally marked object is identity-equivalent.
    YAML/JSON serialization erases the marker and must fail closed on re-entry,
    as must a genuinely ambiguous authored node with both selector forms.
    """

    def test_lowered_output_round_trips_without_raising(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        cfg = _config(
            transforms=[_llm_node({"profile": "gw-primary", "prompt_template": "{{ row }}", "schema": {"mode": "observed"}})],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        lowered_settings = load_settings_from_config_dict(cfg, expand_env_vars=True)
        lowered_options = dict(lowered_settings.transforms[0].options)
        # Sanity: this really is the shape the ambiguity check is checking
        # for — `provider` present — just keyed as `profile_alias`, not
        # `profile`.
        assert "provider" in lowered_options
        assert "profile_alias" in lowered_options
        assert type(lowered_options["profile_alias"]) is LoweredLLMProfileAlias
        assert "profile" not in lowered_options

        # Feed the still-owned in-memory output back through the same pass.
        # This must not raise or erase its nominal provenance marker.
        in_memory_cfg = _config(
            transforms=[_llm_node(dict(lowered_options))],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        again = load_settings_from_config_dict(in_memory_cfg, expand_env_vars=True)
        # No `profile` key means this pass has nothing to lower — the node
        # is inert to it, exactly like any other explicit-provider node.
        assert dict(again.transforms[0].options) == lowered_options
        assert type(again.transforms[0].options["profile_alias"]) is LoweredLLMProfileAlias

    def test_serialized_lowered_transform_is_not_retrusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-test-value")
        settings = load_settings_from_config_dict(
            _config(
                transforms=[
                    _llm_node(
                        {
                            "profile": "gw-primary",
                            "prompt_template": "{{ row }}",
                            "schema": {"mode": "observed"},
                        }
                    )
                ],
                llm_profiles={"gw-primary": _GATEWAY_PROFILE},
            ),
            expand_env_vars=True,
        )
        serialized = settings.model_dump(mode="json")

        with pytest.raises(ValueError, match="reserved for trusted LLM profile lowering"):
            load_settings_from_config_dict(serialized, expand_env_vars=True)

    def test_genuinely_ambiguous_authored_node_still_raises(self) -> None:
        """The ambiguity check remains intact — not just inert for lowered output."""
        cfg = _config(
            transforms=[
                _llm_node(
                    {
                        "profile": "gw-primary",
                        "provider": "gateway",
                        "prompt_template": "{{ row }}",
                        "schema": {"mode": "observed"},
                    }
                )
            ],
            llm_profiles={"gw-primary": _GATEWAY_PROFILE},
        )
        with pytest.raises(ValueError, match="both 'profile' and 'provider'"):
            load_settings_from_config_dict(cfg, expand_env_vars=True)


class TestBatchProfileSourceLowering:
    """The plural runtime source map gets the same profile lowering as transforms."""

    @staticmethod
    def _source_config(
        *,
        source_name: str = "source",
        options: dict[str, object] | None = None,
        transforms: object = None,
        profile: dict[str, object] | None = None,
    ) -> dict[str, object]:
        config: dict[str, object] = {
            "sources": {
                source_name: {
                    "plugin": "llm",
                    "on_success": "output",
                    "options": options
                    or {
                        "profile": "gw-primary",
                        "prompt_template": "Write one audit briefing.",
                        "response_field": "briefing",
                        "schema": {"mode": "observed"},
                        "on_validation_failure": "discard",
                    },
                }
            },
            "sinks": {
                "output": {
                    "plugin": "csv",
                    "on_write_failure": "discard",
                    "options": {"path": "out.csv"},
                }
            },
            "llm_profiles": {"gw-primary": profile or _GATEWAY_PROFILE},
        }
        if transforms is not None:
            config["transforms"] = transforms
        return config

    @pytest.mark.parametrize("source_name", ["source", "generated_brief"])
    def test_plural_source_lowers_without_a_transform_list(
        self,
        monkeypatch: pytest.MonkeyPatch,
        source_name: str,
    ) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-source-test")
        raw = self._source_config(source_name=source_name, transforms={})
        lowered = _lower_llm_profile_nodes(raw, materialize=True)
        del lowered["transforms"]
        settings = load_settings_from_config_dict(lowered, expand_env_vars=True)

        source_settings = settings.sources[source_name]
        assert source_settings.on_success == "output"
        assert source_settings.options["on_validation_failure"] == "discard"
        assert source_settings.options["provider"] == "gateway"
        assert source_settings.options["api_key"] == "sk-source-test"
        assert source_settings.options["profile_alias"] == "gw-primary"
        assert type(source_settings.options["profile_alias"]) is LoweredLLMProfileAlias
        assert "profile" not in source_settings.options

    def test_lowered_source_retains_audit_alias_and_constructs_strict_provider_config(
        self,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-source-test")
        settings = load_settings_from_config_dict(self._source_config(), expand_env_vars=True)

        source = LLMSource(dict(settings.sources["source"].options))
        resolved = resolve_config(settings)

        assert source.config["profile_alias"] == "gw-primary"
        assert type(source.config["profile_alias"]) is str
        assert source.provider_config.provider == "gateway"
        assert source.provider_config.model == "standard"
        assert resolved["sources"]["source"]["options"]["profile_alias"] == "gw-primary"
        assert "LLM_GATEWAY_BEARER_TOKEN" not in resolved["sources"]["source"]["options"]["profile_alias"]

    def test_source_rejects_plain_forged_profile_alias(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-source-test")
        settings = load_settings_from_config_dict(self._source_config(), expand_env_vars=True)
        forged = dict(settings.sources["source"].options)
        forged["profile_alias"] = str(forged["profile_alias"])

        with pytest.raises(ValueError, match="reserved for trusted LLM profile lowering"):
            LLMSource(forged)

    def test_source_unknown_alias_and_profile_provider_ambiguity_fail_closed(self) -> None:
        unknown = self._source_config(
            options={
                "profile": "missing",
                "prompt_template": "Write one audit briefing.",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            }
        )
        with pytest.raises(ValueError, match=r"sources\['source'\].*unknown llm profile"):
            load_settings_from_config_dict(unknown, expand_env_vars=True)

        ambiguous = self._source_config(
            options={
                "profile": "gw-primary",
                "provider": "gateway",
                "prompt_template": "Write one audit briefing.",
                "schema": {"mode": "observed"},
                "on_validation_failure": "discard",
            }
        )
        with pytest.raises(ValueError, match=r"sources\['source'\].*both 'profile' and 'provider'"):
            load_settings_from_config_dict(ambiguous, expand_env_vars=True)

    def test_source_rejects_user_scope_but_accepts_scopeless_bedrock(self) -> None:
        user_profile: dict[str, object] = {
            "provider": "openrouter",
            "model": "openai/gpt-5-mini",
            "credential_scope": "user",
            "credential_ref": "OPENROUTER_API_KEY",
        }
        with pytest.raises(ValueError, match=r"sources\['source'\].*credential_scope"):
            load_settings_from_config_dict(
                self._source_config(profile=user_profile),
                expand_env_vars=True,
            )

        bedrock_profile: dict[str, object] = {
            "provider": "bedrock",
            "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
            "region_name": "ap-southeast-2",
        }
        settings = load_settings_from_config_dict(
            self._source_config(profile=bedrock_profile),
            expand_env_vars=True,
        )
        assert settings.sources["source"].options["provider"] == "bedrock"
        assert "api_key" not in settings.sources["source"].options

    def test_source_lowering_is_idempotent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-source-test")
        raw = self._source_config()
        once = _lower_llm_profile_nodes(raw, materialize=True)
        first_options = dict(once["sources"]["source"]["options"])
        assert first_options["provider"] == "gateway"
        assert first_options["profile_alias"] == "gw-primary"
        marker = first_options["profile_alias"]
        assert type(marker) is LoweredLLMProfileAlias

        twice = _lower_llm_profile_nodes(once, materialize=True)

        assert twice is once
        assert twice["sources"]["source"]["options"] == first_options
        assert twice["sources"]["source"]["options"]["profile_alias"] is marker

    def test_serialized_lowered_source_is_not_retrusted(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("LLM_GATEWAY_BEARER_TOKEN", "sk-source-test")
        settings = load_settings_from_config_dict(self._source_config(), expand_env_vars=True)
        serialized = settings.model_dump(mode="json")

        with pytest.raises(ValueError, match="reserved for trusted LLM profile lowering"):
            load_settings_from_config_dict(serialized, expand_env_vars=True)
        with pytest.raises(ValueError, match="reserved for trusted LLM profile lowering"):
            load_settings_from_yaml_string(yaml.safe_dump(serialized), expand_env_vars=True)

    def test_top_level_singular_source_is_not_a_runtime_contract(self) -> None:
        config = self._source_config()
        source = config.pop("sources")["source"]  # type: ignore[index]
        config["source"] = source

        with pytest.raises(ValueError, match=r"Unknown configuration keys: \['source'\].*'sources'"):
            load_settings_from_config_dict(config, expand_env_vars=True)
