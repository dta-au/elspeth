"""Pin the tier_model rule's judge-free R5 exemption map to the live tree.

``TierModelVisitor._R5_NAMED_BOUNDARY_CONTEXTS`` grants an R5 exemption
without a judge verdict, so every entry must resolve to exactly one live
definition under ``src/elspeth``. A dead entry is a silent grant waiting
for a future function to reuse the name; an ambiguous one grants more
than one body. ``resolve_named_boundary_contexts`` is the single authority
for that question — the whole-tree check reports the same resolutions as
an ERROR (elspeth-0bd4fb6042).
"""

from __future__ import annotations

import argparse
import ast
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
from textwrap import dedent

from elspeth_lints.core.protocols import Severity
from elspeth_lints.rules.trust_tier.tier_model import rule as tier_rule
from elspeth_lints.rules.trust_tier.tier_model.rule import (
    NamedBoundaryContextResolution,
    TierModelVisitor,
    collect_check_result,
    resolve_named_boundary_contexts,
    run_check,
)

SOURCE_ROOT = Path(__file__).resolve().parents[3] / "src" / "elspeth"


def _table(rows: list[NamedBoundaryContextResolution]) -> str:
    width = max((len(r.file_path) for r in rows), default=0)
    lines = [f"{'file':<{width}}  {'qualified_name':<48} status                 defs"]
    lines.extend(f"{r.file_path:<{width}}  {r.qualified_name:<48} {r.status:<22} {r.definition_count}" for r in rows)
    return "\n".join(lines)


def test_every_map_entry_resolves_to_one_live_definition() -> None:
    resolutions = resolve_named_boundary_contexts(SOURCE_ROOT)
    assert resolutions, "the exemption map is empty; the pin has nothing to measure"
    stale = [r for r in resolutions if r.is_stale]
    assert not stale, "stale _R5_NAMED_BOUNDARY_CONTEXTS entries (delete them; moved functions are not successor-included):\n" + _table(
        stale
    )


def test_map_keys_are_qualified_symbol_paths() -> None:
    """A bare method name would silently match on every class; keys must carry the class."""
    for file_path, names in TierModelVisitor._R5_NAMED_BOUNDARY_CONTEXTS.items():
        tree = ast.parse((SOURCE_ROOT / file_path).read_text(encoding="utf-8"))
        module_level = {node.name for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for name in names:
            assert "." in name or name in module_level, f"{file_path}::{name} is a bare name for a nested definition"


def _visitor_findings(file_path: str, source: str) -> list[str]:
    visitor = TierModelVisitor(file_path, source.splitlines(), "f" * 64)
    visitor.visit(ast.parse(source))
    return [f"{f.rule_id}:{'.'.join(f.symbol_context)}" for f in visitor.findings]


def test_qualified_key_exempts_only_the_named_class(monkeypatch) -> None:
    source = dedent(
        """
        class Listed:
            @classmethod
            def from_dict(cls, data):
                if not isinstance(data, dict):
                    raise ValueError("bad")
                return cls()

        class Unlisted:
            @classmethod
            def from_dict(cls, data):
                if not isinstance(data, dict):
                    raise ValueError("bad")
                return cls()
        """
    )
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"Listed.from_dict"})})
    r5 = [f for f in _visitor_findings("pkg/mod.py", source) if f.startswith("R5:")]
    assert r5 == ["R5:Unlisted.from_dict"]


def test_bare_key_no_longer_matches_a_method(monkeypatch) -> None:
    """The old bare-name form is inert for nested definitions, never a wildcard."""
    source = dedent(
        """
        class Only:
            def parse(self, data):
                if not isinstance(data, dict):
                    raise ValueError("bad")
        """
    )
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"parse"})})
    assert [f for f in _visitor_findings("pkg/mod.py", source) if f.startswith("R5:")] == ["R5:Only.parse"]


def _fake_elspeth_root(tmp_path: Path) -> Path:
    root = tmp_path / "src" / "elspeth"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text(
        dedent(
            """
            def live(data):
                return data

            class A:
                def twice(self):
                    return 1

            class B:
                def twice(self):
                    return 2
            """
        ),
        encoding="utf-8",
    )
    return root


