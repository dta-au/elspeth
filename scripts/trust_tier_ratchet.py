#!/usr/bin/env python3
"""Pre-commit ratchet for the trust-tier (``trust_tier.tier_model``) lint corpus.

``elspeth-lints check`` is deliberately fail-closed: it exits 1 whenever the
standing finding corpus is non-empty, and that corpus stays non-empty until the
operator signs the package (AGENTS.md, "Judge-signature stage"). Used directly
as a pre-commit entry it therefore refuses every commit that touches its
trigger paths, including commits that strictly shrink the corpus. This script
is the hook's entry instead. It runs the same rule with the same arguments and
environment twice -- over the tree pre-commit is about to commit and over
``git archive HEAD`` -- and fails when the commit adds an unreviewed finding.
Four temporary, source-sealed admissions cover the two raw R6 catches that
were hidden by HEAD's service.py per-file rule, one R5 CLI finding moved
from service.py to turn_audit.py, and a same-site R6 catch narrowed from
ValueError to InterpretationResolveError. The earlier two-site R5 helper relocation
still requires exact definition and binding parity. The CLI gate itself is
untouched and CI still runs it fail-closed.

Findings are compared as a multiset of line-insensitive keys
``(path, rule id, message, severity)``. Line and column numbers move under unrelated
edits and never count as new; a second occurrence of an identical key does.
Only findings the CLI would fail on take part (every severity except ``note``):
the ``R_TB_SUPPRESSED`` notes that record each ``@trust_boundary`` suppression
appear in the counts but never gate.

The HEAD run extracts ``git archive HEAD`` into a temporary directory and puts
that copy's ``elspeth-lints/src`` on ``PYTHONPATH``, so it sees HEAD's rule
code, HEAD's ``src/elspeth`` and HEAD's allowlists together; the import path is
verified before the rule runs. The working-tree side is whatever is on disk:
the repo-local dispatcher runs pre-commit without stashing, so unstaged edits
are visible here exactly as they are to every sibling whole-repo hook.

Project tooling, not product: no signatures and no key handling. The verify
mode is pinned to ``shape-only-when-key-missing`` for both runs, exactly as the
previous hook entry pinned it, and the operator's HMAC key, when present in the
shell, reaches the rule untouched.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import io
import json
import os
import subprocess
import sys
import tarfile
import tempfile
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

RULE_ID = "trust_tier.tier_model"
SCAN_ROOT = "src/elspeth"
LINTS_SOURCE_ROOT = "elspeth-lints/src"
VERIFY_MODE_ENV = "ELSPETH_JUDGE_METADATA_SIGNATURE_VERIFY_MODE"
VERIFY_MODE = "shape-only-when-key-missing"
NON_GATING_SEVERITY = "note"

FindingKey = tuple[str, str, str, str]

# Reviewed relocation from ComposerServiceImpl's module to the preflight owner.
# A candidate still needs exact source, binding, and finding proof below.
_BLOB_REF_RELOCATION = (
    "web/composer/service.py",
    "web/composer/composer_preflight.py",
    "_contains_blob_ref",
)
_BLOB_REF_MESSAGES = frozenset(
    {
        "isinstance() used: if isinstance(value, Mapping):",
        "isinstance() used: if isinstance(value, (tuple, list)):",
    }
)
_BLOB_REF_GLOBALS = frozenset({"Mapping", "isinstance", "any", "tuple", "list", "object", "bool", "_contains_blob_ref"})
_DYNAMIC_ENV_CALLS = frozenset({"exec", "eval", "globals", "locals", "vars"})

# Temporary, package-local admissions for two raw R6 findings previously covered
# by HEAD's service.py per-file rule. Remove after the package commit makes the
# destination findings part of HEAD; the global CLI remains fail-closed.
_LEGACY_R6_HEAD = "a11977d79509b31f374e8fcdbcd1d83574a27acf"
_LEGACY_R6_STAGED_MANIFEST_SHA256 = "95532d5a33344ba981657a6401162f0dcc10d299fce601e4aa84503b002a0b77"
_SERIALIZE_R5_MESSAGE = "isinstance() used: if isinstance(response, Mapping):"
_TIGHTENED_R6_PATH = "web/composer/interpretation_surfacing.py"
_TIGHTENED_R6_CONTEXT = ("_surface_pending_interpretation_reviews_under_writer",)
_TIGHTENED_R6_OLD_MESSAGE = "Exception swallowed without re-raise or explicit error: except ValueError:"
_TIGHTENED_R6_NEW_MESSAGE = "Exception swallowed without re-raise or explicit error: except InterpretationResolveError:"
_R6_MOVES = (
    (
        "web/composer/service.py",
        "web/composer/composer_preflight.py",
        "ComposerServiceImpl",
        "ComposerPreflight",
        "_runtime_preflight",
        "runtime_preflight",
        "BlobNotFoundError",
        "elspeth.contracts.blobs",
    ),
    (
        "web/composer/service.py",
        "web/composer/planning_application.py",
        "ComposerServiceImpl",
        "PlanningApplication",
        "_stage_pipeline_plan",
        "_stage_pipeline_plan",
        "ComposerRuntimePreflightError",
        "elspeth.web.composer.protocol",
    ),
)


class RatchetError(Exception):
    """The ratchet could not produce a verdict (usage or environment failure)."""


@dataclass(frozen=True, slots=True)
class FindingRecord:
    """One lint finding as the ratchet sees it: tree-relative path, no signature fields."""

    path: str
    rule_id: str
    message: str
    line: int
    column: int
    severity: str

    @property
    def key(self) -> FindingKey:
        """Line-insensitive identity that preserves rule, context, and severity."""
        return (self.path, self.rule_id, self.message, self.severity)

    @property
    def gates(self) -> bool:
        """Mirror the CLI's exit rule: every severity except ``note`` fails the gate."""
        return self.severity != NON_GATING_SEVERITY

    def render(self) -> str:
        """Same shape as the CLI's text emitter, so an added line is recognisable."""
        return f"{self.path}:{self.line}:{self.column}: {self.rule_id}: {self.message}"


