"""CLI admission rejects damaged archives before any secret effects."""

from typing import Never

import pytest
import yaml
from sqlalchemy import update
from typer.testing import CliRunner

from elspeth.cli import _admit_raw_cli_nonlive_run, _load_runtime_settings_with_secrets, app, bootstrap_and_run
from elspeth.contracts.enums import Determinism, NodeType, RunStatus
from elspeth.contracts.errors import AuditIntegrityError
from elspeth.contracts.hashing import CANONICAL_VERSION
from elspeth.contracts.schema import SchemaConfig
from elspeth.core.canonical import canonical_json, stable_hash
from elspeth.core.config import ElspethSettings, resolve_config
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.landscape.factory import RecorderFactory
from elspeth.core.landscape.schema import nodes_table, run_sources_table, runs_table
from elspeth.plugins.infrastructure.power_automate_nonlive import PowerAutomateArchive
from tests.fixtures.landscape import leader_coordination_token


class _ForbiddenExternalCall:
    """Record and refuse any external-effect entry point reached by admission."""

    def __init__(self, message: str) -> None:
        self.message = message
        self.calls: list[tuple[tuple[object, ...], dict[str, object]]] = []

    def __call__(self, *args: object, **kwargs: object) -> Never:
        self.calls.append((args, kwargs))
        raise AssertionError(self.message)


@pytest.fixture
def archived_pipeline(tmp_path, monkeypatch):
    from elspeth.config_loading import load_settings_from_config_dict
    from elspeth.plugins.sources.power_automate import PowerAutomateSource

    monkeypatch.setenv("ELSPETH_FINGERPRINT_KEY", "test-key-for-power-automate")
    monkeypatch.setenv("TEST_FLOW", "https://example.com/trigger?sig=test")
    raw = {
        "sources": {
            "input": {
                "plugin": "power_automate",
                "on_success": "output",
                "options": {
                    "auth": {"method": "sas_url", "trigger_url_secret": "${TEST_FLOW}"},
                    "allowed_origin": "https://example.com",
                    "on_validation_failure": "discard",
                    "schema": {"mode": "observed"},
                },
            }
        },
        "sinks": {
            "output": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(tmp_path / "output.json"),
                    "schema": {"mode": "observed"},
                },
            }
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
    }
    settings = load_settings_from_config_dict(raw, expand_env_vars=True)
    source = PowerAutomateSource(settings.sources["input"].options)
    safe = resolve_config(settings)
    node_options = {**source.config, "source_name": "input"}
    node_id = f"source_input_{stable_hash(node_options)[:12]}"
    db = LandscapeDB.from_url(settings.landscape.url)
    factory = RecorderFactory(db)
    factory.run_lifecycle.begin_run(config=safe, canonical_version=CANONICAL_VERSION, run_id="old-run")
    token = leader_coordination_token(factory, "old-run")
    factory.data_flow.register_node(
        "power_automate",
        NodeType.SOURCE,
        source.plugin_version,
        node_options,
        coordination_token=token,
        node_id=node_id,
        source_file_hash=source.source_file_hash,
        determinism=Determinism.EXTERNAL_CALL,
        schema_config=SchemaConfig.from_dict({"mode": "observed"}),
    )
    factory.run_lifecycle.record_run_source(
        source_node_id=node_id,
        source_name="input",
        plugin_name="power_automate",
        config_hash=stable_hash(source.config),
        lifecycle_state="loaded",
        coordination_token=token,
    )
    factory.run_lifecycle.complete_run(RunStatus.COMPLETED, coordination_token=token)
    db.close()
    raw.update({"run_mode": "replay", "replay_from": "old-run"})
    settings_path = tmp_path / "settings.yaml"
    settings_path.write_text(yaml.safe_dump(raw))
    monkeypatch.delenv("TEST_FLOW")
    monkeypatch.delenv("ELSPETH_FINGERPRINT_KEY")
    return settings_path, settings.landscape.url


