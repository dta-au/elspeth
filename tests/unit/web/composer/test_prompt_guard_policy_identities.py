"""Independent guard policy identities on the actual fenced admission path."""

from __future__ import annotations

import hashlib
import json
import re

import httpx
import litellm
import pytest
from sqlalchemy import select

from elspeth.contracts import credential_material
from elspeth.contracts.credential_material import find_credential_material
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import is_lower_sha256_hex
from elspeth.web import credential_guard
from elspeth.web.composer import prompt_guard, provider_gateway
from elspeth.web.composer.prompt_guard import PromptGuardMount, enforce_prompt_guard
from elspeth.web.composer.prompt_guard_clients import AzurePromptGuardClient
from elspeth.web.composer.provider_quota import ProviderCallCustody, ProviderInvocationFamily
from elspeth.web.prompt_guard_config import PromptGuardSettings
from elspeth.web.sessions.models import quota_provider_attempts_table, token_usage_ledger_table
from tests.unit.web.composer.test_prepared_provider_input import _prepare, _verify
from tests.unit.web.composer.test_prepared_provider_input import invocation as _invocation
from tests.unit.web.composer.test_prompt_guard_admission import _checks
from tests.unit.web.composer.test_prompt_guard_admission import authority as _authority

authority = _authority
invocation = _invocation


def _settings(**changes: object) -> PromptGuardSettings:
    return PromptGuardSettings.model_validate(
        {"endpoint": "https://guard.example.test", "subscription_key": "fixture-guard-credential", **changes}
    )


def _allow_response(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    return httpx.Response(
        200,
        json={
            "userPromptAnalysis": {"attackDetected": False},
            "documentsAnalysis": [{"attackDetected": False} for _ in body["documents"]],
        },
    )


def _client(settings: PromptGuardSettings) -> AzurePromptGuardClient:
    return AzurePromptGuardClient(settings, http_transport=httpx.MockTransport(_allow_response))


async def _allow_check(authority, settings, *, text="Build an ordinary graph"):
    engine, _service, owner, coordinator = authority
    client = _client(settings)
    await enforce_prompt_guard(
        mount=PromptGuardMount(client),
        mode="required",
        role="composer",
        provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        messages=[{"role": "user", "content": text}],
        user_prompt=text,
    )
    check = _checks(engine)[-1]
    assert check["schema_version"] == 2
    assert is_lower_sha256_hex(check["validity_policy_fingerprint"])
    assert check["policy_fingerprint"] == client.policy_fingerprint
    assert check["disposition"] == "allow"
    coordinator.assert_completed()
    return check


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "changes,physical_changes",
    [
        pytest.param({"endpoint": "https://other.example.test"}, True, id="endpoint"),
        pytest.param({"timeout_seconds": 11.0}, True, id="timeout"),
        pytest.param({"subscription_key": "rotated-fixture-credential"}, False, id="credential-rotation"),
    ],
)
async def test_physical_settings_do_not_relabel_logical_validity(authority, changes, physical_changes):
    baseline = await _allow_check(authority, _settings())
    repeated = await _allow_check(authority, _settings())
    changed = await _allow_check(authority, _settings(**changes))
    for field in ("validity_policy_fingerprint", "policy_fingerprint", "request_hash"):
        assert repeated[field] == baseline[field]
    assert changed["validity_policy_fingerprint"] == baseline["validity_policy_fingerprint"]
    assert (changed["policy_fingerprint"] != baseline["policy_fingerprint"]) is physical_changes
    assert (changed["request_hash"] != baseline["request_hash"]) is physical_changes


@pytest.mark.asyncio
async def test_request_bytes_change_request_identity_without_changing_either_policy(authority):
    first = await _allow_check(authority, _settings(), text="Build an ordinary graph")
    second = await _allow_check(authority, _settings(), text="Inspect the ordinary graph")
    assert first["validity_policy_fingerprint"] == second["validity_policy_fingerprint"]
    assert first["policy_fingerprint"] == second["policy_fingerprint"]
    assert first["request_hash"] != second["request_hash"]


