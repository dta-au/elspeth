"""WS1b Phase B flip: TokenInfo retires branch_name/fork_group_id/expand_group_id
as stored fields and re-exposes them as read-only derived properties over
lineage_path (spec §4.1a, ruling 21)."""

import dataclasses

import pytest

from elspeth.contracts.enums import FrameKind
from elspeth.contracts.identity import LineageFrame, TokenInfo
from elspeth.testing import make_row

FORK = LineageFrame(kind=FrameKind.FORK, group_id="fg-1", member_key="path_a")
EXPAND = LineageFrame(kind=FrameKind.EXPAND, group_id="eg-1", member_key="tok-c1")


def _token(path: tuple[LineageFrame, ...]) -> TokenInfo:
    return TokenInfo(row_id="r1", token_id="t1", row_data=make_row({}), lineage_path=path)


def test_stored_lineage_fields_are_retired() -> None:
    field_names = {f.name for f in dataclasses.fields(TokenInfo)}
    assert {"branch_name", "fork_group_id", "join_group_id", "expand_group_id"} & field_names == set()
    assert "lineage_path" in field_names


def test_derived_accessors_read_the_path() -> None:
    token = _token((FORK, EXPAND))
    assert token.branch_name == "path_a"
    assert token.fork_group_id == "fg-1"
    assert token.expand_group_id == "eg-1"
    assert _token(()).branch_name is None


def test_accessors_are_read_only() -> None:
    token = _token((FORK,))
    # CPython's frozen+slots dataclass __setattr__ raises TypeError (a broken
    # super() binding, cpython/issues/91126) rather than AttributeError for a
    # non-field attribute like a read-only property; both prove the write is
    # rejected.
    with pytest.raises((AttributeError, TypeError)):
        token.branch_name = "x"  # type: ignore[misc]


def test_with_updated_data_preserves_the_path() -> None:
    token = _token((FORK, EXPAND))
    assert token.with_updated_data(make_row({"a": 1})).lineage_path == (FORK, EXPAND)
