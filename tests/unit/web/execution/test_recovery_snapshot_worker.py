"""Real secret stores remain fresh when recovery builds authority in a worker."""

import asyncio
import threading
from dataclasses import replace

import pytest

from elspeth.web.execution.envelope import (
    EnvelopeRecoveryReason,
    ExecutionEnvelopeRefused,
    ExecutionRecoveryAuthority,
    build_run_execution_input,
    capture_execution_envelope,
    restore_run_execution_input,
)
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.plugin_policy.availability import RequestPluginSnapshotFactory
from elspeth.web.plugin_policy.models import PluginId, WebPluginPolicy
from elspeth.web.plugin_policy.profiles import OperatorProfileRegistry, RuntimeWebPluginConfig
from elspeth.web.secrets.server_store import ServerSecretStore
from elspeth.web.secrets.service import ScopedSecretResolver, WebSecretService
from elspeth.web.secrets.user_store import UserSecretStore
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.schema import initialize_session_schema
from tests.fixtures.identities import ensure_test_identity
from tests.unit.web.plugin_policy.test_availability import _InternalLLMSourceCatalog, _settings


@pytest.mark.asyncio
@pytest.mark.parametrize("scope", ["user", "server"])
async def test_real_snapshot_factory_recovery_worker_rechecks_rotation_revocation_and_principal(tmp_path, monkeypatch, scope):
    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-worker-fingerprint")
    name = "RECOVERY_WORKER_KEY"
    engine = create_session_engine(f"sqlite:///{tmp_path / 'secrets.db'}")
    initialize_session_schema(engine)
    with engine.begin() as conn:
        ensure_test_identity(conn, identity_id="alice")
        ensure_test_identity(conn, identity_id="bob")
    user_store = UserSecretStore(engine, master_key="test-worker-master-key")
    server_store = ServerSecretStore(allowlist=(name,))
    service = WebSecretService(user_store, server_store)
    resolver = ScopedSecretResolver(service, "local")
    source = PluginId("source", "llm")
    policy = WebPluginPolicy.create(
        required=frozenset({source}),
        configured_optional=frozenset(),
        preferences=(),
        control_modes=(),
        plugin_code_identities=((source, "1.0.0", "worker-test"),),
    )
    runtime = RuntimeWebPluginConfig.from_settings(
        _settings(
            llm_profiles={
                "source-profile": {
                    "provider": "openrouter",
                    "credential_scope": scope,
                    "credential_ref": name,
                    "model": "openai/gpt-5-mini",
                }
            },
            default_llm_profile="source-profile",
        )
    )
    factory = RequestPluginSnapshotFactory(
        policy=policy,
        catalog=_InternalLLMSourceCatalog(),
        profiles=OperatorProfileRegistry(policy=policy, settings=runtime),
        auth_provider="local",
        secret_service=service,
        server_store=server_store,
        user_store=user_store,
        generation_key=b"worker-test-generation-key",
    )

    def set_value(value):
        if scope == "user":
            user_store.set_secret(name, value=value, user_id="alice", auth_provider_type="local")
        else:
            monkeypatch.setenv(name, value)

    try:
        set_value("first-worker-test-value")
        initial = factory.for_user_id("alice")
        assert source in initial.available
        frozen = FrozenRunSettings(initial, {"api_key": {"secret_ref": name}}, {})
        envelope = capture_execution_envelope(
            frozen,
            user_id="alice",
            auth_provider_type="local",
            resolver=resolver,
            env_ref_names=frozenset(),
            implementation_fingerprint="d" * 64,
            deployment_generation="image-1",
        )
        admitted = build_run_execution_input(envelope, frozen, "d" * 64, "image-1", ())

        event_loop_thread = threading.get_ident()

        async def recover(user_id="alice", changed_policy=False):
            def authority():
                assert threading.get_ident() != event_loop_thread
                current = factory.for_user_id(user_id)
                if changed_policy:
                    current = replace(current, policy_hash="e" * 64)
                return ExecutionRecoveryAuthority(current, "d" * 64, "image-1")

            return await asyncio.to_thread(
                restore_run_execution_input,
                admitted,
                current_authority=authority,
                user_id=user_id,
                auth_provider_type="local",
                resolver=resolver,
            )

        assert (await recover()).settings == frozen
        with pytest.raises(ExecutionEnvelopeRefused) as refused:
            await recover("bob")
        assert refused.value.reason is EnvelopeRecoveryReason.PRINCIPAL_CHANGED
        with pytest.raises(ExecutionEnvelopeRefused) as refused:
            await recover(changed_policy=True)
        assert refused.value.reason is EnvelopeRecoveryReason.POLICY_CHANGED
        set_value("rotated-worker-test-value")
        with pytest.raises(ExecutionEnvelopeRefused) as refused:
            await recover()
        assert refused.value.reason is EnvelopeRecoveryReason.BINDING_CHANGED
        if scope == "user":
            user_store.delete_secret(name, user_id="alice", auth_provider_type="local")
        else:
            monkeypatch.delenv(name)
        with pytest.raises(ExecutionEnvelopeRefused) as refused:
            await recover()
        assert refused.value.reason is EnvelopeRecoveryReason.BINDING_CHANGED
    finally:
        engine.dispose()
