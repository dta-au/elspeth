"""Whole-tree gate: every repair-feedback fact key the composer ships to the planner is taught or fenced.

Repair feedback crosses the planner's message-redaction boundary as structured
facts keyed by a closed ``error_code`` — ``contract`` / ``row_union_schema`` /
``coalesce_union_type`` details and ``connectivity`` facts on the freeform
surface (``pipeline_planner._allowlisted_candidate_feedback``), and the raw
``connectivity`` dict of a ``GuidedCandidateBindingRejected`` on the guided
surface (``pipeline_planner._binding_rejection_feedback``). A fact key is only
usable if the ``(explanation, suggested_fix)`` that
``tools.generation.explain_validation_code(code)`` resolves names it: a key the
model is never told how to read cannot repair anything, and
``sink_targeting_branches`` shipped untaught for its whole life before
cc2b19ce4 noticed (elspeth-68721c71d7).

Both sides are DERIVED, never hand-listed: the shipped key set comes from the
live TypedDicts plus the constructor keywords at every producer site (a
``NotRequired`` key a site never passes can never reach the planner from it)
and from the AST of every guided rejection site; the taught set comes from the
catalogue itself. The only curated input is the fence fixture
(``planner_teaching_fence.json``): keys deliberately left untaught, each with a
reason a reviewer can check. A fence entry that has since become taught, or
whose key no longer ships, is itself a failure — the fence must not outlive
what it fences.
"""

from __future__ import annotations

import ast
import json
import re
import typing
from collections.abc import Iterable, Iterator
from pathlib import Path
from typing import NamedTuple, TypedDict

import pytest

from elspeth.web.composer import pipeline_planner, state
from elspeth.web.composer.guided import planning as guided_planning
from elspeth.web.composer.pipeline_planner import route_destination_fact_keys
from elspeth.web.composer.reviewed_output_projection import ReviewedOutputProjectionConflict
from elspeth.web.composer.tools import generation
from elspeth_lints.core.ast_walker import iter_python_files

REPO_ROOT = Path(__file__).resolve().parents[4]
COMPOSER_SRC = REPO_ROOT / "src" / "elspeth" / "web" / "composer"
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
_GUIDED_CONSTRUCTORS = frozenset({"GuidedCandidateBindingRejected", "_guided_delta_rejection"})
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
    surface: str  # "freeform" | "guided"
    code: str
    key: str  # dotted path from the entry, e.g. "contract.missing_fields", "connectivity.delta_member"
    site: str


class FenceEntry(NamedTuple):
    surface: str
    code: str
    key: str
    reason: str


# --- derivation ---------------------------------------------------------------------------------


def _typed_keys(payload: type, prefix: str) -> list[str]:
    """Flatten a TypedDict to dotted key paths, recursing through nested and list-of TypedDicts."""
    keys: list[str] = []
    for name, hint in typing.get_type_hints(payload, include_extras=False).items():
        path = f"{prefix}{name}"
        keys.append(path)
        args = typing.get_args(hint)
        if typing.is_typeddict(hint):
            keys.extend(_typed_keys(hint, path + "."))
        elif typing.get_origin(hint) is list and args and typing.is_typeddict(args[0]):
            keys.extend(_typed_keys(args[0], path + "[]."))
        else:
            # ``X | None`` around a record: recurse through the record member.
            for arg in args:
                if typing.is_typeddict(arg):
                    keys.extend(_typed_keys(arg, path + "."))
    return keys


def _is_dict_literal(node: ast.AST) -> bool:
    """A dict built inline: a literal, a comprehension, or a ``dict(...)`` call."""
    if isinstance(node, ast.Dict | ast.DictComp):
        return True
    return isinstance(node, ast.Call) and _call_name(node) == "dict"


def _call_name(node: ast.Call) -> str | None:
    if isinstance(node.func, ast.Name):
        return node.func.id
    if isinstance(node.func, ast.Attribute):
        return node.func.attr
    return None