@dataclass(frozen=True, slots=True)
class AddedKey:
    """A key whose staged multiplicity exceeds HEAD's, with every staged occurrence."""

    head_multiplicity: int
    staged_multiplicity: int
    occurrences: tuple[FindingRecord, ...]


@dataclass(frozen=True, slots=True)
class RawFindingRecord:
    """Unfiltered visitor observation, including its lexical owner."""

    finding: FindingRecord
    symbol_context: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class RatchetResult:
    """Outcome of comparing the staged corpus against HEAD's."""

    head_count: int
    staged_count: int
    head_notes: int
    staged_notes: int
    added: tuple[AddedKey, ...]
    removed_count: int
    relocated: tuple[FindingRecord, ...] = ()
    legacy_r6_transfers: tuple[FindingRecord, ...] = ()
    sealed_r5_relocations: tuple[FindingRecord, ...] = ()
    sealed_r6_tightenings: tuple[FindingRecord, ...] = ()
    raw_cli_added_count: int = 0

    @property
    def added_count(self) -> int:
        return sum(entry.staged_multiplicity - entry.head_multiplicity for entry in self.added)

    @property
    def exit_code(self) -> int:
        return 1 if self.added else 0


def _unique_definition(tree: ast.Module, name: str) -> ast.FunctionDef | None:
    matches = [node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name]
    return matches[0] if len(matches) == 1 else None


