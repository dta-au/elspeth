"""Whole-tree gate: every repair-feedback fact key the composer ships to the planner is taught or fenced.

Repair feedback crosses the planner's message-redaction boundary as structured
facts keyed by a closed ``error_code`` — ``contract`` / ``row_union_schema`` /
``coalesce_union_type`` details and ``connectivity`` facts on the freeform
surface (``pipeline_planner._allowlisted_candidate_feedback``). A fact key is only
usable if the ``(explanation, suggested_fix)`` that
``tools.generation.explain_validation_code(code)`` resolves names it: a key the
model is never told how to read cannot repair anything, and
``sink_targeting_branches`` shipped untaught for its whole life before
cc2b19ce4 noticed (elspeth-68721c71d7).

Both sides are DERIVED, never hand-listed: the shipped key set comes from the
live TypedDicts plus the constructor keywords at every producer site (a
``NotRequired`` key a site never passes can never reach the planner from it)
and the taught set comes from the catalogue itself. The only curated input is the fence fixture
(``planner_teaching_fence.json``): keys deliberately left untaught, each with a
reason a reviewer can check. A fence entry that has since become taught, or
whose key no longer ships, is itself a failure — the fence must not outlive
what it fences.
"""

from __future__ import annotations

import ast
import json
import re
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, TypedDict

import pytest
from scripts.cicd.composer_teaching import (
    _call_name,
    _display,
    _literal_str,
    _typed_keys,
    composer_python_files,
    is_quoted_leaf,
)

from elspeth.web.composer import pipeline_planner, state
from elspeth.web.composer.pipeline_planner import route_destination_fact_keys
from elspeth.web.composer.tools import generation

FENCE_PATH = Path(__file__).with_name("planner_teaching_fence.json")

# The three detail payloads a ValidationEntry can carry, by constructor keyword,
# with the TypedDict each serialises to via ``to_dict``.
_DETAIL_PAYLOADS: dict[str, type] = {
    "contract": state.SchemaContractDetailDict,
    "row_union_schema": state.RowUnionSchemaDetailDict,
    "coalesce_union_type": state.CoalesceUnionTypeDetailDict,
}
# The owned constructor each detail keyword must be built by, at the site. Any
# other call (a helper) hides its keywords from the walker and is refused.
_DETAIL_CONSTRUCTORS: dict[str, str] = {
    "contract": state.SchemaContractDetail.__name__,
    "row_union_schema": state.RowUnionSchemaDetail.__name__,
    "coalesce_union_type": state.CoalesceUnionTypeDetail.__name__,
}
_ENTRY_CONSTRUCTORS = frozenset({"ValidationEntry", "_err"})
# Positional layout shared by ``ValidationEntry(component, message, severity, error_code, ...)``
# and ``state._err`` (same order).
_ERROR_CODE_POSITION = 3


# Synthetic payloads for the typed-walker self-test; module level because the
# file's postponed annotations resolve names through the module namespace.
class _ProbeInner(TypedDict):
    leaf: str


class _ProbeOuter(TypedDict):
    record: _ProbeInner
    records: list[_ProbeInner]
    flat: int


class ShippedKey(NamedTuple):
    surface: str  # "freeform"
    code: str
    key: str  # dotted path from the entry, e.g. "contract.missing_fields", "connectivity.delta_member"
    site: str


class FenceEntry(NamedTuple):
    surface: str
    code: str
    key: str
    reason: str


# --- derivation ---------------------------------------------------------------------------------


def _module_aliases(tree: ast.Module, names: Iterable[str]) -> dict[str, str]:
    """Module-level ``alias = Name`` and ``from m import Name as alias`` bindings onto ``names``."""
    canonical = set(names)
    aliases: dict[str, str] = {}
    for stmt in tree.body:
        if isinstance(stmt, ast.Assign) and isinstance(stmt.value, ast.Name) and stmt.value.id in canonical:
            for target in stmt.targets:
                if isinstance(target, ast.Name):
                    aliases[target.id] = stmt.value.id
        if isinstance(stmt, ast.ImportFrom):
            for alias in stmt.names:
                if alias.name in canonical and alias.asname:
                    aliases[alias.asname] = alias.name
    return aliases


