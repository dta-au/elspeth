"""The ``web_instances`` writer: register / heartbeat / drain / stop under database time."""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import Engine, select, update
from sqlalchemy.exc import OperationalError

from elspeth.web.config import WebSettings
from elspeth.web.coordination import membership_lifecycle as lifecycle_module
from elspeth.web.coordination.contracts import CompatibilityKey, InstanceState
from elspeth.web.coordination.membership_authority import (
    RepositoryWebInstanceMembershipAuthority,
    WebInstanceIdentity,
    WebInstanceMembershipLost,
    WebInstanceRegistrationConflict,
    current_compatibility_key,
    web_instance_identity_from_settings,
)
from elspeth.web.coordination.membership_lifecycle import (
    RegisteredWebInstanceMembership,
    SingleProcessWebInstanceMembership,
    heartbeat_interval_seconds,
)
from elspeth.web.sessions.engine import create_session_engine
from elspeth.web.sessions.models import web_instances_table
from elspeth.web.sessions.schema import initialize_session_schema


@pytest.fixture()
def engine(tmp_path: Path) -> Iterator[Engine]:
    session_engine = create_session_engine(f"sqlite:///{tmp_path / 'session.db'}")
    initialize_session_schema(session_engine)
    try:
        yield session_engine
    finally:
        session_engine.dispose()


def _identity(instance_id: str | None = None, **overrides: Any) -> WebInstanceIdentity:
    values: dict[str, Any] = {
        "instance_id": f"sqlite-{uuid4()}" if instance_id is None else instance_id,
        "deployment_target": "kubernetes",
        "deployment_generation": "generation-1",
        "compatibility_key": current_compatibility_key(),
        "image_digest": "sha256:membership-test",
        "revision_label": "revision-1",
    }
    values.update(overrides)
    return WebInstanceIdentity(**values)


def _row(engine: Engine, instance_id: str) -> Any:
    with engine.connect() as conn:
        return conn.execute(select(web_instances_table).where(web_instances_table.c.instance_id == instance_id)).one()


def _row_count(engine: Engine) -> int:
    with engine.connect() as conn:
        return len(conn.execute(select(web_instances_table.c.instance_id)).all())


def _expire(engine: Engine, instance_id: str) -> None:
    """Simulate clock passage: the lease ends one second before the last heartbeat."""
    with engine.begin() as conn:
        last_heartbeat_at = conn.execute(
            select(web_instances_table.c.last_heartbeat_at).where(web_instances_table.c.instance_id == instance_id)
        ).scalar_one()
        conn.execute(
            update(web_instances_table)
            .where(web_instances_table.c.instance_id == instance_id)
            .values(lease_expires_at=last_heartbeat_at - timedelta(seconds=1))
        )


class TestWebInstanceIdentity:
    @pytest.mark.parametrize("field", ["instance_id", "deployment_target", "deployment_generation", "image_digest", "revision_label"])
    @pytest.mark.parametrize("blank", ["", "   ", None, 7])
    def test_every_text_field_must_be_a_nonblank_exact_string(self, field: str, blank: object) -> None:
        values: dict[str, Any] = {
            "instance_id": "sqlite-identity",
            "deployment_target": "kubernetes",
            "deployment_generation": "generation-1",
            "compatibility_key": current_compatibility_key(),
            "image_digest": "sha256:membership-test",
            "revision_label": "revision-1",
            field: blank,
        }
        with pytest.raises(ValueError, match=field):
            WebInstanceIdentity(**values)

    def test_compatibility_key_must_be_the_owned_type(self) -> None:
        with pytest.raises(TypeError, match="CompatibilityKey"):
            _identity(compatibility_key=(52, 37, 1))

    @pytest.mark.parametrize("field", ["session_epoch", "landscape_epoch", "coordination_protocol"])
    def test_refuses_a_non_positive_compatibility_key(self, field: str) -> None:
        values = {"session_epoch": 52, "landscape_epoch": 37, "coordination_protocol": 1, field: 0}
        with pytest.raises(ValueError, match=field):
            CompatibilityKey(**values)

    def test_current_compatibility_key_names_the_live_epochs(self) -> None:
        from elspeth.core.landscape.schema import SQLITE_SCHEMA_EPOCH
        from elspeth.web.coordination.contracts import WEB_COORDINATION_PROTOCOL_VERSION
        from elspeth.web.sessions.models import SESSION_SCHEMA_EPOCH

        assert current_compatibility_key() == CompatibilityKey(SESSION_SCHEMA_EPOCH, SQLITE_SCHEMA_EPOCH, WEB_COORDINATION_PROTOCOL_VERSION)


