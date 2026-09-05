#!/usr/bin/env python3
"""AST-based enforcement for contracts package.

Scans the codebase for:
1. dataclasses, TypedDicts, NamedTuples, and Enums used across module boundaries
2. dict[str, Any] type hints that should be typed contracts
3. Settings classes without Runtime counterparts (orphaned settings)

Also validates that all whitelist entries are still valid (not stale).

SCOPE OF CHECK 2 — read this before treating a green run as "no soft types".
This scan is syntactic and deliberately narrow. It reports a site only when ALL
of the following hold, and a green result says nothing about anything else:

  * the annotation spells ``dict[str, Any]`` / ``Dict[str, Any]`` — directly,
    unioned with ``None``, wrapped in ``list[...]``, or through a module-level
    alias resolved by :class:`DictAliasIndex`;
  * the value type is ``Any`` — ``dict[str, object]`` and ``Mapping[str,
    object]`` are out of scope by design, because ``object`` already forces
    narrowing at every use site;
  * the container is ``dict`` — ``Mapping``, ``MutableMapping`` and every other
    mapping ABC are NOT scanned, even with an ``Any`` value type;
  * the annotation sits on a function PARAMETER (positional-or-keyword or
    keyword-only) or a RETURN. Positional-only parameters, ``*args`` and
    ``**kwargs`` are not scanned, and neither is any variable or class-attribute
    annotation (``ast.AnnAssign``);
  * the file lives under ``src/elspeth`` and outside ``src/elspeth/contracts``.

Measured at 2026-09-04 against ``src/elspeth``: 2,162 soft-mapping annotations
exist in total, of which 1,601 are ``Any``-valued. This check can reach roughly a
third of them. Widening it to the remaining forms is tracked work, not an
oversight — see the ``[str, Any]`` burn-down epic — and the point of stating the
scope here is that the gap stays visible while that work is outstanding.

``tests/unit/scripts/test_check_contracts.py`` pins this scope: each exclusion
above has a test that asserts the scanner does NOT report it, so widening the
scanner without updating the record fails loudly.

CHECK 4 — SOFT-MAPPING CENSUS (elspeth-10d605be55). Check 2's whitelist is a
ratchet over the third it can see, and "whitelist rows retired" credits a
``dict[str, Any]`` -> ``Mapping[str, Any]`` rewrite exactly like an owned-type
conversion. The census is the second measure: it counts EVERY soft mapping
form (``dict``/``Mapping``/``MutableMapping`` with a ``str`` key and an ``Any``
or ``object`` value) in EVERY annotation position, all of ``src/elspeth``, and
pins the per-file, per-form counts in ``config/cicd/soft-mapping-census.yaml``.
Any drift from the pin fails; a rewrite between forms is a swap the re-pin diff
shows, and only the ``soft`` total falling is progress. A parameter parsed at a
``@trust_boundary`` (its ``source_param``) scores as a boundary conversion, i.e.
a removal. The exact counting rule is :data:`CENSUS_METHOD`, emitted verbatim
into the pin file. Re-pin with ``--write-census`` in the same commit that
changes a soft site.

Usage:
    python scripts/check_contracts.py
    python scripts/check_contracts.py --no-fail-on-stale  # Skip stale check
    python scripts/check_contracts.py --write-census      # Re-pin the soft-mapping census

Exit codes:
    0: All contracts properly centralized
    1: Violations found or stale whitelist entries
"""

from __future__ import annotations

import argparse
import ast
import sys
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Final

import yaml


@dataclass
class Violation:
    """A contract violation found during scanning."""

    file: str
    line: int
    type_name: str
    kind: str
    used_in: list[str]


@dataclass
class DictViolation:
    """A dict[str, Any] usage that should be a typed contract."""

    file: str
    line: int
    context: str  # function name or class.method
    param_name: str  # parameter name or "return"


@dataclass
class StaleEntry:
    """A whitelist entry that doesn't match any code."""

    entry: str
    category: str  # "type" or "dict_pattern"
    reason: str


@dataclass
class WhitelistEntry:
    """Tracked whitelist entry with match status."""

    value: str
    category: str
    matched: bool = field(default=False)


@dataclass
class SettingsViolation:
    """A Settings class without a Runtime counterpart."""

    class_name: str
    file: str
    line: int


@dataclass
class FieldCoverageViolation:
    """A Settings field not accessed in from_settings() method.

    Note: line is always 0 because tracking exact line numbers for Settings
    fields would require significant AST complexity. The settings_class +
    orphaned_field combination is sufficient for locating the issue - users
    can search for "class {settings_class}" and find the field definition.
    """

    settings_class: str
    runtime_class: str
    orphaned_field: str
    file: str
    line: int  # Always 0 - see docstring


@dataclass
class FieldMappingViolation:
    """A field mapping that doesn't match FIELD_MAPPINGS.

    This catches "misrouted" fields where code maps a settings field to
    the wrong runtime field. For example:
        base_delay=settings.max_delay_seconds  # Wrong! Should be initial_delay_seconds

    Note: line is always 0 because tracking exact line numbers for field
    mappings would require additional AST position tracking. The
    runtime_class + runtime_field combination is sufficient for locating
    the issue - users can search for the from_settings() method.
    """

    runtime_class: str
    runtime_field: str
    settings_field: str
    expected_settings_field: str
    file: str
    line: int  # Always 0 - see docstring


@dataclass
class HardcodeViolation:
    """A hardcoded literal in from_settings() not documented in INTERNAL_DEFAULTS.

    This catches undocumented internal defaults where code uses a literal
    value instead of settings.X but the literal is not documented in
    INTERNAL_DEFAULTS. For example:
        jitter=1.0  # OK if INTERNAL_DEFAULTS["retry"]["jitter"] = 1.0
        magic_number=42  # VIOLATION - not documented anywhere

    Note: line is always 0 because tracking exact line numbers for hardcodes
    would require additional AST position tracking. The runtime_class +
    runtime_field combination is sufficient for locating the issue.
    """

    runtime_class: str
    runtime_field: str
    literal_value: str  # String representation of the literal
    subsystem: str  # Expected subsystem key in INTERNAL_DEFAULTS
    file: str
    line: int  # Always 0 - see docstring


def load_whitelist(path: Path) -> tuple[dict[str, set[str]], list[WhitelistEntry]]:
    """Load whitelisted type definitions and dict patterns.

    Returns:
        Tuple of (whitelist dict for matching, list of entries for stale tracking)
    """
    if not path.exists():
        return {"types": set(), "dicts": set()}, []

    with open(path) as f:
        data = yaml.safe_load(f) or {}

    entries: list[WhitelistEntry] = []

    type_entries = data.get("allowed_external_types", [])
    dict_entries = data.get("allowed_dict_patterns", [])

    for t in type_entries:
        entries.append(WhitelistEntry(value=t, category="type"))
    for d in dict_entries:
        entries.append(WhitelistEntry(value=d, category="dict_pattern"))

    return {
        "types": set(type_entries),
        "dicts": set(dict_entries),
    }, entries


def find_type_definitions(file_path: Path) -> list[tuple[str, int, str]]:
    """Find dataclass, TypedDict, NamedTuple, Enum definitions in a file.

    Returns: List of (type_name, line_number, kind)
    """
    try:
        source = file_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        # Skip files that cannot be parsed (syntax errors or invalid encoding)
        return []

    definitions = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            # Check for @dataclass decorator
            for decorator in node.decorator_list:
                decorator_target = decorator.func if isinstance(decorator, ast.Call) else decorator
                decorator_name = _dotted_name(decorator_target)
                if decorator_name is not None and decorator_name.rsplit(".", 1)[-1] == "dataclass":
                    definitions.append((node.name, node.lineno, "dataclass"))

            # Check for TypedDict, NamedTuple, Enum base classes
            for base in node.bases:
                base_name = _dotted_name(base)
                if base_name is None:
                    continue
                base_leaf = base_name.rsplit(".", 1)[-1]
                if base_leaf == "TypedDict":
                    definitions.append((node.name, node.lineno, "TypedDict"))
                elif base_leaf == "NamedTuple":
                    definitions.append((node.name, node.lineno, "NamedTuple"))
                elif base_leaf == "Enum":
                    definitions.append((node.name, node.lineno, "Enum"))
                elif base_leaf in ("BaseModel", "PluginSchema"):
                    # Pydantic models in config are OK (trust boundary)
                    pass

    return definitions