def _blob_ref_bindings(tree: ast.Module, definition: ast.FunctionDef) -> bool:
    """Prove the moved helper still resolves every nonlocal name identically."""
    imports = [
        alias
        for node in tree.body
        if isinstance(node, ast.ImportFrom) and node.module == "collections.abc" and node.level == 0
        for alias in node.names
        if alias.name == "Mapping" and alias.asname is None
    ]
    if len(imports) != 1:
        return False
    names = {node.id for node in ast.walk(definition) if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)}
    if names - {"value", "child"} != _BLOB_REF_GLOBALS:
        return False
    # The referenced globals may only be bound by the reviewed import, builtins, or
    # this very definition. Module rebinding would invalidate the proof.
    for node in tree.body:
        if node is definition:
            continue
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if node.name in _BLOB_REF_GLOBALS:
                return False
            if any(isinstance(child, ast.Global) and set(child.names) & _BLOB_REF_GLOBALS for child in ast.walk(node)):
                return False
            if any(
                isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in _DYNAMIC_ENV_CALLS
                for child in ast.walk(node)
            ):
                return False
            # Only the header of a definition executes in the enclosing
            # scope. Its body has a different binding scope.
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                evaluated: list[ast.AST] = [*node.decorator_list, node.args, *node.type_params]
                if node.returns is not None:
                    evaluated.append(node.returns)
            else:
                evaluated = [*node.decorator_list, *node.bases, *node.keywords, *node.type_params]
        else:
            evaluated = [node]
        for child in (descendant for expression in evaluated for descendant in ast.walk(expression)):
            if isinstance(child, ast.ImportFrom) and any(alias.name == "*" for alias in child.names):
                return False
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and child.name in _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, ast.Global) and set(child.names) & _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)) and child.id in _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, ast.ExceptHandler) and child.name in _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, (ast.MatchAs, ast.MatchStar)) and child.name in _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, ast.MatchMapping) and child.rest in _BLOB_REF_GLOBALS:
                return False
            if isinstance(child, ast.alias):
                bound = child.asname or child.name.split(".")[0]
                if bound in _BLOB_REF_GLOBALS and child is not imports[0]:
                    return False
            if isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id in _DYNAMIC_ENV_CALLS:
                return False
    return True


def _proven_blob_ref_relocation(head_root: Path, staged_root: Path, old: Sequence[FindingRecord], new: Sequence[FindingRecord]) -> bool:
    old_path, new_path, name = _BLOB_REF_RELOCATION
    if len(old) != 2 or len(new) != 2:
        return False
    if {f.message for f in old} != _BLOB_REF_MESSAGES or {f.message for f in new} != _BLOB_REF_MESSAGES:
        return False
    if {(f.rule_id, f.message, f.severity) for f in old} != {(f.rule_id, f.message, f.severity) for f in new}:
        return False
    if any(f.path != old_path or f.rule_id != "R5" for f in old):
        return False
    if any(f.path != new_path or f.rule_id != "R5" for f in new):
        return False
    try:
        old_tree = ast.parse((head_root / SCAN_ROOT / old_path).read_text(encoding="utf-8"))
        new_tree = ast.parse((staged_root / SCAN_ROOT / new_path).read_text(encoding="utf-8"))
    except (OSError, SyntaxError, UnicodeError):
        return False
    old_def = _unique_definition(old_tree, name)
    new_def = _unique_definition(new_tree, name)
    if old_def is None or new_def is None:
        return False
    if old_def.end_lineno is None or new_def.end_lineno is None:
        return False
    if any(not old_def.lineno <= f.line <= old_def.end_lineno for f in old):
        return False
    if any(not new_def.lineno <= f.line <= new_def.end_lineno for f in new):
        return False
    old_future = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
        for node in old_tree.body
    )
    new_future = any(
        isinstance(node, ast.ImportFrom) and node.module == "__future__" and any(alias.name == "annotations" for alias in node.names)
        for node in new_tree.body
    )
    return (
        old_future == new_future
        and ast.dump(old_def, include_attributes=False) == ast.dump(new_def, include_attributes=False)
        and _blob_ref_bindings(old_tree, old_def)
        and _blob_ref_bindings(new_tree, new_def)
    )


def _legacy_r6_allowance(head_root: Path) -> bool:
    try:
        document = yaml.safe_load((head_root / "config/cicd/enforce_tier_model/web.yaml").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError):
        return False
    if not isinstance(document, dict) or not isinstance(document.get("per_file_rules"), list):
        return False
    entries = [
        entry for entry in document["per_file_rules"] if isinstance(entry, dict) and entry.get("pattern") == "web/composer/service.py"
    ]
    return len(entries) == 1 and entries[0].get("rules") == ["R6"] and entries[0].get("max_hits") is None


