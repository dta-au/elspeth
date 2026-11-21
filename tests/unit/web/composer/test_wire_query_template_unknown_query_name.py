"""What ``wire_blob_inline_ref`` does today when the field path names a query the node does not have.

Describes current behaviour only (adjacent finding 7 of the F2 recon). Not part
of the F2 change: no node-level term review is involved, so the F2 check never
runs here.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from tests.unit.web.composer import test_wire_query_template_pending_term as f2

# The F2 module's session fixture, registered here under the same name.
env = f2.env


def test_wiring_a_file_to_a_query_name_the_node_does_not_have(env: dict[str, Any], tmp_path: Path) -> None:
    state = f2._admitted_state(tmp_path, term=False)
    assert sorted(f2._options(state)["queries"]) == ["first", "second"]

    result = f2._wire(env, state, f"node:{f2._NODE}.options.queries.third.template", f2._prompt_file(env))

    # Today: accepted. The handler creates the entry, holding the file marker and nothing else.
    assert result.success is True
    queries = f2._options(result.updated_state)["queries"]
    assert sorted(queries) == ["first", "second", "third"]
    assert sorted(queries["third"]) == ["template"]
    # The stored state is invalid from then on: the new query has no input_fields.
    assert [error.error_code for error in result.validation.errors] == ["plugin_options_invalid"]
