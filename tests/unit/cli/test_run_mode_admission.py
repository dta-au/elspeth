"""The CLI must settle replay authority before external startup work."""

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from typer.testing import CliRunner

from elspeth.cli import app, bootstrap_and_run
from elspeth.contracts.enums import RunMode, RunStatus
from elspeth.contracts.errors import OrchestrationInvariantError
from elspeth.core.config import ElspethSettings
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.plugins.infrastructure.manager import get_shared_plugin_manager
from elspeth.plugins.infrastructure.run_mode_capabilities import (
    admit_nonlive_plugin_classes,
    build_nonlive_plugin_manager,
    precheck_nonlive_plugin_names,
    precheck_nonlive_plugin_names_from_raw,
)
from tests.fixtures.landscape import leader_coordination_token


def _settings_path(tmp_path: Path, *, mode: str, source_plugin: str = "csv", keyvault: bool = False) -> Path:
    (tmp_path / "input.csv").write_text("id\n1\n")
    landscape_path = tmp_path / "landscape.db"
    db = LandscapeDB.from_url(f"sqlite:///{landscape_path}")
    db.close()
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(
        yaml.safe_dump(
            {
                "run_mode": mode,
                "replay_from": "run-does-not-exist",
                "sources": {
                    "primary": {
                        "plugin": source_plugin,
                        "on_success": "output",
                        "options": {
                            "path": str(tmp_path / "input.csv"),
                            "on_validation_failure": "discard",
                            "schema": {"mode": "observed"},
                        },
                    }
                },
                "sinks": {
                    "output": {
                        "plugin": "json",
                        "on_write_failure": "discard",
                        "options": {"path": str(tmp_path / "output.json"), "schema": {"mode": "observed"}},
                    }
                },
                "landscape": {"url": f"sqlite:///{landscape_path}"},
                "concurrency": {"max_workers": 1},
                "payload_store": {"base_path": str(tmp_path / "payloads")},
                **(
                    {"secrets": {"source": "keyvault", "vault_url": "https://example.vault.azure.net", "mapping": {"TOKEN": "secret"}}}
                    if keyvault
                    else {}
                ),
            }
        )
    )
    return settings_path


@pytest.mark.parametrize("mode", ["replay", "verify"])
@pytest.mark.parametrize("run_flag", ["--execute", "--dry-run"])
def test_missing_source_run_refused_before_secrets_loader_plugin_import_or_constructor(tmp_path: Path, mode: str, run_flag: str) -> None:
    settings_path = _settings_path(tmp_path, mode=mode)
    with (
        patch("elspeth.cli.load_secrets_from_config", side_effect=AssertionError("Key Vault contacted")) as secrets,
        patch("elspeth.cli.load_settings", side_effect=AssertionError("plugin imports during settings load")) as loader,
        patch("elspeth.cli._preflight_raw_settings_sink_effects", side_effect=AssertionError("raw preflight")) as raw_preflight,
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=AssertionError("constructor")) as construct,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), run_flag])

    assert result.exit_code == 1, result.output
    assert "does not exist" in result.output
    secrets.assert_not_called()
    loader.assert_not_called()
    raw_preflight.assert_not_called()
    construct.assert_not_called()
    assert not (tmp_path / "output.json").exists()
    assert not (tmp_path / "payloads").exists()


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_keyvault_refused_before_any_secret_or_plugin_work(tmp_path: Path, mode: str) -> None:
    settings_path = _settings_path(tmp_path, mode=mode, keyvault=True)
    with (
        patch("elspeth.cli.load_secrets_from_config", side_effect=AssertionError("Key Vault contacted")) as secrets,
        patch("elspeth.cli.load_settings", side_effect=AssertionError("plugin import")) as loader,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])

    assert result.exit_code == 1, result.output
    assert "cannot fetch Key Vault" in result.output
    secrets.assert_not_called()
    loader.assert_not_called()


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_valid_source_keyvault_refused_before_remote_secret_or_constructor(tmp_path: Path, mode: str) -> None:
    settings_path = _settings_path(tmp_path, mode=mode, keyvault=True)
    raw = yaml.safe_load(settings_path.read_text())
    raw["replay_from"] = "source-run"
    settings_path.write_text(yaml.safe_dump(raw))
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        lifecycle = RecorderFactory(db).run_lifecycle
        lifecycle.begin_run(config={}, canonical_version="v1", run_id="source-run")
        lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_coordination_token(RecorderFactory(db), "source-run"))
    finally:
        db.close()
    with (
        patch("elspeth.cli.load_secrets_from_config", side_effect=AssertionError("Key Vault contacted")) as secrets,
        patch("elspeth.cli.load_settings", side_effect=AssertionError("settings loaded")) as loader,
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=AssertionError("constructor")) as construct,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])
    assert result.exit_code == 1, result.output
    assert "cannot fetch Key Vault" in result.output
    secrets.assert_not_called()
    loader.assert_not_called()
    construct.assert_not_called()


