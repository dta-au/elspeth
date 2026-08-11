"""Contracts for the reviewed first-party state-engine plugin matrix."""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
SCRIPT = REPOSITORY_ROOT / "scripts/state_engine_plugin_matrix.py"
GOLDEN = REPOSITORY_ROOT / "tests/golden/state_engine/plugin_lifecycle_matrix.json"


def _run(*arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        cwd=REPOSITORY_ROOT,
        text=True,
        capture_output=True,
        check=False,
    )


def test_checked_golden_matches_live_mechanical_projection_without_writing() -> None:
    before = GOLDEN.read_bytes()

    result = _run("check", str(GOLDEN))

    assert result.returncode == 0, result.stderr
    assert GOLDEN.read_bytes() == before
    assert "51 plugins" in result.stdout


def test_checked_golden_has_exact_counts_hashes_and_reviewed_fields() -> None:
    matrix = json.loads(GOLDEN.read_text(encoding="utf-8"))
    plugins = matrix["plugins"]

    assert matrix["schema_version"] == 1
    assert [sum(entry["kind"] == kind for entry in plugins) for kind in ("source", "transform", "sink")] == [9, 33, 9]
    assert all(entry["source_hash_present"] is True for entry in plugins)
    assert "UNCLASSIFIED" not in GOLDEN.read_text(encoding="utf-8")
    assert all(
        set(entry)
        == {
            "plugin_key",
            "kind",
            "module",
            "qualname",
            "source_path",
            "determinism",
            "execution_model",
            "lifecycle_owners",
            "source_hash_present",
            "sink_effect_capability",
            "sink_effect_modes",
            "variants",
            "external_observation_required",
            "applicable_pb_boundaries",
            "local_fixture",
            "release_lane",
        }
        for entry in plugins
    )


def test_render_skeleton_preserves_reviewed_fields_but_refuses_unclassified_entries(tmp_path: Path) -> None:
    skeleton = tmp_path / "matrix.json"
    skeleton.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "plugins": [
                    {
                        "plugin_key": "source:aws_s3",
                        "variants": ["reviewed-variant"],
                        "external_observation_required": True,
                        "applicable_pb_boundaries": ["PB-09"],
                        "local_fixture": "reviewed-fixture",
                        "release_lane": "reviewed-lane",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    result = _run("render-skeleton", str(skeleton))
    rendered = json.loads(skeleton.read_text(encoding="utf-8"))
    aws = next(entry for entry in rendered["plugins"] if entry["plugin_key"] == "source:aws_s3")

    assert result.returncode == 1
    assert "UNCLASSIFIED" in result.stderr
    assert aws["variants"] == ["reviewed-variant"]
    assert aws["external_observation_required"] is True
    assert aws["applicable_pb_boundaries"] == ["PB-09"]
    assert aws["local_fixture"] == "reviewed-fixture"
    assert aws["release_lane"] == "reviewed-lane"