def _reviewed_source_manifest_sha256(root: Path) -> str | None:
    """Seal this temporary transfer to one reviewed executable/lint/config tree."""
    groups = (
        (Path(SCAN_ROOT), "*.py"),
        (Path(LINTS_SOURCE_ROOT), "*.py"),
        (Path("config/cicd/enforce_tier_model"), "*.yaml"),
    )
    digest = hashlib.sha256()
    for directory, pattern in groups:
        base = root / directory
        if not base.is_dir():
            return None
        paths = sorted(base.rglob(pattern))
        if not paths:
            return None
        for path in paths:
            if not path.is_file() or path.is_symlink():
                return None
            try:
                content = path.read_bytes()
            except OSError:
                return None
            relative = path.relative_to(root).as_posix().encode()
            digest.update(len(relative).to_bytes(8, "big"))
            digest.update(relative)
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


def _effective_allowlist_is_sealed(root: Path) -> bool:
    """Reject local/ancestor allowlists that outrank the reviewed root config."""
    canonical = root / "config/cicd/enforce_tier_model"
    if not canonical.is_dir():
        return False
    for prefix in (root / SCAN_ROOT, root / "src"):
        if (prefix / "config/cicd/enforce_tier_model").exists():
            return False
        if (prefix / "config/cicd/enforce_tier_model.yaml").exists():
            return False
    return True


