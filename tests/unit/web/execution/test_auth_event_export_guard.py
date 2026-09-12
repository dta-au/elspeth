"""Web session downloads cannot authorize deployment-wide authentication history."""

import json
import threading
from typing import cast
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.web.dependencies import create_catalog_service
from elspeth.web.execution.protocol import FrozenRunSettings
from elspeth.web.execution.service import ExecutionServiceImpl
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot
from tests.unit.web.execution.test_service import (
    _execute_lease,
)
from tests.unit.web.execution.test_service import (
    _live_execute_lease as _live_execute_lease,
)
from tests.unit.web.execution.test_service import (
    broadcaster as broadcaster,
)
from tests.unit.web.execution.test_service import (
    mock_loop as mock_loop,
)
from tests.unit.web.execution.test_service import (
    mock_session_service as mock_session_service,
)
from tests.unit.web.execution.test_service import (
    mock_settings as mock_settings,
)
from tests.unit.web.execution.test_service import (
    real_loop as real_loop,
)
from tests.unit.web.execution.test_service import (
    service as service,
)


class _ReachedSettingsLoad(Exception):
    """Stop after authorization, before any runtime resources are constructed."""


@pytest.mark.parametrize("frozen", [False, True], ids=["operator-yaml", "frozen-policy-settings"])
@pytest.mark.parametrize("auth_events", ["omitted", "deployment_snapshot"])
@pytest.mark.parametrize("enabled", [False, True])
def test_web_auth_event_export_authorization(request: pytest.FixtureRequest, frozen: bool, auth_events: str, enabled: bool) -> None:
    execution_service = cast(ExecutionServiceImpl, request.getfixturevalue("service"))
    if frozen:
        execution_service._trained_operator_mode = False
    config = {
        "sinks": {"audit": {"plugin": "json", "options": {"path": "audit.json", "schema": {"mode": "observed"}}}},
        "landscape": {
            "export": {
                "enabled": enabled,
                "sink": "audit",
                "format": "json",
                "auth_events": auth_events,
                "total_record_limit": 10,
                "total_byte_limit": 1000,
                "chunk_limit": 1,
                "per_chunk_record_limit": 10,
                "per_chunk_byte_limit": 1000,
                "spool_root": ".elspeth/audit-export-spool/web-guard-test",
                "content_store": {
                    "content_store_id": "web-guard-test",
                    "namespace": "web-guard-test",
                    "root": ".elspeth/audit-export-content-store/web-guard-test",
                    "policy_version": "v1",
                    "retention_days": 1,
                    "durability": "fsync",
                },
            }
        },
    }
    frozen_settings = (
        FrozenRunSettings(
            plugin_snapshot=PluginAvailabilitySnapshot.for_trained_operator(create_catalog_service()),
            executable_config=config,
            audit_safe_config=config,
        )
        if frozen
        else None
    )
    if frozen_settings is not None:
        snapshot = frozen_settings.plugin_snapshot
        execution_service._plugin_snapshot_factory = lambda _user_id: snapshot
    with (
        patch("elspeth.web.execution.service.load_settings_from_config_dict", side_effect=_ReachedSettingsLoad) as load_config,
        patch("elspeth.web.execution.service.load_settings_from_yaml_string", side_effect=_ReachedSettingsLoad) as load_yaml,
        patch("elspeth.core.secrets.resolve_secret_refs") as resolve_secrets,
        patch("elspeth.web.execution.service.open_landscape_db") as open_landscape,
        patch("elspeth.core.audit_export_content_store.create_audit_export_content_store") as create_store,
    ):
        expected_error = ValueError if auth_events == "deployment_snapshot" else _ReachedSettingsLoad
        match = "deployment_snapshot.*not permitted in Web execution" if auth_events == "deployment_snapshot" else None
        with pytest.raises(expected_error, match=match):
            execution_service._run_pipeline(
                str(uuid4()),
                json.dumps(config),
                threading.Event(),
                user_id="alice",
                frozen_run_settings=frozen_settings,
                session_operation_lease=_execute_lease(),
            )
        if auth_events == "deployment_snapshot":
            load_config.assert_not_called()
            load_yaml.assert_not_called()
        else:
            assert load_config.call_count + load_yaml.call_count == 1
        resolve_secrets.assert_not_called()
        open_landscape.assert_not_called()
        create_store.assert_not_called()
