"""Malformed-input honesty tests for the routes/_helpers Tier-3 boundaries.

Each test pins the raising invariant declared by the matching
``@trust_boundary`` metadata in ``web/sessions/routes/_helpers.py``. The
``trust_boundary.tests`` gate binds these nodeids (and their AST
fingerprints) to the decorated functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.sessions.protocol import CompositionStateRecord
from elspeth.web.sessions.routes._helpers import (
    _extract_runtime_model_snapshot,
    _guided_source_commit_failure_detail,
)


def test_guided_source_commit_failure_detail_rejects_non_tool_result() -> None:
    with pytest.raises(TypeError):
        _guided_source_commit_failure_detail({"data": {"error": "Path violation (S2): Source file paths"}})


def _state_with_options(options: object) -> CompositionStateRecord:
    return CompositionStateRecord(
        id=uuid4(),
        session_id=uuid4(),
        version=1,
        nodes=[{"id": "n1", "node_type": "transform", "plugin": "llm", "options": options}],
        edges=None,
        outputs=None,
        metadata_=None,
        is_valid=True,
        validation_errors=None,
        created_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
        derived_from_state_id=None,
    )


def test_extract_runtime_model_snapshot_rejects_non_string_model() -> None:
    state = _state_with_options({"model": 123})
    with pytest.raises(AuditIntegrityError):
        _extract_runtime_model_snapshot(state, "n1")


def test_extract_runtime_model_snapshot_rejects_non_mapping_options() -> None:
    state = _state_with_options("not-a-mapping")
    with pytest.raises(AuditIntegrityError):
        _extract_runtime_model_snapshot(state, "n1")


def test_extract_runtime_model_snapshot_absent_pin_projects_none() -> None:
    state = _state_with_options({})
    assert _extract_runtime_model_snapshot(state, "n1") == (None, None)
