"""Durable recompose lease loss remains distinct from explicit user Stop."""

from __future__ import annotations

import pytest

from tests.unit.web.sessions.routes.test_compose_heartbeat_renewal import (
    _assert_joined_lifecycle_children as _assert_joined_lifecycle_children,
)
from tests.unit.web.sessions.routes.test_compose_heartbeat_renewal import _durable_heartbeat


@pytest.mark.asyncio
async def test_recompose_heartbeat_cancel_records_failed_and_answers_503(tmp_path, monkeypatch):
    await _durable_heartbeat(tmp_path, monkeypatch, recompose=True)


@pytest.mark.asyncio
async def test_recompose_plain_task_cancel_is_still_a_client_cancel(tmp_path, monkeypatch):
    """A durable user Stop replaces the old socket-owned cancellation signal."""
    await _durable_heartbeat(tmp_path, monkeypatch, recompose=True, explicit_stop=True)