def _enclosing_class(tree: ast.Module, lineno: int) -> str | None:
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.lineno <= lineno <= (node.end_lineno or node.lineno):
            return node.name
    return None


def _detail_sites(files: Iterable[Path]) -> Iterator[ShippedKey]:
    """Every typed-detail key a ``ValidationEntry`` / ``_err`` construction can ship, per site.

    A site is a call to an entry constructor, to a module-level alias of one,
    or a ``replace(...)`` carrying a detail keyword. Every call to an owned
    detail constructor must then sit INSIDE a site: one built anywhere else —
    a prebuilt variable, a helper, a partial — is refused, because the walker
    could not attribute it to a code (final red-team, third round). The
    detail class's own body (``from_dict`` and friends) is exempt.
    """
    detail_classes = set(_DETAIL_CONSTRUCTORS.values())
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        entry_aliases = _module_aliases(tree, _ENTRY_CONSTRUCTORS)
        detail_aliases = _module_aliases(tree, detail_classes)
        attributed: set[int] = set()
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            callee = _call_name(node)
            carries_detail = any(kw.arg in _DETAIL_PAYLOADS for kw in node.keywords)
            is_entry = callee in _ENTRY_CONSTRUCTORS or callee in entry_aliases
            if not is_entry and not (callee == "replace" and carries_detail):
                continue
            site = f"{_display(path)}:{node.lineno}"
            # Fail CLOSED: a ``**spread`` or a detail
            # passed positionally could carry a payload this walker cannot see.
            if any(kw.arg is None for kw in node.keywords):
                raise AssertionError(f"{site}: entry built with a **spread; the gate cannot derive its detail keys")
            if any(isinstance(arg, ast.Starred) for arg in node.args):
                raise AssertionError(f"{site}: entry built from *args; the gate cannot derive its detail keys")
            if len(node.args) > _ERROR_CODE_POSITION + 1:
                raise AssertionError(f"{site}: entry passes a detail positionally; the gate cannot derive its keys")
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            details = [name for name in _DETAIL_PAYLOADS if name in keywords]
            if not details:
                continue
            code_node = keywords.get("error_code")
            if code_node is None and len(node.args) > _ERROR_CODE_POSITION:
                code_node = node.args[_ERROR_CODE_POSITION]
            code = _literal_str(code_node)
            if code is None:
                raise AssertionError(
                    f"{site}: cannot derive error_code for a typed-detail entry (non-literal); the gate needs a literal code"
                )
            for name in details:
                ctor = keywords[name]
                emitted: set[str] | None = None
                if isinstance(ctor, ast.Call):
                    # Same fail-closed rule one level down: a detail built
                    # positionally or from a **spread would read as "passes
                    # nothing" and skip every leaf (final red-team F2).
                    # A helper call hides its keywords entirely (second round):
                    # only the owned constructor, called by keyword, is derivable.
                    ctor_name = _call_name(ctor)
                    if detail_aliases.get(ctor_name or "", ctor_name) != _DETAIL_CONSTRUCTORS[name]:
                        raise AssertionError(
                            f"{site}: detail '{name}' built by '{ctor_name}', not {_DETAIL_CONSTRUCTORS[name]}; "
                            "the gate cannot derive its keys"
                        )
                    attributed.add(id(ctor))
                    if ctor.args or any(kw.arg is None for kw in ctor.keywords):
                        raise AssertionError(
                            f"{site}: detail '{name}' built positionally or with a **spread; the gate cannot derive its keys"
                        )
                    emitted = {kw.arg for kw in ctor.keywords if kw.arg}
                # The envelope key itself is a fact the model must be able to find.
                yield ShippedKey("freeform", code, name, site)
                for key in _typed_keys(_DETAIL_PAYLOADS[name], name + "."):
                    top = key.split(".")[1].replace("[]", "")
                    if emitted is not None and top not in emitted:
                        continue
                    yield ShippedKey("freeform", code, key, site)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or id(node) in attributed:
                continue
            callee = _call_name(node)
            canonical = detail_aliases.get(callee or "", callee)
            if canonical in detail_classes and _enclosing_class(tree, node.lineno) != canonical:
                raise AssertionError(
                    f"{_display(path)}:{node.lineno}: {canonical} constructed outside an entry site; "
                    "the gate cannot attribute its keys to a code"
                )