@pytest.mark.parametrize("mode", ["replay", "verify"])
@pytest.mark.parametrize("run_flag", ["--execute", "--dry-run"])
def test_cli_file_backed_settings_use_admitted_source_content(tmp_path: Path, mode: str, run_flag: str) -> None:
    settings_path = _settings_path(tmp_path, mode=mode)
    raw = yaml.safe_load(settings_path.read_text())
    raw["replay_from"] = "source-run"
    raw["transforms"] = [
        {
            "name": "join",
            "plugin": "reference_join",
            "input": "primary",
            "on_success": "output",
            "on_error": "discard",
            "options": {
                "reference_file": "reference.csv",
                "reference_format": "csv",
                "key_field": "id",
                "reference_key_name": "id",
                "output": {"description": "ref['description']"},
                "schema": {"mode": "observed"},
            },
        }
    ]
    settings_path.write_text(yaml.safe_dump(raw))
    source_settings = {
        "transforms": [
            {
                "name": "join",
                "plugin": "reference_join",
                "options": {"reference_source": "reference.csv", "reference_content": "id,description\n1,archived\n"},
            }
        ]
    }
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        lifecycle = RecorderFactory(db).run_lifecycle
        lifecycle.begin_run(config=source_settings, canonical_version="v1", run_id="source-run")
        lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=leader_coordination_token(RecorderFactory(db), "source-run"))
    finally:
        db.close()
    reference_path = tmp_path / "reference.csv"
    if mode == "verify":
        reference_path.write_text("id,description\n1,changed\n")
    with patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=RuntimeError("AFTER_FILE_ADMISSION")) as construct:
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), run_flag])
    assert result.exit_code == 1, result.output
    if mode == "replay":
        assert "AFTER_FILE_ADMISSION" in result.output
        construct.assert_called_once()
        assert not reference_path.exists()
    else:
        assert "differs from source-run" in result.output
        construct.assert_not_called()


def test_unsupported_plugin_name_refused_before_registry_import(tmp_path: Path) -> None:
    settings_path = _settings_path(tmp_path, mode="replay", source_plugin="malicious_source")
    with patch("elspeth.cli.load_settings", side_effect=AssertionError("plugin import")) as loader:
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])
    assert result.exit_code == 1, result.output
    assert "malicious_source" in result.output
    loader.assert_not_called()


def test_programmatic_bootstrap_uses_same_early_admission(tmp_path: Path) -> None:
    settings_path = _settings_path(tmp_path, mode="replay")
    with (
        patch("elspeth.cli.load_settings", side_effect=AssertionError("plugin import")) as loader,
        pytest.raises(OrchestrationInvariantError, match="does not exist"),
    ):
        bootstrap_and_run(settings_path)
    loader.assert_not_called()


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_supported_mode_reaches_constructor_only_after_cli_admission(tmp_path: Path, mode: str) -> None:
    settings_path = _settings_path(tmp_path, mode=mode)
    raw = yaml.safe_load(settings_path.read_text())
    raw["replay_from"] = "source-run"
    settings_path.write_text(yaml.safe_dump(raw))
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="source-run")
        factory.run_lifecycle.complete_run(
            RunStatus.COMPLETED,
            coordination_token=leader_coordination_token(factory, "source-run"),
        )
    finally:
        db.close()
    external_dir = tmp_path / "untrusted_plugins"
    external_dir.mkdir()
    imported_marker = tmp_path / "plugin_imported"
    (external_dir / "malicious.py").write_text("from pathlib import Path\n" + f"Path({str(imported_marker)!r}).write_text('imported')\n")
    with (
        patch("elspeth.plugins.infrastructure.manager._shared_instance", None),
        patch(
            "elspeth.plugins.infrastructure.discovery.PLUGIN_SCAN_CONFIG",
            {"sources": [str(external_dir)], "transforms": [], "sinks": []},
        ),
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=RuntimeError("AFTER_ADMISSION")) as construct,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])
        assert not imported_marker.exists()
        # Positive instrument control: ordinary global discovery still fires
        # after the CLI's invocation-scoped registry has been released.
        get_shared_plugin_manager()
        assert imported_marker.read_text() == "imported"
    assert result.exit_code == 1, result.output
    assert "AFTER_ADMISSION" in result.output
    construct.assert_called_once()
    assert not (tmp_path / "output.json").exists()


