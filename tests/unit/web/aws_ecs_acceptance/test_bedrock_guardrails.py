"""Contract tests for the AWS ECS Bedrock acceptance lane."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from elspeth.contracts.plugin_policy_audit import WebPluginPolicyEvidence
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.web import aws_ecs_acceptance as acceptance
from elspeth.web._aws_ecs_acceptance import bedrock
from tests.unit.web.aws_ecs_acceptance.test_receipt_contracts import _plugin_policy_receipt


def test_facade_reexports_bedrock_owners_by_identity() -> None:
    assert acceptance.verify_bedrock is bedrock.verify_bedrock
    assert acceptance.verify_bedrock_guardrails is bedrock.verify_bedrock_guardrails
    assert acceptance.run_bedrock_guardrails_live is bedrock.run_bedrock_guardrails_live
    assert acceptance.build_plugin_policy_acceptance is bedrock.build_plugin_policy_acceptance


def test_suppress_process_output_restores_writable_stdout_and_stderr(capfd: pytest.CaptureFixture[str]) -> None:
    with bedrock._suppress_process_output():
        os.write(1, b"suppressed-stdout\n")
        os.write(2, b"suppressed-stderr\n")

    os.write(1, b"restored-stdout\n")
    os.write(2, b"restored-stderr\n")
    captured = capfd.readouterr()

    assert captured.out == "restored-stdout\n"
    assert captured.err == "restored-stderr\n"


def _bedrock_env(**updates: str) -> dict[str, str]:
    values = {
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/anthropic.claude-test-v1:0",
        "AWS_REGION": "ap-southeast-2",
    }
    values.update(updates)
    return values


def _bedrock_response() -> object:
    return SimpleNamespace(
        id="provider-request-id-secret",
        model="bedrock/provider-returned-model-secret",
        choices=[SimpleNamespace(message=SimpleNamespace(content="Bedrock smoke passed."))],
        usage={
            "prompt_tokens": 19,
            "completion_tokens": 5,
            "total_tokens": 24,
            "prompt_tokens_details": {"cached_tokens": 7},
            "cost": 0.0042,
        },
    )


@pytest.mark.asyncio
async def test_verify_bedrock_uses_production_call_shape_timeout_and_ordinary_metadata_parser(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, object]] = []
    timeouts: list[float] = []
    real_wait_for = asyncio.wait_for

    async def completion(**kwargs: object) -> object:
        calls.append(kwargs)
        return _bedrock_response()

    async def bounded_wait(awaitable: object, *, timeout: float) -> object:
        timeouts.append(timeout)
        return await real_wait_for(awaitable, timeout=timeout)  # type: ignore[arg-type]

    monkeypatch.setattr(bedrock.asyncio, "wait_for", bounded_wait)

    receipt = await acceptance.verify_bedrock(_bedrock_env(), completion=completion)

    assert calls == [
        {
            "model": "bedrock/anthropic.claude-test-v1:0",
            "messages": [{"role": "user", "content": "Reply with exactly: Bedrock smoke passed."}],
            "max_tokens": 16,
            "aws_region_name": "ap-southeast-2",
        }
    ]
    assert timeouts == [60.0]
    assert receipt == {
        "returned_model_sha256": hashlib.sha256(b"bedrock/provider-returned-model-secret").hexdigest(),
        "provider_request_id_sha256": hashlib.sha256(b"provider-request-id-secret").hexdigest(),
        "prompt_tokens_present": True,
        "completion_tokens_present": True,
        "cache_tokens_present": True,
        "cost": 0.0042,
        "cost_source": "provider_reported",
    }
    rendered = json.dumps(receipt)
    assert "provider-returned-model-secret" not in rendered
    assert "provider-request-id-secret" not in rendered


@pytest.mark.parametrize(
    "env",
    [
        {"AWS_REGION": "ap-southeast-2"},
        _bedrock_env(ELSPETH_BEDROCK_LIVE_TEST_MODEL="anthropic.claude-test"),
        _bedrock_env(ELSPETH_BEDROCK_LIVE_TEST_MODEL="bedrock/"),
        {"ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/test"},
        _bedrock_env(AWS_DEFAULT_REGION="us-east-1"),
    ],
)
@pytest.mark.asyncio
async def test_verify_bedrock_rejects_missing_invalid_model_or_region(env: dict[str, str]) -> None:
    async def completion(**_kwargs: object) -> object:
        pytest.fail("provider must not be called")

    with pytest.raises(acceptance.AcceptanceCheckError, match="bedrock_input"):
        await acceptance.verify_bedrock(env, completion=completion)


@pytest.mark.asyncio
async def test_verify_bedrock_rejects_credential_endpoint_profile_and_role_overrides() -> None:
    async def completion(**_kwargs: object) -> object:
        pytest.fail("provider must not be called")

    for forbidden in acceptance.FORBIDDEN_AWS_OVERRIDE_ENV:
        raw = "raw-credential-url-role-arn-sentinel"
        with pytest.raises(acceptance.AcceptanceCheckError, match="bedrock_aws_override") as raised:
            await acceptance.verify_bedrock(_bedrock_env(**{forbidden: raw}), completion=completion)
        assert raw not in str(raised.value)


@pytest.mark.parametrize(
    ("response", "check"),
    [
        (SimpleNamespace(choices=[]), "bedrock_content"),
        (SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content="  "))]), "bedrock_content"),
        (
            SimpleNamespace(
                id="request-id",
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            ),
            "bedrock_metadata",
        ),
        (
            SimpleNamespace(
                model="bedrock/model",
                choices=[SimpleNamespace(message=SimpleNamespace(content="ok"))],
                usage={"prompt_tokens": 1, "completion_tokens": 1},
            ),
            "bedrock_metadata",
        ),
    ],
)
@pytest.mark.asyncio
async def test_verify_bedrock_rejects_empty_content_or_malformed_metadata_with_static_checks(response: object, check: str) -> None:
    async def completion(**_kwargs: object) -> object:
        return response

    with pytest.raises(acceptance.AcceptanceCheckError, match=check) as raised:
        await acceptance.verify_bedrock(_bedrock_env(), completion=completion)

    assert "request-id" not in str(raised.value)
    assert "bedrock/model" not in str(raised.value)


@pytest.mark.asyncio
async def test_verify_bedrock_timeout_and_provider_failures_are_static_and_fd_suppressed(capfd: pytest.CaptureFixture[str]) -> None:
    async def provider_failure(**_kwargs: object) -> object:
        os.write(1, b"raw-provider-content-model-request-id-credential-arn-stdout\n")
        os.write(2, b"raw-provider-content-model-request-id-credential-arn-stderr\n")
        raise RuntimeError("raw provider response URL model request-id credential ARN")

    with pytest.raises(acceptance.AcceptanceCheckError, match="bedrock_provider") as raised:
        await acceptance.verify_bedrock(_bedrock_env(), completion=provider_failure)
    captured = capfd.readouterr()
    assert "raw-provider" not in captured.out
    assert "raw-provider" not in captured.err
    assert "raw provider" not in str(raised.value)

    async def timeout(**_kwargs: object) -> object:
        raise TimeoutError("raw timeout provider URL")

    with pytest.raises(acceptance.AcceptanceCheckError, match="bedrock_timeout") as timeout_raised:
        await acceptance.verify_bedrock(_bedrock_env(), completion=timeout)
    assert "raw timeout" not in str(timeout_raised.value)


@pytest.mark.asyncio
async def test_verify_bedrock_suppresses_fd_output_on_success(capfd: pytest.CaptureFixture[str]) -> None:
    async def noisy_success(**_kwargs: object) -> object:
        os.write(1, b"raw-success-provider-model-request-id-content-stdout\n")
        os.write(2, b"raw-success-provider-model-request-id-content-stderr\n")
        return _bedrock_response()

    receipt = await acceptance.verify_bedrock(_bedrock_env(), completion=noisy_success)

    captured = capfd.readouterr()
    assert captured.out == ""
    assert captured.err == ""
    assert receipt["returned_model_sha256"] == hashlib.sha256(b"bedrock/provider-returned-model-secret").hexdigest()


def _guardrail_env(**updates: str) -> dict[str, str]:
    values = {
        "ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS": "1",
        "ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS": "prompt-approved",
        "ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT": "safe prompt fixture secret",
        "ELSPETH_LIVE_BEDROCK_PROMPT_BLOCKED_TEXT": "blocked prompt fixture secret",
        "ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION": "7",
        "ELSPETH_LIVE_BEDROCK_CONTENT_PROFILE_ALIAS": "content-approved",
        "ELSPETH_LIVE_BEDROCK_CONTENT_SAFE_TEXT": "safe content fixture secret",
        "ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT": "blocked content fixture secret",
        "ELSPETH_LIVE_BEDROCK_CONTENT_EXPECTED_VERSION": "11",
        "ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
        "AWS_REGION": "ap-southeast-2",
        "ELSPETH_WEB__PLUGIN_ALLOWLIST": json.dumps(
            ["transform:aws_bedrock_prompt_shield", "transform:aws_bedrock_content_safety"], separators=(",", ":")
        ),
        "ELSPETH_WEB__PLUGIN_PREFERENCES": json.dumps(
            {
                "prompt_shield": ["transform:aws_bedrock_prompt_shield"],
                "content_safety": ["transform:aws_bedrock_content_safety"],
            },
            separators=(",", ":"),
        ),
        "ELSPETH_WEB__PLUGIN_CONTROL_MODES": '{"prompt_shield":"required","content_safety":"required"}',
        "ELSPETH_WEB__LLM_PROFILES": json.dumps(
            {
                "tutorial": {
                    "provider": "bedrock",
                    "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                    "region_name": "ap-southeast-2",
                }
            },
            separators=(",", ":"),
        ),
        "ELSPETH_WEB__DEFAULT_LLM_PROFILE": "tutorial",
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_PROFILES": json.dumps(
            [
                {
                    "alias": "prompt-approved",
                    "plugin": "aws_bedrock_prompt_shield",
                    "guardrail_identifier": "privatepromptguardrail",
                    "guardrail_version": "7",
                    "region": "ap-southeast-2",
                },
                {
                    "alias": "content-approved",
                    "plugin": "aws_bedrock_content_safety",
                    "guardrail_identifier": "privatecontentguardrail",
                    "guardrail_version": "11",
                    "region": "ap-southeast-2",
                },
            ],
            separators=(",", ":"),
        ),
        "ELSPETH_WEB__BEDROCK_GUARDRAIL_DEFAULT_PROFILES": (
            '{"aws_bedrock_prompt_shield":"prompt-approved","aws_bedrock_content_safety":"content-approved"}'
        ),
    }
    values.update(updates)
    values.setdefault("ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256", acceptance.plugin_policy_binding_sha256(values))
    return values


def _web_policy_evidence() -> WebPluginPolicyEvidence:
    return WebPluginPolicyEvidence(
        schema_version=1,
        policy_hash="1" * 64,
        snapshot_hash="2" * 64,
        authorized_plugin_ids=(
            "transform:aws_bedrock_content_safety",
            "transform:aws_bedrock_prompt_shield",
            "transform:llm",
        ),
        available_plugin_ids=(
            "transform:aws_bedrock_content_safety",
            "transform:aws_bedrock_prompt_shield",
            "transform:llm",
        ),
        control_modes=(("content_safety", "required"), ("prompt_shield", "required")),
        selected_implementations=(
            ("content_safety", "transform:aws_bedrock_content_safety"),
            ("llm", "transform:llm"),
            ("prompt_shield", "transform:aws_bedrock_prompt_shield"),
        ),
        selected_profile_aliases=(
            ("transform:aws_bedrock_content_safety", "content-approved"),
            ("transform:aws_bedrock_prompt_shield", "prompt-approved"),
            ("transform:llm", "tutorial"),
        ),
        plugin_code_identities=(
            ("transform:aws_bedrock_content_safety", "1.0.0", "sha256:" + "a" * 16),
            ("transform:aws_bedrock_prompt_shield", "1.0.0", "sha256:" + "b" * 16),
            ("transform:llm", "1.0.0", "sha256:" + "c" * 16),
        ),
        binding_generation_fingerprint="3" * 64,
        decision_codes=("policy_allowed",),
    )


def test_verify_bedrock_guardrails_uses_shared_profile_registry_and_reusable_checker_audit_first() -> None:
    env = _guardrail_env()
    settings = object()
    profiles = {
        "transform:aws_bedrock_prompt_shield": SimpleNamespace(
            alias="prompt-approved", plugin="aws_bedrock_prompt_shield", guardrail_version="7"
        ),
        "transform:aws_bedrock_content_safety": SimpleNamespace(
            alias="content-approved", plugin="aws_bedrock_content_safety", guardrail_version="11"
        ),
    }
    resolved: list[tuple[str, str]] = []

    class Registry:
        def approved_bedrock_guardrail_profile(self, plugin_id: object, *, alias: str) -> object:
            resolved.append((str(plugin_id), alias))
            return profiles[str(plugin_id)]

    registry_inputs: list[object] = []

    def registry_factory(value: object) -> Registry:
        registry_inputs.append(value)
        return Registry()

    order: list[str] = []

    class Execution:
        def record_call(self) -> None:
            order.append("audit")

    checker_calls: list[dict[str, object]] = []

    def checker(**kwargs: object) -> object:
        checker_calls.append(kwargs)
        execution = kwargs["execution"]
        telemetry_emit = kwargs["telemetry_emit"]
        for _ in range(2):
            execution.record_call()  # type: ignore[attr-defined]
            telemetry_emit(object())  # type: ignore[operator]
        profile = kwargs["profile"]
        return SimpleNamespace(
            plugin_id=profile.plugin,  # type: ignore[attr-defined]
            profile_alias=profile.alias,  # type: ignore[attr-defined]
            safe_case_passed=True,
            attack_case_blocked=True,
            request_ids_present=True,
        )

    receipt = acceptance.verify_bedrock_guardrails(
        env,
        settings_loader=lambda: settings,
        registry_factory=registry_factory,
        execution=Execution(),
        checker=checker,
        telemetry_emit=lambda _event: order.append("telemetry"),
        run_id="guardrail-run",
        state_id="guardrail-state",
        now=lambda: datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC),
    )

    assert registry_inputs == [settings]
    assert resolved == [
        ("transform:aws_bedrock_prompt_shield", "prompt-approved"),
        ("transform:aws_bedrock_content_safety", "content-approved"),
    ]
    assert [call["safe_text"] for call in checker_calls] == [
        "safe prompt fixture secret",
        "safe content fixture secret",
    ]
    assert order == ["audit", "telemetry"] * 4
    assert receipt == {
        "controls": [
            {
                "plugin_id": "aws_bedrock_prompt_shield",
                "profile_alias": "prompt-approved",
                "guardrail_version": "7",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": hashlib.sha256(b"safe prompt fixture secret").hexdigest(),
                "blocked_text_sha256": hashlib.sha256(b"blocked prompt fixture secret").hexdigest(),
                "checked_at": "2026-07-14T01:02:03Z",
            },
            {
                "plugin_id": "aws_bedrock_content_safety",
                "profile_alias": "content-approved",
                "guardrail_version": "11",
                "safe_case_passed": True,
                "attack_case_blocked": True,
                "request_ids_present": True,
                "safe_text_sha256": hashlib.sha256(b"safe content fixture secret").hexdigest(),
                "blocked_text_sha256": hashlib.sha256(b"blocked content fixture secret").hexdigest(),
                "checked_at": "2026-07-14T01:02:03Z",
            },
        ]
    }
    rendered = json.dumps(receipt)
    for forbidden in ("safe prompt fixture secret", "blocked content fixture secret", "privateguardrail", "request-id"):
        assert forbidden not in rendered


@pytest.mark.parametrize(
    ("updates", "check"),
    [
        ({"ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS": "0"}, "guardrails_gate"),
        ({"ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS": ""}, "guardrails_input"),
        ({"ELSPETH_LIVE_BEDROCK_CONTENT_SAFE_TEXT": ""}, "guardrails_input"),
        ({"ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION": "DRAFT"}, "guardrails_input"),
    ],
)
def test_verify_bedrock_guardrails_fails_closed_on_invalid_gate_or_fixture_inputs(updates: dict[str, str], check: str) -> None:
    with pytest.raises(acceptance.AcceptanceCheckError, match=check):
        acceptance.verify_bedrock_guardrails(
            _guardrail_env(**updates),
            settings_loader=pytest.fail,
            registry_factory=pytest.fail,
            execution=object(),
        )


def test_verify_bedrock_guardrails_names_missing_live_inputs_exactly() -> None:
    env = _guardrail_env()
    del env["ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT"]
    del env["ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_live_inputs_missing") as raised:
        acceptance.verify_bedrock_guardrails(
            env,
            settings_loader=pytest.fail,
            registry_factory=pytest.fail,
            execution=object(),
        )

    assert raised.value.missing == (
        "ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",
        "ELSPETH_LIVE_BEDROCK_CONTENT_BLOCKED_TEXT",
    )


def test_verify_bedrock_guardrails_reports_absent_gate_env_as_missing_input() -> None:
    env = _guardrail_env()
    del env["ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_live_inputs_missing") as raised:
        acceptance.verify_bedrock_guardrails(
            env,
            settings_loader=pytest.fail,
            registry_factory=pytest.fail,
            execution=object(),
        )

    assert raised.value.missing == ("ELSPETH_RUN_LIVE_BEDROCK_GUARDRAILS",)


def test_verify_bedrock_guardrails_defaults_alias_and_version_from_rendered_policy_env() -> None:
    env = _guardrail_env()
    for name in (
        "ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS",
        "ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION",
        "ELSPETH_LIVE_BEDROCK_CONTENT_PROFILE_ALIAS",
        "ELSPETH_LIVE_BEDROCK_CONTENT_EXPECTED_VERSION",
    ):
        del env[name]
    profiles = {
        "transform:aws_bedrock_prompt_shield": SimpleNamespace(
            alias="prompt-approved", plugin="aws_bedrock_prompt_shield", guardrail_version="7"
        ),
        "transform:aws_bedrock_content_safety": SimpleNamespace(
            alias="content-approved", plugin="aws_bedrock_content_safety", guardrail_version="11"
        ),
    }
    resolved: list[tuple[str, str]] = []

    class Registry:
        def approved_bedrock_guardrail_profile(self, plugin_id: object, *, alias: str) -> object:
            resolved.append((str(plugin_id), alias))
            return profiles[str(plugin_id)]

    def checker(**kwargs: object) -> object:
        profile = kwargs["profile"]
        return SimpleNamespace(
            plugin_id=profile.plugin,  # type: ignore[attr-defined]
            profile_alias=profile.alias,  # type: ignore[attr-defined]
            safe_case_passed=True,
            attack_case_blocked=True,
            request_ids_present=True,
        )

    receipt = acceptance.verify_bedrock_guardrails(
        env,
        settings_loader=lambda: object(),
        registry_factory=lambda _settings: Registry(),
        execution=object(),
        checker=checker,
        now=lambda: datetime(2026, 7, 30, 1, 2, 3, tzinfo=UTC),
    )

    assert resolved == [
        ("transform:aws_bedrock_prompt_shield", "prompt-approved"),
        ("transform:aws_bedrock_content_safety", "content-approved"),
    ]
    controls = receipt["controls"]
    assert [control["guardrail_version"] for control in controls] == ["7", "11"]  # type: ignore[index]


def test_verify_bedrock_guardrails_never_defaults_version_for_divergent_operator_alias() -> None:
    env = _guardrail_env(ELSPETH_LIVE_BEDROCK_PROMPT_PROFILE_ALIAS="operator-divergent-alias")
    del env["ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION"]

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_live_inputs_missing") as raised:
        acceptance.verify_bedrock_guardrails(
            env,
            settings_loader=pytest.fail,
            registry_factory=pytest.fail,
            execution=object(),
        )

    assert raised.value.missing == ("ELSPETH_LIVE_BEDROCK_PROMPT_EXPECTED_VERSION",)


def test_verify_bedrock_guardrails_keeps_settings_code_for_genuine_settings_failures() -> None:
    def settings_loader() -> object:
        raise RuntimeError("raw settings failure sentinel")

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_settings") as raised:
        acceptance.verify_bedrock_guardrails(
            _guardrail_env(),
            settings_loader=settings_loader,
            registry_factory=pytest.fail,
            execution=object(),
        )
    assert "raw settings failure sentinel" not in str(raised.value)


def test_guardrail_live_owner_surfaces_named_check_failures_instead_of_settings_code() -> None:
    def policy_factory(_settings: object, _env: object) -> object:
        raise acceptance.AcceptanceCheckError(
            "guardrails_live_inputs_missing",
            missing=("ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",),
        )

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_live_inputs_missing") as raised:
        acceptance.run_bedrock_guardrails_live(
            _guardrail_env(),
            settings_loader=lambda: object(),
            policy_acceptance_factory=policy_factory,  # type: ignore[arg-type]
            telemetry_manager_factory=lambda _settings: pytest.fail("telemetry manager must not be built"),
        )

    assert raised.value.missing == ("ELSPETH_LIVE_BEDROCK_PROMPT_SAFE_TEXT",)


def test_verify_bedrock_guardrails_rejects_aws_overrides_before_settings_load() -> None:
    raw = "raw-credential-endpoint-role-arn-sentinel"
    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_aws_override") as raised:
        acceptance.verify_bedrock_guardrails(
            _guardrail_env(AWS_ACCESS_KEY_ID=raw),
            settings_loader=pytest.fail,
            registry_factory=pytest.fail,
            execution=object(),
        )
    assert raw not in str(raised.value)


def test_verify_bedrock_guardrails_rejects_version_drift_and_redacts_checker_failure() -> None:
    profile = SimpleNamespace(
        alias="prompt-approved",
        plugin="aws_bedrock_prompt_shield",
        guardrail_version="8",
    )
    registry = SimpleNamespace(approved_bedrock_guardrail_profile=lambda *_args, **_kwargs: profile)
    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_profile"):
        acceptance.verify_bedrock_guardrails(
            _guardrail_env(),
            settings_loader=object,
            registry_factory=lambda _settings: registry,
            execution=object(),
        )

    profiles = {
        "transform:aws_bedrock_prompt_shield": SimpleNamespace(
            alias="prompt-approved", plugin="aws_bedrock_prompt_shield", guardrail_version="7"
        ),
        "transform:aws_bedrock_content_safety": SimpleNamespace(
            alias="content-approved", plugin="aws_bedrock_content_safety", guardrail_version="11"
        ),
    }
    registry = SimpleNamespace(approved_bedrock_guardrail_profile=lambda plugin_id, **_kwargs: profiles[str(plugin_id)])

    def checker(**_kwargs: object) -> object:
        raise RuntimeError("raw provider body credential ARN request-id URL sentinel")

    with pytest.raises(acceptance.AcceptanceCheckError, match="guardrails_live_check") as raised:
        acceptance.verify_bedrock_guardrails(
            _guardrail_env(),
            settings_loader=object,
            registry_factory=lambda _settings: registry,
            execution=object(),
            checker=checker,
        )
    assert "raw provider" not in str(raised.value)


def test_plugin_policy_acceptance_binds_effective_bedrock_policy_tutorial_and_safe_aliases(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from elspeth.web.config import WebSettings

    settings = WebSettings(
        data_dir=tmp_path,
        composer_max_composition_turns=4,
        composer_max_discovery_turns=4,
        composer_timeout_seconds=60,
        composer_rate_limit_per_minute=20,
        secret_key="0123456789abcdef0123456789abcdef",
        shareable_link_signing_key=b"0123456789abcdef0123456789abcdef",
        plugin_allowlist=[
            "transform:aws_bedrock_prompt_shield",
            "transform:aws_bedrock_content_safety",
        ],
        plugin_preferences={
            "prompt_shield": ["transform:aws_bedrock_prompt_shield"],
            "content_safety": ["transform:aws_bedrock_content_safety"],
        },
        plugin_control_modes={"prompt_shield": "required", "content_safety": "required"},
        llm_profiles={
            "tutorial": {
                "provider": "bedrock",
                "model": "bedrock/anthropic.claude-3-haiku-20240307-v1:0",
                "region_name": "ap-southeast-2",
            }
        },
        default_llm_profile="tutorial",
        bedrock_guardrail_profiles=[
            {
                "alias": "prompt-approved",
                "plugin": "aws_bedrock_prompt_shield",
                "guardrail_identifier": "privatepromptguardrail",
                "guardrail_version": "7",
                "region": "ap-southeast-2",
            },
            {
                "alias": "content-approved",
                "plugin": "aws_bedrock_content_safety",
                "guardrail_identifier": "privatecontentguardrail",
                "guardrail_version": "11",
                "region": "ap-southeast-2",
            },
        ],
        bedrock_guardrail_default_profiles={
            "aws_bedrock_prompt_shield": "prompt-approved",
            "aws_bedrock_content_safety": "content-approved",
        },
    )

    env = _guardrail_env()
    evidence, receipt = acceptance.build_plugin_policy_acceptance(settings, env)

    expected_receipt = _plugin_policy_receipt(include_landscape=False)
    expected_receipt["policy_hash"] = evidence.policy_hash
    expected_receipt["snapshot_hash"] = evidence.snapshot_hash
    expected_receipt["binding_sha256"] = env["ELSPETH_ACCEPTANCE_PLUGIN_POLICY_BINDING_SHA256"]
    assert receipt == expected_receipt
    assert evidence.policy_hash == receipt["policy_hash"]
    assert evidence.snapshot_hash == receipt["snapshot_hash"]
    assert dict(evidence.selected_implementations)["llm"] == "transform:llm"
    assert dict(evidence.selected_implementations)["prompt_shield"] == "transform:aws_bedrock_prompt_shield"
    assert dict(evidence.selected_implementations)["content_safety"] == "transform:aws_bedrock_content_safety"
    rendered = json.dumps(receipt)
    assert "privatepromptguardrail" not in rendered
    assert "anthropic.claude" not in rendered

    for updates in (
        {"ELSPETH_BEDROCK_LIVE_TEST_MODEL": "bedrock/other-model"},
        {"AWS_REGION": "us-east-1"},
    ):
        with pytest.raises(acceptance.AcceptanceCheckError, match="plugin_policy_selection"):
            acceptance.build_plugin_policy_acceptance(settings, _guardrail_env(**updates))

    drifted = _guardrail_env()
    drifted["ELSPETH_WEB__PLUGIN_ALLOWLIST"] = "[]"
    with pytest.raises(acceptance.AcceptanceCheckError, match="plugin_policy_settings"):
        acceptance.build_plugin_policy_acceptance(settings, drifted)

    monkeypatch.setattr(
        bedrock,
        "build_plugin_policy_readiness",
        lambda **_kwargs: SimpleNamespace(rows=(), tutorial_ready=False),
    )
    with pytest.raises(KeyError, match="tutorial_profile"):
        acceptance.build_plugin_policy_acceptance(settings, env)


def test_guardrail_live_owner_persists_four_calls_before_forwarding_telemetry_and_closes_resources(tmp_path: Path) -> None:
    from elspeth.plugins.transforms.aws.guardrail_profiles import BedrockGuardrailProfileSettings
    from elspeth.plugins.transforms.aws.guardrails_live_check import run_guardrail_live_check
    from tests.unit.plugins.transforms.aws.test_guardrails_client import CONTENT_FILTERS, response

    database_url = f"sqlite:///{tmp_path / 'landscape.db'}"
    with LandscapeDB.from_url(database_url, create_tables=True):
        pass
    settings = SimpleNamespace(
        landscape_passphrase=None,
        get_landscape_url=lambda: database_url,
    )
    profiles = {
        "transform:aws_bedrock_prompt_shield": BedrockGuardrailProfileSettings.model_validate(
            {
                "alias": "prompt-approved",
                "plugin": "aws_bedrock_prompt_shield",
                "guardrail_identifier": "privatepromptguardrail",
                "guardrail_version": "7",
                "region": "ap-southeast-2",
            }
        ),
        "transform:aws_bedrock_content_safety": BedrockGuardrailProfileSettings.model_validate(
            {
                "alias": "content-approved",
                "plugin": "aws_bedrock_content_safety",
                "guardrail_identifier": "privatecontentguardrail",
                "guardrail_version": "11",
                "region": "ap-southeast-2",
            }
        ),
    }
    registry = SimpleNamespace(approved_bedrock_guardrail_profile=lambda plugin_id, **_kwargs: profiles[str(plugin_id)])

    class SequencedSDK:
        def __init__(self, *responses: object) -> None:
            self.responses = iter(responses)

        def apply_guardrail(self, **_kwargs: object) -> object:
            return next(self.responses)

    sdks = iter(
        (
            SequencedSDK(response(), response(detected="PROMPT_ATTACK")),
            SequencedSDK(
                response(CONTENT_FILTERS),
                response(
                    CONTENT_FILTERS,
                    action="GUARDRAIL_INTERVENED",
                    detected="VIOLENCE",
                    blocked=True,
                    outputs=[{"text": "discarded provider output"}],
                ),
            ),
        )
    )

    def checker(**kwargs: object) -> object:
        return run_guardrail_live_check(**kwargs, sdk_client=next(sdks))  # type: ignore[arg-type]

    class Manager:
        def __init__(self) -> None:
            self.events: list[object] = []
            self.flushed = False
            self.closed = False

        def handle_event(self, event: object) -> None:
            self.events.append(event)

        def flush(self) -> None:
            self.flushed = True

        def close(self) -> None:
            self.closed = True

    manager = Manager()
    policy_evidence = _web_policy_evidence()
    policy_receipt = _plugin_policy_receipt(include_landscape=False)
    receipt = acceptance.run_bedrock_guardrails_live(
        _guardrail_env(),
        settings_loader=lambda: settings,
        registry_factory=lambda _settings: registry,
        checker=checker,
        telemetry_manager_factory=lambda _settings: manager,
        policy_acceptance_factory=lambda _settings, _env: (policy_evidence, policy_receipt),
        now=lambda: datetime(2026, 7, 14, 1, 2, 3, tzinfo=UTC),
    )

    assert len(receipt["controls"]) == 2  # type: ignore[arg-type]
    assert receipt["plugin_policy"] == _plugin_policy_receipt()
    assert len(manager.events) == 4
    assert manager.flushed is True
    assert manager.closed is True
    with LandscapeDB.from_url(database_url, create_tables=False) as database:
        repositories = RecorderFactory.writable(database)
        runs = repositories.run_lifecycle.list_runs()
        assert len(runs) == 1
        assert runs[0].status.value == "completed"
        rows = repositories.query.get_rows(runs[0].run_id)
        tokens = repositories.query.get_tokens_for_rows(runs[0].run_id, [rows[0].row_id])
        states = repositories.query.get_node_states_for_tokens(runs[0].run_id, [tokens[0].token_id])
        calls = repositories.query.get_calls(states[0].state_id)
        persisted_policy = repositories.run_lifecycle.get_web_plugin_policy_evidence(runs[0].run_id)
    assert len(calls) == 4
    assert [call.call_index for call in calls] == [0, 1, 2, 3]
    assert all(call.request_hash and call.response_hash for call in calls)
    assert persisted_policy == policy_evidence
