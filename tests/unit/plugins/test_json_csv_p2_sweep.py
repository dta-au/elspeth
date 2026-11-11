"""Regression tests for sweep findings #40, #80 and #103 (20261111-plugins-review).

#40: JSONSource structural quarantines must record the SAME row payload in
Landscape that they yield, so the quarantined emission links to its
validation error (and snapshot_for_resume does not crash).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.plugin_context import PluginContext
from elspeth.plugins.sources.json_source import JSONSource
from tests.fixtures.landscape import make_recorder_with_run


def _landscape_ctx() -> PluginContext:
    setup = make_recorder_with_run(source_plugin_name="json")
    return PluginContext(
        run_id=setup.run_id,
        coordination_token=setup.coordination_token,
        node_id=setup.source_node_id,
        config={},
        landscape=setup.factory.plugin_audit_writer(),
    )


@pytest.mark.parametrize(
    ("content", "data_key"),
    [
        pytest.param('[{"id": 1}]', "results", id="data_key_on_list_root"),
        pytest.param('{"wrong": [1]}', "results", id="data_key_missing"),
        pytest.param('{"results": {"id": 1}}', "results", id="data_key_extracts_non_list"),
        pytest.param('{"a": 1}', None, id="non_array_root"),
    ],
)
def test_json_structural_quarantine_links_to_recorded_validation_error(tmp_path: Path, content: str, data_key: str | None) -> None:
    path = tmp_path / "data.json"
    path.write_text(content)
    config: dict[str, object] = {
        "path": str(path),
        "format": "json",
        "on_validation_failure": "quarantine",
        "schema": {"mode": "observed"},
    }
    if data_key is not None:
        config["data_key"] = data_key
    ctx = _landscape_ctx()

    rows = list(JSONSource(config).load(ctx))

    assert len(rows) == 1
    quarantined = rows[0]
    assert quarantined.is_quarantined
    validation_error_id = ctx.pop_pending_quarantine_validation_error_id(quarantined.row)
    assert validation_error_id is not None, "quarantined structural row is not linked to its recorded validation error"
    assert validation_error_id.startswith("verr_")
