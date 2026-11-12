"""Regression tests for the 20261111-plugins-review P3 infrastructure findings.

Each test names the sweep finding it pins. Every test failed at an assertion
on the lane base and passes after that finding's fix commit.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from elspeth.contracts.enums import Determinism
from elspeth.plugins.infrastructure import discovery
from elspeth.plugins.infrastructure.base import BaseSink, BaseTransform
from elspeth.plugins.infrastructure.hookspecs import hookimpl
from elspeth.plugins.infrastructure.manager import PluginManager
from elspeth.plugins.infrastructure.validation import UnknownPluginTypeError
from elspeth.plugins.transforms.passthrough import PassThrough

# ---------------------------------------------------------------------------
# Finding #126: PluginManager.create_* must validate against its OWN registry
# ---------------------------------------------------------------------------


class _PrivatePassThrough(PassThrough):
    """A transform registered only on a private PluginManager, never on the shared one."""

    name = "lane12_private_passthrough"
    determinism = Determinism.DETERMINISTIC


class _PrivateTransformHooks:
    @hookimpl
    def elspeth_get_transforms(self) -> list[type[_PrivatePassThrough]]:
        return [_PrivatePassThrough]


def test_create_transform_on_private_manager_validates_against_its_own_registry() -> None:
    """Finding #126: a plugin registered only on this manager must be creatable.

    create_transform used to validate by resolving the class through the
    SHARED manager, so a plugin the instance itself resolves was refused as
    an unknown type before the instance's own lookup ever ran.
    """
    manager = PluginManager()
    manager.register(_PrivateTransformHooks())
    assert manager.get_transform_by_name("lane12_private_passthrough") is _PrivatePassThrough

    try:
        created = manager.create_transform("lane12_private_passthrough", {"schema": {"mode": "observed"}})
    except UnknownPluginTypeError as exc:
        raise AssertionError(f"create_transform consulted a registry other than its own: {exc}") from exc

    assert type(created) is _PrivatePassThrough


def test_create_transform_on_private_manager_still_refuses_invalid_config() -> None:
    """Finding #126: validation still runs (against the private class's model)."""
    manager = PluginManager()
    manager.register(_PrivateTransformHooks())

    try:
        manager.create_transform("lane12_private_passthrough", {"schema": {"mode": "observed"}, "not_a_knob": 1})
    except UnknownPluginTypeError as exc:
        raise AssertionError(f"create_transform consulted a registry other than its own: {exc}") from exc
    except ValueError as exc:
        assert "not_a_knob" in str(exc)
    else:
        raise AssertionError("an unknown config key must be refused")