def test_programmatic_bootstrap_uses_filtered_registry_with_valid_source(tmp_path: Path) -> None:
    settings_path = _settings_path(tmp_path, mode="replay")
    raw = yaml.safe_load(settings_path.read_text())
    raw["replay_from"] = "source-run"
    settings_path.write_text(yaml.safe_dump(raw))
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        factory = RecorderFactory(db)
        factory.run_lifecycle.begin_run(config={}, canonical_version="v1", run_id="source-run")
        factory.run_lifecycle.complete_run(
            RunStatus.COMPLETED,
            coordination_token=leader_coordination_token(factory, "source-run"),
        )
    finally:
        db.close()
    with (
        patch("elspeth.plugins.infrastructure.manager._shared_instance", None),
        patch(
            "elspeth.plugins.infrastructure.discovery.discover_all_plugins",
            side_effect=AssertionError("MALICIOUS_PLUGIN_IMPORT"),
        ) as untrusted_scan,
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=RuntimeError("AFTER_ADMISSION")) as construct,
    ):
        with pytest.raises(RuntimeError, match="AFTER_ADMISSION"):
            bootstrap_and_run(settings_path)
        untrusted_scan.assert_not_called()
        with pytest.raises(AssertionError, match="MALICIOUS_PLUGIN_IMPORT"):
            get_shared_plugin_manager()
    construct.assert_called_once()


def test_resume_refuses_persisted_replay_run_before_secrets_or_plugins(tmp_path: Path) -> None:
    settings_path = _settings_path(tmp_path, mode="live")
    db = LandscapeDB.from_url(f"sqlite:///{tmp_path / 'landscape.db'}")
    try:
        lifecycle = RecorderFactory(db).run_lifecycle
        lifecycle.begin_run(config={}, canonical_version="v1", run_id="original-run")
        lifecycle.begin_run(
            config={},
            canonical_version="v1",
            run_id="replay-attempt",
            run_mode=RunMode.REPLAY,
            replay_from_run_id="original-run",
        )
    finally:
        db.close()
    with (
        patch("elspeth.cli.load_secrets_from_config", side_effect=AssertionError("Key Vault contacted")) as secrets,
        patch("elspeth.cli.load_settings", side_effect=AssertionError("plugin import")) as loader,
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=AssertionError("constructor")) as construct,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "resume", "replay-attempt", "--settings", str(settings_path), "--execute"])
    assert result.exit_code == 1, result.output
    assert "Cannot resume replay run" in result.output
    secrets.assert_not_called()
    loader.assert_not_called()
    construct.assert_not_called()


def test_live_mode_remains_outside_nonlive_capability_gate(tmp_path: Path) -> None:
    settings_path = _settings_path(tmp_path, mode="live", source_plugin="malicious_source")
    # Live keeps its existing loader/registry behavior. The raw admission
    # must not reject its plugin vocabulary as a replay capability verdict.
    from elspeth.cli import _admit_raw_cli_nonlive_run

    _admit_raw_cli_nonlive_run(settings_path)