@dataclass
class DictAliasIndex:
    """Module-level ``X = dict[str, Any]`` aliases, resolved per importing module.

    The dict-pattern matcher below is syntactic: it recognises the *spelling*
    ``dict[str, Any]``. A name bound to that type at module scope therefore
    launders it — ``def get_run(...) -> RunDetail | None`` and
    ``def get_run(...) -> dict[str, Any] | None`` are the same type, and without
    this index only the second is ever scanned.

    Resolution is by import, not by name: an alias counts for a file only if that
    file defines it or imports it from the module that does. Two modules may bind
    the same name to different types without one poisoning the other.

    SCOPE, stated so callers do not over-trust it. Resolved: module-level
    ``X = dict[str, Any]`` and ``X: TypeAlias = dict[str, Any]``, reached through
    ``from <module> import <name>`` (absolute or relative). NOT resolved: aliases
    bound inside a function or class body, aliases reached as an attribute
    (``import types_mod`` then ``types_mod.RunDetail``), aliases re-exported
    through an intermediate module, aliases renamed on import (``as``), and
    annotations written as string forward references. Each is a known hole, not
    an oversight; widen this only with a test that witnesses the new form.
    """

    # Dotted module name -> the alias names it defines
    _by_module: dict[str, frozenset[str]] = field(default_factory=dict)

    @staticmethod
    def _module_name(py_file: Path, src_dir: Path) -> str:
        """Return the dotted module name for ``py_file`` (``src/elspeth/mcp/types.py`` -> ``elspeth.mcp.types``)."""
        relative = py_file.relative_to(src_dir.parent).with_suffix("")
        parts = list(relative.parts)
        if parts and parts[-1] == "__init__":
            parts.pop()
        return ".".join(parts)

    @classmethod
    def build(cls, src_dir: Path) -> DictAliasIndex:
        """Parse every file once and record its module-level dict[str, Any] aliases."""
        index = cls()
        for py_file in src_dir.rglob("*.py"):
            try:
                tree = ast.parse(py_file.read_text())
            except (SyntaxError, UnicodeDecodeError):
                continue
            names: set[str] = set()
            # Module scope only: tree.body, never ast.walk. An alias bound inside a
            # function is not importable, so resolving it would report a name the
            # annotation cannot actually refer to.
            for node in tree.body:
                if isinstance(node, ast.Assign) and _is_dict_str_any(node.value):
                    names.update(target.id for target in node.targets if isinstance(target, ast.Name))
                elif (
                    isinstance(node, ast.AnnAssign)
                    and node.value is not None
                    and _is_dict_str_any(node.value)
                    and isinstance(node.target, ast.Name)
                ):
                    names.add(node.target.id)
            if names:
                index._by_module[cls._module_name(py_file, src_dir)] = frozenset(names)
        return index

    def names_in_scope(self, py_file: Path, src_dir: Path) -> frozenset[str]:
        """Return the alias names ``py_file`` can refer to: its own plus those it imports."""
        module = self._module_name(py_file, src_dir)
        in_scope = set(self._by_module.get(module, frozenset()))
        try:
            tree = ast.parse(py_file.read_text())
        except (SyntaxError, UnicodeDecodeError):
            return frozenset(in_scope)
        package = module.rsplit(".", 1)[0] if "." in module else module
        for node in ast.walk(tree):
            if not isinstance(node, ast.ImportFrom):
                continue
            if node.level:
                # from .types import X / from ..mcp.types import X
                base = package.split(".")
                trimmed = base[: len(base) - (node.level - 1)] if node.level > 1 else base
                source = ".".join([*trimmed, node.module]) if node.module else ".".join(trimmed)
            else:
                source = node.module or ""
            defined = self._by_module.get(source)
            if not defined:
                continue
            # asname is deliberately not followed: the renamed name is a different
            # binding and resolving it would need scope tracking this index lacks.
            in_scope.update(alias.name for alias in node.names if alias.asname is None and alias.name in defined)
        return frozenset(in_scope)


def _is_dict_str_any(annotation: ast.expr | None, aliases: frozenset[str] = frozenset()) -> bool:
    """Check if annotation is dict[str, Any], Dict[str, Any], or an alias of one.

    ``aliases`` carries the module-level ``X = dict[str, Any]`` names in scope for
    the file being scanned (see :class:`DictAliasIndex`). Without it this matcher
    is purely syntactic, so ``-> RunDetail`` and ``-> dict[str, Any]`` — the same
    type — are treated differently and only the second is ever reported.
    """
    if annotation is None:
        return False

    def is_str_annotation(expr: ast.expr) -> bool:
        return isinstance(expr, ast.Name) and expr.id == "str"

    def is_any_annotation(expr: ast.expr) -> bool:
        if isinstance(expr, ast.Name) and expr.id == "Any":
            return True
        return isinstance(expr, ast.Attribute) and expr.attr == "Any" and isinstance(expr.value, ast.Name) and expr.value.id == "typing"

    # A bare name bound to dict[str, Any] at module scope, e.g. mcp/types.py's
    # RunDetail. Resolved from the alias index, never from the name's spelling.
    if isinstance(annotation, ast.Name) and annotation.id in aliases:
        return True

    # dict[str, Any] - modern syntax
    if (
        isinstance(annotation, ast.Subscript)
        and isinstance(annotation.value, ast.Name)
        and annotation.value.id in ("dict", "Dict")
        and isinstance(annotation.slice, ast.Tuple)
        and len(annotation.slice.elts) == 2
    ):
        key_type, value_type = annotation.slice.elts
        if is_str_annotation(key_type) and is_any_annotation(value_type):
            return True
    return False


def _is_list_of_dict_str_any(annotation: ast.expr | None, aliases: frozenset[str] = frozenset()) -> bool:
    """Check if annotation is list[dict[str, Any]] (or list of an alias of one)."""
    if annotation is None:
        return False

    if isinstance(annotation, ast.Subscript) and isinstance(annotation.value, ast.Name) and annotation.value.id in ("list", "List"):
        return _is_dict_str_any(annotation.slice, aliases)
    return False


def _is_optional_dict(annotation: ast.expr | None, aliases: frozenset[str] = frozenset()) -> bool:
    """Check if annotation is dict[str, Any] | None (or an alias of one)."""
    if annotation is None:
        return False

    # dict[str, Any] | None
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        left_is_dict = _is_dict_str_any(annotation.left, aliases)
        right_is_none = isinstance(annotation.right, ast.Constant) and annotation.right.value is None
        if left_is_dict and right_is_none:
            return True
        # None | dict[str, Any]
        left_is_none = isinstance(annotation.left, ast.Constant) and annotation.left.value is None
        right_is_dict = _is_dict_str_any(annotation.right, aliases)
        if left_is_none and right_is_dict:
            return True
    return False


def _is_union_with_dict(annotation: ast.expr | None, aliases: frozenset[str] = frozenset()) -> bool:
    """Check if annotation contains dict[str, Any] (or an alias) in a union."""
    if annotation is None:
        return False

    # Check direct dict
    if _is_dict_str_any(annotation, aliases):
        return True

    # Check union types (X | Y | Z)
    if isinstance(annotation, ast.BinOp) and isinstance(annotation.op, ast.BitOr):
        return _is_union_with_dict(annotation.left, aliases) or _is_union_with_dict(annotation.right, aliases)

    return False


def find_dict_patterns_in_file(file_path: Path, aliases: frozenset[str] = frozenset()) -> list[str]:
    """Find all dict[str, Any] patterns in a file.

    Returns list of qualified names like "path:Class.method:param"

    ``aliases`` must match what :func:`find_dict_violations` was given for the same
    file. The stale-entry check compares whitelist rows against this function's
    output, so a narrower view here reports a legitimately whitelisted alias site
    as stale and fails the gate closed on a row that is doing its job.
    """
    try:
        source = file_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        # Skip files that cannot be parsed (syntax errors or invalid encoding)
        return []

    patterns = []
    relative_path = str(file_path)

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            func_name = node.name
            class_name = None

            # Try to find enclosing class
            for parent in ast.walk(tree):
                if isinstance(parent, ast.ClassDef):
                    for child in ast.walk(parent):
                        if child is node:
                            class_name = parent.name
                            break

            context = f"{class_name}.{func_name}" if class_name else func_name

            # Check parameters
            for arg in node.args.args + node.args.kwonlyargs:
                param_name = arg.arg
                annotation = arg.annotation

                if (
                    _is_dict_str_any(annotation, aliases)
                    or _is_optional_dict(annotation, aliases)
                    or _is_union_with_dict(annotation, aliases)
                ):
                    patterns.append(f"{relative_path}:{context}:{param_name}")
                elif _is_list_of_dict_str_any(annotation, aliases):
                    patterns.append(f"{relative_path}:{context}:{param_name} (list)")

            # Check return type
            if node.returns:
                if (
                    _is_dict_str_any(node.returns, aliases)
                    or _is_optional_dict(node.returns, aliases)
                    or _is_union_with_dict(node.returns, aliases)
                ):
                    patterns.append(f"{relative_path}:{context}:return")
                elif _is_list_of_dict_str_any(node.returns, aliases):
                    patterns.append(f"{relative_path}:{context}:return (list)")

    return patterns


