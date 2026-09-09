"""Closed inventory of ``after_verified`` posture at every replay call site.

``reserve_or_replay_guided_operation`` runs the caller's ``replay`` callable
**before** it compares the projected response against the stored response
hash (``routes/guided_operations.py::_replay_completed``). Its docstring
states the rule: "``replay`` MUST be side-effect-free ... Every write a
replay owes belongs in ``after_verified``, which runs solely once the
projected response has been proven identical to the stored one."

A ``replay`` callable that writes therefore mutates audit-primary state under
a projection not yet proven correct, and is rejected only afterwards. The
2026-09-01 ``state_revert`` regression did exactly that: its ``replay``
surfaced interpretation reviews, which inserts ``interpretation_events`` rows
and supersedes existing pending ones, a month after the ordering was
established on the guided RESPOND route. Nothing went red, because nothing in
``tests/`` referenced ``after_verified`` at all.

**Scope, stated honestly.** This gate does not prove purity by reachability.
Whether a given callable reaches a durable write is a transitive property
across service dispatch that no AST walk can decide soundly, so this pins the
*declaration* instead: which route function calls the primitive, **how many
times**, and whether those calls pass ``after_verified``. It is
declaration-enforced, not reachability-enforced, and the behavioural
ordering proof lives in the two paired regressions:

* ``tests/unit/web/sessions/test_routes.py::TestRevertEndpoint::
  test_revert_replay_writes_nothing_when_the_stored_response_hash_mismatches``
* ``tests/integration/web/composer/guided/test_respond.py::TestStep2IntraStep::
  test_confirm_wiring_replay_writes_nothing_when_the_stored_response_hash_mismatches``

**Why the inventory counts rather than collects.** The pinned value is a
per-``(module, function, posture)`` COUNT, so a new call site, a removed one,
and a silently changed posture each go red. A *set* of those tuples would not:
six of the eight entries hold more than one call site today, so an extra site
added inside an already-listed function collapses into its existing tuple and
changes nothing. That is not hypothetical. Measured either side of
``d64cc6106``: ``post_guided_convert`` went from one call site to two and the
tree went from 22 sites to 23, while the set of eight tuples stayed
byte-identical.

**What neither key catches, stated so nobody reads green as absolution.** The
2026-09-01 regression added no call site: ``370e3bdf0^`` and ``370e3bdf0`` both
hold 22 sites, with ``revert_state`` at one site, posture ``False``, on either
side. It was a durable write added *inside* an existing ``replay`` callable,
which moves no declaration and so moves neither a set key nor a count key.
Nothing automated catches that shape. The two paired regressions above cover
``revert_state`` and ``post_guided_respond`` only, and a ``False`` entry below
is a convention addressed to the author, not an enforcement. The question under
"If this test failed for you" is the whole control, and it works only if you
answer it honestly.

Line numbers are deliberately NOT pinned. A line-keyed inventory reds on every
unrelated edit above a call site, and a gate that cries wolf gets widened until
it means nothing. Counting is the least brittle key that still makes all three
claims true: it carries no line data, so arbitrary churn above a call site
cannot move it, and it is sensitive to exactly the three edits the claims name.
A single total-call-count assertion would be weaker: moving a site from one
route to another, or flipping one posture, both keep the total at 23 while
silently re-attributing the posture claim.

**Why rebinding is forbidden rather than resolved.** The walk below matches the
callee by literal identifier, so any REBINDING of the primitive's name would
hide a call site from it completely — an aliased import
(``import reserve_or_replay_guided_operation as _r``), a module-level
``_r = reserve_or_replay_guided_operation``, or a ``functools.partial``. No AST
walk can resolve aliases soundly in general, so the second test closes the hole
at the other end: under ``src/elspeth/`` the name is only ever imported
unaliased and only ever appears in callee position. The identifier match is
then sound *because* rebinding is forbidden and enforced.

**Residual scope, stated plainly.** Both assertions cover ``src/elspeth/``
only. Every *string-keyed* dynamic lookup is caught, in any spelling —
``getattr(module, "…")``, ``globals()["…"]``, ``vars(module)["…"]`` — because
the walk also rejects the primitive's name written as a string constant
anywhere under that root (zero occurrences today). What remains out of reach
of any sound AST walk is a name assembled at runtime rather than written down.

That constant rule is deliberately blunt, and it costs one false positive:
``__all__ = ["reserve_or_replay_guided_operation"]`` reds this gate even though
re-exporting under the *same* name is harmless to the inventory. No module does
that today, and the remedy is to not re-export a routing primitive rather than
to carve ``__all__`` out of the rule — an exemption is a second place a string
can hide, for a pattern nobody needs.

**If this test failed for you**, you added, removed, or re-postured a
``reserve_or_replay_guided_operation`` call. Before touching the inventory,
answer one question: does your ``replay`` callable write anything durable,
directly or through any service method it calls?

1. If it does, that is the defect. Split it the way
   ``routes/composer/state.py::revert_state`` and
   ``routes/composer/guided.py::post_guided_respond`` do — a projection-only
   ``replay``, and a separate callable passed as ``after_verified`` that
   re-resolves the locator and performs the write. Then record the site here
   with ``True``.
2. If it genuinely owes no write, add it with ``False``.

Do not simply widen the inventory. A ``False`` entry is a claim that the
callable is side-effect-free; make it true before you write it down.
"""

