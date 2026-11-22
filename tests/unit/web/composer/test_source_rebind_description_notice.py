"""A rebind that keeps a stored source description says so in one short line.

Rebinding a source to another uploaded file keeps the source's description,
which may still name the earlier file. The result says the description was
kept, without quoting it, so the composer does not tell the user that
everything else is unchanged about text that may now be stale. A new source
and a rebind of a source with no description carry no such line.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import pytest

from tests.unit.web.composer import test_blob_inline_tools as blob_tools
from tests.unit.web.composer.test_source_rebind_keeps_description import _arguments, _empty_state, _execute, _upload_csv

blob_env = blob_tools.blob_env

_DESCRIPTION = "Read every row of the uploaded orders file."
_NOTICE_MARK = "kept its stored description"


def _notices(result: Any) -> list[str]:
    return [hint for hint in result.post_call_hints if _NOTICE_MARK in hint]


@pytest.mark.parametrize("tool", ["set_source_from_blob", "set_source_from_blobs"])
@pytest.mark.parametrize("described", [True, False])
def test_rebind_says_the_description_was_kept_only_when_one_was(blob_env: dict[str, Any], tool: str, described: bool) -> None:
    first = _upload_csv(blob_env, "orders_a.csv", "order_id,total\n1,10\n")
    bound = _execute(tool, _arguments(tool, "submissions", first), _empty_state(), blob_env)
    assert bound.success is True, [error.message for error in bound.validation.errors]
    assert _notices(bound) == []  # a new source: no line
    state = bound.updated_state
    if described:
        state = replace(state, sources={"submissions": replace(state.sources["submissions"], description=_DESCRIPTION)})
    second = _upload_csv(blob_env, "orders_b.csv", "order_id,total\n2,20\n")

    result = _execute(tool, _arguments(tool, "submissions", second), state, blob_env)

    assert result.success is True, [error.message for error in result.validation.errors]
    notices = _notices(result)
    if described:
        assert len(notices) == 1, result.post_call_hints
        notice = notices[0]
        assert "submissions" in notice
        assert _DESCRIPTION not in notice  # a fact about the description, never its text
        assert len(notice) <= 160
    else:
        assert notices == []
