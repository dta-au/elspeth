"""Clean fixture: PEP 562 module __getattr__ gated on a closed literal table.

Amnesty (a): module-level ``__getattr__`` (AST parent is ``Module``) whose
body is a flat sequence of ``if name == "..."`` / ``if name in <closed
table>:`` guards followed by an unconditional ``raise AttributeError``.
Once ``name`` is gated to one of a fixed set of literals, nothing inside
the guarded branch can leak an attacker-chosen name through.
"""

from __future__ import annotations

_EXPORTS = ("Foo", "Bar")


def __getattr__(name: str) -> object:
    if name in _EXPORTS:
        return globals()[f"_resolve_{name}"]()
    raise AttributeError(name)


def _resolve_Foo() -> str:
    return "foo"


def _resolve_Bar() -> str:
    return "bar"
