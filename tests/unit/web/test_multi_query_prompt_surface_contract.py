"""Contract of ``multi_query_prompt_surface_from_options`` on malformed prompt parts.

The observation boundary once claimed it never raises, but the node-level
skeleton hash it computes propagates the same errors a single-prompt node's
``prompt_structure_hash_from_options`` raises. These cases pin that contract
with the exact error text, and the control proves a well-formed surface builds.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.web.interpretation_state import MultiQueryPromptSurface, multi_query_prompt_surface_from_options

_BASE: dict[str, Any] = {
    "prompt_template": "Summarise {{ row.x }}",
    "queries": [{"name": "q", "input_fields": {"x": "x"}}],
}


def test_well_formed_parts_build_a_surface() -> None:
    options = {**_BASE, "prompt_template_parts": [{"kind": "text", "text": "Summarise {{ row.x }}"}]}

    assert isinstance(multi_query_prompt_surface_from_options(options), MultiQueryPromptSurface)


@pytest.mark.parametrize(
    ("parts", "error", "match"),
    [
        ([{"kind": "bogus"}], ValueError, "unknown prompt_template_parts kind 'bogus'"),
        ([{"kind": "interpretation_ref"}], KeyError, "requirement_id"),
        ([{"kind": "text"}], KeyError, "text"),
        ("oops", TypeError, "prompt_template_parts must be a list"),
        ([5], TypeError, "prompt_template_parts entries must be mappings"),
    ],
    ids=["unknown-kind", "ref-without-id", "text-without-text", "not-a-list", "entry-not-a-mapping"],
)
def test_malformed_parts_propagate_the_skeleton_hash_error(parts: object, error: type[Exception], match: str) -> None:
    with pytest.raises(error, match=match):
        multi_query_prompt_surface_from_options({**_BASE, "prompt_template_parts": parts})