def _change_actual_credential_policy(monkeypatch):
    marker = "G7_LOGICAL_VALIDITY_SENTINEL"
    assert find_credential_material(marker) is None
    monkeypatch.setattr(
        credential_material,
        "CREDENTIAL_PATTERN_SPECS",
        (*credential_material.CREDENTIAL_PATTERN_SPECS, ("g7_validity_control", re.compile(marker))),
    )
    # This is an actual detector-policy mutation, not an arbitrary new hash.
    # The marker has not been entered into the guard's negative memo.
    with pytest.raises(credential_guard.CredentialMaterialRefused) as caught:
        credential_guard.require_no_credential_material(marker, surface="g7_logical_policy_control")
    assert caught.value.finding.category == "token_shape"


@pytest.mark.asyncio
async def test_actual_validity_policy_change_does_not_relabel_adapter_settings(authority, monkeypatch):
    baseline = await _allow_check(authority, _settings())
    _change_actual_credential_policy(monkeypatch)
    changed = await _allow_check(authority, _settings())
    assert changed["validity_policy_fingerprint"] != baseline["validity_policy_fingerprint"]
    assert changed["policy_fingerprint"] == baseline["policy_fingerprint"]
    assert changed["request_hash"] != baseline["request_hash"]


def test_prepared_request_refuses_a_changed_actual_validity_policy(monkeypatch):
    client = _client(_settings())
    prepared = _prepare([{"role": "user", "content": "Original user input"}], mount=PromptGuardMount(client))
    _verify(prepared)
    physical = client.policy_fingerprint
    _change_actual_credential_policy(monkeypatch)
    assert client.policy_fingerprint == physical
    with pytest.raises(AuditIntegrityError):
        _verify(prepared)


@pytest.mark.asyncio
async def test_actual_guard_await_revalidates_logical_policy_before_quota_and_sdk(invocation, monkeypatch):
    engine, _sessions, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    sdk_calls = []

    class ChangingClient(AzurePromptGuardClient):
        async def inspect(self, request):
            decision = await super().inspect(request)
            _change_actual_credential_policy(monkeypatch)
            return decision

    client = ChangingClient(_settings(), http_transport=httpx.MockTransport(_allow_response))
    physical = client.policy_fingerprint
    mount = PromptGuardMount(client)
    prepared = _prepare([{"role": "user", "content": "Original user input"}], mount=mount)
    _verify(prepared)

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return object()

    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(AuditIntegrityError):
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=mount,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            prompt_guard_user_prompt=prepared.genuine_user,
            prepared_input=prepared,
            model=prepared.model,
            messages=prepared.evidence.messages,
        )
    assert client.policy_fingerprint == physical
    assert sdk_calls == [] and custody.attempt is None and custody.call is None
    checks = _checks(engine)
    assert len(checks) == 1 and checks[0]["disposition"] == "allow"
    assert checks[0]["schema_version"] == 2
    assert is_lower_sha256_hex(checks[0]["validity_policy_fingerprint"])
    with engine.connect() as conn:
        assert conn.execute(select(quota_provider_attempts_table)).all() == []
        assert conn.execute(select(token_usage_ledger_table)).all() == []
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_actual_quota_await_revalidates_logical_policy_and_settles_without_sdk(invocation, monkeypatch):
    engine, _sessions, owner, coordinator = invocation
    custody = owner.mint(ProviderInvocationFamily.PRIMARY)
    client = _client(_settings())
    mount = PromptGuardMount(client)
    physical = client.policy_fingerprint
    prepared = _prepare([{"role": "user", "content": "Original user input"}], mount=mount)
    _verify(prepared)
    actual_admit = ProviderCallCustody.admit
    sdk_calls = []
    admitted = []

    async def admit_then_change_policy(self, *, model):
        await actual_admit(self, model=model)
        assert self.attempt is not None
        admitted.append(self.attempt)
        _change_actual_credential_policy(monkeypatch)

    async def completion(**kwargs):
        sdk_calls.append(kwargs)
        return object()

    monkeypatch.setattr(ProviderCallCustody, "admit", admit_then_change_policy)
    monkeypatch.setattr(litellm, "acompletion", completion)
    with pytest.raises(AuditIntegrityError):
        await provider_gateway._litellm_acompletion(
            provider_custody=custody,
            prompt_guard_mount=mount,
            prompt_guard_mode="required",
            prompt_guard_role="composer",
            prompt_guard_user_prompt=prepared.genuine_user,
            prepared_input=prepared,
            model=prepared.model,
            messages=prepared.evidence.messages,
        )
    assert client.policy_fingerprint == physical
    assert sdk_calls == [] and custody.attempt is None and custody.call is None
    assert len(admitted) == 1
    assert not custody.has_undispatched_attempt()
    with engine.connect() as conn:
        attempts = conn.execute(select(quota_provider_attempts_table)).mappings().all()
        usage = conn.execute(select(token_usage_ledger_table)).mappings().all()
    assert len(attempts) == 1 and attempts[0]["settled_at"] is not None
    assert attempts[0]["attempt_id"] == admitted[0].attempt_id
    assert len(usage) == 1 and attempts[0]["ledger_entry_id"] == usage[0]["entry_id"]
    assert (usage[0]["prompt_tokens"], usage[0]["completion_tokens"], usage[0]["cached_prompt_tokens"], usage[0]["reasoning_tokens"]) == (
        0,
        0,
        0,
        0,
    )
    coordinator.assert_completed()


