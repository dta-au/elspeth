"""Run streams retain their issuing session's live authorization."""

from datetime import UTC, datetime
from typing import Any, cast
from unittest.mock import patch
from uuid import uuid4

import pytest

from elspeth.web.composer_stream_auth import ComposerStreamAuthServices
from elspeth.web.execution.run_progress_reader import RepositoryRunProgressReader
from elspeth.web.sessions.protocol import RunEventRecord
from tests.fixtures.identities import wire_test_pipeline_user_authority
from tests.unit.web.auth.conftest import build_local_auth_provider, local_auth_engine_scope
from tests.unit.web.execution.test_websocket import _call_websocket, _create_ws_test_app


@pytest.fixture(autouse=True)
def credential_engine_owner():
    with local_auth_engine_scope():
        yield


def _authenticated_app(tmp_path):
    provider = build_local_auth_provider(tmp_path / "auth.db")
    provider.create_user("alice", "old-password", "Alice")
    token = provider._login_sync("alice", "old-password")
    user = provider._authenticate_sync(token)
    app = _create_ws_test_app()
    wire_test_pipeline_user_authority(app, identity_id=user.user_id)
    app.state.auth_provider = provider
    app.state.composer_stream_auth = ComposerStreamAuthServices(
        provider=provider,
        decode=provider._issuer.decode,
        identity_authority=app.state.identity_authority,
        sessions=app.state.session_service,
        provider_type="local",
    )
    run_id = str(uuid4())
    ticket = app.state.websocket_ticket_store.issue(
        run_id=run_id,
        user=user,
        binding=provider._issuer.decode(token).binding,
    )
    return app, provider, token, run_id, ticket.ticket


def _revoke(provider, token, operation):
    if operation == "logout":
        provider._issuer.revoke(token)
    else:
        provider.set_password("alice", "replacement-password")


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["logout", "password-reset"])
async def test_preissued_ticket_is_refused_after_issuing_session_revocation(tmp_path, operation):
    app, provider, token, run_id, ticket = _authenticated_app(tmp_path)
    _revoke(provider, token, operation)
    websocket = await _call_websocket(app, run_id, ticket=ticket)
    assert not websocket.accepted
    assert websocket.close_code == 4001
    assert websocket.sent_json == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["logout", "password-reset"])
@pytest.mark.parametrize("durable", [False, True])
async def test_open_stream_rechecks_after_event_read_before_delivery(tmp_path, operation, durable):
    app, provider, token, run_id, ticket = _authenticated_app(tmp_path)
    event = RunEventRecord(
        id=uuid4(),
        run_id=uuid4(),
        sequence=1,
        timestamp=datetime.now(UTC),
        event_type="error",
        data={"message": "private row detail", "node_id": None, "row_id": None},
    )
    if durable:
        app.state.run_progress_reader = RepositoryRunProgressReader(cast(Any, None))

        def read_then_revoke(*args, **kwargs):
            _revoke(provider, token, operation)
            return (event,)

        with patch.object(RepositoryRunProgressReader, "read_after", side_effect=read_then_revoke):
            websocket = await _call_websocket(app, run_id, ticket=ticket)
    else:
        # The seed status loads before replay. Refuse its first persisted
        # event after revocation committed while the replay read was pending.
        from elspeth.web.execution.schemas import RunStatusResponse

        app.state.execution_service.statuses = [
            RunStatusResponse(
                run_id=run_id,
                status="running",
                started_at=datetime.now(UTC),
                finished_at=None,
                error=None,
                landscape_run_id=None,
            )
        ]

        async def list_then_revoke(_run_id):
            _revoke(provider, token, operation)
            return [event]

        app.state.session_service.list_run_events = list_then_revoke
        websocket = await _call_websocket(app, run_id, ticket=ticket)
    assert websocket.accepted
    assert websocket.close_code == 4001
    assert websocket.sent_json == []
