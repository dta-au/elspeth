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
correctly re-fires for re-adjudication. Because identity excludes the call
site itself, one entry can cover more than one textual occurrence: two
``getattr`` calls in the same function share one key. That collapse would
be a real hole on its own — someone could add a fourth ``getattr`` call to
an already-baselined function (exactly the high-traffic boundaries this
gate exists to protect, e.g. ``tool_batch.py::_admit_tool_batch``) and the
gate would stay green. It is closed by a mandatory ``occurrences`` count
carried on the entry: the rule counts every current non-amnestied
occurrence at that key and requires an EXACT match against the baselined
count, in both directions. An increase without a matching baseline update
is the security property (a new probe landed); a silent decrease is
disallowed too, so the count cannot drift inflated as sites are migrated
or amnestied away. See ``masquerade_baseline.yaml``'s header comment,
which restates this for any human reader of the file directly.

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
# Multiple call sites of the same kind inside one function share one
# entry, but the `occurrences` field on that entry is a REQUIRED,
# EXACT-MATCH COUNT of current non-amnestied occurrences at that key —
# not a one-time snapshot. Adding (or removing, or amnestying away) even
# one probe at an already-baselined site changes the count and fires
# occurrence-count-drift until the entry is updated. This is what stops
# someone from adding a fourth getattr call to an already-adjudicated
# high-traffic boundary (e.g. tool_batch.py::_admit_tool_batch) and having
# the gate stay silently green.
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

    ``occurrences`` is the number of current non-amnestied call sites
    sharing this entry's ``(path, qualname, kind)`` key. It is part of
    the entry's PAYLOAD, not its identity key — a reformat or an
    amnesty-shape refactor does not change identity, but adding or
    removing a probe at an already-baselined site changes this count and
    must trigger re-adjudication (occurrence-count-drift), never a
    silent pass.
    """

    path: str
    qualname: str
    kind: SiteKind
    occurrences: int
    classification: str
    justification: str

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
                "classification": entry.classification,
                "justification": entry.justification,
            }
            for entry in entries
        ]
    }
    body = yaml.safe_dump(payload, sort_keys=False, default_flow_style=False, allow_unicode=True)
    return BASELINE_HEADER + "\n" + body