def find_dict_violations(
    file_path: Path,
    whitelist: set[str],
    matched_entries: dict[str, bool],
    aliases: frozenset[str] = frozenset(),
) -> list[DictViolation]:
    """Find dict[str, Any] type hints that should be typed contracts.

    Scans function PARAMETERS and RETURN annotations only. Variable and
    class-attribute annotations (``ast.AnnAssign``) are not scanned, and neither
    are positional-only parameters, ``*args`` or ``**kwargs`` — see this module's
    docstring for the full scope statement.
    """
    try:
        source = file_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        # Skip files that cannot be parsed (syntax errors or invalid encoding)
        return []

    violations = []
    relative_path = str(file_path)

    # Build parent map once — O(nodes) instead of O(nodes²) for class lookup
    parent_map: dict[int, ast.AST] = {}
    for parent_node in ast.walk(tree):
        for child_node in ast.iter_child_nodes(parent_node):
            parent_map[id(child_node)] = parent_node

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            func_name = node.name

            # Find enclosing class via parent map — O(depth) not O(nodes)
            class_name = None
            ancestor = parent_map.get(id(node))
            while ancestor is not None:
                if isinstance(ancestor, ast.ClassDef):
                    class_name = ancestor.name
                    break
                ancestor = parent_map.get(id(ancestor))

            context = f"{class_name}.{func_name}" if class_name else func_name

            # Check parameters
            for arg in node.args.args + node.args.kwonlyargs:
                param_name = arg.arg
                annotation = arg.annotation

                if (
                    _is_dict_str_any(annotation, aliases)
                    or _is_optional_dict(annotation, aliases)
                    or _is_union_with_dict(annotation, aliases)
                ):
                    # Build qualified name for whitelist check
                    qualified = f"{relative_path}:{context}:{param_name}"
                    if qualified in whitelist:
                        matched_entries[qualified] = True
                    else:
                        violations.append(
                            DictViolation(
                                file=relative_path,
                                line=arg.lineno,
                                context=context,
                                param_name=param_name,
                            )
                        )
                elif _is_list_of_dict_str_any(annotation, aliases):
                    # List types have "(list)" suffix in whitelist
                    qualified = f"{relative_path}:{context}:{param_name} (list)"
                    if qualified in whitelist:
                        matched_entries[qualified] = True
                    else:
                        violations.append(
                            DictViolation(
                                file=relative_path,
                                line=arg.lineno,
                                context=context,
                                param_name=f"{param_name} (list)",
                            )
                        )

            # Check return type
            if node.returns:
                if (
                    _is_dict_str_any(node.returns, aliases)
                    or _is_optional_dict(node.returns, aliases)
                    or _is_union_with_dict(node.returns, aliases)
                ):
                    qualified = f"{relative_path}:{context}:return"
                    if qualified in whitelist:
                        matched_entries[qualified] = True
                    else:
                        violations.append(
                            DictViolation(
                                file=relative_path,
                                line=node.lineno,
                                context=context,
                                param_name="return",
                            )
                        )
                elif _is_list_of_dict_str_any(node.returns, aliases):
                    # List types have "(list)" suffix in whitelist
                    qualified = f"{relative_path}:{context}:return (list)"
                    if qualified in whitelist:
                        matched_entries[qualified] = True
                    else:
                        violations.append(
                            DictViolation(
                                file=relative_path,
                                line=node.lineno,
                                context=context,
                                param_name="return (list)",
                            )
                        )

    return violations


def get_top_level_module(file_path: Path, src_dir: Path) -> str:
    """Get the top-level module name for a file.

    For example:
        src/elspeth/tui/types.py -> tui
        src/elspeth/core/config.py -> core
    """
    relative = file_path.relative_to(src_dir)
    parts = relative.parts
    if len(parts) > 0:
        return parts[0]
    return ""


def is_cross_boundary_usage(defining_file: Path, using_file: Path, src_dir: Path) -> bool:
    """Check if usage crosses module boundaries.

    Cross-boundary means the using file is in a different top-level module
    than the defining file.
    """
    defining_module = get_top_level_module(defining_file, src_dir)
    using_module = get_top_level_module(using_file, src_dir)
    return defining_module != using_module


@dataclass
class ImportIndex:
    """Pre-built index of all imports across the codebase.

    Parses each file once and indexes imports by (module, name) for O(1) lookup.
    This replaces the O(files x types) approach of re-parsing every file for
    each type definition.
    """

    # Map from (module_prefix, imported_name) to list of files that import it
    _by_import: dict[tuple[str, str], list[Path]] = field(default_factory=dict)
    # Map from file to its top-level module (cached)
    _file_modules: dict[Path, str] = field(default_factory=dict)

    def _record_import(self, module: str, name: str, py_file: Path) -> None:
        bucket = self._by_import.setdefault((module, name), [])
        if py_file not in bucket:
            bucket.append(py_file)

    @classmethod
    def build(cls, src_dir: Path) -> ImportIndex:
        """Parse all Python files once and build the import index."""
        index = cls()
        for py_file in src_dir.rglob("*.py"):
            try:
                source = py_file.read_text()
                tree = ast.parse(source)
            except (SyntaxError, UnicodeDecodeError):
                continue

            index._file_modules[py_file] = get_top_level_module(py_file, src_dir)

            import_aliases: dict[str, str] = {}
            imported_modules: set[str] = set()
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom) and node.module:
                    for alias in node.names:
                        index._record_import(node.module, alias.name, py_file)
                        if alias.name == "*":
                            continue
                        local_name = alias.asname or alias.name
                        import_aliases[local_name] = f"{node.module}.{alias.name}"
                elif isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.asname:
                            import_aliases[alias.asname] = alias.name
                        else:
                            imported_modules.add(alias.name)

            for node in ast.walk(tree):
                if not isinstance(node, ast.Attribute):
                    continue
                usage = _qualified_import_usage(node, import_aliases, imported_modules)
                if usage is None:
                    continue
                module, name = usage
                index._record_import(module, name, py_file)

        return index

    def find_cross_boundary_usages(self, src_dir: Path, type_name: str, defining_file: Path) -> list[Path]:
        """Find files that import a type from a DIFFERENT top-level module."""
        defining_module = defining_file.relative_to(src_dir).with_suffix("").as_posix().replace("/", ".")

        usages = []
        # Check all import entries where the imported name matches
        for (module, name), importing_files in self._by_import.items():
            if name != type_name:
                continue
            if not _module_matches_definition(module, defining_module):
                continue
            for py_file in importing_files:
                if py_file == defining_file:
                    continue
                if not is_cross_boundary_usage(defining_file, py_file, src_dir):
                    continue
                usages.append(py_file)

        return usages


def _module_matches_definition(imported_module: str, defining_module: str) -> bool:
    return imported_module == defining_module or imported_module.endswith(f".{defining_module}")


def _qualified_import_usage(
    node: ast.Attribute,
    import_aliases: dict[str, str],
    imported_modules: set[str],
) -> tuple[str, str] | None:
    dotted = _dotted_name(node)
    if dotted is None:
        return None

    parts = dotted.split(".")
    if len(parts) < 2:
        return None

    alias_target = import_aliases.get(parts[0])
    if alias_target is not None:
        resolved_parts = [alias_target, *parts[1:]]
        return ".".join(resolved_parts[:-1]), resolved_parts[-1]

    for imported_module in sorted(imported_modules, key=len, reverse=True):
        prefix = f"{imported_module}."
        if not dotted.startswith(prefix):
            continue
        remainder = dotted[len(prefix) :]
        if not remainder:
            continue
        remainder_parts = remainder.split(".")
        module_parts = [imported_module, *remainder_parts[:-1]]
        return ".".join(module_parts), remainder_parts[-1]

    return None


def _dotted_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        parent = _dotted_name(node.value)
        if parent is None:
            return None
        return f"{parent}.{node.attr}"
    return None


def find_cross_boundary_usages(src_dir: Path, type_name: str, defining_file: Path) -> list[Path]:
    """Legacy wrapper — builds index on every call. Use ImportIndex.build() for batch use."""
    index = ImportIndex.build(src_dir)
    return index.find_cross_boundary_usages(src_dir, type_name, defining_file)


def validate_type_entry(entry: str, src_dir: Path) -> str | None:
    """Validate that an allowed_external_types entry exists.

    Entry format: "module/path:TypeName"

    Returns None if valid, or an error message if stale.
    """
    try:
        module_path, type_name = entry.rsplit(":", 1)
    except ValueError:
        return "Invalid format (expected 'path:TypeName')"

    # Convert module path to file path
    file_path = src_dir / f"{module_path}.py"

    if not file_path.exists():
        return f"File not found: {file_path}"

    # Check if type exists in file
    definitions = find_type_definitions(file_path)
    type_names = {name for name, _, _ in definitions}

    # Also check for regular class definitions (not just dataclass/TypedDict/etc)
    try:
        source = file_path.read_text()
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                type_names.add(node.name)
    except (SyntaxError, UnicodeDecodeError):
        # Skip files that cannot be parsed; rely on definitions from find_type_definitions
        pass

    if type_name not in type_names:
        return f"Type '{type_name}' not found in {file_path}"

    return None