def test_resolver_classifies_missing_file_missing_definition_and_ambiguity(tmp_path: Path, monkeypatch) -> None:
    root = _fake_elspeth_root(tmp_path)
    monkeypatch.setattr(
        TierModelVisitor,
        "_R5_NAMED_BOUNDARY_CONTEXTS",
        {
            "pkg/mod.py": frozenset({"live", "gone", "twice", "A.twice"}),
            "pkg/missing.py": frozenset({"anything"}),
        },
    )
    by_key = {r.key: r.status for r in resolve_named_boundary_contexts(root)}
    assert by_key == {
        "pkg/mod.py::A.twice": "ok",
        "pkg/mod.py::gone": "missing_definition",
        "pkg/mod.py::live": "ok",
        "pkg/mod.py::twice": "missing_definition",
        "pkg/missing.py::anything": "missing_file",
    }


def test_duplicate_qualified_definition_is_ambiguous(tmp_path: Path, monkeypatch) -> None:
    root = _fake_elspeth_root(tmp_path)
    (root / "pkg" / "dup.py").write_text("def f():\n    return 1\n\ndef f():\n    return 2\n", encoding="utf-8")
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/dup.py": frozenset({"f"})})
    (resolution,) = resolve_named_boundary_contexts(root)
    assert resolution.status == "ambiguous_definition"
    assert resolution.definition_count == 2
    assert resolution.is_stale


def _empty_allowlist(tmp_path: Path) -> Path:
    allowlist_dir = tmp_path / "allowlist"
    allowlist_dir.mkdir()
    (allowlist_dir / "_defaults.yaml").write_text("version: 1\ndefaults: {}\n")
    return allowlist_dir


def test_whole_tree_check_reports_stale_map_entries_as_errors(tmp_path: Path, monkeypatch) -> None:
    root = _fake_elspeth_root(tmp_path)
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"live", "gone"})})
    result = collect_check_result(root, allowlist_path=_empty_allowlist(tmp_path))
    assert [r.key for r in result.stale_named_boundary_contexts] == ["pkg/mod.py::gone"]
    assert result.has_errors
    diagnostics = [f for f in tier_rule._allowlist_diagnostics_to_lints(result) if "named-boundary" in f.message]
    assert [d.message for d in diagnostics] == ["Stale tier-model named-boundary map entry (missing_definition): pkg/mod.py::gone"]
    assert diagnostics[0].severity is Severity.ERROR
    assert diagnostics[0].file_path.endswith("trust_tier/tier_model/rule.py")


def test_scoped_file_check_does_not_report_map_staleness(tmp_path: Path, monkeypatch) -> None:
    """Pre-commit (``files=``) mode skips allowlist staleness; the map mirrors that."""
    root = _fake_elspeth_root(tmp_path)
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"gone"})})
    result = collect_check_result(root, allowlist_path=_empty_allowlist(tmp_path), files=[root / "pkg" / "mod.py"])
    assert result.stale_named_boundary_contexts == []


def test_map_staleness_is_only_measured_against_an_elspeth_source_root(tmp_path: Path, monkeypatch) -> None:
    """Fixture roots are not the tree the map is keyed against; every entry would be 'missing'."""
    root = tmp_path / "fixture"
    (root / "pkg").mkdir(parents=True)
    (root / "pkg" / "mod.py").write_text("x = 1\n", encoding="utf-8")
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"gone"})})
    result = collect_check_result(root, allowlist_path=_empty_allowlist(tmp_path))
    assert result.stale_named_boundary_contexts == []
    assert not result.has_errors


def test_run_check_text_and_json_surface_stale_map_entries(tmp_path: Path, monkeypatch) -> None:
    root = _fake_elspeth_root(tmp_path)
    monkeypatch.setattr(TierModelVisitor, "_R5_NAMED_BOUNDARY_CONTEXTS", {"pkg/mod.py": frozenset({"gone"})})
    allowlist_dir = _empty_allowlist(tmp_path)
    for fmt in ("text", "json"):
        args = argparse.Namespace(root=root, allowlist=allowlist_dir, exclude=[], format=fmt, files=[])
        out = io.StringIO()
        with redirect_stdout(out):
            rc = run_check(args)
        assert rc == 1
        if fmt == "text":
            assert "STALE NAMED-BOUNDARY MAP ENTRIES: 1" in out.getvalue()
            assert "Key: pkg/mod.py::gone" in out.getvalue()
        else:
            payload = json.loads(out.getvalue())
            assert payload["stale_named_boundary_contexts"] == [
                {"key": "pkg/mod.py::gone", "status": "missing_definition", "definitions": 0}
            ]
