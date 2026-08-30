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
    iter_python_paths,
    load_class_allowlist,
)

# ADR-010 requires nominal inheritance from THIS class specifically; a base merely
# named ``AuditEvidenceBase`` (a local or wrongly-imported class) does not satisfy it.
_CANONICAL_AUDIT_EVIDENCE_MODULE = "elspeth.contracts.audit_evidence"
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


# A class is identified by the display path of its defining file plus its name.
_ClassKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class _ClassRecord:
    node: ast.ClassDef
    key: _ClassKey
    defines_to_audit_dict: bool


@dataclass(frozen=True, slots=True)
class _FileIndex:
    """Everything AEN1 needs to know about one module, extracted once."""

    file_path: str
    in_canonical_module: bool
    # Local names bound to the canonical AuditEvidenceBase, and aliases bound
    # to its module (``import elspeth.contracts.audit_evidence as M``).
    base_bindings: tuple[frozenset[str], frozenset[str]]
    # ``from elspeth.x.y import Name [as local]`` → local: (module suffix, Name)
    from_imports: dict[str, tuple[str, str]]
    # ``import elspeth.x.y as m`` → m: module suffix
    module_aliases: dict[str, str]
    classes: tuple[_ClassRecord, ...]


def _index_file(tree: ast.AST, file_path: str) -> _FileIndex:
    from_imports: dict[str, tuple[str, str]] = {}
    module_aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            suffix = _module_suffix(node.module)
            if suffix is None:
                continue
            for alias in node.names:
                from_imports[alias.asname or alias.name] = (suffix, alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                suffix = _module_suffix(alias.name)
                if suffix is not None and alias.asname:
                    module_aliases[alias.asname] = suffix
    classes = tuple(
        _ClassRecord(node=node, key=(file_path, node.name), defines_to_audit_dict=_class_defines_to_audit_dict(node))
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef)
    )
    return _FileIndex(
        file_path=file_path,
        in_canonical_module=file_path.endswith(_CANONICAL_MODULE_SUFFIX),
        base_bindings=_audit_evidence_base_bindings(tree),
        from_imports=from_imports,
        module_aliases=module_aliases,
        classes=classes,
    )


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
        _finding(index.file_path, record.node)
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
            if _bases_include_audit_evidence_base(record.node.bases, index.base_bindings, in_canonical_module=index.in_canonical_module):
                evidence.add(record.key)
    changed = True
    while changed:
        changed = False
        for index in indexes:
            for record in index.classes:
                if record.key in evidence:
                    continue
                if any(_resolve_base(base, index, files_by_suffix) in evidence for base in record.node.bases):
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


def _resolve_base(base: ast.expr, index: _FileIndex, files_by_suffix: dict[str, str | None]) -> _ClassKey | None:
    """Resolve a base expression to the class it names, or None if unprovable."""
    if isinstance(base, ast.Name):
        imported = index.from_imports.get(base.id)
        if imported is not None:
            suffix, name = imported
            imported_file = files_by_suffix.get(suffix)
            return None if imported_file is None else (imported_file, name)
        return (index.file_path, base.id)
    if isinstance(base, ast.Attribute) and isinstance(base.value, ast.Name):
        alias_suffix = index.module_aliases.get(base.value.id)
        if alias_suffix is None:
            return None
        alias_file = files_by_suffix.get(alias_suffix)
        return None if alias_file is None else (alias_file, base.attr)
    return None


def _audit_evidence_base_bindings(tree: ast.AST) -> tuple[frozenset[str], frozenset[str]]:
    """Collect the local names / module aliases that resolve to the canonical base.

    Returns ``(local_names, module_aliases)`` where local_names are bound by
    ``from elspeth.contracts.audit_evidence import AuditEvidenceBase [as X]`` and
    module_aliases by ``import elspeth.contracts.audit_evidence as M``.
    """
    local_names: set[str] = set()
    module_aliases: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == _CANONICAL_AUDIT_EVIDENCE_MODULE:
            for alias in node.names:
                if alias.name == _AUDIT_EVIDENCE_BASE_NAME:
                    local_names.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _CANONICAL_AUDIT_EVIDENCE_MODULE and alias.asname:
                    module_aliases.add(alias.asname)
    return frozenset(local_names), frozenset(module_aliases)


def _bases_include_audit_evidence_base(
    bases: list[ast.expr],
    bindings: tuple[frozenset[str], frozenset[str]],
    *,
    in_canonical_module: bool,
) -> bool:
    """True only when a base resolves to the canonical AuditEvidenceBase.

    A base merely named ``AuditEvidenceBase`` does NOT count unless it was
    imported from the canonical module (or is the local definition inside the
    canonical module itself). This closes the spoofing bypass where a local
    ``class AuditEvidenceBase`` satisfied the nominal-inheritance gate.
    """
    local_names, module_aliases = bindings
    for base in bases:
        if isinstance(base, ast.Name):
            if base.id in local_names or (in_canonical_module and base.id == _AUDIT_EVIDENCE_BASE_NAME):
                return True
        elif (
            isinstance(base, ast.Attribute)
            and base.attr == _AUDIT_EVIDENCE_BASE_NAME
            and isinstance(base.value, ast.Name)
            and base.value.id in module_aliases
        ):
            return True
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


def _finding(file_path: str, node: ast.ClassDef) -> Finding:
    key = f"{file_path}:{LEGACY_RULE_ID}:{node.name}"
    return Finding(
        rule_id=LEGACY_RULE_ID,
        file_path=file_path,
        line=node.lineno,
        column=node.col_offset,
        message=f"{node.name} defines to_audit_dict without inheriting AuditEvidenceBase",
        fingerprint=key,
        severity=RULE_METADATA.severity,
        suggestion=SUGGESTION,
    )


RULE = AuditEvidenceNominalRule()