def compare_corpora(
    head: Sequence[FindingRecord],
    staged: Sequence[FindingRecord],
    *,
    head_root: Path | None = None,
    staged_root: Path | None = None,
    head_sha: str | None = None,
    head_raw: Sequence[RawFindingRecord] = (),
    staged_raw: Sequence[RawFindingRecord] = (),
) -> RatchetResult:
    """Staged gating findings must be a sub-multiset of HEAD's; anything in surplus is added."""
    head_gating = [finding for finding in head if finding.gates]
    staged_gating = [finding for finding in staged if finding.gates]
    head_keys = Counter(finding.key for finding in head_gating)
    staged_keys = Counter(finding.key for finding in staged_gating)
    surplus = staged_keys - head_keys
    deficit = head_keys - staged_keys
    raw_cli_added_count = sum(surplus.values())
    relocated: tuple[FindingRecord, ...] = ()
    if head_root is not None and staged_root is not None:
        old_path, new_path, _name = _BLOB_REF_RELOCATION
        old = [f for f in head_gating if f.path == old_path and f.rule_id == "R5" and f.message in _BLOB_REF_MESSAGES]
        new = [f for f in staged_gating if f.path == new_path and f.rule_id == "R5" and f.message in _BLOB_REF_MESSAGES]
        if (
            all(deficit[(f.path, f.rule_id, f.message, f.severity)] == 1 for f in old)
            and all(surplus[(f.path, f.rule_id, f.message, f.severity)] == 1 for f in new)
            and _proven_blob_ref_relocation(head_root, staged_root, old, new)
        ):
            for finding in old:
                deficit.subtract([finding.key])
            for finding in new:
                surplus.subtract([finding.key])
            relocated = tuple(new)
    legacy_r6_transfers: list[FindingRecord] = []
    sealed_r5_relocations: list[FindingRecord] = []
    sealed_r6_tightenings: list[FindingRecord] = []
    if (
        head_sha == _LEGACY_R6_HEAD
        and head_root is not None
        and staged_root is not None
        and _legacy_r6_allowance(head_root)
        and _reviewed_source_manifest_sha256(staged_root) == _LEGACY_R6_STAGED_MANIFEST_SHA256
        and _effective_allowlist_is_sealed(staged_root)
    ):
        for move in _R6_MOVES:
            old_path, new_path, old_owner, new_owner, old_name, new_name, exception, _module = move
            expected_context_old = (
                (old_owner, old_name, "_blob_get_metadata") if exception == "BlobNotFoundError" else (old_owner, old_name)
            )
            expected_context_new = (
                (new_owner, new_name, "_blob_get_metadata") if exception == "BlobNotFoundError" else (new_owner, new_name)
            )
            old_raw = [
                record
                for record in head_raw
                if record.finding.path == old_path and record.symbol_context == expected_context_old and record.finding.rule_id == "R6"
            ]
            new_raw = [
                record
                for record in staged_raw
                if record.finding.path == new_path and record.symbol_context == expected_context_new and record.finding.rule_id == "R6"
            ]
            new_cli = [
                finding
                for finding in staged_gating
                if finding.path == new_path
                and finding.rule_id == "R6"
                and finding.message == f"Exception swallowed without re-raise or explicit error: except {exception}:"
            ]
            if (
                len(old_raw) == len(new_raw) == len(new_cli) == 1
                and head_keys[old_raw[0].finding.key] == 0
                and head_keys[new_cli[0].key] == 0
                and surplus[new_cli[0].key] == 1
                and old_raw[0].finding.message == new_cli[0].message
                and old_raw[0].finding.severity == new_cli[0].severity
                and new_raw[0].finding == new_cli[0]
            ):
                surplus.subtract([new_cli[0].key])
                legacy_r6_transfers.append(new_cli[0])
        old_raw_r5 = [
            record
            for record in head_raw
            if record.finding.path == "web/composer/service.py"
            and record.finding.rule_id == "R5"
            and record.finding.message == _SERIALIZE_R5_MESSAGE
            and record.symbol_context == ("ComposerServiceImpl", "_serialize_response_via_walker")
        ]
        new_raw_r5 = [
            record
            for record in staged_raw
            if record.finding.path == "web/composer/turn_audit.py"
            and record.finding.rule_id == "R5"
            and record.finding.message == _SERIALIZE_R5_MESSAGE
            and record.symbol_context == ("_serialize_response_via_walker",)
        ]
        new_cli_r5 = [
            finding
            for finding in staged_gating
            if finding.path == "web/composer/turn_audit.py" and finding.rule_id == "R5" and finding.message == _SERIALIZE_R5_MESSAGE
        ]
        old_cli_r5 = [
            finding
            for finding in head_gating
            if finding.path == "web/composer/service.py" and finding.rule_id == "R5" and finding.message == _SERIALIZE_R5_MESSAGE
        ]
        if (
            len(old_raw_r5) == len(new_raw_r5) == len(old_cli_r5) == len(new_cli_r5) == 1
            and deficit[old_cli_r5[0].key] == 1
            and head_keys[new_cli_r5[0].key] == 0
            and surplus[new_cli_r5[0].key] == 1
            and old_raw_r5[0].finding == old_cli_r5[0]
            and old_cli_r5[0].severity == new_cli_r5[0].severity
            and new_raw_r5[0].finding == new_cli_r5[0]
        ):
            deficit.subtract([old_cli_r5[0].key])
            surplus.subtract([new_cli_r5[0].key])
            sealed_r5_relocations.append(new_cli_r5[0])
        old_raw_r6 = [
            record
            for record in head_raw
            if record.finding.path == _TIGHTENED_R6_PATH
            and record.finding.rule_id == "R6"
            and record.finding.message == _TIGHTENED_R6_OLD_MESSAGE
            and record.symbol_context == _TIGHTENED_R6_CONTEXT
        ]
        new_raw_r6 = [
            record
            for record in staged_raw
            if record.finding.path == _TIGHTENED_R6_PATH
            and record.finding.rule_id == "R6"
            and record.finding.message == _TIGHTENED_R6_NEW_MESSAGE
            and record.symbol_context == _TIGHTENED_R6_CONTEXT
        ]
        old_cli_r6 = [
            finding
            for finding in head_gating
            if finding.path == _TIGHTENED_R6_PATH
            and finding.rule_id == "R6"
            and finding.message == _TIGHTENED_R6_OLD_MESSAGE
            and any(record.finding == finding for record in old_raw_r6)
        ]
        new_cli_r6 = [
            finding
            for finding in staged_gating
            if finding.path == _TIGHTENED_R6_PATH
            and finding.rule_id == "R6"
            and finding.message == _TIGHTENED_R6_NEW_MESSAGE
            and any(record.finding == finding for record in new_raw_r6)
        ]
        if (
            len(old_raw_r6) == len(new_raw_r6) == len(old_cli_r6) == len(new_cli_r6) == 1
            and deficit[old_cli_r6[0].key] == 1
            and head_keys[new_cli_r6[0].key] == 0
            and surplus[new_cli_r6[0].key] == 1
            and old_raw_r6[0].finding == old_cli_r6[0]
            and new_raw_r6[0].finding == new_cli_r6[0]
            and old_cli_r6[0].severity == new_cli_r6[0].severity
        ):
            deficit.subtract([old_cli_r6[0].key])
            surplus.subtract([new_cli_r6[0].key])
            sealed_r6_tightenings.append(new_cli_r6[0])
    surplus = +surplus
    deficit = +deficit
    added = tuple(
        AddedKey(
            head_multiplicity=head_keys[key],
            staged_multiplicity=staged_keys[key],
            occurrences=tuple(finding for finding in staged_gating if finding.key == key),
        )
        for key in sorted(surplus)
    )
    return RatchetResult(
        head_count=len(head_gating),
        staged_count=len(staged_gating),
        head_notes=len(head) - len(head_gating),
        staged_notes=len(staged) - len(staged_gating),
        added=added,
        removed_count=sum(deficit.values()),
        relocated=relocated,
        legacy_r6_transfers=tuple(legacy_r6_transfers),
        sealed_r5_relocations=tuple(sealed_r5_relocations),
        sealed_r6_tightenings=tuple(sealed_r6_tightenings),
        raw_cli_added_count=raw_cli_added_count,
    )


