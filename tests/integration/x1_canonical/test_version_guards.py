"""Canonical version values used by run and replay entry points."""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app
from elspeth.contracts import hashing
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.source_compatibility import admit_source_configuration

_PATCHED_VERSION = "sha256-rfc8785-test"


def test_cli_run_records_current_constant(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "CANONICAL_VERSION", _PATCHED_VERSION)
    input_path = tmp_path / "input.jsonl"
    input_path.write_text('{"id":1}\n')
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "sources": {
                    "src": {
                        "plugin": "json",
                        "on_success": "out",
                        "options": {
                            "path": str(input_path),
                            "format": "jsonl",
                            "schema": {"mode": "observed"},
                            "on_validation_failure": "discard",
                        },
                    }
                },
                "sinks": {
                    "out": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "out.jsonl"), "format": "jsonl", "schema": {"mode": "observed"}},
                    }
                },
                "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
                "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
            },
            sort_keys=False,
        )
    )
    result = CliRunner().invoke(app, ["run", "--settings", str(settings_path), "--execute"])
    assert result.exit_code == 0, result.output
    with sqlite3.connect(tmp_path / "audit.db") as conn:
        version = conn.execute("SELECT canonical_version FROM runs").fetchone()
    assert version == (_PATCHED_VERSION,)


def test_follower_orchestrator_uses_current_constant(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "CANONICAL_VERSION", _PATCHED_VERSION)
    db = LandscapeDB("sqlite:///:memory:")
    try:
        assert Orchestrator(db)._canonical_version == _PATCHED_VERSION
    finally:
        db.close()


def test_replay_checks_version_before_settings_hash(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "CANONICAL_VERSION", _PATCHED_VERSION)
    source_settings = {"sources": {"src": {"plugin": "json"}}}
    source = SimpleNamespace(
        settings_json=canonical_json(source_settings),
        config_hash="0" * 64,
        canonical_version="sha256-rfc8785-v1",
    )
    factory = Mock(spec=RecorderFactory)
    factory.run_lifecycle.get_run.return_value = source
    config = SimpleNamespace(config=source_settings)
    with pytest.raises(AuditIntegrityError, match="canonical version differs"):
        admit_source_configuration(factory, source_run_id="source", config=config, canonical_version=hashing.CANONICAL_VERSION)


def test_replay_accepts_matching_version(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hashing, "CANONICAL_VERSION", _PATCHED_VERSION)
    source_settings = {"sources": {"src": {"plugin": "json"}}}
    source = SimpleNamespace(
        settings_json=json.dumps(source_settings),
        config_hash=stable_hash(source_settings),
        canonical_version=_PATCHED_VERSION,
    )
    factory = Mock(spec=RecorderFactory)
    factory.run_lifecycle.get_run.return_value = source
    admit_source_configuration(
        factory,
        source_run_id="source",
        config=SimpleNamespace(config=source_settings),
        canonical_version=hashing.CANONICAL_VERSION,
    )
