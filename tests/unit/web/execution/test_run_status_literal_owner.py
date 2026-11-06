"""Real response FieldInfo reads project only the current shared status literals."""

from collections.abc import Mapping
from types import GenericAlias
from typing import Literal, get_args, get_origin

import pytest
from pydantic.fields import FieldInfo

from elspeth.web.execution.schemas import (
    RUN_STATUS_ALL_VALUES,
    RUN_STATUS_TERMINAL_VALUES,
    RunResultsResponse,
    RunStatusResponse,
    _status_literal_values,
)
from elspeth.web.sessions.protocol import SessionRunStatus, TerminalSessionRunStatus


@pytest.mark.parametrize(
    "model,owner,published",
    [
        (RunStatusResponse, SessionRunStatus, RUN_STATUS_ALL_VALUES),
        (RunResultsResponse, TerminalSessionRunStatus, RUN_STATUS_TERMINAL_VALUES),
    ],
)
def test_status_literal_projection_keeps_actual_field_receiver_owner_and_values(model, owner, published):
    fields = model.model_fields
    assert type(fields) is dict
    field = fields["status"]
    assert type(field) is FieldInfo
    assert field.annotation is owner
    assert get_origin(field.annotation) is Literal
    original_projection = frozenset(get_args(field.annotation))
    assert original_projection
    assert all(type(value) is str for value in original_projection)
    assert _status_literal_values(field.annotation, owner) == original_projection == published


def test_status_literal_projection_refuses_different_current_owner():
    field = RunStatusResponse.model_fields["status"]
    assert _status_literal_values(field.annotation, SessionRunStatus) == RUN_STATUS_ALL_VALUES
    assert field.annotation is not TerminalSessionRunStatus
    assert get_origin(TerminalSessionRunStatus) is Literal
    with pytest.raises(TypeError, match="current shared Literal owner"):
        _status_literal_values(field.annotation, TerminalSessionRunStatus)


def test_status_literal_projection_refuses_nonliteral_with_string_arguments():
    assert _status_literal_values(SessionRunStatus, SessionRunStatus) == RUN_STATUS_ALL_VALUES
    foreign = GenericAlias(list, ("pending",))
    assert get_origin(foreign) is list
    assert get_args(foreign) == ("pending",)
    with pytest.raises(TypeError, match="must be a Literal"):
        _status_literal_values(foreign, foreign)


@pytest.mark.parametrize("foreign", [Literal[17], Literal[Mapping]])
def test_status_literal_projection_refuses_nonstring_literal_members(foreign):
    assert _status_literal_values(SessionRunStatus, SessionRunStatus) == RUN_STATUS_ALL_VALUES
    assert get_origin(foreign) is Literal
    assert get_args(foreign)
    assert any(type(value) is not str for value in get_args(foreign))
    with pytest.raises(TypeError, match="members must be exact strings"):
        _status_literal_values(foreign, foreign)


def test_status_literal_projection_refuses_empty_literal():
    assert _status_literal_values(SessionRunStatus, SessionRunStatus) == RUN_STATUS_ALL_VALUES
    foreign = Literal[()]
    assert get_origin(foreign) is Literal
    assert get_args(foreign) == ()
    with pytest.raises(TypeError, match="must not be empty"):
        _status_literal_values(foreign, foreign)