def validate_dict_pattern_entry(entry: str, src_dir: Path, alias_index: DictAliasIndex | None = None) -> str | None:
    """Validate that an allowed_dict_patterns entry exists.

    Entry format: "src/elspeth/path/file.py:Class.method:param"

    Returns None if valid, or an error message if stale.
    """
    try:
        parts = entry.split(":")
        if len(parts) != 3:
            return f"Invalid format (expected 'file:context:param', got {len(parts)} parts)"

        file_path_str, context, param = parts
    except ValueError:
        return "Invalid format (expected 'file:context:param')"

    file_path = Path(file_path_str)

    if not file_path.exists():
        return f"File not found: {file_path}"

    # Find all dict patterns in the file
    aliases = alias_index.names_in_scope(file_path, src_dir) if alias_index is not None else frozenset()
    patterns = find_dict_patterns_in_file(file_path, aliases)

    # Check if this entry matches any pattern
    if entry in patterns:
        return None

    # Check for partial matches to give better error messages
    matching_file_patterns = [p for p in patterns if p.startswith(file_path_str)]
    if not matching_file_patterns:
        return f"No dict[str, Any] patterns found in {file_path}"

    # Check if context exists
    matching_context_patterns = [p for p in matching_file_patterns if f":{context}:" in p]
    if not matching_context_patterns:
        return f"Context '{context}' not found in {file_path}"

    # Parameter doesn't match
    available_params = [p.split(":")[-1] for p in matching_context_patterns]
    return f"Parameter '{param}' not found in {context}. Available: {available_params}"


def find_stale_entries(
    entries: list[WhitelistEntry],
    matched_dict_patterns: dict[str, bool],
    matched_type_patterns: set[str],
    src_dir: Path,
    alias_index: DictAliasIndex | None = None,
) -> list[StaleEntry]:
    """Find whitelist entries that don't match any code."""
    stale = []

    for entry in entries:
        if entry.category == "type":
            # Check if this type entry is valid
            if entry.value in matched_type_patterns:
                continue
            error = validate_type_entry(entry.value, src_dir)
            if error:
                stale.append(StaleEntry(entry=entry.value, category="type", reason=error))

        elif entry.category == "dict_pattern":
            # Check if this pattern was matched during scanning
            if matched_dict_patterns.get(entry.value, False):
                continue
            # Validate the entry
            error = validate_dict_pattern_entry(entry.value, src_dir, alias_index)
            if error:
                stale.append(StaleEntry(entry=entry.value, category="dict_pattern", reason=error))

    return stale


class SettingsAccessVisitor(ast.NodeVisitor):
    """Extract all `settings.X` attribute accesses from AST.

    Used to find which Settings fields are accessed in from_settings() methods.
    Looks for patterns like:
        - settings.field_name
        - settings.field_name.nested (captures just field_name)
    """

    def __init__(self, param_name: str = "settings") -> None:
        self.param_name = param_name
        self.accessed_fields: set[str] = set()

    def visit_Attribute(self, node: ast.Attribute) -> None:
        """Capture attribute access on the settings parameter."""
        # Check if this is `settings.X` - direct attribute access on the parameter
        if isinstance(node.value, ast.Name) and node.value.id == self.param_name:
            self.accessed_fields.add(node.attr)
        # Continue visiting children
        self.generic_visit(node)


class FieldMappingVisitor(ast.NodeVisitor):
    """Extract runtime_field=settings.settings_field mappings from AST.

    Used to validate that field mappings in from_settings() match FIELD_MAPPINGS.
    Looks for keyword arguments in constructor calls like:
        cls(
            base_delay=settings.initial_delay_seconds,
            max_delay=settings.max_delay_seconds,
        )

    Captures tuples of (runtime_field, settings_field).
    """

    def __init__(self, param_name: str = "settings") -> None:
        self.param_name = param_name
        self.field_mappings: list[tuple[str, str]] = []  # (runtime_field, settings_field)

    def visit_Call(self, node: ast.Call) -> None:
        """Capture keyword arguments that map settings fields to runtime fields."""
        for keyword in node.keywords:
            if keyword.arg is None:
                # **kwargs - skip
                continue
            runtime_field = keyword.arg
            # Check if value is settings.X
            if (
                isinstance(keyword.value, ast.Attribute)
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == self.param_name
            ):
                settings_field = keyword.value.attr
                self.field_mappings.append((runtime_field, settings_field))
        # Continue visiting children (nested calls)
        self.generic_visit(node)


class HardcodeLiteralVisitor(ast.NodeVisitor):
    """Extract runtime_field=<literal> assignments from AST.

    Used to find hardcoded literals in from_settings() methods.
    Looks for keyword arguments in constructor calls like:
        cls(
            jitter=1.0,  # Hardcoded literal
            max_attempts=settings.max_attempts,  # Not a literal (skipped)
        )

    Captures tuples of (runtime_field, literal_value).
    Ignores values that are:
    - settings.X accesses (handled by FieldMappingVisitor)
    - Function calls like float(INTERNAL_DEFAULTS["retry"]["jitter"])
    - Subscripts like INTERNAL_DEFAULTS["retry"]["jitter"]
    - Variable references
    """

    def __init__(self, param_name: str = "settings") -> None:
        self.param_name = param_name
        self.hardcoded_literals: list[tuple[str, object]] = []  # (runtime_field, literal_value)

    def _is_plain_literal(self, node: ast.expr) -> tuple[bool, object]:
        """Check if node is a plain literal (not wrapped in function call).

        Returns (is_literal, value).
        """
        # Plain constants: 1.0, 42, "string", True
        if isinstance(node, ast.Constant):
            return True, node.value
        # Negative numbers: -1.0, -42
        if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub) and isinstance(node.operand, ast.Constant):
            operand_value = node.operand.value
            # Only negate numeric types
            if isinstance(operand_value, int | float):
                return True, -operand_value
        return False, None

    def visit_Call(self, node: ast.Call) -> None:
        """Capture keyword arguments that use plain literals."""
        for keyword in node.keywords:
            if keyword.arg is None:
                # **kwargs - skip
                continue
            runtime_field = keyword.arg

            # Skip settings.X accesses
            if (
                isinstance(keyword.value, ast.Attribute)
                and isinstance(keyword.value.value, ast.Name)
                and keyword.value.value.id == self.param_name
            ):
                continue

            # Check if it's a plain literal (not wrapped in float(), int(), etc.)
            is_literal, value = self._is_plain_literal(keyword.value)
            if is_literal:
                self.hardcoded_literals.append((runtime_field, value))

        # Continue visiting children (nested calls)
        self.generic_visit(node)


