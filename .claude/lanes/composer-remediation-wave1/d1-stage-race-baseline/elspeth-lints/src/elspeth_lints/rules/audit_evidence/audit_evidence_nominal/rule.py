"""AuditEvidenceBase nominal inheritance rule implementation."""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.ast_walker import (
    PythonFileReadError,
    PythonSyntaxError,
    parse_python_file,
)
from elspeth_lints.core.protocols import Finding, RuleContext, RuleMetadata, RuleScope
from elspeth_lints.rules.audit_evidence.audit_evidence_nominal.metadata import (
    LEGACY_RULE_ID,
    RULE_ID,
    RULE_METADATA,
    SUGGESTION,
)
from elspeth_lints.rules.audit_evidence.shared import (
    allowlist_path_for_root,
    class_allowlist_governance_findings_for_root,
    display_path,
    enclosing_names,
    iter_python_paths,
    load_class_allowlist,
    parent_map,
)

# ADR-010 requires nominal inheritance from THIS class specifically; a base merely
# named ``AuditEvidenceBase`` (a local or wrongly-imported class) does not satisfy it.
_AUDIT_EVIDENCE_BASE_NAME = "AuditEvidenceBase"
# The canonical module defines the base locally; a subclass there inherits it
# without an import, and that local reference IS the real base (cannot be spoofed
# — an attacker cannot make their file be the canonical module).
_CANONICAL_MODULE_SUFFIX = "contracts/audit_evidence.py"


_ELSPETH_PACKAGE = "elspeth"


@dataclass(frozen=True, slots=True)
class AuditEvidenceNominalRule:
    """Detect classes defining to_audit_dict without nominally inheriting AuditEvidenceBase."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.WHOLE_REPO
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[Finding]:
        """Analyze one syntax tree for tests, or scan a whole repository root."""
        if isinstance(tree, ast.Module) and tree.body and file_path.suffix == ".py":
            return scan_tree(tree, display_path(file_path, context.root))
        return scan_root(context.root, allowlist_dir_override=context.allowlist_dir_override)


def scan_root(root: Path, *, allowlist_dir_override: Path | None = None) -> list[Finding]:
    """Scan a root for AEN1 findings and apply the legacy allowlist."""
    allowlist_dir = (
        allowlist_dir_override if allowlist_dir_override is not None else allowlist_path_for_root(root, "enforce_audit_evidence_nominal")
    )
    allowlist = load_class_allowlist(allowlist_dir)
    indexes: list[_FileIndex] = []
    for path in iter_python_paths(root):
        parsed = parse_python_file(path)
        if isinstance(parsed, PythonSyntaxError):
            raise SyntaxError(f"{parsed.path}:{parsed.line}:{parsed.column}: {parsed.message}")
        if isinstance(parsed, PythonFileReadError):
            # Mirror the syntax-error policy above: this scanner already
            # filtered candidates, so a read error indicates a race
            # between enumeration and parse — be loud, don't paper over.
            raise OSError(f"{parsed.path}: {parsed.message}")
        indexes.append(_index_file(parsed.tree, display_path(parsed.path, root)))
    findings = _findings_for_indexes(indexes)
    active = [finding for finding in findings if allowlist.match_key(finding.fingerprint) is None]
    return [
        *active,
        *class_allowlist_governance_findings_for_root(
            allowlist,
            allowlist_dir,
            root=root,
            allowlist_dir_override=allowlist_dir_override,
        ),
    ]


def scan_tree(tree: ast.AST, file_path: str) -> list[Finding]:
    """Return AEN1 findings for one parsed syntax tree.

    Single-file mode resolves chains defined inside the file; a base imported
    from another module cannot be proven here and is treated as unresolved.
    """
    return _findings_for_indexes([_index_file(tree, file_path)])


# A class is identified by the display path of its defining file plus its
# lexical path. A simple name is not unique: top-level and nested classes may
# legitimately share one without sharing inheritance.
_ScopePath = tuple[str, ...]
_ClassKey = tuple[str, _ScopePath]


@dataclass(frozen=True, slots=True)
class _ClassRecord:
    node: ast.ClassDef
    key: _ClassKey
    lookup_prefixes: tuple[_ScopePath, ...]
    defines_to_audit_dict: bool


@dataclass(frozen=True, slots=True)
class _NameResolution:
    """The first tracked binding for one name along a class header's lookup path."""

    class_key: _ClassKey | None = None
    imported_class: tuple[str, str] | None = None
    module_alias: str | None = None