from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path

from tests.helpers.tree_gate import iter_gate_sources

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _ROOT / "src" / "elspeth"

_PRIMITIVE_NAME = "reserve_or_replay_guided_operation"

# (module path relative to src/elspeth, enclosing function, passes
# ``after_verified``) -> number of call sites. Only two routes owe a
# post-verification write today: both settle durable state in the same
# transaction that terminalizes the operation, then surface interpretation
# reviews afterwards, so an attempt that dies in between leaves debt every
# retry must repair.
_EXPECTED_SITES: dict[tuple[str, str, bool], int] = {
    # 2 -> 3 by the multi-replica merge (elspeth-4d6c0dd0f5, adjudication
    # D-M3-6): mainline's expired-lease takeover classifier and the platform's
    # terminal-replay-without-authority probe are KEPT as two lookups rather
    # than fused. Mainline's classifier sits OUTSIDE ``async with
    # compose_lock``, so fusing them is a real semantic change and therefore an
    # escalation, not a merge-time call; keeping both preserves mainline's
    # takeover semantics AND the platform's replay property. The redundancy is
    # a recorded code-quality follow-up, not a fence loss.
    ("web/sessions/routes/composer/guided.py", "post_guided_convert", False): 3,
    ("web/sessions/routes/composer/guided.py", "post_guided_reenter", False): 2,
    ("web/sessions/routes/composer/guided.py", "post_guided_respond", True): 5,
    ("web/sessions/routes/composer/guided.py", "post_guided_start", False): 2,
    ("web/sessions/routes/composer/guided_chat_atomic.py", "post_guided_chat_schema8", False): 5,
    ("web/sessions/routes/composer/guided_plan.py", "post_guided_plan", False): 5,
    # 1 -> 2 by the same merge: the platform splits mainline's single reserve
    # into a terminal-replay PROBE plus a settling reserve, and
    # ``after_verified`` now rides BOTH. That is what makes the H1 surfacing
    # repair reachable on the terminal-replay path; putting it only on the
    # settling call would leave the repair dead there. The posture stays
    # ``True`` on both, which is the property this gate exists to pin.
    ("web/sessions/routes/composer/state.py", "revert_state", True): 2,
    ("web/sessions/routes/sessions.py", "fork_from_message", False): 1,
}


def _callee_identifier(callee: ast.expr) -> str | None:
    """The bare identifier a call resolves through, in either spelling."""
    if isinstance(callee, ast.Name):
        return callee.id
    if isinstance(callee, ast.Attribute):
        return callee.attr
    return None