def _route_destination_shapes() -> dict[frozenset[str], int]:
    """The distinct ``connectivity`` shapes ``state.route_destination_facts`` emits, from its own AST."""
    tree = ast.parse(Path(state.__file__).read_text(encoding="utf-8"))
    shapes: dict[frozenset[str], int] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == "route_destination_facts":
            for inner in ast.walk(node):
                if isinstance(inner, ast.Dict):
                    keys = frozenset(k for k in (_literal_str(key) for key in inner.keys) if k is not None)
                    if "declared_sinks" in keys:
                        shapes[keys] = shapes.get(keys, 0) + 1
    return shapes


def _connectivity_sites() -> Iterator[ShippedKey]:
    # Per-code shape comes from the CONSUMER's own projection map
    # (``pipeline_planner.route_destination_fact_keys``): the producer merges
    # both routing fields per component, so the entry's key set is decided at
    # the consumer, and that is the authority this gate derives from.
    for code in sorted(pipeline_planner._ROUTE_DESTINATION_FACT_CODES):
        allowed = route_destination_fact_keys(code)
        yield ShippedKey("freeform", code, "connectivity", "state.py:route_destination_facts")
        for key in _typed_keys(state.RouteDestinationFactDict, "connectivity."):
            if key.split(".")[1] in allowed:
                yield ShippedKey("freeform", code, key, "state.py:route_destination_facts")
    yield ShippedKey("freeform", "coalesce_branch_unreachable", "connectivity", "state.py:coalesce_reachability_facts")
    for key in _typed_keys(state.CoalesceReachabilityFactDict, "connectivity."):
        yield ShippedKey("freeform", "coalesce_branch_unreachable", key, "state.py:coalesce_reachability_facts")


def shipped_keys(files: Iterable[Path] | None = None) -> list[ShippedKey]:
    paths = composer_python_files() if files is None else list(files)
    return [*_detail_sites(paths), *_connectivity_sites()]


def is_taught(code: str, key: str, explain=generation.explain_validation_code) -> bool:
    """The key's leaf name appears in the house-style quoted form in the guidance the code resolves to.

    Quoted (``'key'``) or backticked only: a bare-word match let ordinary prose
    ("the consumer node", "a field carried by") count as teaching ``consumer``
    or ``field``, so deleting the deliberate teaching of a common-word key left
    the gate green (red-team finding on bc8b9e237).
    """
    guidance = explain(code)
    if guidance is None:
        return False
    return is_quoted_leaf(key, " ".join(guidance))


def untaught_keys(files: Iterable[Path] | None = None, explain=generation.explain_validation_code) -> dict[tuple[str, str, str], list[str]]:
    """``(surface, code, key) -> sites`` for every shipped key its code's guidance does not name."""
    out: dict[tuple[str, str, str], list[str]] = {}
    for shipped in shipped_keys(files):
        if is_taught(shipped.code, shipped.key, explain):
            continue
        out.setdefault((shipped.surface, shipped.code, shipped.key), []).append(shipped.site)
    return out


def load_fence(path: Path = FENCE_PATH) -> list[FenceEntry]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    return [FenceEntry(e["surface"], e["code"], e["key"], e["reason"]) for e in raw["fenced"]]


# --- the gate -----------------------------------------------------------------------------------


def test_every_shipped_fact_key_is_taught_or_fenced() -> None:
    fenced = {(e.surface, e.code, e.key) for e in load_fence()}
    untaught = untaught_keys()
    unexplained = {k: v for k, v in untaught.items() if k not in fenced}
    lines = [f"{surface} {code} {key}  <- {', '.join(sites)}" for (surface, code, key), sites in sorted(unexplained.items())]
    assert not unexplained, (
        f"{len(unexplained)} repair-feedback key(s) reach the planner with guidance that never names them. "
        "Teach each key in tools/generation.py (a direct guidance record for its code) or fence it with a "
        "checkable reason in planner_teaching_fence.json:\n" + "\n".join(lines)
    )


