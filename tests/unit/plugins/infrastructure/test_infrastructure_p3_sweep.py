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
