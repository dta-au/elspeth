"""The run summary counts rows as succeeded only when their sink write finalized.

A run whose sink refuses the write has processed its rows but written none of
them, so its summary must not report them as succeeded.
"""

from __future__ import annotations

import re
from pathlib import Path

from typer.testing import CliRunner

from elspeth.cli import app


def _settings(tmp_path: Path, *, target_exists: bool) -> Path:
    source = tmp_path / "in.csv"
    source.write_text("id,name\n1,a\n2,b\n3,c\n")
    target = tmp_path / "out.csv"
    if target_exists:
        target.write_text("existing\n")
    settings = tmp_path / "settings.yaml"
    settings.write_text(
        f"""
sources:
  primary:
    plugin: csv
    on_success: out
    options:
      path: {source}
      schema: {{mode: fixed, fields: ['id: int', 'name: str']}}
      on_validation_failure: discard
sinks:
  out:
    plugin: csv
    on_write_failure: discard
    options:
      path: {target}
      schema: {{mode: fixed, fields: ['id: int', 'name: str']}}
      mode: write
      collision_policy: fail_if_exists
landscape:
  url: sqlite:///{tmp_path / "audit.db"}
payload_store:
  base_path: {tmp_path / "payloads"}
"""
    )
    return settings


def test_failed_sink_write_is_not_reported_as_succeeded_rows(tmp_path: Path) -> None:
    result = CliRunner().invoke(app, ["run", "--settings", str(_settings(tmp_path, target_exists=True)), "--execute"])

    assert result.exit_code != 0
    assert (tmp_path / "out.csv").read_text() == "existing\n"
    summary = [line for line in result.output.splitlines() if "Run FAILED" in line]
    assert len(summary) == 1, result.output
    # Whatever the wording, no tick-marked count may claim a row that was never written.
    ticked = [int(count.replace(",", "")) for count in re.findall(r"✓([\d,]+)", summary[0])]
    assert ticked, summary[0]
    assert ticked == [0] * len(ticked), summary[0]


def test_written_rows_are_reported_as_succeeded(tmp_path: Path) -> None:
    """Control: the same run with a free target writes and reports all three rows."""
    result = CliRunner().invoke(app, ["run", "--settings", str(_settings(tmp_path, target_exists=False)), "--execute"])

    assert result.exit_code == 0, result.output
    assert (tmp_path / "out.csv").read_text().splitlines()[1:] == ["1,a", "2,b", "3,c"]
    summary = [line for line in result.output.splitlines() if "Run COMPLETED" in line]
    assert len(summary) == 1, result.output
    assert "✓3 " in summary[0], summary[0]
