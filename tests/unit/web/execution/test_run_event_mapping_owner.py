"""The event consistency check admits exact string keys before hashing them."""

from typing import Literal

import pytest

from elspeth.web.execution.schemas import RunEvent, RunEventType, _require_run_event_mapping


def test_actual_run_event_field_and_mapping_preserve_current_consistency_contract():
    annotation = RunEvent.model_fields["event_type"].annotation
    mapping = RunEvent._EVENT_TYPE_TO_DATA_TYPE
    assert annotation is RunEventType
    assert type(mapping) is dict
    before = tuple(mapping.items())
    assert _require_run_event_mapping(annotation, mapping) is None
    assert tuple(mapping.items()) == before
    assert all(current is retained for (_, current), (_, retained) in zip(mapping.items(), before, strict=True))


@pytest.mark.parametrize("annotation", [str, list[str], Literal["unowned"]])
def test_run_event_mapping_refuses_a_foreign_annotation_owner(annotation):
    _require_run_event_mapping(RunEventType, RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    with pytest.raises(TypeError, match="current Literal owner"):
        _require_run_event_mapping(annotation, RunEvent._EVENT_TYPE_TO_DATA_TYPE)


def test_run_event_mapping_refuses_string_subclass_keys_before_equality_or_rehash():
    _require_run_event_mapping(RunEventType, RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    effects: list[str] = []

    class EventKey(str):
        def __hash__(self):
            effects.append("hash")
            return str.__hash__(self)

        def __eq__(self, other):
            effects.append("equality")
            return str.__eq__(self, other)

    mapping = dict(RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    value = mapping.pop("progress")
    key = EventKey("progress")
    mapping[key] = value
    assert next(iter(mapping.items()))[0] == "error"
    assert any(retained is key for retained in mapping)
    effects.clear()
    with pytest.raises(TypeError, match="keys must be exact strings"):
        _require_run_event_mapping(RunEventType, mapping)
    assert effects == []


def test_run_event_mapping_refuses_mapping_subclass_before_its_iterator():
    _require_run_event_mapping(RunEventType, RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    effects: list[str] = []

    class ForeignMapping(dict):
        def __iter__(self):
            effects.append("iteration")
            return super().__iter__()

    mapping = ForeignMapping(RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    with pytest.raises(TypeError, match="exact dict"):
        _require_run_event_mapping(RunEventType, mapping)
    assert effects == []


@pytest.mark.parametrize("changed", ["missing", "extra"])
def test_run_event_mapping_keeps_the_original_label_set_drift_refusal(changed):
    _require_run_event_mapping(RunEventType, RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    mapping = dict(RunEvent._EVENT_TYPE_TO_DATA_TYPE)
    if changed == "missing":
        del mapping["progress"]
    else:
        mapping["unowned"] = mapping["progress"]
    with pytest.raises(AssertionError, match="event_type Literal values"):
        _require_run_event_mapping(RunEventType, mapping)