def test_cli_loading_never_resolves_unused_source_secret(archived_pipeline, monkeypatch):
    import elspeth.cli as cli

    path, _ = archived_pipeline
    trap = _ForbiddenExternalCall("secret loader reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    _, _, archive = _admit_raw_cli_nonlive_run(path)
    assert type(archive) is PowerAutomateArchive
    loaded = _load_runtime_settings_with_secrets(path, source_settings=archive)
    assert loaded.secret_resolutions == ()
    assert loaded.power_automate_nonlive.source_credentials == {}
    assert loaded.settings.sources["input"].options == dict(archive.sources["input"].safe_options)
    assert trap.calls == []


@pytest.mark.parametrize("field", ["run_mode", "replay_from", "concurrency.max_workers", "checkpoint"])
def test_owned_archive_fields_cannot_be_deleted_even_with_recomputed_hash(archived_pipeline, monkeypatch, field):
    import elspeth.cli as cli

    path, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        source = RecorderFactory.read_only(db).run_lifecycle.get_run("old-run")
        raw = yaml.safe_load(source.settings_json)
        if field == "concurrency.max_workers":
            del raw["concurrency"]["max_workers"]
        else:
            del raw[field]
        with db.engine.begin() as connection:
            connection.execute(update(runs_table).values(settings_json=canonical_json(raw), config_hash=stable_hash(raw)))
    finally:
        db.close()
    trap = _ForbiddenExternalCall("secret loader reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    with pytest.raises(AuditIntegrityError, match="power_automate_archive_settings_invalid"):
        _, _, archive = _admit_raw_cli_nonlive_run(path)
        _load_runtime_settings_with_secrets(path, source_settings=archive)
    assert trap.calls == []


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("run_mode", None),
        ("run_mode", "not-a-mode"),
        ("run_mode", False),
        ("run_mode", {"unexpected": 42}),
        ("run_mode", "verify"),
        ("replay_from", []),
        ("replay_from", False),
    ],
)
def test_masked_archive_values_refused_even_with_recomputed_hash(archived_pipeline, monkeypatch, field, value):
    import elspeth.cli as cli

    path, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        source = RecorderFactory.read_only(db).run_lifecycle.get_run("old-run")
        raw = yaml.safe_load(source.settings_json)
        raw[field] = value
        if field == "run_mode" and value == "verify":
            raw["replay_from"] = "original-source-reference"
        with db.engine.begin() as connection:
            connection.execute(update(runs_table).values(settings_json=canonical_json(raw), config_hash=stable_hash(raw)))
    finally:
        db.close()
    trap = _ForbiddenExternalCall("secret loader reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    with pytest.raises(AuditIntegrityError, match="power_automate_archive_settings_invalid"):
        _, _, archive = _admit_raw_cli_nonlive_run(path)
        _load_runtime_settings_with_secrets(path, source_settings=archive)
    assert trap.calls == []


def test_live_archive_ignored_string_replay_reference_preserves_producer_behavior(archived_pipeline):
    path, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        source = RecorderFactory.read_only(db).run_lifecycle.get_run("old-run")
        authored = yaml.safe_load(source.settings_json)
        authored["replay_from"] = "unused-source-reference"
        settings = ElspethSettings.model_validate(authored)
        raw = resolve_config(settings)
        with db.engine.begin() as connection:
            connection.execute(update(runs_table).values(settings_json=canonical_json(raw), config_hash=stable_hash(raw)))
    finally:
        db.close()
    _, _, archive = _admit_raw_cli_nonlive_run(path)
    assert _load_runtime_settings_with_secrets(path, source_settings=archive).settings.replay_from == "old-run"


@pytest.mark.parametrize("status", [RunStatus.COMPLETED, RunStatus.COMPLETED_WITH_FAILURES, RunStatus.EMPTY])
def test_archived_admission_accepts_same_complete_statuses_as_generic_replay(archived_pipeline, status):
    from elspeth.plugins.infrastructure.power_automate_nonlive import admit_power_automate_archive

    _, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        with db.engine.begin() as connection:
            connection.execute(update(runs_table).values(status=status.value))
        archive = admit_power_automate_archive(RecorderFactory.read_only(db), "old-run")
        assert set(archive.sources) == {"input"}
        assert archive.source_run_id == "old-run"
    finally:
        db.close()


@pytest.mark.parametrize("damage", ["running", "failed", "interrupted", "missing_timestamp"])
def test_archived_admission_refuses_unfinished_runs(archived_pipeline, damage):
    from elspeth.plugins.infrastructure.power_automate_nonlive import admit_power_automate_archive

    _, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        with db.engine.begin() as connection:
            if damage == "missing_timestamp":
                connection.execute(update(runs_table).values(completed_at=None))
            elif damage == "running":
                connection.execute(update(runs_table).values(status=damage, completed_at=None))
            else:
                connection.execute(update(runs_table).values(status=damage))
        expected = "completed_at" if damage == "missing_timestamp" else "archive_not_completed"
        with pytest.raises(AuditIntegrityError, match=expected):
            admit_power_automate_archive(RecorderFactory.read_only(db), "old-run")
    finally:
        db.close()


@pytest.mark.parametrize(
    "damage",
    ["settings_hash", "canonical_version", "node_hash", "source_file_hash", "plugin_version", "source_config_hash", "source_lifecycle"],
)
@pytest.mark.parametrize("command", ["run", "validate", "bootstrap"])
def test_archive_corruption_precedes_secret_loading(archived_pipeline, monkeypatch, damage, command):
    import elspeth.cli as cli

    path, url = archived_pipeline
    db = LandscapeDB.from_url(url)
    try:
        with db.engine.begin() as connection:
            if damage == "settings_hash":
                connection.execute(update(runs_table).values(config_hash="b" * 64))
            elif damage == "canonical_version":
                connection.execute(update(runs_table).values(canonical_version="unsupported"))
            elif damage == "node_hash":
                connection.execute(update(nodes_table).values(config_hash="b" * 64))
            elif damage == "source_file_hash":
                connection.execute(update(nodes_table).values(source_file_hash="sha256:" + "b" * 16))
            elif damage == "source_config_hash":
                connection.execute(update(run_sources_table).values(config_hash="b" * 64))
            elif damage == "source_lifecycle":
                connection.execute(update(run_sources_table).values(lifecycle_state="ready"))
            else:
                connection.execute(update(nodes_table).values(plugin_version="unknown"))
    finally:
        db.close()
    trap = _ForbiddenExternalCall("secret loader reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    if command == "bootstrap":
        with pytest.raises(AuditIntegrityError, match="power_automate_archive"):
            bootstrap_and_run(path)
        assert trap.calls == []
        return
    args = [command, "--settings", str(path)]
    if command == "run":
        args.append("--execute")
    result = CliRunner().invoke(app, args)
    assert result.exit_code == 1, result.output
    assert isinstance(result.exception, AuditIntegrityError)
    assert str(result.exception).startswith("power_automate_archive")
    assert trap.calls == []


def test_outer_execution_drift_precedes_secrets(archived_pipeline, monkeypatch):
    import elspeth.cli as cli

    path, _ = archived_pipeline
    raw = yaml.safe_load(path.read_text())
    raw["checkpoint"] = {"enabled": False}
    path.write_text(yaml.safe_dump(raw))
    trap = _ForbiddenExternalCall("secret loader reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    _, _, archive = _admit_raw_cli_nonlive_run(path)
    with pytest.raises(ValueError, match="execution_settings_differ"):
        _load_runtime_settings_with_secrets(path, source_settings=archive)
    assert trap.calls == []


@pytest.mark.parametrize("mode", ["replay", "verify"])
def test_real_cli_validate_has_no_source_or_sink_credential_effects(archived_pipeline, monkeypatch, mode):
    import socket

    import azure.identity

    import elspeth.cli as cli
    import elspeth.plugins.infrastructure.clients.power_automate as client

    path, _ = archived_pipeline
    raw = yaml.safe_load(path.read_text())
    raw["run_mode"] = mode
    path.write_text(yaml.safe_dump(raw))
    trap = _ForbiddenExternalCall("external startup reached")
    monkeypatch.setattr(cli, "load_secrets_from_config", trap)
    monkeypatch.setattr(socket, "getaddrinfo", trap)
    monkeypatch.setattr(azure.identity, "ClientSecretCredential", trap)
    monkeypatch.setattr(azure.identity, "ManagedIdentityCredential", trap)
    monkeypatch.setattr(client, "PowerAutomateOperationClient", trap)
    result = CliRunner().invoke(app, ["validate", "--settings", str(path)])
    assert result.exit_code == 0, result.output
    assert trap.calls == []