def format_report(result: RatchetResult, *, head_sha: str) -> str:
    """Human-readable verdict: counts first, then every added finding verbatim."""
    lines = [
        f"trust-tier ratchet: working tree vs HEAD {head_sha}",
        (
            f"  gating findings: head {result.head_count}, staged {result.staged_count}, "
            f"added {result.added_count}, removed {result.removed_count}"
        ),
        f"  note findings (never gate): head {result.head_notes}, staged {result.staged_notes}",
    ]
    if result.relocated:
        lines.append("  reviewed relocation (unchanged definition and bindings, one-to-one):")
        lines.extend(f"~ {finding.render()}" for finding in result.relocated)
    if result.legacy_r6_transfers:
        lines.append(
            f"  newly surfaced CLI findings from reviewed legacy raw R6 coverage: {len(result.legacy_r6_transfers)} "
            "(global CLI remains fail-closed):"
        )
        lines.extend(f"+ reviewed legacy R6: {finding.render()}" for finding in result.legacy_r6_transfers)
    if result.sealed_r5_relocations:
        lines.append(f"  reviewed one-to-one CLI R5 relocation under the source seal: {len(result.sealed_r5_relocations)}:")
        lines.extend(f"~ reviewed CLI R5: {finding.render()}" for finding in result.sealed_r5_relocations)
    if result.sealed_r6_tightenings:
        lines.append(f"  reviewed same-site R6 exception-type tightening under the source seal: {len(result.sealed_r6_tightenings)}:")
        lines.extend(f"~ tightened CLI R6: {finding.render()}" for finding in result.sealed_r6_tightenings)
    if result.raw_cli_added_count:
        lines.append(f"  raw CLI additions before reviewed transfers: {result.raw_cli_added_count}")
    if not result.added:
        if result.legacy_r6_transfers or result.sealed_r5_relocations or result.sealed_r6_tightenings:
            lines.append("OK: no unreviewed finding; reviewed transfers and tightening are source-sealed.")
        else:
            lines.append("OK: the staged tree adds no trust-tier finding relative to HEAD.")
        return "\n".join(lines) + "\n"
    lines.append(f"FAIL: the staged tree adds {result.added_count} trust-tier finding(s) relative to HEAD:")
    for entry in result.added:
        if entry.head_multiplicity:
            lines.append(f"  (key already present {entry.head_multiplicity}x in HEAD, {entry.staged_multiplicity}x staged)")
        lines.extend(f"+ {finding.render()}" for finding in entry.occurrences)
    lines.append(
        "Remove the new finding or, for an honest Tier-3 boundary, stage it for the judge; never hand-edit an allowlist signature."
    )
    return "\n".join(lines) + "\n"


def normalise_path(raw: str, *, tree_root: Path) -> str:
    """Make an absolute path under ``tree_root`` tree-relative; leave relative paths as emitted."""
    if not os.path.isabs(raw):
        return raw
    resolved = Path(os.path.realpath(raw))
    root = Path(os.path.realpath(tree_root))
    if resolved.is_relative_to(root):
        return resolved.relative_to(root).as_posix()
    return raw


