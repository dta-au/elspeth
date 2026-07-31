"""Closed inventory of production ``ComposerLLMCall`` construction sites.

Provider-authored strings on a composer LLM audit row (``model_returned``,
``provider_request_id``, ``finish_reason``) are bounded at their extraction
point in ``web/composer/llm_response_parsing.build_llm_call_record`` — the
sole place in ``src`` that constructs a ``ComposerLLMCall``. The contract
itself deliberately does **not** re-check their length (see the class
docstring on ``contracts/composer_llm_audit.ComposerLLMCall``): a contract
that rejected an oversized value would raise instead of recording, which
discards the very evidence that the endpoint misbehaved, and a second bound
in a second place would be a second limit to keep in sync.

That makes the guarantee **capture-point-enforced, not contract-enforced**.
It holds only while the builder remains the single construction site. A
second one would silently inherit no bounds at all, and no other test in the
suite would go red — hence this guard.

**If this test failed for you**, you added a ``ComposerLLMCall(...)`` outside
the builder. Two options, in order of preference:

1. Route your construction through ``build_llm_call_record`` so it inherits
   the bounds (and the token-usage, cost, and hashing logic alongside them).
2. If that is genuinely impossible, replicate every bound the builder
   applies, add your site to ``_EXPECTED_SITES`` below with a comment saying
   why, and update the ``ComposerLLMCall`` class docstring — which currently
   states in prose that the builder is the sole site.

Do not simply widen the inventory. Doctrine: ADR-032; the bounds landed in
``fix(composer): bound provider-authored strings on composer audit rows``.
"""

from __future__ import annotations

import ast
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[3]
_SOURCE_ROOT = _ROOT / "src" / "elspeth"

_CONTRACT_NAME = "ComposerLLMCall"

# (module path relative to src/elspeth, enclosing function). The builder is
# the only site that may bound provider strings on the way in.
_EXPECTED_SITES: frozenset[tuple[str, str]] = frozenset(
    {
        ("web/composer/llm_response_parsing.py", "build_llm_call_record"),
    }
)


class _ConstructionVisitor(ast.NodeVisitor):
    """Collect ``ComposerLLMCall(...)`` calls with their enclosing function.

    Matches the callee name exactly, so the sibling ``ComposerLLMCallStatus``
    enum — referenced all over the composer — is not mistaken for a
    construction of the record itself. Both the bare ``ComposerLLMCall(...)``
    and a qualified ``module.ComposerLLMCall(...)`` form are caught.
    """

    def __init__(self, relative: str, sites: set[tuple[str, str]]) -> None:
        self._relative = relative
        self._sites = sites
        self._functions: list[str] = []

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._functions.append(node.name)
        self.generic_visit(node)
        self._functions.pop()

    def visit_Call(self, node: ast.Call) -> None:
        callee = node.func
        name = callee.id if isinstance(callee, ast.Name) else callee.attr if isinstance(callee, ast.Attribute) else None
        if name == _CONTRACT_NAME:
            enclosing = self._functions[-1] if self._functions else "<module>"
            self._sites.add((self._relative, enclosing))
        self.generic_visit(node)


def _production_construction_sites() -> set[tuple[str, str]]:
    sites: set[tuple[str, str]] = set()
    for path in _SOURCE_ROOT.rglob("*.py"):
        relative = path.relative_to(_SOURCE_ROOT).as_posix()
        _ConstructionVisitor(relative, sites).visit(ast.parse(path.read_text(encoding="utf-8"), filename=str(path)))
    return sites


def test_build_llm_call_record_is_the_sole_production_construction_site() -> None:
    """A second construction site would silently bypass the string bounds."""
    assert _production_construction_sites() == set(_EXPECTED_SITES)
