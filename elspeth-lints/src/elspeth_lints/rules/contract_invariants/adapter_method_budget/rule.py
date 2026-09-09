"""Adapter facade method-budget rule implementation.

Port of ``scripts/cicd/enforce_adapter_budget.py`` (elspeth-3575a7f15d). The
legacy script imported ``PluginAuditWriterAdapter`` and counted
``inspect.isfunction`` members; this rule counts distinct public ``def`` names
in the class body via AST instead. The two agree for the adapter as written
(no inheritance, no descriptors, no dynamic members). Where they could
diverge, this rule is deliberately stricter: any public callable-shaped
member declared in the class body counts as adapter surface, and a missing
file or class is an ERROR finding rather than a crash.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

from elspeth_lints.core.ast_walker import PythonFileReadError, PythonSyntaxError, parse_python_file
from elspeth_lints.core.protocols import Finding, RuleContext, RuleMetadata, RuleScope
from elspeth_lints.rules.contract_invariants.adapter_method_budget.metadata import (
    FAIL_CLOSED_SUGGESTION,
    GROWTH_SUGGESTION,
    RULE_ID,
    RULE_METADATA,
    SLACK_SUGGESTION,
)

# Ratchet, not budget: the ceiling tracks the current method count exactly.
# Growth fails; slack also fails, with instructions to lower the ratchet so
# every reduction is locked in.
RATCHET = 13
ADAPTER_CLASS_NAME = "PluginAuditWriterAdapter"
ADAPTER_RELATIVE_PATH = Path("src/elspeth/core/landscape/plugin_audit_writer.py")


@dataclass(frozen=True, slots=True)
class AdapterMethodBudgetRule:
    """Fail when the plugin audit writer adapter's public surface drifts off the ratchet."""

    id: str = RULE_ID
    scope: RuleScope = RuleScope.WHOLE_REPO
    metadata: RuleMetadata = RULE_METADATA

    def analyze(self, tree: ast.AST, file_path: Path, context: RuleContext) -> list[Finding]:
        """Verify the adapter's public method count against the ratchet."""
        del tree
        repo_root = _repository_root(file_path, context.repo_root)
        adapter_file = repo_root / ADAPTER_RELATIVE_PATH
        display_path = ADAPTER_RELATIVE_PATH.as_posix()

        if not adapter_file.is_file():
            return [
                _fail_closed_finding(
                    display_path,
                    message=f"{display_path} not found; the adapter method budget cannot be verified.",
                    fingerprint="adapter-file-missing",
                )
            ]

        parsed = parse_python_file(adapter_file)
        if isinstance(parsed, PythonSyntaxError):
            return [
                _fail_closed_finding(
                    display_path,
                    message=f"{display_path} has a syntax error; the adapter method budget cannot be verified: {parsed.message}",
                    fingerprint="adapter-unparseable",
                )
            ]
        if isinstance(parsed, PythonFileReadError):
            return [
                _fail_closed_finding(
                    display_path,
                    message=(
                        f"{display_path} could not be read ({parsed.error_type}); "
                        f"the adapter method budget cannot be verified: {parsed.message}"
                    ),
                    fingerprint="adapter-unreadable",
                )
            ]

        adapter_class = _last_module_level_class(parsed.tree, ADAPTER_CLASS_NAME)
        if adapter_class is None:
            return [
                _fail_closed_finding(
                    display_path,
                    message=f"{ADAPTER_CLASS_NAME} class not found in {display_path}; the adapter method budget cannot be verified.",
                    fingerprint="adapter-class-missing",
                )
            ]

        public_methods = _public_method_names(adapter_class)
        count = len(public_methods)
        if count > RATCHET:
            return [
                Finding(
                    rule_id=RULE_ID,
                    file_path=display_path,
                    line=adapter_class.lineno,
                    column=adapter_class.col_offset,
                    message=(
                        f"{ADAPTER_CLASS_NAME} has {count} public methods (ratchet: {RATCHET}). "
                        f"Methods: {', '.join(sorted(public_methods))}"
                    ),
                    fingerprint=f"budget-growth:{count}",
                    suggestion=GROWTH_SUGGESTION,
                    symbol_context=(ADAPTER_CLASS_NAME,),
                )
            ]
        if count < RATCHET:
            return [
                Finding(
                    rule_id=RULE_ID,
                    file_path=display_path,
                    line=adapter_class.lineno,
                    column=adapter_class.col_offset,
                    message=(f"Ratchet has slack: {ADAPTER_CLASS_NAME} has {count} public methods but the ratchet allows {RATCHET}."),
                    fingerprint=f"ratchet-slack:{count}",
                    suggestion=SLACK_SUGGESTION,
                    symbol_context=(ADAPTER_CLASS_NAME,),
                )
            ]
        return []


def _repository_root(root: Path, explicit_repo_root: Path | None) -> Path:
    """Return the repository root for a scan root.

    Mirrors ``trust_boundary.shared.repository_root``: an explicit
    ``RuleContext.repo_root`` wins; the canonical ``<repo>/src/elspeth`` scan
    root resolves to ``<repo>``; any other scan root is its own repository
    root (fixture case directories rely on this).
    """
    if explicit_repo_root is not None:
        return explicit_repo_root
    if root.name == "elspeth" and root.parent.name == "src":
        return root.parent.parent
    return root


def _last_module_level_class(tree: ast.Module, class_name: str) -> ast.ClassDef | None:
    """Return the last module-level class binding for ``class_name``.

    The last binding wins to match import semantics if the class were ever
    conditionally redefined.
    """
    found: ast.ClassDef | None = None
    for node in tree.body:
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            found = node
    return found


def _public_method_names(class_node: ast.ClassDef) -> frozenset[str]:
    """Return distinct public method names declared directly in the class body.

    Decorated defs (``@staticmethod``, ``@property``, ...) count: any public
    callable-shaped member is adapter surface. Names are deduplicated so
    ``@overload`` stacks or redefinitions count once, matching runtime.
    """
    return frozenset(
        stmt.name for stmt in class_node.body if isinstance(stmt, ast.FunctionDef | ast.AsyncFunctionDef) and not stmt.name.startswith("_")
    )


def _fail_closed_finding(display_path: str, *, message: str, fingerprint: str) -> Finding:
    return Finding(
        rule_id=RULE_ID,
        file_path=display_path,
        line=1,
        column=0,
        message=message,
        fingerprint=fingerprint,
        suggestion=FAIL_CLOSED_SUGGESTION,
    )


RULE = AdapterMethodBudgetRule()
