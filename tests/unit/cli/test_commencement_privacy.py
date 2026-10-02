"""Raw failed-gate context stays private at the CLI diagnostic boundary."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts.errors import CommencementGateFailedError
from elspeth.core.dependency_config import CommencementGateConfig
from elspeth.engine.commencement import evaluate_commencement_gates


@pytest.mark.parametrize("output_format", ["console", "json"])
def test_failed_commencement_snapshot_is_not_disclosed(tmp_path: Path, output_format: str) -> None:
    """A real evaluator failure reaches the CLI without exposing raw context."""
    secret = "sentinel-private-context-91370"  # secret-scan: allow-this-line
    gate = CommencementGateConfig(name="privacy_gate", condition="dependency_runs['index']['run_id']")
    context = {"dependency_runs": {"index": {"run_id": secret}}, "collections": {}}
    with pytest.raises(CommencementGateFailedError) as exc_info:
        evaluate_commencement_gates([gate], context)
    failure = exc_info.value
    # Prove the sensitive value exists in the retained snapshot; the output
    # assertion below is the privacy obligation, not this retention check.
    assert failure.context_snapshot["dependency_runs"]["index"]["run_id"] == secret

    source_path = tmp_path / "data.csv"
    source_path.write_text("id,name\n1,alice\n")
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "primary": {
                        "plugin": "csv",
                        "on_success": "default",
                        "options": {
                            "path": str(source_path),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "observed"},
                        },
                    }
                },
                "sinks": {
                    "default": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "output.jsonl"), "schema": {"mode": "observed"}},
                    }
                },
                "landscape": {"url": f"sqlite:///{tmp_path / 'landscape.db'}"},
                "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
            }
        )
    )
    (tmp_path / "payloads").mkdir(mode=0o700)
    with patch("elspeth.engine.bootstrap.resolve_preflight", autospec=True, side_effect=failure) as resolve:
        result = CliRunner().invoke(app, ["run", "--settings", str(settings_path), "--execute", "--format", output_format])

    resolve.assert_called_once()
    assert result.exit_code == 1, result.output
    assert "privacy_gate" in result.output
    assert "not bool" in result.output
    assert secret not in result.output
    assert not (tmp_path / "landscape.db").exists()
