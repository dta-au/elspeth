"""``ast.dump`` in one shape on every supported interpreter.

Python 3.13 changed :func:`ast.dump`'s default output: fields that are an
empty list, and ``None`` fields whose node class declares a ``None`` default,
are omitted (``show_empty=False``). Every AST fingerprint this repository pins
— canonical route definitions, session-writer inventories, Landscape DML
identities, masquerade probe shapes — was generated on the 3.13 development
runtime, so a gate that calls :func:`ast.dump` directly certifies a different
inventory on 3.12 than on 3.13 (elspeth-b4f1be3f80). :func:`stable_ast_dump`
is a version-independent port of the 3.13 algorithm; on 3.13 it is
byte-identical to ``ast.dump(node, annotate_fields=..., include_attributes=False)``
(pinned over the whole tree by tests/unit/elspeth_lints/test_stable_ast_dump.py),
and on older interpreters it produces that same string.

Fields are read through :func:`ast.iter_fields` and class defaults through
``vars()`` over the MRO — no dynamic attribute probe, so the module is not
itself a masquerade site. ``include_attributes`` is not offered: no
fingerprint in this repository includes positions, and offering the flag
would only invite one.
"""

from __future__ import annotations

import ast

__all__ = ["stable_ast_dump"]

# ``None`` is the VALUE of these nodes, never an absent optional field.
_NONE_IS_A_VALUE = (ast.Constant, ast.MatchSingleton)


def stable_ast_dump(node: ast.AST, *, annotate_fields: bool = True) -> str:
    """Return ``node`` in Python 3.13's default ``ast.dump`` shape.

    ``annotate_fields`` keeps :func:`ast.dump`'s meaning: ``True`` renders
    every field as ``name=value``; ``False`` renders unambiguous leading
    fields positionally and switches to keywords at the first omitted field.
    """
    if not isinstance(node, ast.AST):
        raise TypeError(f"expected AST, got {type(node).__name__}")
    return _format(node, annotate_fields)[0]


def _class_default_is_none(cls: type, name: str) -> bool:
    """Whether ``cls`` declares ``name`` as an optional field defaulting to ``None``."""
    for base in cls.__mro__:
        namespace = vars(base)
        if name in namespace:
            return namespace[name] is None
    return False


def _format(value: object, annotate_fields: bool) -> tuple[str, bool]:
    if isinstance(value, ast.AST):
        cls = type(value)
        present = dict(ast.iter_fields(value))
        args: list[str] = []
        args_buffer: list[str] = []
        allsimple = True
        keywords = annotate_fields
        for name in value._fields:
            if name not in present:
                keywords = True
                continue
            field = present[name]
            if field is None and _class_default_is_none(cls, name):
                keywords = True
                continue
            if isinstance(field, list) and not field and not isinstance(value, _NONE_IS_A_VALUE):
                if not keywords:
                    args_buffer.append(repr(field))
                continue
            if not keywords:
                args.extend(args_buffer)
                args_buffer = []
            rendered, simple = _format(field, annotate_fields)
            allsimple = allsimple and simple
            args.append(f"{name}={rendered}" if keywords else rendered)
        if allsimple and len(args) <= 3:
            return f"{cls.__name__}({', '.join(args)})", not args
        return f"{cls.__name__}({', '.join(args)})", False
    if isinstance(value, list):
        if not value:
            return "[]", True
        return "[" + ", ".join(_format(item, annotate_fields)[0] for item in value) + "]", False
    return repr(value), True