def _settings(**overrides: Any) -> WebSettings:
    values: dict[str, Any] = {
        "composer_max_composition_turns": 15,
        "composer_max_discovery_turns": 10,
        "composer_timeout_seconds": 85.0,
        "composer_rate_limit_per_minute": 10,
        "secret_key": "this-non-loopback-secret-is-long-enough",
        "shareable_link_signing_key": bytes(range(32)),
    }
    values.update(overrides)
    return WebSettings(**values)


class TestIdentityFromSettings:
    def test_aws_ecs_registers_the_contract_carried_identity(self) -> None:
        settings = _settings(
            deployment_target="aws-ecs",
            operator_telemetry_task_definition_family="elspeth-web-task",
            operator_telemetry_task_definition_revision="7",
            operator_telemetry_release="git-deadbeef",
        )
        identity = web_instance_identity_from_settings(settings, instance_id="postgresql-abc")
        assert identity == WebInstanceIdentity(
            instance_id="postgresql-abc",
            deployment_target="aws-ecs",
            deployment_generation="elspeth-web-task",
            compatibility_key=current_compatibility_key(),
            image_digest="git-deadbeef",
            revision_label="7",
        )

    @pytest.mark.parametrize(
        "missing",
        ["operator_telemetry_task_definition_family", "operator_telemetry_task_definition_revision", "operator_telemetry_release"],
    )
    def test_aws_ecs_without_a_contract_field_is_a_breach_not_a_default(self, missing: str) -> None:
        values = {
            "operator_telemetry_task_definition_family": "elspeth-web-task",
            "operator_telemetry_task_definition_revision": "7",
            "operator_telemetry_release": "git-deadbeef",
            missing: None,
        }
        settings = _settings(deployment_target="aws-ecs", **values)
        with pytest.raises(ValueError, match=missing):
            web_instance_identity_from_settings(settings, instance_id="postgresql-abc")

    def test_other_targets_register_the_package_version(self) -> None:
        from elspeth import __version__

        identity = web_instance_identity_from_settings(_settings(deployment_target="kubernetes"), instance_id="postgresql-abc")
        assert identity.deployment_target == "kubernetes"
        assert identity.deployment_generation == identity.image_digest == identity.revision_label == f"elspeth-{__version__}"

    def test_rejects_a_settings_impostor(self) -> None:
        with pytest.raises(TypeError):
            web_instance_identity_from_settings(object(), instance_id="postgresql-abc")  # type: ignore[arg-type]


