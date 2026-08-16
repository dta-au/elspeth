"""Runtime-rejection parity census: enumerate every raise site the runtime
preflight can reach and require an authoring-side disposition for each.

Why this exists (elspeth-2ed41f0a4a)
------------------------------------
The composer validates twice: Stage 1 (``CompositionState.validate()``, the
authoring validator the LLM tool loop reads on every mutation) and Stage 2
(the runtime preflight — settings load, plugin construction, DAG build).
Every rule the runtime enforces that Stage 1 does not mirror is a
"validate green / runtime red" shape: the authoring loop is told the
pipeline is valid when it is not runnable. Eighteen such shapes were found
one eval at a time; the census behind the panel review counted 101 raise
sites in ``core/dag/`` and found the inflow UNGATED — a new runtime rule
could land with nothing requiring its authoring counterpart (Shape 17 did).

This module is that gate. It AST-enumerates every ``raise <Exception>(...)``
under the runtime-rejection roots — PLUS every declarative pydantic
``Field(min_length=..., max_length=..., gt=..., ...)`` constraint on a
settings model, which rejects without any raise site at all (a one-branch
coalesce was found green/red only by probe because ``branches:
Field(min_length=2)`` is invisible to a raise census). Each site is keyed by
``(path, qualname, exception, message-skeleton, ordinal)`` — no line
numbers, so reformatting does not churn the baseline; a Field constraint
uses the exception name ``FieldConstraint`` and the message
``<field>: <constraint kwargs>``. The baseline
(``config/cicd/runtime_rejection_parity.yaml``) records ONE disposition per
site:

``mirrored``
    Stage 1 rejects the same predicate. ``counterpart`` names the Stage-1
    ``error_code`` (or codes); the gate verifies each code exists as a
    string literal under ``src/elspeth/web/`` so a mirror claim cannot be
    folklore.
``abstains``
    Stage 1 checks a narrower predicate and deliberately abstains on some
    topology (dynamic schemas, fan-in soundness). ``note`` says which; a
    warning ``counterpart`` may be named. Abstentions render as
    ``is_valid: true`` — the terminal Stage-2 gate is what catches them.
``structural``
    An invariant of runtime construction that no composer-authored state
    can violate (defensive re-checks, ``FrameworkBugError``, internal
    consistency of already-validated inputs).
``not_authorable``
    The rejected shape cannot be produced through the composer at all
    (settings the composer never emits; fields ``NodeSpec`` cannot carry).
``unmirrored``
    Authorable, Stage 1 silent — a KNOWN validate-green/runtime-red gap.
    ``note`` must name the tracking ticket. The gate holds these to a
    ratchet ceiling; the ceiling only goes down.
``unadjudicated``
    Seeded, never reviewed. The gate FAILS on any.

The gate test (``tests/integration/pipeline/test_runtime_rejection_parity_gate.py``)
asserts: every live site has a baseline entry; every baseline entry is
live; no ``unadjudicated``; every ``mirrored`` counterpart is a real
Stage-1 code; ``unmirrored`` count is at or under the ratchet.

Usage::

    .venv/bin/python scripts/cicd/runtime_rejection_parity.py           # report drift
    .venv/bin/python scripts/cicd/runtime_rejection_parity.py --write   # refresh baseline

``--write`` preserves the disposition/counterpart/note of every entry whose
key still exists, seeds new sites as ``unadjudicated``, and drops entries
whose site is gone. Review the diff before committing; never hand-edit a
``key``.
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import sys
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Final

import yaml

REPO_ROOT: Final = Path(__file__).resolve().parents[2]
BASELINE_PATH: Final = REPO_ROOT / "config" / "cicd" / "runtime_rejection_parity.yaml"

# Roots whose raise sites the composer's Stage-2 preflight can reach on an
# authored pipeline: the DAG builder/validator family and the settings
# models whose validators fire at ``settings_load``.
SCAN_ROOTS: Final[tuple[str, ...]] = (
    "src/elspeth/core/dag",
    "src/elspeth/core/config.py",
)
# Stage-1 error codes are string literals under this tree.
STAGE1_ROOT: Final = "src/elspeth/web"

DISPOSITIONS: Final[frozenset[str]] = frozenset({"mirrored", "abstains", "structural", "not_authorable", "unmirrored", "unadjudicated"})
_COUNTERPART_REQUIRED: Final[frozenset[str]] = frozenset({"mirrored"})
_NOTE_REQUIRED: Final[frozenset[str]] = frozenset({"abstains", "structural", "not_authorable", "unmirrored"})

_MAX_MESSAGE_SKELETON: Final = 160


@dataclass(frozen=True)
class RaiseSite:
    """One enumerated raise site (identity fields only)."""

    path: str
    qualname: str
    exception: str
    message: str
    ordinal: int

    @property
    def key(self) -> str:
        digest = hashlib.sha256(
            "|".join((self.path, self.qualname, self.exception, self.message, str(self.ordinal))).encode("utf-8")
        ).hexdigest()
        return digest[:16]


@dataclass(frozen=True)
class BaselineEntry:
    """A raise site plus its adjudicated authoring-side disposition."""

    site: RaiseSite
    disposition: str
    counterpart: tuple[str, ...]
    note: str

    def to_yaml_dict(self) -> dict[str, Any]:
        record: dict[str, Any] = {"key": self.site.key, **asdict(self.site)}
        record["disposition"] = self.disposition
        if self.counterpart:
            record["counterpart"] = list(self.counterpart)
        if self.note:
            record["note"] = self.note
        return record


# --------------------------------------------------------------------------
# Enumeration
# --------------------------------------------------------------------------


def _iter_python_files(root: Path) -> Iterator[Path]:
    if root.is_file():
        yield root
        return
    yield from sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _exception_name(node: ast.expr) -> str:
    if isinstance(node, ast.Call):
        return _exception_name(node.func)
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return node.attr
    return f"<{type(node).__name__}>"


def _message_skeleton(node: ast.expr | None) -> str:
    """Reduce the first constructor argument to a stable, literal-only skeleton.

    Literal text survives; every interpolated expression becomes ``{}``;
    non-literal messages collapse to a bracketed marker. Whitespace is
    normalised and the result truncated so the key stays stable under
    wording churn only where the wording is genuinely the identity.
    """
    if node is None:
        return "<no-message>"
    parts = _skeleton_parts(node)
    text = " ".join("".join(parts).split())
    return text[:_MAX_MESSAGE_SKELETON]


def _skeleton_parts(node: ast.expr) -> list[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return [node.value]
    if isinstance(node, ast.JoinedStr):
        out: list[str] = []
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                out.append(value.value)
            else:
                out.append("{}")
        return out
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        return [*_skeleton_parts(node.left), *_skeleton_parts(node.right)]
    if isinstance(node, ast.Name):
        return [f"<dynamic:{node.id}>"]
    if isinstance(node, ast.Call):
        return [f"<call:{_exception_name(node.func)}>"]
    return [f"<{type(node).__name__}>"]


# pydantic ``Field(...)`` keyword arguments that reject input declaratively.
_FIELD_CONSTRAINT_KWARGS: Final[frozenset[str]] = frozenset(
    {"min_length", "max_length", "gt", "ge", "lt", "le", "pattern", "min_items", "max_items", "multiple_of"}
)
FIELD_CONSTRAINT_EXCEPTION: Final = "FieldConstraint"


class _RaiseCollector(ast.NodeVisitor):
    def __init__(self, path: str) -> None:
        self._path = path
        self._scope: list[str] = []
        self.sites: list[tuple[str, str, str]] = []

    def _visit_scoped(self, node: ast.AST, name: str) -> None:
        self._scope.append(name)
        try:
            self.generic_visit(node)
        finally:
            self._scope.pop()

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        # Declarative constraints on the class's own annotated fields.
        for statement in node.body:
            if not isinstance(statement, ast.AnnAssign) or not isinstance(statement.target, ast.Name):
                continue
            value = statement.value
            if not isinstance(value, ast.Call) or _exception_name(value.func) != "Field":
                continue
            constraints = sorted(kw.arg for kw in value.keywords if kw.arg in _FIELD_CONSTRAINT_KWARGS)
            if constraints:
                qualname = ".".join((*self._scope, node.name))
                self.sites.append((qualname, FIELD_CONSTRAINT_EXCEPTION, f"{statement.target.id}: {', '.join(constraints)}"))
        self._visit_scoped(node, node.name)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_scoped(node, node.name)

    def visit_Raise(self, node: ast.Raise) -> None:
        if node.exc is not None:
            exception = _exception_name(node.exc)
            first_arg = node.exc.args[0] if isinstance(node.exc, ast.Call) and node.exc.args else None
            if isinstance(node.exc, ast.Call):
                message = _message_skeleton(first_arg)
            else:
                message = "<reraise-instance>"
            self.sites.append((".".join(self._scope) or "<module>", exception, message))
        self.generic_visit(node)


def enumerate_raise_sites(repo_root: Path = REPO_ROOT, roots: Sequence[str] = SCAN_ROOTS) -> tuple[RaiseSite, ...]:
    """Enumerate every constructed-exception raise site under the scan roots."""
    sites: list[RaiseSite] = []
    for root in roots:
        for path in _iter_python_files(repo_root / root):
            rel = path.relative_to(repo_root).as_posix()
            collector = _RaiseCollector(rel)
            collector.visit(ast.parse(path.read_text(encoding="utf-8"), filename=rel))
            seen: Counter[tuple[str, str, str]] = Counter()
            for qualname, exception, message in collector.sites:
                ordinal = seen[(qualname, exception, message)]
                seen[(qualname, exception, message)] += 1
                sites.append(RaiseSite(rel, qualname, exception, message, ordinal))
    return tuple(sites)


# --------------------------------------------------------------------------
# Baseline I/O
# --------------------------------------------------------------------------

_HEADER: Final = """\
# runtime-rejection parity baseline (elspeth-2ed41f0a4a)
#
# READ THIS BEFORE TRUSTING A GREEN GATE.
#
# One entry per constructed-exception raise site under the runtime
# preflight roots (src/elspeth/core/dag/, src/elspeth/core/config.py).
# Each entry adjudicates whether the composer's Stage-1 authoring
# validator mirrors that runtime rejection. Identity is
# (path, qualname, exception, message, ordinal) - no line numbers.
#
#   mirrored        Stage 1 rejects the same predicate; `counterpart` names
#                   the Stage-1 error_code(s), verified to exist.
#   abstains        Stage 1 checks a narrower predicate and deliberately
#                   abstains on some topology; renders is_valid:true.
#   structural      no composer-authored state can violate it.
#   not_authorable  the composer cannot produce the rejected shape at all.
#   unmirrored      authorable, Stage 1 silent - a KNOWN validate-green /
#                   runtime-red gap. `note` names the ticket. Ratcheted.
#   unadjudicated   seeded, never reviewed. THE GATE FAILS ON ANY.
#
# A passing gate means "every runtime rejection site has a reviewed
# disposition and no unmirrored site was added" - NOT "Stage 1 mirrors
# everything". Refresh with:
#   .venv/bin/python scripts/cicd/runtime_rejection_parity.py --write
# Never hand-edit a `key`.
"""


def load_baseline(path: Path = BASELINE_PATH) -> tuple[BaselineEntry, ...]:
    if not path.exists():
        return ()
    loaded = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    entries: list[BaselineEntry] = []
    for record in loaded.get("sites", ()):
        site = RaiseSite(
            path=str(record["path"]),
            qualname=str(record["qualname"]),
            exception=str(record["exception"]),
            message=str(record["message"]),
            ordinal=int(record["ordinal"]),
        )
        counterpart_raw = record.get("counterpart") or ()
        counterpart = (counterpart_raw,) if isinstance(counterpart_raw, str) else tuple(str(c) for c in counterpart_raw)
        entries.append(
            BaselineEntry(
                site=site,
                disposition=str(record.get("disposition", "unadjudicated")),
                counterpart=counterpart,
                note=str(record.get("note") or ""),
            )
        )
    return tuple(entries)


def _sort_key(entry: BaselineEntry) -> tuple[str, str, str, str, int]:
    s = entry.site
    return (s.path, s.qualname, s.exception, s.message, s.ordinal)


def merge_baseline(live: Iterable[RaiseSite], existing: Iterable[BaselineEntry]) -> tuple[BaselineEntry, ...]:
    """Carry every existing adjudication forward by key; seed new sites unadjudicated."""
    by_key = {entry.site.key: entry for entry in existing}
    merged: list[BaselineEntry] = []
    for site in live:
        prior = by_key.get(site.key)
        if prior is None:
            merged.append(BaselineEntry(site=site, disposition="unadjudicated", counterpart=(), note=""))
        else:
            merged.append(replace(prior, site=site))
    return tuple(sorted(merged, key=_sort_key))


def render_baseline(entries: Iterable[BaselineEntry]) -> str:
    body = yaml.safe_dump(
        {"sites": [entry.to_yaml_dict() for entry in sorted(entries, key=_sort_key)]},
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return _HEADER + "\n" + body


# --------------------------------------------------------------------------
# Verification
# --------------------------------------------------------------------------


def stage1_error_code_literals(repo_root: Path = REPO_ROOT, root: str = STAGE1_ROOT) -> frozenset[str]:
    """The universe a ``mirrored`` counterpart may name.

    Two forms are accepted, both verified against the live tree so a mirror
    claim can never be folklore:

    * a bare string — must be a string literal under the Stage-1 tree
      (the ``error_code`` argument of ``_err(...)`` and friends);
    * ``fn:<name>`` — must be the name of a function/method defined under
      the Stage-1 tree. This is for rules mirrored at the TOOL-ARGUMENT layer
      (Pydantic validators on ``upsert_node`` etc.), which reject before
      ``state.validate()`` runs and carry no ``error_code``. Either layer
      keeps the model from ever reading a green verdict for the shape.
    """
    literals: set[str] = set()
    for path in _iter_python_files(repo_root / root):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"), filename=str(path))):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                literals.add(node.value)
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                literals.add(f"fn:{node.name}")
    return frozenset(literals)


@dataclass(frozen=True)
class ParityReport:
    missing_from_baseline: tuple[RaiseSite, ...]
    stale_in_baseline: tuple[BaselineEntry, ...]
    unadjudicated: tuple[BaselineEntry, ...]
    invalid_disposition: tuple[BaselineEntry, ...]
    missing_counterpart: tuple[BaselineEntry, ...]
    unknown_counterpart: tuple[tuple[BaselineEntry, str], ...]
    missing_note: tuple[BaselineEntry, ...]
    unmirrored: tuple[BaselineEntry, ...]

    @property
    def clean(self) -> bool:
        return not (
            self.missing_from_baseline
            or self.stale_in_baseline
            or self.unadjudicated
            or self.invalid_disposition
            or self.missing_counterpart
            or self.unknown_counterpart
            or self.missing_note
        )


def verify(
    live: Iterable[RaiseSite],
    baseline: Iterable[BaselineEntry],
    *,
    stage1_literals: frozenset[str],
) -> ParityReport:
    live_by_key = {site.key: site for site in live}
    baseline_entries = tuple(baseline)
    baseline_by_key = {entry.site.key: entry for entry in baseline_entries}
    missing = tuple(site for key, site in live_by_key.items() if key not in baseline_by_key)
    stale = tuple(entry for key, entry in baseline_by_key.items() if key not in live_by_key)
    unadjudicated = tuple(e for e in baseline_entries if e.disposition == "unadjudicated")
    invalid = tuple(e for e in baseline_entries if e.disposition not in DISPOSITIONS)
    missing_counterpart = tuple(e for e in baseline_entries if e.disposition in _COUNTERPART_REQUIRED and not e.counterpart)
    unknown_counterpart = tuple((e, code) for e in baseline_entries for code in e.counterpart if code not in stage1_literals)
    missing_note = tuple(e for e in baseline_entries if e.disposition in _NOTE_REQUIRED and not e.note.strip())
    unmirrored = tuple(e for e in baseline_entries if e.disposition == "unmirrored")
    return ParityReport(
        missing_from_baseline=missing,
        stale_in_baseline=stale,
        unadjudicated=unadjudicated,
        invalid_disposition=invalid,
        missing_counterpart=missing_counterpart,
        unknown_counterpart=unknown_counterpart,
        missing_note=missing_note,
        unmirrored=unmirrored,
    )


def _describe(site: RaiseSite) -> str:
    return f"{site.path}::{site.qualname} raise {site.exception}({site.message!r}) #{site.ordinal} [{site.key}]"


def format_report(report: ParityReport) -> str:
    lines: list[str] = []

    def section(title: str, rows: Iterable[str]) -> None:
        rows = list(rows)
        if rows:
            lines.append(f"{title} ({len(rows)}):")
            lines.extend(f"  - {row}" for row in rows)

    section("NEW runtime rejection sites without a parity disposition", (_describe(s) for s in report.missing_from_baseline))
    section("STALE baseline entries (site no longer exists)", (_describe(e.site) for e in report.stale_in_baseline))
    section("UNADJUDICATED entries", (_describe(e.site) for e in report.unadjudicated))
    section("INVALID dispositions", (f"{_describe(e.site)} -> {e.disposition!r}" for e in report.invalid_disposition))
    section("mirrored entries missing a counterpart", (_describe(e.site) for e in report.missing_counterpart))
    section(
        "counterparts that are not a Stage-1 string literal",
        (f"{_describe(e.site)} -> {code!r}" for e, code in report.unknown_counterpart),
    )
    section("entries missing a required note", (f"{_describe(e.site)} ({e.disposition})" for e in report.missing_note))
    lines.append(f"unmirrored (ratcheted): {len(report.unmirrored)}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--write", action="store_true", help="refresh the baseline in place (preserving adjudications)")
    parser.add_argument("--baseline", type=Path, default=BASELINE_PATH)
    args = parser.parse_args(argv)

    live = enumerate_raise_sites()
    existing = load_baseline(args.baseline)
    if args.write:
        merged = merge_baseline(live, existing)
        args.baseline.write_text(render_baseline(merged), encoding="utf-8")
        seeded = sum(1 for e in merged if e.disposition == "unadjudicated")
        dropped = len({e.site.key for e in existing} - {s.key for s in live})
        print(f"wrote {args.baseline.relative_to(REPO_ROOT)}: {len(merged)} sites, {seeded} unadjudicated, {dropped} dropped")
        return 0

    report = verify(live, existing, stage1_literals=stage1_error_code_literals())
    print(format_report(report))
    return 0 if report.clean else 1


if __name__ == "__main__":
    sys.exit(main())
