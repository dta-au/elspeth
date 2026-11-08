"""Source-location reuse must preserve inspect's manifest input exactly."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import linecache
import sys
from pathlib import Path

import pytest

from elspeth.contracts.runtime_val_manifest import _class_source, _ClassSourceIndex, _ManifestBuildCache

_SOURCE = '''\
def decorate(cls):
    return cls

@decorate
class Outer:
    """Class documentation."""
    class Inner:
        value = 1

def factory():
    class Local:
        value = 2
    return Local

async def async_factory():
    class AsyncLocal:
        value = 3
    return AsyncLocal

class Repeated:
    value = "first"

class Repeated:
    value = "second"

class Inherited(Outer):
    pass
'''


def _module(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    source = tmp_path / "manifest_source_control.py"
    source.write_text(_SOURCE)
    name = "manifest_source_lookup_control"
    spec = importlib.util.spec_from_file_location(name, source)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module, source


def test_source_index_matches_inspect_for_decorators_nested_and_repeated_classes(tmp_path, monkeypatch):
    import asyncio

    module, _source = _module(tmp_path, monkeypatch)
    cache = _ManifestBuildCache()
    classes = (module.Outer, module.Outer.Inner, module.factory(), asyncio.run(module.async_factory()), module.Repeated, module.Inherited)
    for cls in classes:
        assert _class_source(cls, cache) == inspect.getsource(cls)
    assert len(cache.source_indexes) == 1
    assert 'value = "first"' in _class_source(module.Repeated, cache)


def test_one_module_index_serves_multiple_classes_and_rechecks_changed_lines(tmp_path, monkeypatch):
    module, source = _module(tmp_path, monkeypatch)
    cache = _ManifestBuildCache()
    original_parse = ast.parse
    full_module_parses = []

    def parse(text, *args, **kwargs):
        if text.startswith("def decorate"):
            full_module_parses.append(text)
        return original_parse(text, *args, **kwargs)

    monkeypatch.setattr(ast, "parse", parse)
    assert "value = 1" in _class_source(module.Outer.Inner, cache)
    _class_source(module.Outer, cache)
    assert len(full_module_parses) == 1
    source.write_text(_SOURCE.replace("value = 1", "value = 12345"))
    linecache.checkcache(str(source))
    assert "value = 12345" in _class_source(module.Outer.Inner, cache)
    assert len(full_module_parses) == 2
    assert _class_source(module.Outer.Inner, _ManifestBuildCache()) == inspect.getsource(module.Outer.Inner)


def test_missing_class_and_builtin_source_still_refuse(tmp_path, monkeypatch):
    module, _source = _module(tmp_path, monkeypatch)
    with pytest.raises((TypeError, OSError)):
        _class_source(int, _ManifestBuildCache())
    original_name = module.Outer.__qualname__
    try:
        module.Outer.__qualname__ = "Missing"
        with pytest.raises(OSError, match="could not find class definition"):
            _class_source(module.Outer, _ManifestBuildCache())
    finally:
        module.Outer.__qualname__ = original_name


def test_wrapped_class_and_deleted_source_follow_inspect(tmp_path, monkeypatch):
    module, source = _module(tmp_path, monkeypatch)
    monkeypatch.setattr(module.Outer, "__wrapped__", module.factory(), raising=False)
    cache = _ManifestBuildCache()
    assert _class_source(module.Outer, cache) == inspect.getsource(module.Outer)
    source.unlink()
    with pytest.raises(OSError):
        _class_source(module.Outer, cache)


def test_source_index_records_async_and_first_definition_names():
    index = _ClassSourceIndex()
    index.visit(ast.parse(_SOURCE))
    assert "async_factory.<locals>.AsyncLocal" in index.starts
    assert "Outer.Inner" in index.starts
    first_line = _SOURCE.splitlines()[index.starts["Repeated"] + 1]
    assert first_line.strip() == 'value = "first"'
