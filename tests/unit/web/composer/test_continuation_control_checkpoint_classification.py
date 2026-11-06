"""A semantic PRIMARY checkpoint must not masquerade as a control completion."""

from __future__ import annotations

import json

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.continuation_control_transitions import _closed_control_payload


@pytest.mark.parametrize("kind", ["semantic_primary_ready", "semantic_read_applied"])
def test_pending_control_ignores_only_known_ordinary_semantic_checkpoint_kinds(kind: str) -> None:
    assert _closed_control_payload(json.dumps({"kind": kind}), invalid_message="invalid control", expected_completion=True) is None


@pytest.mark.parametrize(
    "payload,expected_completion",
    [
        ({"kind": "semantic_primary_ready", "owner": "composer_control_completion"}, True),
        ({"kind": "unrecognized_checkpoint"}, True),
        ({"kind": "semantic_primary_ready"}, False),
        ({"previous_cursor": 1, "sequence": 1}, True),
    ],
)
def test_pending_control_refuses_claimed_or_unclassified_checkpoint_without_complete_owner(
    payload: dict[str, str | int], expected_completion: bool
) -> None:
    with pytest.raises(AuditIntegrityError):
        _closed_control_payload(json.dumps(payload), invalid_message="invalid control", expected_completion=expected_completion)