def test_capability_inventory_positive_and_negative_controls(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = ElspethSettings(
        sources={"primary": {"plugin": "csv", "on_success": "output"}},
        transforms=[{"name": "pass", "plugin": "passthrough", "input": "primary", "on_success": "output", "on_error": "discard"}],
        sinks={"output": {"plugin": "json", "on_write_failure": "discard"}},
        run_mode=RunMode.REPLAY,
        replay_from="source-run",
    )
    precheck_nonlive_plugin_names(settings)
    admit_nonlive_plugin_classes(settings)
    precheck_nonlive_plugin_names_from_raw(
        {"sources": {"primary": {"plugin": "csv"}}, "transforms": [{"plugin": "passthrough"}], "sinks": {"output": {"plugin": "json"}}}
    )
    with pytest.raises(OrchestrationInvariantError, match="unsupported_plugin"):
        precheck_nonlive_plugin_names_from_raw({"sources": {"primary": {"plugin": "unsupported_plugin"}}})

    manager = get_shared_plugin_manager()

    class Impostor:
        name = "csv"

    monkeypatch.setattr(manager, "get_source_by_name", lambda _name: Impostor)
    with pytest.raises(OrchestrationInvariantError, match="not the reviewed built-in class"):
        admit_nonlive_plugin_classes(settings)


def test_file_and_http_transform_capabilities_resolve_exact_builtin_classes() -> None:
    from elspeth.plugins.transforms.reference_join import ReferenceJoin
    from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

    requested = precheck_nonlive_plugin_names_from_raw({"transforms": [{"plugin": "reference_join"}, {"plugin": "web_scrape"}]})
    manager = build_nonlive_plugin_manager(requested)
    assert manager.get_transform_by_name("reference_join") is ReferenceJoin
    assert manager.get_transform_by_name("web_scrape") is WebScrapeTransform


def test_nonlive_transform_capability_inventory_matches_live_builtin_registry() -> None:
    live_names = {plugin.name for plugin in get_shared_plugin_manager().get_transforms()}
    assert "web_scrape" in live_names
    assert "llm" in live_names
    assert "not_a_plugin" not in live_names
    nonlive = build_nonlive_plugin_manager(live_names)
    assert {plugin.name for plugin in nonlive.get_transforms()} == live_names


@pytest.mark.parametrize("provider", ["langfuse", "azure_ai"])
@pytest.mark.parametrize("section", ["sources", "transforms"])
def test_nonlive_llm_tracing_refused_before_settings_loader_or_constructor(tmp_path: Path, provider: str, section: str) -> None:
    settings_path = _settings_path(tmp_path, mode="verify")
    raw = yaml.safe_load(settings_path.read_text())
    llm = {"plugin": "llm", "options": {"tracing": {"provider": provider}}}
    if section == "sources":
        raw["sources"]["primary"] = llm
    else:
        raw["transforms"] = [{"name": "model", **llm}]
    settings_path.write_text(yaml.safe_dump(raw))
    with (
        patch("elspeth.cli.load_settings", side_effect=AssertionError("settings loaded")) as loader,
        patch("elspeth.cli._instantiate_plugins_for_runtime_preflight", side_effect=AssertionError("constructor")) as construct,
    ):
        result = CliRunner().invoke(app, ["--no-dotenv", "run", "--settings", str(settings_path), "--execute"])
    assert result.exit_code == 1, result.output
    assert "LLM tracing is unsupported" in result.output
    loader.assert_not_called()
    construct.assert_not_called()


def test_nonlive_llm_noop_tracing_remains_admissible() -> None:
    requested = precheck_nonlive_plugin_names_from_raw({"transforms": [{"plugin": "llm", "options": {"tracing": {"provider": "none"}}}]})
    assert requested == frozenset({"llm"})


def test_parsed_nonlive_llm_tracing_refused_before_constructor() -> None:
    settings = ElspethSettings(
        sources={"primary": {"plugin": "csv", "on_success": "output"}},
        transforms=[
            {
                "name": "model",
                "plugin": "llm",
                "input": "primary",
                "on_success": "output",
                "on_error": "discard",
                "options": {"tracing": {"provider": "langfuse"}},
            }
        ],
        sinks={"output": {"plugin": "json", "on_write_failure": "discard"}},
        run_mode=RunMode.VERIFY,
        replay_from="source-run",
    )
    with pytest.raises(OrchestrationInvariantError, match="LLM tracing is unsupported"):
        precheck_nonlive_plugin_names(settings)