class _ReplaySiteVisitor(ast.NodeVisitor):
    """Collect ``reserve_or_replay_guided_operation(...)`` calls and posture.

    Matches the callee name exactly, in both the bare ``name(...)`` and the
    qualified ``module.name(...)`` forms. The primitive's parameters are
    keyword-only (``*`` in its signature), so a posture can never be smuggled
    in positionally and reading ``node.keywords`` is complete.

    Every call site is recorded, not a deduplicated tuple: multiplicity is
    part of the pinned value (see the module docstring).

    The same walk records every REBINDING of the identifier, because the
    identifier match above is only sound while no rebinding exists. Callee
    position is the one legitimate use; an aliased import, a store, or a
    load anywhere else (an argument to ``functools.partial``, a dispatch
    table, a decorator) is reported, as is the name written as a string
    constant, which is how every dynamic lookup must spell it to reach the
    primitive at all. ``@overload`` stubs and the
    implementation signature spell the parameters as annotations on a ``def``
    and the primitive's own name as a ``FunctionDef.name`` string, so neither
    is a ``Call`` nor a ``Name`` and no exclusion list is needed for them.
    """

    def __init__(self, relative: str, sites: Counter[tuple[str, str, bool]], rebindings: list[tuple[str, int, str]]) -> None:
        self._relative = relative
        self._sites = sites
        self._rebindings = rebindings
        self._functions: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            # ``import x as x`` is an explicit re-export, not a rebinding.
            if alias.name == _PRIMITIVE_NAME and alias.asname not in (None, alias.name):
                self._rebindings.append((self._relative, node.lineno, f"aliased import as {alias.asname!r}"))
        self.generic_visit(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            if alias.name.rsplit(".", 1)[-1] == _PRIMITIVE_NAME and alias.asname not in (None, alias.name):
                self._rebindings.append((self._relative, node.lineno, f"aliased import as {alias.asname!r}"))
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, f"bare reference ({type(node.ctx).__name__})"))
        self.generic_visit(node)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if node.attr == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, f"bare attribute reference ({type(node.ctx).__name__})"))
        self.generic_visit(node)

    def visit_Constant(self, node: ast.Constant) -> None:
        # A string-keyed lookup -- ``getattr(module, "...")``, ``globals()["..."]``,
        # ``vars(module)["..."]`` -- reaches the primitive without ever placing
        # its identifier in callee position, so the inventory above cannot see
        # the call. Every such spelling must name it as a string exactly once;
        # rejecting that constant closes all of them at their common seam.
        if isinstance(node.value, str) and node.value == _PRIMITIVE_NAME:
            self._rebindings.append((self._relative, node.lineno, "name as a string constant (dynamic lookup)"))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        if _callee_identifier(node.func) == _PRIMITIVE_NAME:
            enclosing = self._functions[-1] if self._functions else "<module>"
            passes_after_verified = any(keyword.arg == "after_verified" for keyword in node.keywords)
            self._sites[(self._relative, enclosing, passes_after_verified)] += 1
            # Callee position is the legitimate use, so it is NOT a rebinding.
            # Descend past the callee node itself, keeping any qualifying
            # expression (``module`` in ``module.name(...)``) under the walk.
            if isinstance(node.func, ast.Attribute):
                self.visit(node.func.value)
        else:
            self.visit(node.func)
        for argument in node.args:
            self.visit(argument)
        for keyword in node.keywords:
            self.visit(keyword)


def _scan_production_tree() -> tuple[Counter[tuple[str, str, bool]], list[tuple[str, int, str]]]:
    """Call-site counts and identifier rebindings across ``src/elspeth``."""
    sites: Counter[tuple[str, str, bool]] = Counter()
    rebindings: list[tuple[str, int, str]] = []
    for parsed in iter_gate_sources(_SOURCE_ROOT):
        relative = parsed.path.relative_to(_SOURCE_ROOT).as_posix()
        _ReplaySiteVisitor(relative, sites, rebindings).visit(parsed.tree)
    return sites, rebindings


def test_every_guided_operation_replay_site_declares_its_after_verified_posture() -> None:
    """A replay that writes before the hash check corrupts audit-primary state."""
    sites, _rebindings = _scan_production_tree()
    assert dict(sites) == _EXPECTED_SITES


def test_the_guided_operation_replay_primitive_is_never_rebound() -> None:
    """Rebinding the name would hide a call site from the inventory above.

    The inventory matches the callee by literal identifier. That match is
    sound only while ``reserve_or_replay_guided_operation`` means exactly one
    thing everywhere under ``src/elspeth`` — so an aliased import, an
    assignment of the bare name, the name passed as an argument (which is how
    ``functools.partial`` and decorator-style wrapping capture it), or the name
    written as a string constant (which is how every dynamic lookup spells it)
    is rejected here rather than silently narrowing the gate above.
    """
    sites, rebindings = _scan_production_tree()
    # Non-vacuity anchor. ``rebindings == []`` is equally satisfied by a walk
    # that read nothing at all, so pin that this walk reached the same corpus
    # the inventory above measures: a silent narrowing of ``iter_gate_sources``
    # reds here instead of reporting "no rebindings" over files it never read.
    assert sum(sites.values()) == sum(_EXPECTED_SITES.values())
    assert rebindings == []