def test_fence_entries_are_live_untaught_keys() -> None:
    """A fence must not outlive what it fences: a taught or no-longer-shipped key leaves the fixture."""
    untaught = untaught_keys()
    shipped = {(s.surface, s.code, s.key) for s in shipped_keys()}
    stale = []
    for entry in load_fence():
        ident = (entry.surface, entry.code, entry.key)
        if ident not in shipped:
            stale.append(f"{ident}: no producer ships this key any more")
        elif ident not in untaught:
            stale.append(f"{ident}: now taught — remove the fence")
    assert not stale, "stale fence entries:\n" + "\n".join(stale)


def test_fence_entries_carry_a_checkable_reason() -> None:
    """A fence is an adjudicated decision, not a parking spot (elspeth-68721c71d7)."""
    placeholder = re.compile(r"^\s*(pending|todo|tbd|fixme|wip)\b", re.IGNORECASE)
    pending = [e for e in load_fence() if len(e.reason.split()) < 12 or placeholder.match(e.reason)]
    assert not pending, f"{len(pending)} fence entr{'y' if len(pending) == 1 else 'ies'} await adjudication:\n" + "\n".join(
        f"{e.surface} {e.code} {e.key}: {e.reason!r}" for e in pending
    )


def test_fence_fixture_has_no_duplicates() -> None:
    entries = load_fence()
    assert len({(e.surface, e.code, e.key) for e in entries}) == len(entries)


# --- the gate's own derivation ------------------------------------------------------------------


def test_route_destination_facts_emit_exactly_the_three_pinned_shapes() -> None:
    """The producer's dict literals are exactly the three shapes the consumer projects to.

    The producer MERGES shapes per component (a transform whose on_success and
    on_error both dangle carries all four keys), so this pins the building
    blocks; the per-entry projection is pinned in test_validation_error_codes.
    """
    shapes = _route_destination_shapes()
    assert set(shapes) == {route_destination_fact_keys(code) for code in pipeline_planner._ROUTE_DESTINATION_FACT_CODES}, shapes


def test_is_taught_requires_the_quoted_form_not_a_bare_or_super_string() -> None:
    """A key counts as taught only when named in the house style, and only as itself."""

    def explain(_code: str) -> tuple[str, str]:
        return ("the consumer node's input; 'branches' holds records; each 'field_type' is set", "use `producer` here")

    assert is_taught("x", "contract.producer", explain)  # backticked
    assert is_taught("x", "row_union_schema.branches", explain)  # quoted
    assert not is_taught("x", "contract.consumer", explain)  # bare word in ordinary prose
    assert not is_taught("x", "row_union_schema.branches[].branch", explain)  # 'branches' is not 'branch'
    assert not is_taught("x", "row_union_schema.branches[].fields[].name", explain)  # 'field_type' is not 'name'


def test_gate_derives_a_new_typed_detail_key_from_the_constructor_keywords(tmp_path: Path) -> None:
    """Only keys a site actually passes to the detail constructor count as shipped from that site."""
    module = tmp_path / "state_probe.py"
    module.write_text(
        "def f():\n"
        "    return _err('node:x', 'm', 'high', 'sink_locked_extras', contract=SchemaContractDetail(producer='p', consumer='c', extra_fields=('a',)))\n",
        encoding="utf-8",
    )
    shipped = {(s.code, s.key) for s in _detail_sites([module])}
    assert shipped == {
        ("sink_locked_extras", "contract"),
        ("sink_locked_extras", "contract.producer"),
        ("sink_locked_extras", "contract.consumer"),
        ("sink_locked_extras", "contract.extra_fields"),
    }


@pytest.mark.parametrize(
    "prelude",
    [
        "build = ValidationEntry\nDetail = SchemaContractDetail\n",
        "from elspeth.web.composer.state import ValidationEntry as build, SchemaContractDetail as Detail\n",
    ],
    ids=["assignment-alias", "import-alias"],
)
def test_gate_derives_a_detail_from_aliased_constructors(tmp_path: Path, prelude: str) -> None:
    """Module-level aliases of the entry and detail constructors are resolved, so an alias is a site like any other."""
    module = tmp_path / "alias_probe.py"
    module.write_text(
        prelude + "def f():\n    return build('node:x', 'm', 'high', 'sink_locked_extras', contract=Detail(producer='p'))\n",
        encoding="utf-8",
    )
    shipped = {(s.code, s.key) for s in _detail_sites([module])}
    assert shipped == {("sink_locked_extras", "contract"), ("sink_locked_extras", "contract.producer")}