def extract_from_settings_accesses(runtime_path: Path) -> dict[str, set[str]]:
    """Extract all settings.X accesses from from_settings() methods in a file.

    Parses the runtime.py file and finds all Runtime*Config classes with
    from_settings() methods. For each, extracts which settings fields are accessed.

    Args:
        runtime_path: Path to contracts/config/runtime.py

    Returns:
        Dict mapping RuntimeClassName -> set of accessed settings fields
    """
    try:
        source = runtime_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return {}

    result: dict[str, set[str]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Runtime") and node.name.endswith("Config"):
            # Find from_settings() method in this class
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "from_settings":
                    # Get the parameter name (first param after cls)
                    # from_settings(cls, settings: "RetrySettings") -> settings
                    param_name = "settings"  # default
                    if len(item.args.args) > 1:
                        param_name = item.args.args[1].arg

                    # Visit the method body to find settings.X accesses
                    visitor = SettingsAccessVisitor(param_name)
                    for stmt in item.body:
                        visitor.visit(stmt)

                    result[node.name] = visitor.accessed_fields
                    break

    return result


def extract_from_settings_field_mappings(runtime_path: Path) -> dict[str, list[tuple[str, str]]]:
    """Extract runtime_field=settings.settings_field mappings from from_settings() methods.

    Parses the runtime.py file and finds all Runtime*Config classes with
    from_settings() methods. For each, extracts the field mappings.

    Args:
        runtime_path: Path to contracts/config/runtime.py

    Returns:
        Dict mapping RuntimeClassName -> list of (runtime_field, settings_field) tuples
    """
    try:
        source = runtime_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return {}

    result: dict[str, list[tuple[str, str]]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Runtime") and node.name.endswith("Config"):
            # Find from_settings() method in this class
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "from_settings":
                    # Get the parameter name (first param after cls)
                    param_name = "settings"  # default
                    if len(item.args.args) > 1:
                        param_name = item.args.args[1].arg

                    # Visit the method body to find runtime_field=settings.X mappings
                    visitor = FieldMappingVisitor(param_name)
                    for stmt in item.body:
                        visitor.visit(stmt)

                    result[node.name] = visitor.field_mappings
                    break

    return result


def check_field_name_mappings(runtime_path: Path) -> list[FieldMappingViolation]:
    """Check that field mappings in from_settings() match FIELD_MAPPINGS.

    For each Runtime*Config class with a from_settings() method:
    1. Extract all runtime_field=settings.settings_field assignments
    2. For each renamed field (in FIELD_MAPPINGS), verify the mapping is correct
    3. Report violations where settings field is mapped to wrong runtime field

    Example violation (misrouted field):
        If FIELD_MAPPINGS says initial_delay_seconds -> base_delay but code has:
            base_delay=settings.max_delay_seconds
        This is a misroute - max_delay_seconds should map to max_delay, not base_delay.

    Args:
        runtime_path: Path to contracts/config/runtime.py (Runtime classes)

    Returns:
        List of FieldMappingViolation for misrouted fields
    """
    from elspeth.contracts.config.alignment import FIELD_MAPPINGS, SETTINGS_TO_RUNTIME

    # Get all runtime_field=settings.X mappings from from_settings() methods
    runtime_mappings = extract_from_settings_field_mappings(runtime_path)

    violations: list[FieldMappingViolation] = []

    # For each Settings -> Runtime mapping that has field renames
    for settings_class, runtime_class in SETTINGS_TO_RUNTIME.items():
        if settings_class not in FIELD_MAPPINGS:
            # No renamed fields for this class - skip
            continue

        if runtime_class not in runtime_mappings:
            # No from_settings() method found - skip (different check handles this)
            continue

        field_renames = FIELD_MAPPINGS[settings_class]
        actual_mappings = runtime_mappings[runtime_class]

        # Build reverse lookup: for each runtime_field that's a rename target,
        # what settings_field SHOULD map to it?
        # field_renames: {settings_field: runtime_field}
        # We need: {runtime_field: expected_settings_field}
        expected_for_runtime: dict[str, str] = {runtime_field: settings_field for settings_field, runtime_field in field_renames.items()}

        # Check each actual mapping
        for runtime_field, actual_settings_field in actual_mappings:
            # Is this runtime_field one that requires a specific settings_field?
            if runtime_field in expected_for_runtime:
                expected_settings_field = expected_for_runtime[runtime_field]
                if actual_settings_field != expected_settings_field:
                    violations.append(
                        FieldMappingViolation(
                            runtime_class=runtime_class,
                            runtime_field=runtime_field,
                            settings_field=actual_settings_field,
                            expected_settings_field=expected_settings_field,
                            file=str(runtime_path),
                            line=0,  # Line number would require more complex tracking
                        )
                    )

    return violations


def extract_from_settings_hardcodes(runtime_path: Path) -> dict[str, list[tuple[str, object]]]:
    """Extract runtime_field=<literal> assignments from from_settings() methods.

    Parses the runtime.py file and finds all Runtime*Config classes with
    from_settings() methods. For each, extracts hardcoded literal values.

    Args:
        runtime_path: Path to contracts/config/runtime.py

    Returns:
        Dict mapping RuntimeClassName -> list of (runtime_field, literal_value) tuples
    """
    try:
        source = runtime_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return {}

    result: dict[str, list[tuple[str, object]]] = {}

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.startswith("Runtime") and node.name.endswith("Config"):
            # Find from_settings() method in this class
            for item in node.body:
                if isinstance(item, ast.FunctionDef) and item.name == "from_settings":
                    # Get the parameter name (first param after cls)
                    param_name = "settings"  # default
                    if len(item.args.args) > 1:
                        param_name = item.args.args[1].arg

                    # Visit the method body to find hardcoded literals
                    visitor = HardcodeLiteralVisitor(param_name)
                    for stmt in item.body:
                        visitor.visit(stmt)

                    result[node.name] = visitor.hardcoded_literals
                    break

    return result


def check_hardcode_documentation(runtime_path: Path) -> list[HardcodeViolation]:
    """Check that hardcoded literals in from_settings() are documented in INTERNAL_DEFAULTS.

    For each Runtime*Config class with a from_settings() method:
    1. Extract all runtime_field=<literal> assignments (plain literals only)
    2. Look up the expected subsystem in RUNTIME_TO_SUBSYSTEM
    3. Check if the literal is documented in INTERNAL_DEFAULTS[subsystem][field]
    4. Report violations for undocumented hardcodes

    Example violation (undocumented hardcode):
        def from_settings(cls, settings):
            return cls(
                jitter=1.0,  # OK if INTERNAL_DEFAULTS["retry"]["jitter"] = 1.0
                magic_number=42,  # VIOLATION - not documented anywhere
            )

    Note: This check only catches PLAIN literals (1.0, 42, "string").
    Wrapped literals like float(INTERNAL_DEFAULTS["retry"]["jitter"]) are not checked
    because they explicitly reference INTERNAL_DEFAULTS (self-documenting).

    Args:
        runtime_path: Path to contracts/config/runtime.py (Runtime classes)

    Returns:
        List of HardcodeViolation for undocumented hardcodes
    """
    from elspeth.contracts.config.alignment import RUNTIME_TO_SUBSYSTEM
    from elspeth.contracts.config.defaults import INTERNAL_DEFAULTS

    # Get all hardcoded literals from from_settings() methods
    runtime_hardcodes = extract_from_settings_hardcodes(runtime_path)

    violations: list[HardcodeViolation] = []

    for runtime_class, hardcodes in runtime_hardcodes.items():
        # Get the subsystem for this runtime class
        subsystem = RUNTIME_TO_SUBSYSTEM.get(runtime_class)
        if subsystem is None:
            # No subsystem mapping - all hardcodes in this class are violations
            for runtime_field, literal_value in hardcodes:
                violations.append(
                    HardcodeViolation(
                        runtime_class=runtime_class,
                        runtime_field=runtime_field,
                        literal_value=repr(literal_value),
                        subsystem="(no subsystem mapping)",
                        file=str(runtime_path),
                        line=0,
                    )
                )
            continue

        # Get the documented defaults for this subsystem
        subsystem_defaults: dict[str, int | float | bool | str] | MappingProxyType[str, int | float | bool | str] = INTERNAL_DEFAULTS.get(
            subsystem, {}
        )

        # Check each hardcoded literal
        for runtime_field, literal_value in hardcodes:
            if runtime_field not in subsystem_defaults:
                # Field not documented in INTERNAL_DEFAULTS
                violations.append(
                    HardcodeViolation(
                        runtime_class=runtime_class,
                        runtime_field=runtime_field,
                        literal_value=repr(literal_value),
                        subsystem=subsystem,
                        file=str(runtime_path),
                        line=0,
                    )
                )
            elif subsystem_defaults[runtime_field] != literal_value:
                # Field documented but value doesn't match (more serious!)
                # This means the code has a different value than documented
                violations.append(
                    HardcodeViolation(
                        runtime_class=runtime_class,
                        runtime_field=runtime_field,
                        literal_value=f"{literal_value!r} (documented: {subsystem_defaults[runtime_field]!r})",
                        subsystem=subsystem,
                        file=str(runtime_path),
                        line=0,
                    )
                )

    return violations


def get_settings_class_fields(config_path: Path, class_name: str) -> set[str]:
    """Get all field names from a Settings class using AST.

    Parses the config.py file to find the Settings class and extracts
    field definitions. Handles both Pydantic Field() and simple annotations.

    Args:
        config_path: Path to core/config.py
        class_name: Name of the Settings class (e.g., "RetrySettings")

    Returns:
        Set of field names defined in the class
    """
    try:
        source = config_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return set()

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            fields: set[str] = set()
            for item in node.body:
                # Look for annotated assignments: field: Type = ...
                if isinstance(item, ast.AnnAssign) and isinstance(item.target, ast.Name):
                    fields.add(item.target.id)
            return fields

    return set()


def check_from_settings_coverage(
    config_path: Path,
    runtime_path: Path,
) -> list[FieldCoverageViolation]:
    """Check that from_settings() methods access all Settings fields.

    For each Runtime*Config class with a from_settings() method:
    1. Parse the method body to find all settings.X accesses
    2. Get the corresponding Settings class fields
    3. Report any Settings field NOT accessed (potential orphan)

    Why no exemption mechanism for Settings fields:
        If a Settings field exists, it SHOULD be used. Orphaned Settings fields
        are always bugs (like the P2-2026-01-21 exponential_base bug), never
        intentional. The INTERNAL exemption in FIELD_MAPPINGS is for *Runtime*
        fields that don't come from Settings (like `jitter`), not for Settings
        fields to skip. If a field shouldn't be mapped to Runtime, it shouldn't
        be in the Settings class at all.

    Args:
        config_path: Path to core/config.py (Settings classes)
        runtime_path: Path to contracts/config/runtime.py (Runtime classes)

    Returns:
        List of FieldCoverageViolation for orphaned fields
    """
    from elspeth.contracts.config.alignment import SETTINGS_TO_RUNTIME

    # Get all settings.X accesses from from_settings() methods
    runtime_accesses = extract_from_settings_accesses(runtime_path)

    violations: list[FieldCoverageViolation] = []

    # For each Settings -> Runtime mapping, check coverage
    for settings_class, runtime_class in SETTINGS_TO_RUNTIME.items():
        if runtime_class not in runtime_accesses:
            # No from_settings() method found - skip (different check handles this)
            continue

        accessed_fields = runtime_accesses[runtime_class]
        settings_fields = get_settings_class_fields(config_path, settings_class)

        # Find orphaned fields (in Settings but not accessed in from_settings)
        orphaned = settings_fields - accessed_fields

        for field_name in sorted(orphaned):
            violations.append(
                FieldCoverageViolation(
                    settings_class=settings_class,
                    runtime_class=runtime_class,
                    orphaned_field=field_name,
                    file=str(runtime_path),
                    line=0,  # Line number would require more complex tracking
                )
            )

    return violations


def find_settings_classes(config_path: Path) -> list[tuple[str, int]]:
    """Find all Settings classes in core/config.py.

    A Settings class is identified by its name ending in 'Settings'.
    These are Pydantic BaseModel classes that define configuration schemas.

    Args:
        config_path: Path to core/config.py

    Returns:
        List of (class_name, line_number) tuples
    """
    try:
        source = config_path.read_text()
        tree = ast.parse(source)
    except (SyntaxError, UnicodeDecodeError):
        return []

    settings_classes = []

    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name.endswith("Settings"):
            settings_classes.append((node.name, node.lineno))

    return settings_classes


def check_settings_alignment(config_path: Path) -> list[SettingsViolation]:
    """Check that all Settings classes have Runtime counterparts or are exempt.

    Uses SETTINGS_TO_RUNTIME and EXEMPT_SETTINGS from contracts/config/alignment.py
    to determine which Settings classes need Runtime counterparts.

    Args:
        config_path: Path to core/config.py

    Returns:
        List of SettingsViolation for orphaned Settings classes
    """
    # Import alignment mappings
    from elspeth.contracts.config.alignment import EXEMPT_SETTINGS, SETTINGS_TO_RUNTIME

    settings_classes = find_settings_classes(config_path)
    violations = []

    for class_name, line_no in settings_classes:
        # Check if exempt (doesn't need Runtime counterpart)
        if class_name in EXEMPT_SETTINGS:
            continue
        # Check if has Runtime counterpart
        if class_name in SETTINGS_TO_RUNTIME:
            continue
        # Orphaned - no Runtime counterpart and not exempt
        violations.append(
            SettingsViolation(
                class_name=class_name,
                file=str(config_path),
                line=line_no,
            )
        )

    return violations


# ---------------------------------------------------------------------------
# Soft-mapping census (elspeth-10d605be55)
#
# Check 2 above counts one SPELLING (``dict[str, Any]``) in two positions and
# credits every retired whitelist row identically — a ``dict[str, Any]`` ->
# ``Mapping[str, Any]`` rewrite and a genuine owned-type conversion both retire
# exactly one row, and the 2026-08-31 campaign that replaced ``Any`` with
# ``object`` wholesale went green on it while retiring no risk. The census is
# the second measure: it counts every soft mapping form in every annotation
# position, pins the counts per file, and refuses ANY drift from the pin. A
# rewrite between forms therefore shows up as a swap in the re-pin diff (one
# column down, another up in the same file) instead of scoring; only a site
# that leaves the soft family altogether lowers the soft total, and that total
# is the burn-down score.
# ---------------------------------------------------------------------------

SOFT_MAPPING_FORMS: Final[tuple[str, ...]] = (
    "dict[str, Any]",
    "Mapping[str, Any]",
    "MutableMapping[str, Any]",
    "dict[str, object]",
    "Mapping[str, object]",
)
"""The five soft mapping forms, keyed on container and value type.

``object``-valued forms are soft here even though check 2 excludes them: they
force narrowing at every use site instead of at a boundary, which is the shape
the failed ``Any`` -> ``object`` sweep produced. Counting them is what stops
that sweep from ever scoring again.
"""

BOUNDARY_COLUMN: Final = "boundary"

CENSUS_METHOD: Final = (
    "Every dict/Dict/Mapping/MutableMapping[str, Any|object] subscript occurring anywhere inside an "
    "annotation (unions, Optional and container wrappers included, each occurrence counted) on any "
    "function parameter (positional-only, ordinary, *args, keyword-only, **kwargs), any return, and any "
    "AnnAssign at module, class or function scope, in every parseable .py file under src/elspeth "
    "including contracts/; module-level dict[str, Any] aliases resolved by import through DictAliasIndex. "
    "A parameter named as source_param by a @trust_boundary decorator on the same function counts in the "
    "'boundary' column (a boundary conversion scores as a removal), not in its form column. Measured "
    "2,739 soft + 63 boundary occurrences across 386 files at release/0.8.0@e8998f20a, six of them "
    "reached only through alias resolution; the 2,162 quoted by the 2026-09-04 scope statement used a "
    "narrower rule that could not be recovered and is NOT this census."
)

_SOFT_CONTAINERS: Final[dict[str, str]] = {
    "dict": "dict",
    "Dict": "dict",
    "Mapping": "Mapping",
    "MutableMapping": "MutableMapping",
}


@dataclass(frozen=True)
class CensusSite:
    """One soft-mapping occurrence: where it is, which form, and whether it is a boundary parse."""

    file: str
    line: int
    context: str
    position: str
    form: str
    boundary: bool


@dataclass(frozen=True)
class CensusDrift:
    """One per-file, per-form disagreement between the pinned census and the live tree."""

    file: str
    form: str
    pinned: int
    live: int


@dataclass(frozen=True)
class CensusReport:
    """Outcome of :func:`check_soft_mapping_census`: pass/fail, printable lines, and the score."""

    ok: bool
    lines: tuple[str, ...]
    totals: dict[str, int]
    drifts: tuple[CensusDrift, ...]


def _annotation_name(expr: ast.expr) -> str | None:
    """Return the bare or attribute name an annotation node spells, else ``None``."""
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute):
        return expr.attr
    return None


def soft_mapping_form(node: ast.AST, aliases: frozenset[str] = frozenset()) -> str | None:
    """Classify ONE annotation node as a soft mapping form, or ``None``.

    ``typing.Mapping`` / ``collections.abc.Mapping`` / bare ``Mapping`` all read
    as the same container; ``typing.Any`` and ``Any`` as the same value. A name
    in ``aliases`` is a module-level ``dict[str, Any]`` alias resolved by import.
    """
    if isinstance(node, ast.Name) and node.id in aliases:
        return "dict[str, Any]"
    if not (isinstance(node, ast.Subscript) and isinstance(node.slice, ast.Tuple) and len(node.slice.elts) == 2):
        return None
    container = _SOFT_CONTAINERS.get(_annotation_name(node.value) or "")
    key_type, value_type = node.slice.elts
    value = _annotation_name(value_type)
    if container is None or _annotation_name(key_type) != "str" or value not in ("Any", "object"):
        return None
    return f"{container}[str, {value}]"


def iter_soft_mapping_forms(annotation: ast.expr, aliases: frozenset[str] = frozenset()) -> list[str]:
    """Every soft form occurring anywhere inside ``annotation``, in source order.

    Walks the whole expression so a union carrying two soft forms yields two
    and a wrapper such as ``list[dict[str, Any]]`` yields one — per-occurrence
    counting is what turns a form-to-form rewrite into a visible swap.
    """
    forms: list[str] = []
    for node in ast.walk(annotation):
        form = soft_mapping_form(node, aliases)
        if form is not None:
            forms.append(form)
    return forms


def _trust_boundary_source_param(func: ast.FunctionDef | ast.AsyncFunctionDef) -> str | None:
    """The ``source_param`` a ``@trust_boundary(...)`` decorator on ``func`` names, else ``None``."""
    for decorator in func.decorator_list:
        if not isinstance(decorator, ast.Call) or _annotation_name(decorator.func) != "trust_boundary":
            continue
        for keyword in decorator.keywords:
            if keyword.arg == "source_param" and isinstance(keyword.value, ast.Constant) and isinstance(keyword.value.value, str):
                return keyword.value.value
    return None


def _annassign_target(node: ast.AnnAssign) -> str:
    dotted = _dotted_name(node.target)
    return dotted if dotted is not None else ast.unparse(node.target)


def census_file(file_path: Path, aliases: frozenset[str] = frozenset()) -> list[CensusSite]:
    """Every soft-mapping occurrence in one file, in source order (see :data:`CENSUS_METHOD`)."""
    try:
        tree = ast.parse(file_path.read_text())
    except (SyntaxError, UnicodeDecodeError):
        return []

    parent_map: dict[int, ast.AST] = {}
    for parent_node in ast.walk(tree):
        for child_node in ast.iter_child_nodes(parent_node):
            parent_map[id(child_node)] = parent_node

    def context_of(node: ast.AST) -> str:
        """Dotted enclosing class/function names, ``<module>`` at module scope."""
        names: list[str] = []
        ancestor = parent_map.get(id(node))
        while ancestor is not None:
            if isinstance(ancestor, ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef):
                names.append(ancestor.name)
            ancestor = parent_map.get(id(ancestor))
        return ".".join(reversed(names)) if names else "<module>"

    sites: list[CensusSite] = []
    relative_path = str(file_path)

    def record(line: int, context: str, position: str, annotation: ast.expr, *, boundary: bool) -> None:
        for form in iter_soft_mapping_forms(annotation, aliases):
            sites.append(CensusSite(relative_path, line, context, position, form, boundary))

    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            own_context = context_of(node)
            context = f"{own_context}.{node.name}" if own_context != "<module>" else node.name
            source_param = _trust_boundary_source_param(node)
            args = node.args
            labelled: list[tuple[str, ast.arg]] = [(arg.arg, arg) for arg in args.posonlyargs + args.args]
            if args.vararg is not None:
                labelled.append((f"*{args.vararg.arg}", args.vararg))
            labelled.extend((arg.arg, arg) for arg in args.kwonlyargs)
            if args.kwarg is not None:
                labelled.append((f"**{args.kwarg.arg}", args.kwarg))
            for label, arg in labelled:
                if arg.annotation is not None:
                    record(arg.lineno, context, f"param:{label}", arg.annotation, boundary=arg.arg == source_param)
            if node.returns is not None:
                record(node.lineno, context, "return", node.returns, boundary=False)
        elif isinstance(node, ast.AnnAssign):
            record(node.lineno, context_of(node), f"annassign:{_annassign_target(node)}", node.annotation, boundary=False)
    return sites


def build_census(src_dir: Path, alias_index: DictAliasIndex | None = None) -> list[CensusSite]:
    """Every soft-mapping occurrence under ``src_dir`` (contracts/ included), file order sorted."""
    sites: list[CensusSite] = []
    for py_file in sorted(src_dir.rglob("*.py")):
        aliases = alias_index.names_in_scope(py_file, src_dir) if alias_index is not None else frozenset()
        sites.extend(census_file(py_file, aliases))
    return sites


def tabulate_census(sites: list[CensusSite]) -> dict[str, dict[str, int]]:
    """Per-file counts: ``{file: {form: n, ..., "boundary": n}}`` with zero columns omitted."""
    table: dict[str, dict[str, int]] = {}
    for site in sites:
        column = BOUNDARY_COLUMN if site.boundary else site.form
        row = table.setdefault(site.file, {})
        row[column] = row.get(column, 0) + 1
    return table


def census_totals(table: dict[str, dict[str, int]]) -> dict[str, int]:
    """The score: ``soft`` (every form column summed), ``boundary``, and one entry per form."""
    totals = dict.fromkeys(SOFT_MAPPING_FORMS, 0)
    totals[BOUNDARY_COLUMN] = 0
    for row in table.values():
        for column, count in row.items():
            totals[column] = totals.get(column, 0) + count
    return {
        "soft": sum(totals[form] for form in SOFT_MAPPING_FORMS),
        BOUNDARY_COLUMN: totals[BOUNDARY_COLUMN],
        **{form: totals[form] for form in SOFT_MAPPING_FORMS},
    }


def compare_census(
    pinned: dict[str, dict[str, int]],
    stored_totals: dict[str, int],
    live: dict[str, dict[str, int]],
) -> list[CensusDrift]:
    """Every per-file, per-form disagreement, either direction, plus a forged-totals check.

    Decreases are drifts too: a stale-high pin is slack a later addition could
    hide in, so the pin must move with every change and the re-pin diff is the
    record of what moved.
    """
    drifts: list[CensusDrift] = []
    for file in sorted(set(pinned) | set(live)):
        pinned_row = pinned.get(file, {})
        live_row = live.get(file, {})
        for column in sorted(set(pinned_row) | set(live_row)):
            before, after = pinned_row.get(column, 0), live_row.get(column, 0)
            if before != after:
                drifts.append(CensusDrift(file=file, form=column, pinned=before, live=after))
    recomputed = census_totals(pinned)
    for column in sorted(set(stored_totals) | set(recomputed)):
        if stored_totals.get(column, 0) != recomputed.get(column, 0):
            drifts.append(CensusDrift(file="<totals>", form=column, pinned=stored_totals.get(column, 0), live=recomputed.get(column, 0)))
    return drifts


def write_census(path: Path, table: dict[str, dict[str, int]]) -> None:
    """Pin ``table`` to ``path`` with the counting rule in the header, keys sorted for stable diffs."""
    document = {
        "totals": census_totals(table),
        "files": {file: dict(sorted(row.items())) for file, row in sorted(table.items())},
    }
    header = (
        "# Soft-mapping census — GENERATED by `python scripts/check_contracts.py --write-census`.\n"
        "# Do not hand-edit: every per-file, per-form count is compared against the live tree and\n"
        "# any drift fails the contracts gate. Re-pin in the same commit that changes a soft site;\n"
        "# the diff of this file is the record of what moved (a dict -> Mapping rewrite is a swap,\n"
        "# not a retirement — only the `soft` total falling is progress).\n"
        "# Counting rule: `method` below, verbatim from CENSUS_METHOD in scripts/check_contracts.py.\n"
    )
    # The method is emitted as a literal block scalar (``|-``) by hand so it lands
    # verbatim on one line — safe_dump would fold it at the line width and the
    # stated rule would no longer be greppable as written.
    method_block = f"method: |-\n  {CENSUS_METHOD}\n"
    path.write_text(
        header + method_block + yaml.safe_dump(document, sort_keys=True, default_flow_style=False, allow_unicode=True, width=120)
    )


def load_census(path: Path) -> tuple[dict[str, dict[str, int]], dict[str, int]]:
    """Read a pin written by :func:`write_census`: ``(files table, stored totals)``.

    The file is repository-owned config, but its shape is still asserted rather
    than trusted, so a hand-edit that breaks it fails here instead of reading
    as "no soft sites".
    """
    loaded = yaml.safe_load(path.read_text())
    if not isinstance(loaded, dict) or not isinstance(loaded.get("files"), dict) or not isinstance(loaded.get("totals"), dict):
        raise ValueError(f"{path}: expected a mapping with 'files' and 'totals' blocks")
    table: dict[str, dict[str, int]] = {}
    for file, row in loaded["files"].items():
        if not isinstance(file, str) or not isinstance(row, dict):
            raise ValueError(f"{path}: malformed files row {file!r}")
        counts: dict[str, int] = {}
        for column, count in row.items():
            if column not in SOFT_MAPPING_FORMS and column != BOUNDARY_COLUMN:
                raise ValueError(f"{path}: {file}: unknown census column {column!r}")
            if type(count) is not int or count < 0:
                raise ValueError(f"{path}: {file}: {column} count must be a non-negative int, got {count!r}")
            counts[column] = count
        table[file] = counts
    totals: dict[str, int] = {}
    for column, count in loaded["totals"].items():
        if not isinstance(column, str) or type(count) is not int:
            raise ValueError(f"{path}: malformed totals entry {column!r}")
        totals[column] = count
    return table, totals


def check_soft_mapping_census(
    src_dir: Path,
    census_path: Path,
    alias_index: DictAliasIndex | None,
    *,
    write: bool,
) -> CensusReport:
    """Build the live census, pin it when asked, otherwise compare it against the pin."""
    sites = build_census(src_dir, alias_index)
    live = tabulate_census(sites)
    totals = census_totals(live)
    score = f"{totals['soft']} soft occurrences across {len(live)} files, {totals[BOUNDARY_COLUMN]} boundary-parsed"

    if write:
        write_census(census_path, live)
        return CensusReport(True, (f"✅ Soft-mapping census pinned to {census_path}: {score}",), totals, ())

    if not census_path.exists():
        return CensusReport(
            False,
            (
                f"❌ Soft-mapping census pin missing: {census_path}",
                f"   Live tree: {score}",
                "   Fix: python scripts/check_contracts.py --write-census, and commit the file",
            ),
            totals,
            (),
        )

    pinned, stored_totals = load_census(census_path)
    drifts = compare_census(pinned, stored_totals, live)
    if not drifts:
        return CensusReport(True, (f"✅ Soft-mapping census matches {census_path}: {score}",), totals, ())

    pinned_soft = stored_totals.get("soft", 0)
    lines = [
        "❌ Soft-mapping census drift (the live tree disagrees with the pin):\n",
        f"  soft total pinned {pinned_soft} -> live {totals['soft']} "
        f"({'progress' if totals['soft'] < pinned_soft else 'REGRESSION' if totals['soft'] > pinned_soft else 'no change — a swap between forms is not a retirement'})\n",
    ]
    by_key: dict[tuple[str, str], list[CensusSite]] = {}
    for site in sites:
        by_key.setdefault((site.file, BOUNDARY_COLUMN if site.boundary else site.form), []).append(site)
    for drift in drifts:
        lines.append(f"  {drift.file}: {drift.form} pinned {drift.pinned} -> live {drift.live}")
        for site in by_key.get((drift.file, drift.form), [])[:20]:
            lines.append(f"    line {site.line}: {site.context} {site.position}")
    lines.append("")
    lines.append("    Fix: convert the site to an owned type (or parse it at a @trust_boundary), then")
    lines.append("    re-pin with `python scripts/check_contracts.py --write-census` in the same commit.")
    lines.append("    Rewriting one soft form as another does not retire it and will not pass.\n")
    return CensusReport(False, tuple(lines), totals, tuple(drifts))


def main() -> int:
    """Run the contracts enforcement check."""
    parser = argparse.ArgumentParser(description="Check that cross-boundary types are in contracts/ and whitelist entries are valid")
    parser.add_argument(
        "--no-fail-on-stale",
        action="store_true",
        help="Don't fail on stale whitelist entries (just warn)",
    )
    parser.add_argument(
        "--write-census",
        action="store_true",
        help="Re-pin config/cicd/soft-mapping-census.yaml from the live tree instead of comparing against it",
    )
    args = parser.parse_args()

    src_dir = Path("src/elspeth")
    contracts_dir = src_dir / "contracts"
    whitelist_path = Path("config/cicd/contracts-whitelist.yaml")
    census_path = Path("config/cicd/soft-mapping-census.yaml")

    whitelist, all_entries = load_whitelist(whitelist_path)
    violations: list[Violation] = []
    dict_violations: list[DictViolation] = []
    matched_dict_patterns: dict[str, bool] = dict.fromkeys(whitelist["dicts"], False)
    matched_type_patterns: set[str] = set()

    # Build import index once (O(files) instead of O(files x types))
    import_index = ImportIndex.build(src_dir)
    # Module-level `X = dict[str, Any]` aliases, so a name bound to the pattern is
    # scanned like the pattern itself rather than walked past.
    alias_index = DictAliasIndex.build(src_dir)

    # Scan all Python files outside contracts/
    for py_file in src_dir.rglob("*.py"):
        if contracts_dir in py_file.parents or py_file.parent == contracts_dir:
            continue  # Skip contracts/ itself

        # Check for type definitions
        definitions = find_type_definitions(py_file)
        for type_name, line_no, kind in definitions:
            qualified_name = f"{py_file.relative_to(src_dir).with_suffix('')}:{type_name}"

            if qualified_name in whitelist["types"]:
                matched_type_patterns.add(qualified_name)
                continue

            # Check if used across module boundaries (O(1) lookup via index)
            usages = import_index.find_cross_boundary_usages(src_dir, type_name, py_file)
            if usages:
                violations.append(
                    Violation(
                        file=str(py_file),
                        line=line_no,
                        type_name=type_name,
                        kind=kind,
                        used_in=[str(u) for u in usages[:3]],  # First 3
                    )
                )

        # Check for dict[str, Any] patterns
        dict_violations.extend(
            find_dict_violations(py_file, whitelist["dicts"], matched_dict_patterns, alias_index.names_in_scope(py_file, src_dir))
        )

    # Find stale whitelist entries
    stale_entries = find_stale_entries(all_entries, matched_dict_patterns, matched_type_patterns, src_dir, alias_index)

    # Check Settings → Runtime alignment
    config_path = src_dir / "core" / "config.py"
    runtime_path = contracts_dir / "config" / "runtime.py"
    settings_violations = check_settings_alignment(config_path)

    # Check from_settings() field coverage
    coverage_violations = check_from_settings_coverage(config_path, runtime_path)

    # Check from_settings() field name mappings match FIELD_MAPPINGS
    mapping_violations = check_field_name_mappings(runtime_path)

    # Check hardcoded literals in from_settings() are documented in INTERNAL_DEFAULTS
    hardcode_violations = check_hardcode_documentation(runtime_path)

    # Soft-mapping census: every form, every position, pinned per file
    census = check_soft_mapping_census(src_dir, census_path, alias_index, write=args.write_census)

    has_violations = False
    has_stale = False

    if violations:
        has_violations = True
        print("❌ Type definition violations found:\n")
        for v in violations:
            print(f"  {v.file}:{v.line}: {v.kind} '{v.type_name}'")
            print(f"    Used in: {', '.join(v.used_in)}")
            fix_msg = "    Fix: Move to src/elspeth/contracts/ or add to config/cicd/contracts-whitelist.yaml\n"
            print(fix_msg)

    if dict_violations:
        has_violations = True
        print("❌ dict[str, Any] violations found:\n")
        for dv in dict_violations:
            print(f"  {dv.file}:{dv.line}: {dv.context} - {dv.param_name}")
            print("    Fix: Use TypedDict/dataclass or add to allowed_dict_patterns\n")

    if settings_violations:
        has_violations = True
        print("❌ Orphaned Settings classes found:\n")
        print("  (Settings classes without Runtime counterparts)\n")
        for sv in settings_violations:
            print(f"  {sv.file}:{sv.line}: {sv.class_name}")
            print("    Fix: Add to SETTINGS_TO_RUNTIME mapping in contracts/config/alignment.py")
            print("    Or add to EXEMPT_SETTINGS if no Runtime counterpart is needed\n")

    if coverage_violations:
        has_violations = True
        print("❌ Settings field coverage violations found:\n")
        print("  (Settings fields not accessed in from_settings() methods)\n")
        for cv in coverage_violations:
            print(f"  {cv.settings_class}.{cv.orphaned_field} not accessed in {cv.runtime_class}.from_settings()")
            print("    Fix: Access the field in from_settings() and map it to a Runtime field")
            print("    Or document why the field is unused\n")

    if mapping_violations:
        has_violations = True
        print("❌ Field mapping violations found:\n")
        print("  (Settings fields mapped to wrong Runtime fields - misrouted)\n")
        for mv in mapping_violations:
            print(f"  {mv.runtime_class}: {mv.runtime_field}=settings.{mv.settings_field}")
            print(f"    Expected: {mv.runtime_field}=settings.{mv.expected_settings_field}")
            print(f"    Fix: Update from_settings() to use settings.{mv.expected_settings_field}")
            print("    Or update FIELD_MAPPINGS in contracts/config/alignment.py\n")

    if hardcode_violations:
        has_violations = True
        print("❌ Undocumented hardcoded values in from_settings() found:\n")
        print("  (Literal values in from_settings() must be documented in INTERNAL_DEFAULTS)\n")
        for hv in hardcode_violations:
            print(f"  {hv.runtime_class}.{hv.runtime_field} = {hv.literal_value}")
            print(f"    Subsystem: {hv.subsystem}")
            print(f"    Fix: Add to INTERNAL_DEFAULTS['{hv.subsystem}']['{hv.runtime_field}']")
            print("    in contracts/config/defaults.py\n")

    if stale_entries:
        has_stale = True
        print("❌ Stale whitelist entries found:\n")
        print("  (These entries don't match any code - remove them from the whitelist)\n")
        for se in stale_entries:
            print(f"  [{se.category}] {se.entry}")
            print(f"    Reason: {se.reason}\n")

    if not census.ok:
        has_violations = True
        for line in census.lines:
            print(line)

    if has_violations:
        return 1

    if has_stale:
        if args.no_fail_on_stale:
            print("⚠️  Stale entries found but --no-fail-on-stale was specified")
        else:
            print("❌ Stale whitelist entries cause check failure")
            print("   Use --no-fail-on-stale to warn instead of fail")
            return 1

    print("✅ All cross-boundary types are properly centralized in contracts/")
    if not stale_entries:
        print("✅ All whitelist entries are valid")
    print("✅ All Settings classes have Runtime counterparts or are exempt")
    print("✅ All Settings fields are accessed in from_settings() methods")
    print("✅ All field name mappings match FIELD_MAPPINGS")
    print("✅ All hardcoded values are documented in INTERNAL_DEFAULTS")
    for line in census.lines:
        print(line)
    return 0


if __name__ == "__main__":
    sys.exit(main())