class TestRegister:
    def test_inserts_an_active_row_leased_from_the_database_clock(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()

        record = authority.register(identity, lease_seconds=30)

        assert record.instance_id == identity.instance_id
        assert record.state is InstanceState.ACTIVE
        assert record.compatibility_key == identity.compatibility_key
        assert record.lease_expires_at - record.last_heartbeat_at == timedelta(seconds=30)
        assert record.started_at == record.last_heartbeat_at
        row = _row(engine, identity.instance_id)
        assert (row.state, row.deployment_target, row.deployment_generation, row.image_digest, row.revision_label) == (
            "active",
            "kubernetes",
            "generation-1",
            "sha256:membership-test",
            "revision-1",
        )
        assert (row.session_epoch, row.landscape_epoch, row.coordination_protocol) == (
            identity.compatibility_key.session_epoch,
            identity.compatibility_key.landscape_epoch,
            identity.compatibility_key.coordination_protocol,
        )

    def test_refuses_an_instance_id_held_under_a_live_lease(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)

        with pytest.raises(WebInstanceRegistrationConflict):
            authority.register(identity, lease_seconds=300)
        assert _row_count(engine) == 1

    def test_draining_row_under_a_live_lease_is_still_held(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)
        authority.begin_drain(identity.instance_id, lease_seconds=300)

        with pytest.raises(WebInstanceRegistrationConflict):
            authority.register(identity, lease_seconds=300)

    def test_reclaims_a_dead_incarnation_whose_lease_expired(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        first = authority.register(identity, lease_seconds=300)
        _expire(engine, identity.instance_id)

        second = authority.register(_identity(identity.instance_id, deployment_generation="generation-2"), lease_seconds=60)

        assert second.state is InstanceState.ACTIVE
        assert second.deployment_generation == "generation-2"
        assert second.lease_expires_at - second.last_heartbeat_at == timedelta(seconds=60)
        assert second.started_at >= first.started_at
        assert _row_count(engine) == 1

    def test_reclaims_a_stopped_incarnation(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)
        authority.stop(identity.instance_id)

        record = authority.register(identity, lease_seconds=300)

        assert record.state is InstanceState.ACTIVE
        assert _row_count(engine) == 1

    @pytest.mark.parametrize("lease_seconds", [0, -1, 3601, 30.0, "30", True])
    def test_lease_seconds_is_an_exact_bounded_integer(self, engine: Engine, lease_seconds: object) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        with pytest.raises(ValueError, match="lease_seconds"):
            authority.register(_identity(), lease_seconds=lease_seconds)  # type: ignore[arg-type]
        assert _row_count(engine) == 0

    def test_identity_must_be_the_owned_type(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        with pytest.raises(TypeError):
            authority.register({"instance_id": "x"}, lease_seconds=30)  # type: ignore[arg-type]

    def test_unsupported_dialect_is_refused_at_construction(self) -> None:
        class _Dialect:
            name = "oracle"

        class _Engine:
            dialect = _Dialect()

        with pytest.raises(NotImplementedError, match="oracle"):
            RepositoryWebInstanceMembershipAuthority(_Engine())  # type: ignore[arg-type]


class TestHeartbeat:
    def test_renews_the_lease_and_keeps_the_state(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        registered = authority.register(identity, lease_seconds=30)
        _expire(engine, identity.instance_id)

        renewed = authority.heartbeat(identity.instance_id, lease_seconds=45)

        assert renewed.state is InstanceState.ACTIVE
        assert renewed.started_at == registered.started_at
        assert renewed.last_heartbeat_at >= registered.last_heartbeat_at
        assert renewed.lease_expires_at - renewed.last_heartbeat_at == timedelta(seconds=45)

    def test_renews_while_draining(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=30)
        authority.begin_drain(identity.instance_id, lease_seconds=30)

        assert authority.heartbeat(identity.instance_id, lease_seconds=30).state is InstanceState.DRAINING

    def test_refuses_an_unregistered_or_stopped_instance(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        with pytest.raises(WebInstanceMembershipLost):
            authority.heartbeat("never-registered", lease_seconds=30)
        identity = _identity()
        authority.register(identity, lease_seconds=30)
        authority.stop(identity.instance_id)
        with pytest.raises(WebInstanceMembershipLost):
            authority.heartbeat(identity.instance_id, lease_seconds=30)
        assert _row(engine, identity.instance_id).state == "stopped"


class TestDrainAndStop:
    def test_begin_drain_moves_active_to_draining_once(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=30)

        draining = authority.begin_drain(identity.instance_id, lease_seconds=30)

        assert draining.state is InstanceState.DRAINING
        assert draining.lease_expires_at - draining.last_heartbeat_at == timedelta(seconds=30)
        with pytest.raises(WebInstanceMembershipLost):
            authority.begin_drain(identity.instance_id, lease_seconds=30)

    def test_stop_records_stopped_with_an_already_expired_lease(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)
        authority.begin_drain(identity.instance_id, lease_seconds=300)

        stopped = authority.stop(identity.instance_id)

        assert stopped.state is InstanceState.STOPPED
        assert stopped.lease_expires_at == stopped.last_heartbeat_at
        with pytest.raises(WebInstanceMembershipLost):
            authority.stop(identity.instance_id)

    def test_stop_without_a_drain_write_still_lands(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)

        assert authority.stop(identity.instance_id).state is InstanceState.STOPPED


class TestHeartbeatInterval:
    def test_three_renewals_per_lease_with_a_floor_of_one_second(self) -> None:
        assert heartbeat_interval_seconds(30) == 10
        assert heartbeat_interval_seconds(2) == 1
        assert heartbeat_interval_seconds(1) == 1

    @pytest.mark.parametrize("lease_seconds", [0, 3601, "30"])
    def test_rejects_an_invalid_lease(self, lease_seconds: object) -> None:
        with pytest.raises(ValueError):
            heartbeat_interval_seconds(lease_seconds)  # type: ignore[arg-type]


class TestSingleProcessMembership:
    @pytest.mark.asyncio
    async def test_owns_only_the_draining_signal(self, engine: Engine) -> None:
        membership = SingleProcessWebInstanceMembership()
        await membership.start()
        assert not membership.draining.is_set()
        await membership.begin_drain()
        assert membership.draining.is_set()
        await membership.stop()
        assert _row_count(engine) == 0


class TestRegisteredMembership:
    def test_requires_the_owned_authority_and_identity_types(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        with pytest.raises(TypeError):
            RegisteredWebInstanceMembership(object(), _identity(), lease_seconds=30)  # type: ignore[arg-type]
        with pytest.raises(TypeError):
            RegisteredWebInstanceMembership(authority, object(), lease_seconds=30)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="interval_seconds"):
            RegisteredWebInstanceMembership(authority, _identity(), lease_seconds=30, interval_seconds=31)

    @pytest.mark.asyncio
    async def test_start_registers_and_the_heartbeat_renews_the_lease(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)

        await membership.start()
        try:
            registered = _row(engine, identity.instance_id)
            assert registered.state == "active"
            _expire(engine, identity.instance_id)
            deadline = asyncio.get_running_loop().time() + 10.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.sleep(0.2)
                row = _row(engine, identity.instance_id)
                if row.lease_expires_at > row.last_heartbeat_at:
                    break
            else:
                pytest.fail("heartbeat never renewed the lease")
            assert row.lease_expires_at - row.last_heartbeat_at == timedelta(seconds=30)
            assert not membership.draining.is_set()
        finally:
            await membership.stop()
        assert _row(engine, identity.instance_id).state == "stopped"

    @pytest.mark.asyncio
    async def test_start_twice_is_refused_and_registration_failure_fails_start(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        authority.register(identity, lease_seconds=300)
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)

        with pytest.raises(WebInstanceRegistrationConflict):
            await membership.start()
        assert membership._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_drain_sets_the_signal_before_the_row_and_stop_records_stopped(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)
        await membership.start()

        await membership.begin_drain()
        assert membership.draining.is_set()
        assert _row(engine, identity.instance_id).state == "draining"

        await membership.stop()
        row = _row(engine, identity.instance_id)
        assert row.state == "stopped"
        assert row.lease_expires_at == row.last_heartbeat_at
        assert membership._heartbeat_task is None

    @pytest.mark.asyncio
    async def test_drain_and_stop_survive_a_lost_row(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)
        await membership.start()
        with engine.begin() as conn:
            conn.execute(web_instances_table.delete().where(web_instances_table.c.instance_id == identity.instance_id))

        await membership.begin_drain()
        await membership.stop()

        assert membership.draining.is_set()
        assert _row_count(engine) == 0

    @pytest.mark.asyncio
    async def test_bounded_transient_heartbeat_failures_cancel_the_owning_task(
        self, engine: Engine, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        calls = 0
        real_run_sync_in_worker = lifecycle_module.run_sync_in_worker

        async def contended_worker(func: Any, *args: Any, **kwargs: Any) -> Any:
            nonlocal calls
            if func == authority.heartbeat:
                calls += 1
                raise OperationalError("heartbeat", {}, Exception("database unavailable"))
            return await real_run_sync_in_worker(func, *args, **kwargs)

        monkeypatch.setattr(lifecycle_module, "run_sync_in_worker", contended_worker)
        monkeypatch.setattr(lifecycle_module, "_HEARTBEAT_MAX_CONSECUTIVE_FAILURES", 3)
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)

        async def owner() -> None:
            await membership.start()
            await asyncio.sleep(30)

        owning = asyncio.create_task(owner())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owning, timeout=20)
        assert calls == 3
        with pytest.raises(OperationalError):
            await membership.stop()

    @pytest.mark.asyncio
    async def test_a_non_transient_heartbeat_failure_escalates_at_once(self, engine: Engine) -> None:
        authority = RepositoryWebInstanceMembershipAuthority(engine)
        identity = _identity()
        membership = RegisteredWebInstanceMembership(authority, identity, lease_seconds=30, interval_seconds=1)

        async def owner() -> None:
            await membership.start()
            with engine.begin() as conn:
                conn.execute(web_instances_table.delete().where(web_instances_table.c.instance_id == identity.instance_id))
            await asyncio.sleep(30)

        owning = asyncio.create_task(owner())
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(owning, timeout=20)
        with pytest.raises(WebInstanceMembershipLost):
            await membership.stop()