@pytest.mark.parametrize(
    ("body", "reason"),
    [
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', **extra)", "built with a \\*\\*spread"),
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', detail)", "passes a detail positionally"),
        (
            "_err('node:x', 'm', 'high', 'sink_locked_extras', contract=SchemaContractDetail('p', 'c', ('a',)))",
            "built positionally",
        ),
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', contract=SchemaContractDetail(**kw))", "built positionally"),
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', contract=build_contract_detail())", "built by 'build_contract_detail'"),
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', contract=_detail_for(producer='p'))", "built by '_detail_for'"),
        ("ValidationEntry(*parts, contract=SchemaContractDetail(producer='p'))", "built from \\*args"),
        ("replace(entry, contract=SchemaContractDetail(producer='p'))", "cannot derive error_code"),
        ("_err('node:x', 'm', 'high', 'sink_locked_extras', contract=prebuilt)", "constructed outside an entry site"),
        (
            "functools.partial(ValidationEntry, 'node:x')('m', 'high', 'sink_locked_extras', contract=SchemaContractDetail(producer='p'))",
            "constructed outside an entry site",
        ),
    ],
    ids=[
        "entry-spread",
        "entry-positional-detail",
        "detail-positional",
        "detail-spread",
        "detail-helper-call",
        "detail-helper-with-keywords",
        "entry-starred",
        "replace-with-detail",
        "prebuilt-variable",
        "partial",
    ],
)
def test_gate_refuses_a_typed_detail_site_it_cannot_derive(tmp_path: Path, body: str, reason: str) -> None:
    """Every under-derivable entry shape is refused, at the entry AND at the detail constructor.

    Without the detail-level rule a positional or **-built detail read as
    "passes nothing" and silently skipped every leaf; without these probes the
    entry-level rules could be deleted unnoticed (final red-team F2).
    """
    module = tmp_path / "detail_probe.py"
    module.write_text(
        f"prebuilt = SchemaContractDetail(producer='p')\ndef f(extra, detail, kw, parts, entry):\n    return {body}\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match=reason):
        list(_detail_sites([module]))


def test_gate_catches_prose_that_stops_naming_a_taught_key() -> None:
    """Deleting a key's name from its guidance turns the gate red — the taught side is derived, not listed."""
    target = ("freeform", "source_on_success_dangling", "connectivity.dangling_on_success")
    assert target not in untaught_keys(), "precondition: the catalogue names this key today"

    def explain_without_the_key(code: str) -> tuple[str, str] | None:
        guidance = generation.explain_validation_code(code)
        if guidance is None or code != target[1]:
            return guidance
        return tuple(part.replace("dangling_on_success", "the offending value") for part in guidance)  # type: ignore[return-value]

    assert target in untaught_keys(explain=explain_without_the_key)


def test_typed_keys_recurse_through_nested_and_list_of_typed_dicts() -> None:
    """The typed walker's recursion is pinned by a synthetic payload, not only by today's real ones.

    Dropping the recursion left every gate test green because no self-test
    exercised it directly (final red-team P15): a nested record's inner keys
    ship to the planner exactly as the envelope does.
    """
    assert _typed_keys(_ProbeOuter, "c.") == ["c.record", "c.record.leaf", "c.records", "c.records[].leaf", "c.flat"]


def test_connectivity_sites_enumerate_every_coalesce_reachability_key() -> None:
    """The freeform coalesce facts are enumerated in full, derived from their TypedDict (final red-team P17)."""
    coalesce = {s.key for s in _connectivity_sites() if s.code == "coalesce_branch_unreachable"}
    expected = {"connectivity", *_typed_keys(state.CoalesceReachabilityFactDict, "connectivity.")}
    assert coalesce == expected
    assert len(expected) > 2, "the coalesce payload has nested keys; an envelope-only set means the walker regressed"