def _field(record: dict[str, object], name: str) -> object:
    if name not in record:
        raise RatchetError(f"lint JSON finding lacks the {name!r} field: {record!r}")
    return record[name]


def _string_field(record: dict[str, object], name: str) -> str:
    value = _field(record, name)
    if not isinstance(value, str):
        raise RatchetError(f"lint JSON finding field {name!r} is not a string: {value!r}")
    return value


def _int_field(record: dict[str, object], name: str) -> int:
    value = _field(record, name)
    if isinstance(value, bool) or not isinstance(value, int):
        raise RatchetError(f"lint JSON finding field {name!r} is not an integer: {value!r}")
    return value


def parse_findings(payload: str, *, tree_root: Path) -> list[FindingRecord]:
    """Parse the CLI's ``--format json`` document into owned records."""
    try:
        document = json.loads(payload)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"lint output is not JSON: {exc}") from exc
    if not isinstance(document, list):
        raise RatchetError(f"lint JSON document is not a list: {type(document).__name__}")
    findings: list[FindingRecord] = []
    for record in document:
        if not isinstance(record, dict):
            raise RatchetError(f"lint JSON finding is not an object: {record!r}")
        findings.append(
            FindingRecord(
                path=normalise_path(_string_field(record, "file_path"), tree_root=tree_root),
                rule_id=_string_field(record, "rule_id"),
                message=_string_field(record, "message"),
                line=_int_field(record, "line"),
                column=_int_field(record, "column"),
                severity=_string_field(record, "severity"),
            )
        )
    return findings


def lint_environment() -> dict[str, str]:
    """The previous hook entry's environment: relative lints path, pinned verify mode, nothing else touched."""
    env = dict(os.environ)
    env["PYTHONPATH"] = LINTS_SOURCE_ROOT
    env[VERIFY_MODE_ENV] = VERIFY_MODE
    return env


