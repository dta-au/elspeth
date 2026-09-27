# tests/integration/pipeline/test_batch_rank_output_modes.py
"""``batch_rank`` run end to end under both aggregation output modes.

``batch_rank`` is the shipped batch plugin that declares
``flush_emits_one_row_per_buffered_row``, so ``output_mode: passthrough``
admits it. Under passthrough every buffered token continues with its own
annotated row: the run records no new tokens and no parent links. Under
``output_mode: transform`` the same flush ends each buffered token in the batch
and emits new child tokens. Either way every input field reaches the sink
unchanged, the row with no score passes through unranked, and every token ends
with exactly one recorded terminal outcome.

Every case is a real ``elspeth validate`` + ``elspeth run --execute`` (the
in-process CLI) with the built-in plugins.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from tests.integration.pipeline.test_output_declaration_routing import _json_sink, _json_source, _read_jsonl, _write_jsonl

_ROWS = [
    {"id": 1, "note": "a", "score": 0.9},
    {"id": 2, "note": "b", "score": 0.7},
    {"id": 3, "note": "c", "score": None},
    {"id": 4, "note": "d", "score": 0.7},
    {"id": 5, "note": "e", "score": 0.4},
]

# Descending competition ranking of the four scored rows; percentile is the
# share of ranked rows strictly worse, so the lowest score sits at 0.0.
_ANNOTATIONS = {
    1: {"rank_rank": 1, "rank_percentile": 75.0},
    2: {"rank_rank": 2, "rank_percentile": 25.0},
    3: {"rank_rank": None, "rank_percentile": None},
    4: {"rank_rank": 2, "rank_percentile": 25.0},
    5: {"rank_rank": 4, "rank_percentile": 0.0},
}


def _settings_file(tmp_path: Path, output_mode: str) -> Path:
    settings: dict[str, Any] = {
        "sources": {"src": _json_source(tmp_path / "in.jsonl")},
        "concurrency": {"max_workers": 1},
        "aggregations": [
            {
                "name": "rank",
                "plugin": "batch_rank",
                "input": "rows",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": len(_ROWS)},
                "output_mode": output_mode,
                "options": {"schema": {"mode": "observed"}, "value_field": "score"},
            }
        ],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _scalar(tmp_path: Path, sql: str) -> int:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        (value,) = con.execute(sql).fetchone()
    finally:
        con.close()
    return int(value)


@pytest.mark.parametrize(
    ("output_mode", "expected_tokens", "expected_parent_links"),
    [
        ("passthrough", len(_ROWS), 0),
        ("transform", 2 * len(_ROWS), len(_ROWS)),
    ],
)
def test_batch_rank_annotates_every_row_and_records_the_mode_lineage(
    tmp_path: Path, output_mode: str, expected_tokens: int, expected_parent_links: int
) -> None:
    _write_jsonl(tmp_path / "in.jsonl", _ROWS)
    settings = _settings_file(tmp_path, output_mode)
    runner = CliRunner()
    validated = runner.invoke(app, ["validate", "-s", str(settings)])
    assert validated.exit_code == 0, validated.output

    result = runner.invoke(app, ["run", "-s", str(settings), "--execute"])

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "q.jsonl") == []
    # One worker, one flush: the sink sees the rows in buffered order, so a flush
    # that hands a token another row's values shows up as a reordered file.
    emitted = _read_jsonl(tmp_path / "out.jsonl")
    assert len(emitted) == len(_ROWS)
    for source_row, row in zip(_ROWS, emitted, strict=True):
        assert row == {
            **source_row,
            **_ANNOTATIONS[source_row["id"]],
            "rank_ranked_count": 4,
            "rank_batch_size": len(_ROWS),
        }
    assert _scalar(tmp_path, "select count(*) from tokens") == expected_tokens
    assert _scalar(tmp_path, "select count(*) from token_parents") == expected_parent_links
    assert (
        _scalar(
            tmp_path,
            "select count(*) from tokens t where "
            "(select count(*) from token_outcomes o where o.token_id = t.token_id and o.completed = 1) != 1",
        )
        == 0
    )
