"""Load/render ``config/cicd/masquerade_baseline.yaml``.

This is a **per-site classification ledger**, not a signed allowlist and
not a tier_model suppression surface. It carries no signatures, no
``judge_metadata_signature``, and no coupling to
``trust_tier.tier_model`` or ``BoundaryRule`` — the masquerade gate is a
third, independent consumer of the ``@trust_boundary`` metadata (the
second being Wardline, per AGENTS.md), and it never suppresses a
tier_model finding.

Identity, and why it is not a security hole
---------------------------------------------
Each entry is keyed by ``(path, qualname, kind)`` — no line number, so a
reformatted or moved site keeps its identity, and a renamed/relocated one
correctly re-fires for re-adjudication. Because identity excludes source
locations, one entry can cover more than one textual occurrence: two
``getattr`` calls in the same function share one key. The payload therefore
binds both an exact ``occurrences`` count and a normalized, sorted
``probe_shapes`` fingerprint for every occurrence. Counts catch additions
and removals; the shape multiset catches one-for-one replacement of a
reviewed literal admission with materially different reflection while
remaining stable across whitespace-only reformatting.

``unadjudicated`` honesty
--------------------------
Most seeded entries carry ``classification: unadjudicated``. That means
NOT YET REVIEWED — the gate passing means "nothing new landed", not "the
corpus is clean". Any code that reports a baseline summary (the seeder,
the pytest gate) must say so explicitly rather than let a green run imply
a clean tree.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from elspeth_lints.rules.masquerade.inventory import ALL_KINDS, SiteKind

BASELINE_RELATIVE_PATH = "config/cicd/masquerade_baseline.yaml"

CLASSIFICATIONS: frozenset[str] = frozenset(
    {
        "external-parse",
        "approved-introspection",
        "reflection-owned-table",
        "module-getattr",
        "data-container-api",
        "unadjudicated",
    }
)

BASELINE_HEADER = """\
# masquerade.attribute-probes baseline
#
# READ THIS BEFORE TRUSTING A GREEN GATE.
#
# This file adjudicates every getattr/hasattr/inspect.getattr_static/
# __getattr__ site in src/elspeth, tests, scripts, and elspeth-lints/src
# that is NOT covered by one of the gate's three permanent, structural
# amnesties (a sentinel getattr on a @trust_boundary/@observation_boundary
# source_param; assert [not] hasattr(...) in tests; a PEP 562 module
# __getattr__ gated on a closed literal table). A site listed here is
# TRACKED, not CLEARED.
#
# `classification: unadjudicated` means NOT YET REVIEWED. Most entries in
# this file were seeded automatically from the corpus measured at gate
# creation time (elspeth-b9ad1bbee3) and carry no human judgement about
# whether the site is correct, defective, or should be rewritten. A
# PASSING GATE MEANS "NOTHING NEW LANDED SINCE THE BASELINE WAS SEEDED" —
# it does NOT mean "this corpus is clean". Only entries whose
# classification is one of external-parse / approved-introspection /
# reflection-owned-table / module-getattr / data-container-api, with a
# real justification, have been reviewed.
#
# Identity is (path, qualname, kind) — no line numbers, so reformatting or
# moving a site does not require re-adjudication, but renaming or
# relocating it does (the old entry goes stale, a new one is required).
# Multiple probe sites of the same kind inside one function share one
# entry. `occurrences` is a REQUIRED exact count, and `probe_shapes` is a
# sorted per-occurrence multiset of SHA-256 fingerprints over normalized
# AST shapes (source positions excluded). Count drift catches additions /
# removals; shape drift catches one-for-one semantic substitutions at the
# same key and count. Whitespace-only reformatting keeps the same shape. A
# legacy entry with no shapes may be loaded for migration, but it always
# fires shape drift and its old review is not preserved by refresh.
#
# A baseline entry whose (path, qualname, kind) no longer exists AT ALL in
# the tree is itself a gate failure (stale-baseline-entry) so this file
# tracks reality and cannot silently rot as code moves out from under it.
#
# Governing document: docs/architecture/adr/032-validate-by-trust-domain.md
"""


@dataclass(frozen=True, slots=True)
class BaselineEntry:
    """One adjudicated (or not-yet-adjudicated) masquerade site.

    ``occurrences`` is the number of current non-amnestied probe sites
    sharing this entry's ``(path, qualname, kind)`` key. It is part of
    the entry's PAYLOAD, not its identity key. ``probe_shapes`` binds the
    normalized AST of each occurrence without source positions. Empty
    shapes are accepted only as the legacy migration representation; the
    rule treats them as drift until the seeder refreshes them.
    """

    path: str
    qualname: str
    kind: SiteKind
    occurrences: int
    classification: str
    justification: str
    probe_shapes: tuple[str, ...] = ()

    @property
    def key(self) -> tuple[str, str, str]:
        """Return the ``(path, qualname, kind)`` identity tuple."""
        return (self.path, self.qualname, self.kind)


@dataclass(frozen=True, slots=True)
class MasqueradeBaseline:
    """Parsed baseline: an ordered, de-duplicated set of entries."""

    entries: tuple[BaselineEntry, ...]

    def keys(self) -> frozenset[tuple[str, str, str]]:
        """Return the set of adjudicated ``(path, qualname, kind)`` identities."""
        return frozenset(entry.key for entry in self.entries)


def load_baseline(path: Path) -> MasqueradeBaseline:
    """Load the baseline YAML. A missing file is an EMPTY baseline, not an error.

    Mirrors the ``meta.no-new-bespoke-cicd-enforcer`` manifest loader's
    treatment of an absent manifest: a fresh checkout (or a clean-fixture
    case with no baseline file at all) has nothing adjudicated yet, which
    is a valid — if maximally strict — starting state, not a load failure.
    """
    if not path.exists():
        return MasqueradeBaseline(entries=())
    raw = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"{path}: masquerade baseline must be a YAML mapping")
    entries_raw = raw.get("entries", [])
    if not isinstance(entries_raw, list):
        raise ValueError(f"{path}: 'entries' must be a list")
    entries: list[BaselineEntry] = []
    seen: set[tuple[str, str, str]] = set()
    for index, item in enumerate(entries_raw):
        entry = _entry_from_mapping(item, path=path, index=index)
        if entry.key in seen:
            raise ValueError(f"{path}: duplicate baseline entry {entry.key!r} at entries[{index}]")
        seen.add(entry.key)
        entries.append(entry)
    return MasqueradeBaseline(entries=tuple(entries))


def _entry_from_mapping(item: Any, *, path: Path, index: int) -> BaselineEntry:
    if not isinstance(item, Mapping):
        raise ValueError(f"{path}: entries[{index}] must be a mapping")

    def required(key: str) -> str:
        value = item.get(key)
        if not isinstance(value, str) or not value:
            raise ValueError(f"{path}: entries[{index}].{key} must be a non-empty string")
        return value

    entry_path = required("path")
    qualname = required("qualname")
    kind = required("kind")
    if kind not in ALL_KINDS:
        raise ValueError(f"{path}: entries[{index}].kind {kind!r} is not one of {ALL_KINDS!r}")
    occurrences = item.get("occurrences")
    # bool is an int subclass; reject it explicitly so `occurrences: true`
    # cannot silently parse as 1.
    if isinstance(occurrences, bool) or not isinstance(occurrences, int) or occurrences < 1:
        raise ValueError(f"{path}: entries[{index}].occurrences must be an integer >= 1, got {occurrences!r}")
    probe_shapes_raw = item.get("probe_shapes", [])
    if not isinstance(probe_shapes_raw, list) or not all(isinstance(shape, str) for shape in probe_shapes_raw):
        raise ValueError(f"{path}: entries[{index}].probe_shapes must be a list of SHA-256 strings")
    probe_shapes = tuple(probe_shapes_raw)
    if probe_shapes:
        if len(probe_shapes) != occurrences:
            raise ValueError(f"{path}: entries[{index}].probe_shapes must contain exactly occurrences={occurrences} entries")
        if any(len(shape) != 64 or any(character not in "0123456789abcdef" for character in shape) for shape in probe_shapes):
            raise ValueError(f"{path}: entries[{index}].probe_shapes entries must be lowercase SHA-256 hex strings")
        if probe_shapes != tuple(sorted(probe_shapes)):
            raise ValueError(f"{path}: entries[{index}].probe_shapes must be sorted")
    classification = required("classification")
    if classification not in CLASSIFICATIONS:
        raise ValueError(f"{path}: entries[{index}].classification {classification!r} is not one of {sorted(CLASSIFICATIONS)!r}")
    justification = required("justification")
    return BaselineEntry(
        path=entry_path,
        qualname=qualname,
        kind=kind,
        occurrences=occurrences,
        classification=classification,
        justification=justification,
        probe_shapes=probe_shapes,
    )


def render_baseline_yaml(entries: Sequence[BaselineEntry]) -> str:
    """Render ``entries`` (already sorted by the caller) into baseline YAML text."""
    payload = {
        "entries": [
            {
                "path": entry.path,
                "qualname": entry.qualname,
                "kind": entry.kind,
                "occurrences": entry.occurrences,
                "probe_shapes": list(entry.probe_shapes),
                "classification": entry.classification,
                "justification": entry.justification,
            }
            for entry in entries
        ]
    }
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return BASELINE_HEADER + "\n" + body
