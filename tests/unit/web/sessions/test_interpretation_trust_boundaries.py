"""Malformed-input honesty tests for the interpretation Tier-3 boundaries.

Each test pins the raising invariant declared by the matching
``@trust_boundary`` metadata in ``web/sessions/service.py``: the boundary
rejects a malformed persisted composer shape with
``InterpretationPlaceholderConsumedError`` instead of silently routing it to
a legacy arm or defaulting it away. The ``trust_boundary.tests`` gate binds
these nodeids (and their AST fingerprints) to the decorated functions.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.contracts.composer_interpretation import InterpretationKind
from elspeth.web.sessions.protocol import CompositionStateRecord, InterpretationPlaceholderConsumedError
from elspeth.web.sessions.service import (
    _has_matching_vague_term_requirement,
    _matching_pending_requirement_index,
    _patch_structured_interpretation_prompt,
    _require_mapping,
    _reviewed_content_identity,
)


def _llm_node(options: dict[str, object]) -> dict[str, object]:
    return {
        "id": "n1",
        "node_type": "transform",
        "plugin": "llm",
        "options": options,
    }


def _state_record(nodes: list[dict[str, object]]) -> CompositionStateRecord:
    return CompositionStateRecord(
        id=uuid4(),
        session_id=uuid4(),
        version=1,
        nodes=nodes,
        edges=None,
        outputs=None,
        metadata_=None,
        is_valid=True,
        validation_errors=None,
        created_at=datetime(2026, 8, 31, 12, 0, 0, tzinfo=UTC),
        derived_from_state_id=None,
    )


def test_require_mapping_rejects_non_mapping() -> None:
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _require_mapping(["not", "a", "mapping"], message="ctx: value is not a mapping")


def test_matching_pending_requirement_index_rejects_non_list() -> None:
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _matching_pending_requirement_index(
            "not-a-list",
            kind=InterpretationKind.VAGUE_TERM,
            user_term="recent",
            context="ctx",
        )


def test_matching_pending_requirement_index_rejects_non_mapping_entry() -> None:
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _matching_pending_requirement_index(
            ["not-a-mapping"],
            kind=InterpretationKind.VAGUE_TERM,
            user_term="recent",
            context="ctx",
        )


def test_has_matching_vague_term_requirement_rejects_malformed_value() -> None:
    # A PRESENT non-list value must raise — it may never read as "legacy node".
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _has_matching_vague_term_requirement(
            "not-a-list",
            user_term="recent",
            context="ctx",
            require_pending=True,
        )
    # A non-mapping row inside a present list must raise too.
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _has_matching_vague_term_requirement(
            ["not-a-mapping"],
            user_term="recent",
            context="ctx",
            require_pending=False,
        )


def test_has_matching_vague_term_requirement_absent_is_legacy_false() -> None:
    assert _has_matching_vague_term_requirement(None, user_term="recent", context="ctx", require_pending=True) is False


def test_has_matching_vague_term_requirement_matches_pending_row() -> None:
    rows = [
        {
            "id": "req-1",
            "kind": InterpretationKind.VAGUE_TERM.value,
            "user_term": "recent",
            "status": "pending",
            "draft": "last 30 days",
        }
    ]
    assert _has_matching_vague_term_requirement(rows, user_term="recent", context="ctx", require_pending=True) is True
    resolved = [dict(rows[0], status="resolved")]
    assert _has_matching_vague_term_requirement(resolved, user_term="recent", context="ctx", require_pending=True) is False
    assert _has_matching_vague_term_requirement(resolved, user_term="recent", context="ctx", require_pending=False) is True


def test_patch_structured_interpretation_prompt_rejects_non_list_requirements() -> None:
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _patch_structured_interpretation_prompt(
            options={"interpretation_requirements": "not-a-list"},
            affected_node_id="n1",
            user_term="recent",
            accepted_value="last 30 days",
        )


def test_reviewed_content_identity_rejects_malformed_requirements() -> None:
    state_record = _state_record(
        [
            _llm_node(
                {
                    "prompt_template": "classify {{interpretation:recent}} rows",
                    "interpretation_requirements": "not-a-list",
                }
            )
        ]
    )
    with pytest.raises(InterpretationPlaceholderConsumedError):
        _reviewed_content_identity(
            state_record,
            kind=InterpretationKind.VAGUE_TERM,
            affected_node_id="n1",
            user_term="recent",
            context="ctx",
        )