@dataclass(frozen=True, slots=True)
class _FileIndex:
    """Everything AEN1 needs to know about one module, extracted once."""

    file_path: str
    in_canonical_module: bool
    # execution namespace → local import name → (module suffix, imported name)
    from_imports: dict[_ScopePath, dict[str, tuple[str, str]]]
    # execution namespace → module alias → module suffix
    module_aliases: dict[_ScopePath, dict[str, str]]
    classes: tuple[_ClassRecord, ...]


def _index_file(tree: ast.AST, file_path: str) -> _FileIndex:
    parents = parent_map(tree)
    from_imports: dict[_ScopePath, dict[str, tuple[str, str]]] = {}
    module_aliases: dict[_ScopePath, dict[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            suffix = _module_suffix(node.module)
            if suffix is None:
                continue
            scope = enclosing_names(node, parents)
            scoped_imports = from_imports.setdefault(scope, {})
            for alias in node.names:
                scoped_imports[alias.asname or alias.name] = (suffix, alias.name)
        elif isinstance(node, ast.Import):
            scope = enclosing_names(node, parents)
            scoped_aliases = module_aliases.setdefault(scope, {})
            for alias in node.names:
                suffix = _module_suffix(alias.name)
                if suffix is not None and alias.asname:
                    scoped_aliases[alias.asname] = suffix
    classes: list[_ClassRecord] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            lookup_prefixes = _class_base_lookup_prefixes(node, parents)
            classes.append(
                _ClassRecord(
                    node=node,
                    key=(file_path, (*lookup_prefixes[0], node.name)),
                    lookup_prefixes=lookup_prefixes,
                    defines_to_audit_dict=_class_defines_to_audit_dict(node),
                )
            )
    return _FileIndex(
        file_path=file_path,
        in_canonical_module=file_path.endswith(_CANONICAL_MODULE_SUFFIX),
        from_imports=from_imports,
        module_aliases=module_aliases,
        classes=tuple(classes),
    )


def _class_base_lookup_prefixes(node: ast.ClassDef, parents: dict[int, ast.AST]) -> tuple[_ScopePath, ...]:
    """Return execution namespaces searched while evaluating ``node``'s bases.

    The immediately containing class or function namespace is visible. Beyond
    it, Python closes over function scopes but never enclosing class scopes,
    before finally consulting the module namespace.
    """
    definitions: list[ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef] = []
    current = parents.get(id(node))
    while current is not None:
        if isinstance(current, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            definitions.append(current)
        current = parents.get(id(current))
    definitions.reverse()

    current_namespace = tuple(definition.name for definition in definitions)
    prefixes: list[_ScopePath] = [current_namespace]
    for index in range(len(definitions) - 1, -1, -1):
        if not isinstance(definitions[index], (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        function_namespace = tuple(definition.name for definition in definitions[: index + 1])
        if function_namespace not in prefixes:
            prefixes.append(function_namespace)
    if () not in prefixes:
        prefixes.append(())
    return tuple(prefixes)


def _module_suffix(module: str | None) -> str | None:
    """Map ``elspeth.a.b`` to the path suffix ``a/b.py`` (None outside the package).

    Mirrors ``_CANONICAL_MODULE_SUFFIX``: the scan root is normally
    ``src/elspeth`` and display paths are root-relative, so the package name is
    dropped and the remainder is matched as a path suffix.
    """
    if module is None:
        return None
    parts = module.split(".")
    if parts[0] != _ELSPETH_PACKAGE or len(parts) < 2:
        return None
    return "/".join(parts[1:]) + ".py"


def _findings_for_indexes(indexes: list[_FileIndex]) -> list[Finding]:
    evidence = _resolve_evidence_classes(indexes)
    return [
        _finding(index.file_path, record.node, record.key[1])
        for index in indexes
        for record in index.classes
        if record.defines_to_audit_dict and record.key not in evidence
    ]


def _resolve_evidence_classes(indexes: list[_FileIndex]) -> set[_ClassKey]:
    """Fixed point: classes provably descended from the canonical base.

    Seed with classes whose own bases resolve to the canonical
    ``AuditEvidenceBase`` by import provenance, then repeatedly admit classes
    with a base that resolves — locally, or through an ``elspeth`` import
    that maps to exactly one scanned module — to an admitted class. A base
    that cannot be resolved this way contributes nothing, so an unknown,
    unscanned, or ambiguous parent leaves its child unproven (fail closed).
    """
    files_by_suffix = _files_by_module_suffix(indexes)
    evidence: set[_ClassKey] = set()
    for index in indexes:
        for record in index.classes:
            if any(_base_is_canonical(base, record, index) for base in record.node.bases):
                evidence.add(record.key)
    changed = True
    while changed:
        changed = False
        for index in indexes:
            for record in index.classes:
                if record.key in evidence:
                    continue
                if any(_resolve_base(base, record, index, files_by_suffix) in evidence for base in record.node.bases):
                    evidence.add(record.key)
                    changed = True
    return evidence


def _files_by_module_suffix(indexes: list[_FileIndex]) -> dict[str, str | None]:
    """Map each ``a/b.py`` suffix to the one scanned file ending with it.

    A suffix matched by more than one scanned file maps to ``None`` so that
    resolution refuses to guess between them.
    """
    files_by_suffix: dict[str, str | None] = {}
    for index in indexes:
        for suffix in _suffixes_of(index.file_path):
            files_by_suffix[suffix] = None if suffix in files_by_suffix else index.file_path
    return files_by_suffix


def _suffixes_of(file_path: str) -> list[str]:
    parts = file_path.split("/")
    return ["/".join(parts[i:]) for i in range(len(parts))]


def _resolve_base(
    base: ast.expr,
    record: _ClassRecord,
    index: _FileIndex,
    files_by_suffix: dict[str, str | None],
) -> _ClassKey | None:
    """Resolve a base expression to the class it names, or None if unprovable."""
    if isinstance(base, ast.Name):
        resolution = _resolve_name(base.id, record, index)
        if resolution.class_key is not None:
            return resolution.class_key
        if resolution.imported_class is not None:
            suffix, name = resolution.imported_class
            imported_file = files_by_suffix.get(suffix)
            return None if imported_file is None else (imported_file, (name,))
        return None
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        alias_suffix = _resolve_name(base.value.id, record, index).module_alias
        if alias_suffix is None:
            return None
        alias_file = files_by_suffix.get(alias_suffix)
        return None if alias_file is None else (alias_file, (base.attr,))
    return None


def _resolve_name(name: str, record: _ClassRecord, index: _FileIndex) -> _NameResolution:
    """Resolve the first tracked binding for ``name`` along Python's lookup path.

    A class definition in the current namespace shadows an import with the same
    local name. Multiple class definitions at the first matching namespace are
    ambiguous and resolve to nothing, so inheritance remains fail closed.
    """
    for prefix in record.lookup_prefixes:
        local_key = (index.file_path, (*prefix, name))
        matching_count = sum(candidate.key == local_key for candidate in index.classes)
        if matching_count == 1:
            return _NameResolution(class_key=local_key)
        if matching_count > 1:
            return _NameResolution()
        imported_class = index.from_imports.get(prefix, {}).get(name)
        if imported_class is not None:
            return _NameResolution(imported_class=imported_class)
        module_alias = index.module_aliases.get(prefix, {}).get(name)
        if module_alias is not None:
            return _NameResolution(module_alias=module_alias)
    return _NameResolution()


def _base_is_canonical(base: ast.expr, record: _ClassRecord, index: _FileIndex) -> bool:
    """Return whether ``base`` resolves to the canonical AuditEvidenceBase."""
    if isinstance(base, ast.Name):
        resolution = _resolve_name(base.id, record, index)
        if resolution.class_key is not None:
            return index.in_canonical_module and resolution.class_key == (
                index.file_path,
                (_AUDIT_EVIDENCE_BASE_NAME,),
            )
        return resolution.imported_class == (_CANONICAL_MODULE_SUFFIX, _AUDIT_EVIDENCE_BASE_NAME)
    if isinstance(base, ast.Attribute) and base.attr == _AUDIT_EVIDENCE_BASE_NAME and isinstance(base.value, ast.Name):
        return _resolve_name(base.value.id, record, index).module_alias == _CANONICAL_MODULE_SUFFIX
    return False


def _class_defines_to_audit_dict(class_node: ast.ClassDef) -> bool:
    for node in class_node.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == "to_audit_dict":
            return True
        if isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and target.id == "to_audit_dict":
                    return True
        # Annotated assignment WITH a value defines a real descriptor at runtime
        # (`to_audit_dict: object = lambda ...`); a bare annotation defines nothing.
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "to_audit_dict"
            and node.value is not None
        ):
            return True
    return False


def _finding(file_path: str, node: ast.ClassDef, qualified_name: tuple[str, ...]) -> Finding:
    display_name = ".".join(qualified_name)
    key = f"{file_path}:{LEGACY_RULE_ID}:{display_name}"
    return Finding(
        rule_id=LEGACY_RULE_ID,
        file_path=file_path,
        line=node.lineno,
        column=node.col_offset,
        message=f"{display_name} defines to_audit_dict without inheriting AuditEvidenceBase",
        fingerprint=key,
        severity=RULE_METADATA.severity,
        suggestion=SUGGESTION,
    )


RULE = AuditEvidenceNominalRule()