def _literal_str(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and type(node.value) is str else None


def _enclosing_function(tree: ast.Module, lineno: int) -> str | None:
    best: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if node.lineno <= lineno <= (node.end_lineno or lineno) and (best is None or node.lineno > best.lineno):
            best = node
    return None if best is None else best.name


def composer_python_files() -> list[Path]:
    return list(iter_python_files(COMPOSER_SRC))


def _display(path: Path) -> str:
    try:
        return str(path.relative_to(REPO_ROOT))
    except ValueError:
        return str(path)


def _detail_sites(files: Iterable[Path]) -> Iterator[ShippedKey]:
    """Every typed-detail key a ``ValidationEntry`` / ``_err`` construction can ship, per site."""
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or _call_name(node) not in _ENTRY_CONSTRUCTORS:
                continue
            site = f"{_display(path)}:{node.lineno}"
            # Fail CLOSED, as the guided walker does: a ``**spread`` or a detail
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
                    if _call_name(ctor) != _DETAIL_CONSTRUCTORS[name]:
                        raise AssertionError(
                            f"{site}: detail '{name}' built by '{_call_name(ctor)}', not {_DETAIL_CONSTRUCTORS[name]}; "
                            "the gate cannot derive its keys"
                        )
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


def _resolve_guided_code(expr: ast.AST | None, site: str) -> str:
    code = _literal_str(expr)
    if code is not None:
        return code
    if isinstance(expr, ast.Attribute) and expr.attr == "error_code":
        # ``projection_conflict.error_code`` — a Literal-typed field on the conflict type.
        hints = typing.get_type_hints(ReviewedOutputProjectionConflict)
        (literal,) = typing.get_args(hints["error_code"])
        return str(literal)
    raise AssertionError(f"{site}: cannot derive error_code for a guided rejection ({ast.unparse(expr) if expr else 'missing'})")


def _guided_sites(files: Iterable[Path]) -> Iterator[ShippedKey]:
    """Every ``connectivity`` key a guided binder rejection can ship, per raise site."""
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = _call_name(node)
            if name not in _GUIDED_CONSTRUCTORS:
                continue
            site = f"{_display(path)}:{node.lineno}"
            keywords = {kw.arg: kw.value for kw in node.keywords if kw.arg}
            facts: ast.expr | None
            if name == "_guided_delta_rejection":
                code = _resolve_guided_code(node.args[0] if node.args else keywords.get("error_code"), site)
                facts = keywords.get("facts", ast.Dict(keys=[], values=[]))
            else:
                if _enclosing_function(tree, node.lineno) == "_guided_delta_rejection":
                    continue  # the helper's own body forwards its ``facts`` argument; the call sites carry the literals
                code = _resolve_guided_code(keywords.get("error_code"), site)
                facts = keywords.get("connectivity")
            if not isinstance(facts, ast.Dict):
                raise AssertionError(
                    f"{site}: connectivity facts are not a dict literal ({ast.unparse(facts) if facts else 'missing'}); the gate cannot derive their keys"
                )
            if facts.keys:
                # ``_binding_rejection_feedback`` attaches the dict only when non-empty, so an empty
                # literal ships no envelope either.
                yield ShippedKey("guided", code, "connectivity", site)
            for key_node, value_node in zip(facts.keys, facts.values, strict=True):
                key = _literal_str(key_node)
                if key is None:
                    raise AssertionError(f"{site}: non-literal connectivity key {ast.unparse(key_node) if key_node else '**'}")
                if any(_is_dict_literal(inner) for inner in ast.walk(value_node)):
                    # A nested record ships its inner keys just as the envelope
                    # does, and this walker enumerates one level. Refuse a dict
                    # ANYWHERE in the value — under ``cast(...)``, in a list or
                    # tuple, or via ``dict(...)`` — rather than under-report
                    # (final red-team F1, both rounds): ship a typed record the
                    # freeform walker recurses through, or flatten the facts.
                    raise AssertionError(f"{site}: connectivity['{key}'] carries a nested dict; the gate cannot derive its inner keys")
                yield ShippedKey("guided", code, f"connectivity.{key}", site)


def shipped_keys(files: Iterable[Path] | None = None) -> list[ShippedKey]:
    paths = composer_python_files() if files is None else list(files)
    return [*_detail_sites(paths), *_connectivity_sites(), *_guided_sites(paths)]


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
    leaf = key.split(".")[-1].replace("[]", "")
    text = " ".join(guidance)
    return re.search(rf"['`]{re.escape(leaf)}['`]", text) is not None


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
        "Teach each key in tools/generation.py (_VALIDATION_ERROR_PATTERNS entry for its code) or fence it with a "
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


def test_gate_derives_a_new_guided_key_from_the_ast(tmp_path: Path) -> None:
    """A fresh fact key at a guided raise site must surface as untaught, with no list to update."""
    module = tmp_path / "planning_probe.py"
    module.write_text(
        "def f():\n"
        "    raise _guided_delta_rejection('guided_delta_authority_violation', facts={'delta_member': 'x', 'brand_new_fact': 1})\n"
        "def g():\n"
        "    raise GuidedCandidateBindingRejected('m', error_code='guided_route_target_unknown', connectivity={'declared_sinks': [], 'another_new_fact': 2})\n",
        encoding="utf-8",
    )
    untaught = untaught_keys([module])
    assert ("guided", "guided_delta_authority_violation", "connectivity.brand_new_fact") in untaught
    assert ("guided", "guided_route_target_unknown", "connectivity.another_new_fact") in untaught
    # and a key the catalogue already names is NOT reported
    assert ("guided", "guided_route_target_unknown", "connectivity.declared_sinks") not in untaught


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


def test_gate_refuses_a_guided_site_it_cannot_derive(tmp_path: Path) -> None:
    module = tmp_path / "opaque_probe.py"
    module.write_text(
        "def f(facts):\n    raise GuidedCandidateBindingRejected('m', error_code='guided_route_target_unknown', connectivity=facts)\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="cannot derive their keys"):
        list(_guided_sites([module]))


@pytest.mark.parametrize(
    "value",
    [
        "{'inner_untaught': 1}",
        "cast(JsonValue, {'inner_untaught': 1})",
        "[{'inner_untaught': 1}]",
        "({'inner_untaught': 1},)",
        "dict(inner_untaught=1)",
        "{k: 1 for k in ('inner_untaught',)}",
    ],
    ids=["literal", "cast-wrapped", "in-list", "in-tuple", "dict-call", "comprehension"],
)
def test_gate_refuses_a_nested_dict_value_at_a_guided_site(tmp_path: Path, value: str) -> None:
    """Inner keys of a dict value would ship untaught and unenumerated; the walker refuses a dict anywhere in the value.

    The first fix refused only a bare literal; ``cast(JsonValue, {...})`` — the
    house-style wrapper every real site uses — a list, a tuple and ``dict(...)``
    all slipped past it (final red-team F1, second round).
    """
    module = tmp_path / "nested_probe.py"
    module.write_text(
        f"def f():\n    raise _guided_delta_rejection('guided_delta_authority_violation', facts={{'extra': {value}}})\n",
        encoding="utf-8",
    )
    with pytest.raises(AssertionError, match="nested dict"):
        list(_guided_sites([module]))


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
    ],
    ids=[
        "entry-spread",
        "entry-positional-detail",
        "detail-positional",
        "detail-spread",
        "detail-helper-call",
        "detail-helper-with-keywords",
        "entry-starred",
    ],
)
def test_gate_refuses_a_typed_detail_site_it_cannot_derive(tmp_path: Path, body: str, reason: str) -> None:
    """Every under-derivable entry shape is refused, at the entry AND at the detail constructor.

    Without the detail-level rule a positional or **-built detail read as
    "passes nothing" and silently skipped every leaf; without these probes the
    entry-level rules could be deleted unnoticed (final red-team F2).
    """
    module = tmp_path / "detail_probe.py"
    module.write_text(f"def f(extra, detail, kw, parts):\n    return {body}\n", encoding="utf-8")
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


def test_every_guided_site_is_derivable() -> None:
    """Every guided raise site in the tree carries a literal code and a dict-literal fact set."""
    sites = list(_guided_sites([Path(guided_planning.__file__)]))
    assert sites, "no guided rejection sites found — the walker or the constructor names drifted"
    assert all(s.surface == "guided" for s in sites)
