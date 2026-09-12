"""Persisted envelopes cannot authorize deployment auth exports on recovery."""

import threading
from datetime import UTC, datetime
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.secrets import resolve_secret_refs
from elspeth.web.coordination.contracts import StartPermitState
from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.envelope import capture_execution_envelope, restore_execution_envelope
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from elspeth.web.sessions.protocol import RunStartPermitRecord
from tests.unit.web.execution.test_auth_event_export_guard import (
    _execute_lease,
    _ReachedSettingsLoad,
)
from tests.unit.web.execution.test_auth_event_export_guard import _live_execute_lease as _live_execute_lease
from tests.unit.web.execution.test_auth_event_export_guard import broadcaster as broadcaster
from tests.unit.web.execution.test_auth_event_export_guard import mock_loop as mock_loop
from tests.unit.web.execution.test_auth_event_export_guard import mock_session_service as mock_session_service
from tests.unit.web.execution.test_auth_event_export_guard import mock_settings as mock_settings
from tests.unit.web.execution.test_auth_event_export_guard import real_loop as real_loop
from tests.unit.web.execution.test_auth_event_export_guard import service as service


@pytest.mark.parametrize("auth_events", ["omitted", "deployment_snapshot"])
def test_restored_envelope_rechecks_auth_export_scope(request: pytest.FixtureRequest, auth_events: str) -> None:
    execution_service = cast(ExecutionServiceImpl, request.getfixturevalue("service"))
    sessions = request.getfixturevalue("mock_session_service")
    snapshot = PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service())
    config = {
        "sinks": {"audit": {"plugin": "json", "options": {"path": "audit.json", "schema": {"mode": "observed"}}}},
        "landscape": {"export": {"auth_events": auth_events}},
    }
    frozen = FrozenRunSettings(snapshot, config, config)
    envelope = capture_execution_envelope(
        frozen,
        user_id="alice",
        auth_provider_type="oidc",
        resolver=None,
        env_ref_names=frozenset(),
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    restored = restore_execution_envelope(
        envelope.to_json(),
        current_snapshot=snapshot,
        user_id="alice",
        auth_provider_type="oidc",
        resolver=None,
        implementation_fingerprint="d" * 64,
        deployment_generation="image-1",
    )
    run_id = str(uuid4())
    sessions.issue_run_start_permit.return_value = RunStartPermitRecord(
        run_id,
        StartPermitState.START_PERMITTED,
        str(uuid4()),
        1,
        "a" * 64,
        datetime.now(UTC),
        None,
    )
    execution_service._plugin_snapshot_factory = lambda _user_id: snapshot
    with (
        LandscapeDB.in_memory() as db,
        patch("elspeth.web.execution.service.open_landscape_db", return_value=db),
        patch("elspeth.web.execution.service.load_settings_from_config_dict", side_effect=_ReachedSettingsLoad) as load,
        patch("elspeth.core.audit_export_content_store.create_audit_export_content_store") as create_store,
        patch("elspeth.core.secrets.resolve_secret_refs", wraps=resolve_secret_refs) as resolve_secrets,
    ):
        expected = ValueError if auth_events == "deployment_snapshot" else _ReachedSettingsLoad
        match = "deployment_snapshot.*not permitted" if auth_events == "deployment_snapshot" else None
        with pytest.raises(expected, match=match):
            execution_service._run_pipeline(
                run_id,
                "{}",
                threading.Event(),
                frozen_run_settings=restored.settings,
                user_id="alice",
                auth_provider_type="oidc",
                session_operation_lease=_execute_lease(),
                durable_admission=True,
                resume_existing=True,
                restored_envelope=restored,
            )
        assert load.call_count == (0 if auth_events == "deployment_snapshot" else 1)
        create_store.assert_not_called()
        assert resolve_secrets.call_count == (0 if auth_events == "deployment_snapshot" else 1)