# ---------------------------------------------------------------------------
# Finding #129: a missing CORE dependency must crash discovery, not skip
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("core_module", ["chromadb", "bs4", "html2text"])
def test_missing_core_plugin_dependency_crashes_discovery(core_module: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #129: chromadb/bs4/html2text are core [project] dependencies.

    A plugin file whose import of one of them fails is a broken install; the
    discovery loop used to log a warning and silently drop the plugin from the
    catalog (chroma_sink, web_scrape) because the module was on the optional
    allowlist.
    """
    (tmp_path / "core_dep_plugin.py").write_text("raise AssertionError('patched discovery must not execute this file')\n")

    def missing_core_module(_py_file: Path, _base_class: type) -> list[type]:
        raise ModuleNotFoundError(f"No module named '{core_module}'", name=core_module)

    monkeypatch.setattr(discovery, "_discover_in_file", missing_core_module)

    try:
        skipped = discovery.discover_plugins_in_directory(tmp_path, BaseSink)
    except ModuleNotFoundError as exc:
        assert exc.name == core_module
    else:
        raise AssertionError(f"missing core dependency {core_module!r} was skipped as optional; discovery returned {skipped!r}")


def test_optional_plugin_import_allowlist_holds_only_optional_extras() -> None:
    """Finding #129: only jinja2 ships in an optional extra ([llm]/[azure]/[aws])."""
    allowlist = discovery.OPTIONAL_PLUGIN_IMPORT_MODULES
    assert allowlist == frozenset({"jinja2"})


def test_missing_jinja2_still_skips_as_optional_extra(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #129 control: the genuine optional extra keeps its skip path."""
    (tmp_path / "llm_plugin.py").write_text("raise AssertionError('patched discovery must not execute this file')\n")

    def missing_jinja2(_py_file: Path, _base_class: type) -> list[type]:
        raise ModuleNotFoundError("No module named 'jinja2'", name="jinja2")

    monkeypatch.setattr(discovery, "_discover_in_file", missing_jinja2)

    assert discovery.discover_plugins_in_directory(tmp_path, BaseTransform) == []


# ---------------------------------------------------------------------------
# Finding #149: the readiness probe must not create a store at a missing path
# ---------------------------------------------------------------------------


class _SpyChromaClientFactory:
    """Stands in for chromadb.PersistentClient and records every construction."""

    def __init__(self) -> None:
        self.paths: list[str] = []

    def __call__(self, *, path: str) -> object:
        import chromadb

        self.paths.append(path)

        class _MissingCollectionClient:
            def get_collection(self, name: str) -> object:
                raise chromadb.errors.NotFoundError(f"Collection {name} does not exist.")

            def close(self) -> None:
                return None

        return _MissingCollectionClient()


@pytest.mark.parametrize("make_target", ["missing", "regular_file"])
def test_chroma_probe_refuses_missing_persist_directory_without_creating_it(
    make_target: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Finding #149: a mistyped persist_directory is 'store missing', not reachable.

    chromadb.PersistentClient creates the directory and a fresh chroma.sqlite3,
    so the read-only readiness check used to mutate the filesystem and report
    the typo as reachable=True with "Collection ... not found".
    """
    import chromadb

    from elspeth.plugins.infrastructure.probe_factory import ChromaCollectionProbe

    target = tmp_path / "chroma_typo"
    if make_target == "regular_file":
        target.write_text("not a directory\n")
    spy = _SpyChromaClientFactory()
    monkeypatch.setattr(chromadb, "PersistentClient", spy)

    probe = ChromaCollectionProbe("abcde", {"mode": "persistent", "persist_directory": str(target)})
    result = probe.probe()

    assert result.reachable is False, result
    assert result.count is None
    assert "persist_directory" in result.message
    assert "does not exist" in result.message
    assert spy.paths == []
    if make_target == "missing":
        assert not target.exists()
    else:
        assert target.read_text() == "not a directory\n"


# ---------------------------------------------------------------------------
# Finding #104: raw sink-effect eligibility and the landscape.export section
# ---------------------------------------------------------------------------


def _raw_export_config(*, enabled: object = True, sink: str = "audit") -> dict[str, object]:
    """A valid enabled-export raw config (shape from tests/unit/cli/test_cli_preflight.py)."""
    return {
        "sinks": {
            "pipeline": {"plugin": "csv", "options": {"path": "out.csv", "schema": {"mode": "observed"}}},
            "audit": {"plugin": "json", "options": {"path": "audit.json", "schema": {"mode": "observed"}}},
        },
        "landscape": {
            "export": {
                "enabled": enabled,
                "compartment_id": "test-compartment",
                "sink": sink,
                "format": "json",
                "signing_mode": "unsigned",
                "total_record_limit": 1_000,
                "total_byte_limit": 1_000_000,
                "chunk_limit": 10,
                "per_chunk_record_limit": 100,
                "per_chunk_byte_limit": 100_000,
                "spool_root": ".elspeth/audit-export-spool/p3-sweep",
                "content_store": {
                    "content_store_id": "p3-sweep-store-v1",
                    "namespace": "audit/export",
                    "root": ".elspeth/audit-export-content-store/p3-sweep",
                    "policy_version": "v1",
                    "retention_days": 30,
                    "durability": "fsync",
                },
            }
        },
    }


@pytest.mark.parametrize("purpose_name", ["FRESH", "AUDIT_EXPORT"])
def test_export_sink_missing_from_sinks_is_a_value_error(purpose_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #104: a typo'd landscape.export.sink must be a ValueError, not a KeyError.

    The CLI run path reports ValueError as a configuration error; a bare
    KeyError escaped it as a raw traceback (AUDIT_EXPORT), and FRESH accepted
    the config silently.
    """
    from elspeth.engine.orchestrator.preflight import SinkEffectExecutionPurpose
    from elspeth.plugins.infrastructure.runtime_factory import validate_sink_effect_eligibility_from_raw_config

    monkeypatch.chdir(tmp_path)
    raw = _raw_export_config(sink="audti")
    try:
        validate_sink_effect_eligibility_from_raw_config(
            raw, purpose=SinkEffectExecutionPurpose[purpose_name], expand_env_placeholders=True
        )
    except KeyError as exc:
        raise AssertionError(f"missing export sink escaped as KeyError: {exc!r}") from exc
    except ValueError as exc:
        assert "audti" in str(exc)
    else:
        raise AssertionError("an export sink name absent from 'sinks' must be refused")


def test_export_sink_placeholder_is_expanded_like_the_loader(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #104: landscape.export.sink=${AUDIT_SINK} names the real export sink.

    Unexpanded, FRESH validated the export sink as an ordinary pipeline sink
    and AUDIT_EXPORT raised KeyError('${AUDIT_SINK}').
    """
    from elspeth.engine.orchestrator.preflight import SinkEffectExecutionPurpose
    from elspeth.plugins.infrastructure.runtime_factory import validate_sink_effect_eligibility_from_raw_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AUDIT_SINK", "audit")
    raw = _raw_export_config(sink="${AUDIT_SINK}")

    fresh = validate_sink_effect_eligibility_from_raw_config(raw, purpose=SinkEffectExecutionPurpose.FRESH, expand_env_placeholders=True)
    assert sorted(fresh) == ["pipeline"]
    try:
        export = validate_sink_effect_eligibility_from_raw_config(
            raw, purpose=SinkEffectExecutionPurpose.AUDIT_EXPORT, expand_env_placeholders=True
        )
    except KeyError as exc:
        raise AssertionError(f"export sink placeholder was not expanded: {exc!r}") from exc
    assert sorted(export) == ["audit"]


@pytest.mark.parametrize("purpose_name", ["FRESH", "AUDIT_EXPORT"])
def test_export_enabled_placeholder_is_expanded_like_the_loader(purpose_name: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #104: enabled=${EXPORT_ON:-true} is accepted by the loader, so the preflight must accept it."""
    from pydantic import ValidationError

    from elspeth.engine.orchestrator.preflight import SinkEffectExecutionPurpose
    from elspeth.plugins.infrastructure.runtime_factory import validate_sink_effect_eligibility_from_raw_config

    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("EXPORT_ON", raising=False)
    raw = _raw_export_config(enabled="${EXPORT_ON:-true}")
    try:
        modes = validate_sink_effect_eligibility_from_raw_config(
            raw, purpose=SinkEffectExecutionPurpose[purpose_name], expand_env_placeholders=True
        )
    except ValidationError as exc:
        raise AssertionError(f"landscape.export was validated unexpanded: {exc}") from exc
    assert sorted(modes) == (["audit"] if purpose_name == "AUDIT_EXPORT" else ["pipeline"])


def test_landscape_export_settings_reader_expands_placeholders_for_the_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #104: the CLI decides whether to run the AUDIT_EXPORT pass from this reader."""
    import inspect

    from elspeth.plugins.infrastructure.runtime_factory import validate_landscape_export_settings_from_raw_config

    parameters = inspect.signature(validate_landscape_export_settings_from_raw_config).parameters
    assert "expand_env_placeholders" in parameters
    assert "deferrable_env_vars" in parameters

    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("EXPORT_ON", "true")
    monkeypatch.setenv("AUDIT_SINK", "audit")
    settings = validate_landscape_export_settings_from_raw_config(
        _raw_export_config(enabled="${EXPORT_ON}", sink="${AUDIT_SINK}"),
        expand_env_placeholders=True,
        deferrable_env_vars=frozenset(),
    )
    assert settings.enabled is True
    assert settings.sink == "audit"


def test_export_sink_naming_a_deferred_secret_defers_the_raw_gate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Finding #104: a Key Vault-deferred export sink name cannot be resolved yet.

    Like a sink whose options reference a deferred secret, the raw gate must
    defer (the post-expansion gates are authoritative) rather than validate
    the export sink as a pipeline member or refuse a config the loader accepts.
    """
    from elspeth.engine.orchestrator.preflight import SinkEffectExecutionPurpose
    from elspeth.plugins.infrastructure.runtime_factory import validate_sink_effect_eligibility_from_raw_config

    monkeypatch.chdir(tmp_path)
    raw = _raw_export_config(sink="${AUDIT_SINK}")
    for purpose in (SinkEffectExecutionPurpose.FRESH, SinkEffectExecutionPurpose.AUDIT_EXPORT):
        try:
            modes = validate_sink_effect_eligibility_from_raw_config(
                raw,
                purpose=purpose,
                expand_env_placeholders=True,
                deferrable_env_vars=frozenset({"AUDIT_SINK"}),
            )
        except KeyError as exc:
            raise AssertionError(f"deferred export sink escaped as KeyError: {exc!r}") from exc
        assert "audit" not in modes
        assert "${AUDIT_SINK}" not in modes


# ---------------------------------------------------------------------------
# Finding #0: dict-KEYED column options (truncate.fields) reach the fixed-schema check
# ---------------------------------------------------------------------------


def test_truncate_fixed_schema_omitting_a_truncated_field_is_refused_at_construction() -> None:
    """Finding #0: truncate.fields names its columns as dict KEYS.

    The fixed-schema read check saw only str/list option values, so
    ``fields: {notes: 5}`` under a fixed schema without ``notes`` constructed,
    and every row carrying ``notes`` then failed the input model at run time.
    """
    from elspeth.plugins.infrastructure.config_base import PluginConfigError
    from elspeth.plugins.transforms.truncate import Truncate

    try:
        Truncate({"schema": {"mode": "fixed", "fields": ["id: int"]}, "fields": {"notes": 5}})
    except PluginConfigError as exc:
        assert "'notes' (named by fields)" in str(exc)
    else:
        raise AssertionError("a fixed schema forbidding a truncated field must be refused at construction")


def test_truncate_keys_are_config_named_input_columns() -> None:
    """Finding #0: demotion protection must see truncate's keyed columns too."""
    from elspeth.plugins.transforms.truncate import Truncate

    transform = Truncate({"schema": {"mode": "observed"}, "fields": {"notes": 5, "title": 10}})
    assert {"notes", "title"} <= transform._config_named_input_columns()


def test_truncate_header_spelling_key_of_a_declared_field_still_constructs() -> None:
    """Finding #0 caveat: a key whose canonical spelling IS declared is not refused.

    truncate resolves a header-spelled key through the row contract, so
    ``{'Value Text': 10}`` / ``{'Notes': 5}`` against fixed ``value_text`` /
    ``notes`` reach the declared field; only the canonical form is compared.
    """
    from elspeth.plugins.transforms.truncate import Truncate

    Truncate({"schema": {"mode": "fixed", "fields": ["id: int", "value_text: str"]}, "fields": {"Value Text": 10}})
    Truncate({"schema": {"mode": "fixed", "fields": ["id: int", "notes: str"]}, "fields": {"Notes": 5}})


def test_web_scrape_query_parameter_names_are_not_read_columns() -> None:
    """Finding #0 control: only options a plugin declares column-keyed contribute keys.

    web_scrape.query_fields maps query PARAMETER names (keys) to row fields
    (values); its keys must not be treated as input columns.
    """
    from elspeth.plugins.infrastructure.base import BaseTransform
    from elspeth.plugins.transforms.truncate import Truncate
    from elspeth.plugins.transforms.web_scrape import WebScrapeTransform

    assert "column_keyed_config_keys" in vars(BaseTransform)
    assert vars(Truncate)["column_keyed_config_keys"] == frozenset({"fields"})
    assert "column_keyed_config_keys" not in vars(WebScrapeTransform)