def verify_lints_import_path(tree_root: Path, *, python: str) -> Path:
    """Prove the rule code the run will import lives inside ``tree_root``."""
    probe = subprocess.run(
        [python, "-c", "import os, elspeth_lints; print(os.path.realpath(elspeth_lints.__file__))"],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0:
        raise RatchetError(f"could not import elspeth_lints from {tree_root}:\n{probe.stderr}")
    imported = Path(probe.stdout.strip())
    expected = Path(os.path.realpath(tree_root)) / LINTS_SOURCE_ROOT
    if not imported.is_relative_to(expected):
        raise RatchetError(f"elspeth_lints imported from {imported}, expected a module under {expected}")
    return imported


def run_tier_model_check(tree_root: Path, *, python: str) -> tuple[list[FindingRecord], str]:
    """Run the rule exactly as the previous hook entry did; return findings and the run's stderr."""
    completed = subprocess.run(
        [python, "-m", "elspeth_lints.core.cli", "check", "--rules", RULE_ID, "--root", SCAN_ROOT, "--format", "json"],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode not in (0, 1):
        raise RatchetError(f"elspeth-lints check exited {completed.returncode} in {tree_root}:\n{completed.stderr}")
    return parse_findings(completed.stdout, tree_root=tree_root), completed.stderr


def run_raw_observations(tree_root: Path, *, python: str, paths: Sequence[str], rule_ids: Sequence[str]) -> list[RawFindingRecord]:
    """Measure unsuppressed visitor findings in each tree's own lint implementation."""
    probe = """
import json
import sys
from pathlib import Path
from elspeth_lints.rules.trust_tier.tier_model.rule import scan_file
root = Path('src/elspeth')
records = []
rules = set(sys.argv[1].split(','))
for relative in sys.argv[2:]:
    target = root / relative
    if not target.is_file():
        raise FileNotFoundError(target)
    for finding in scan_file(target, root):
        if finding.rule_id in rules:
            records.append({'file_path': finding.file_path, 'rule_id': finding.rule_id,
                            'message': finding.message, 'line': finding.line, 'column': finding.col,
                            'severity': 'error', 'symbol_context': finding.symbol_context})
print(json.dumps(records))
"""
    completed = subprocess.run(
        [python, "-c", probe, ",".join(rule_ids), *paths],
        cwd=tree_root,
        env=lint_environment(),
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise RatchetError(f"raw visitor failed in {tree_root}:\n{completed.stderr}")
    try:
        document = json.loads(completed.stdout)
    except json.JSONDecodeError as exc:
        raise RatchetError(f"raw visitor did not emit JSON: {exc}") from exc
    if not isinstance(document, list):
        raise RatchetError("raw visitor did not emit a list")
    records: list[RawFindingRecord] = []
    for item in document:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("symbol_context"), list)
            or not all(isinstance(s, str) for s in item["symbol_context"])
        ):
            raise RatchetError("raw visitor emitted an invalid context")
        finding_payload = {key: value for key, value in item.items() if key != "symbol_context"}
        finding = parse_findings(json.dumps([finding_payload]), tree_root=tree_root)[0]
        records.append(RawFindingRecord(finding=finding, symbol_context=tuple(item["symbol_context"])))
    return records


def export_head(repo_root: Path, destination: Path) -> str:
    """Extract ``git archive HEAD`` into ``destination``; return HEAD's sha."""
    head = subprocess.run(["git", "-C", str(repo_root), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    if head.returncode != 0:
        raise RatchetError(f"git rev-parse HEAD failed in {repo_root}:\n{head.stderr}")
    archive = subprocess.run(["git", "-C", str(repo_root), "archive", "--format=tar", "HEAD"], capture_output=True, check=False)
    if archive.returncode != 0:
        raise RatchetError(f"git archive HEAD failed in {repo_root}:\n{archive.stderr.decode(errors='replace')}")
    with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
        tar.extractall(destination, filter="data")
    return head.stdout.strip()


def _repo_root_from_cwd() -> Path:
    toplevel = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=False)
    if toplevel.returncode != 0:
        raise RatchetError(f"not inside a git repository:\n{toplevel.stderr}")
    return Path(toplevel.stdout.strip())


def ratchet(repo_root: Path, *, python: str) -> tuple[RatchetResult, str]:
    """Run both sides and compare; returns the result and HEAD's sha."""
    with tempfile.TemporaryDirectory(prefix="trust-tier-ratchet-head-") as scratch:
        head_tree = Path(scratch) / "head"
        head_tree.mkdir()
        head_sha = export_head(repo_root, head_tree)
        verify_lints_import_path(head_tree, python=python)
        head_findings, _head_stderr = run_tier_model_check(head_tree, python=python)
        verify_lints_import_path(repo_root, python=python)
        staged_findings, staged_stderr = run_tier_model_check(repo_root, python=python)
        sys.stderr.write(staged_stderr)
        head_raw: list[RawFindingRecord] = []
        staged_raw: list[RawFindingRecord] = []
        if head_sha == _LEGACY_R6_HEAD:
            head_raw = run_raw_observations(
                head_tree, python=python, paths=["web/composer/service.py", _TIGHTENED_R6_PATH], rule_ids=["R5", "R6"]
            )
            staged_raw = run_raw_observations(
                repo_root,
                python=python,
                paths=[
                    "web/composer/composer_preflight.py",
                    "web/composer/planning_application.py",
                    "web/composer/turn_audit.py",
                    _TIGHTENED_R6_PATH,
                ],
                rule_ids=["R5", "R6"],
            )
        return compare_corpora(
            head_findings,
            staged_findings,
            head_root=head_tree,
            staged_root=repo_root,
            head_sha=head_sha,
            head_raw=head_raw,
            staged_raw=staged_raw,
        ), head_sha


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Fail only when the staged tree adds a trust-tier finding HEAD does not have.")
    parser.add_argument("--repo-root", type=Path, default=None, help="Repository root (default: git rev-parse --show-toplevel)")
    parser.add_argument("--python", default=sys.executable, help="Interpreter for both lint runs (default: this one)")
    args = parser.parse_args(argv)
    try:
        repo_root = args.repo_root if args.repo_root is not None else _repo_root_from_cwd()
        result, head_sha = ratchet(repo_root, python=args.python)
    except RatchetError as exc:
        sys.stderr.write(f"trust-tier ratchet: {exc}\n")
        return 2
    sys.stdout.write(format_report(result, head_sha=head_sha))
    return result.exit_code


if __name__ == "__main__":
    sys.exit(main())