@pytest.mark.asyncio
async def test_canonical_request_binding_includes_both_policy_identities(authority):
    text = "Build an ordinary graph"
    check = await _allow_check(authority, _settings(), text=text)
    exact = {
        "role": "composer",
        "validity_policy_fingerprint": check["validity_policy_fingerprint"],
        "policy_fingerprint": check["policy_fingerprint"],
        "enforce_azure_character_limits": True,
        "user_prompt": text,
        "messages": [{"role": "user", "content": text}],
        "tools": [],
        "trusted_message_indexes": [],
        "content_parts": [None],
        "tool_argument_parts": [None],
    }
    encoded = json.dumps(exact, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
    assert check["request_hash"] == hashlib.sha256(encoded.encode("utf-8")).hexdigest()
    for field in ("validity_policy_fingerprint", "policy_fingerprint"):
        changed = dict(exact, **{field: "0" * 64})
        assert changed[field] != exact[field]
        encoded = json.dumps(changed, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False)
        assert check["request_hash"] != hashlib.sha256(encoded.encode("utf-8")).hexdigest()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,disposition", [("required", "unavailable"), ("debug-when-unconfigured", "debug_bypass")])
async def test_unconfigured_policy_identity_still_names_absence_and_actual_disposition(authority, mode, disposition):
    engine, _service, owner, coordinator = authority
    operation = enforce_prompt_guard(
        mount=None,
        mode=mode,
        role="composer",
        provider_custody=owner.mint(ProviderInvocationFamily.PRIMARY),
        messages=[{"role": "user", "content": "Ordinary input"}],
        user_prompt="Ordinary input",
    )
    if mode == "required":
        with pytest.raises(prompt_guard.PromptGuardRefused):
            await operation
    else:
        await operation
    check = _checks(engine)[-1]
    assert check["schema_version"] == 2
    assert is_lower_sha256_hex(check["validity_policy_fingerprint"])
    assert is_lower_sha256_hex(check["policy_fingerprint"])
    assert check["adapter"] == "unconfigured" and check["mode"] == mode
    assert (check["disposition"], check["reason"]) == (disposition, "guard_not_configured")
    coordinator.assert_completed()
