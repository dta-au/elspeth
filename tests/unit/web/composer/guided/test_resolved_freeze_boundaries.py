"""Malformed-input rejection pins for the guided strict-JSON freeze boundaries.

These are the honesty tests referenced by the ``@trust_boundary`` metadata on
``resolved._validate_and_freeze_guided_json``, ``resolved.freeze_guided_json_mapping``,
and ``resolved.freeze_guided_str_sequence``: each boundary must REJECT malformed
externally submitted values (raise), never coerce or default them.
"""

from __future__ import annotations

from types import MappingProxyType

import pytest

from elspeth.web.composer.guided.errors import InvariantError
from elspeth.web.composer.guided.resolved import (
    GuidedJsonBudget,
    _validate_and_freeze_guided_json,
    freeze_guided_json_mapping,
    freeze_guided_str_sequence,
)


def test_validate_and_freeze_guided_json_rejects_non_json_leaf() -> None:
    with pytest.raises(InvariantError, match="exact JSON leaf"):
        _validate_and_freeze_guided_json(
            object(),
            "field",
            path="$",
            depth=0,
            budget=GuidedJsonBudget(),
            active_container_ids=set(),
        )


def test_validate_and_freeze_guided_json_rejects_non_str_mapping_key() -> None:
    with pytest.raises(InvariantError, match="must be an exact str"):
        _validate_and_freeze_guided_json(
            {1: "value"},
            "field",
            path="$",
            depth=0,
            budget=GuidedJsonBudget(),
            active_container_ids=set(),
        )


def test_freeze_guided_json_mapping_rejects_non_mapping() -> None:
    with pytest.raises(TypeError, match="must be a mapping"):
        freeze_guided_json_mapping(["not", "a", "mapping"], "field")


def test_freeze_guided_json_mapping_accepts_frozen_replay_mapping() -> None:
    frozen = freeze_guided_json_mapping(MappingProxyType({"key": ["value"]}), "field")
    # Interior lists freeze to FrozenJsonArray (tuple-comparable), and the
    # container itself is a detached read-only mapping.
    assert dict(frozen) == {"key": ("value",)}
    assert isinstance(frozen, MappingProxyType)


def test_freeze_guided_str_sequence_rejects_non_sequence() -> None:
    with pytest.raises(TypeError, match="must be a sequence"):
        freeze_guided_str_sequence("a-str-is-a-character-sequence-trap", "field")


def test_freeze_guided_str_sequence_rejects_non_str_member() -> None:
    with pytest.raises(TypeError, match="must be an exact str"):
        freeze_guided_str_sequence(["fine", 7], "field")
