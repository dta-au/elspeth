"""Accepted interpretations may follow ordinary prose ending in a colon."""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest

from elspeth.web.sessions.pending_interpretation import _patch_llm_transform_prompt
from elspeth.web.sessions.protocol import CompositionStateRecord, InterpretationPlaceholderConsumedError


def _state(options: dict[str, object]) -> CompositionStateRecord:
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
        created_at=datetime.now(UTC),
        derived_from_state_id=None,
    )


@pytest.mark.parametrize(
    "prefix",
    ["Describe the user's role:", "Classify each row by operating system:", "Use these instructions:"],
)
@pytest.mark.parametrize("structured", [False, True], ids=["legacy-placeholder", "structured-parts"])
def test_accepted_prose_resolves(prefix: str, structured: bool) -> None:
    accepted = "a concise answer"
    options: dict[str, object] = {"prompt_template": f"{prefix} {{{{interpretation:term}}}}"}
    if structured:
        options["prompt_template_parts"] = [
            {"kind": "text", "text": f"{prefix} "},
            {"kind": "interpretation_ref", "requirement_id": "req-1"},
        ]
        options["interpretation_requirements"] = [
            {"id": "req-1", "kind": "vague_term", "user_term": "term", "status": "pending", "draft": "draft"}
        ]

    try:
        nodes = _patch_llm_transform_prompt(_state(options), affected_node_id="n1", user_term="term", accepted_value=accepted)
        actual = nodes[0]["options"]["prompt_template"]
    except InterpretationPlaceholderConsumedError as exc:
        actual = str(exc)

    assert actual == f"{prefix} {accepted}"


def test_consumed_placeholder_still_raises() -> None:
    with pytest.raises(InterpretationPlaceholderConsumedError, match="does not contain placeholder"):
        _patch_llm_transform_prompt(
            _state({"prompt_template": "The placeholder has already been consumed."}),
            affected_node_id="n1",
            user_term="term",
            accepted_value="a concise answer",
        )
