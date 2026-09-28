"""Conservative result types for validated row expressions.

Only successful evaluations contribute a result kind. Unknown syntax or input
types abstain to ``any``; this module never reads row values.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from typing import Literal

from elspeth.contracts.schema import FieldDefinition
from elspeth.core.expression_parser import ExpressionParser

type ResultKinds = frozenset[str] | None
type SchemaFieldType = Literal["str", "int", "float", "bool", "any"]
_NUMERIC = frozenset({"bool", "int", "float"})


def input_kinds(field: FieldDefinition | None, *, unknown_nullable: bool = False) -> ResultKinds:
    """Map an enforced input declaration to the values its check admits."""
    if field is None or field.field_type == "any":
        return None
    kinds: set[str] = {"int", "float"} if field.field_type == "float" else {field.field_type}
    if field.nullable or unknown_nullable:
        kinds.add("none")
    return frozenset(kinds)


def _union(*groups: ResultKinds) -> ResultKinds:
    if any(group is None for group in groups):
        return None
    result: set[str] = set()
    for group in groups:
        assert group is not None
        result.update(group)
    return frozenset(result)


def _binary(op: ast.operator, left: ResultKinds, right: ResultKinds) -> ResultKinds:
    if left is None or right is None:
        return None
    results: set[str] = set()
    for a in left:
        for b in right:
            if a in _NUMERIC and b in _NUMERIC:
                if isinstance(op, ast.Div):
                    results.add("float")
                elif isinstance(op, (ast.Add, ast.Sub, ast.Mult, ast.FloorDiv, ast.Mod)):
                    results.add("float" if "float" in (a, b) else "int")
            elif isinstance(op, ast.Add) and a == b and a in {"str", "list", "tuple"}:
                results.add(a)
            elif isinstance(op, ast.Mult) and (
                (a in {"str", "list", "tuple"} and b in {"int", "bool"}) or (b in {"str", "list", "tuple"} and a in {"int", "bool"})
            ):
                results.add(a if a in {"str", "list", "tuple"} else b)
            elif isinstance(op, ast.Mod) and a == "str":
                results.add("str")
    return frozenset(results)


def expression_kinds(parser: ExpressionParser, lookup: Callable[[str], ResultKinds]) -> ResultKinds:
    """Infer result kinds from an already validated ExpressionParser."""

    def infer(node: ast.expr) -> ResultKinds:
        if isinstance(node, ast.Constant):
            value = node.value
            if value is None:
                return frozenset({"none"})
            if isinstance(value, bool):
                return frozenset({"bool"})
            if isinstance(value, int):
                return frozenset({"int"})
            if isinstance(value, float):
                return frozenset({"float"})
            if isinstance(value, str):
                return frozenset({"str"})
            return None
        if isinstance(node, ast.Subscript):
            if isinstance(node.value, ast.Name) and node.value.id == "row":
                key = node.slice
                if isinstance(key, ast.Constant) and isinstance(key.value, str):
                    return lookup(key.value)
            return None
        if isinstance(node, ast.Call):
            func = node.func
            if isinstance(func, ast.Attribute) and func.attr == "get" and isinstance(func.value, ast.Name) and func.value.id == "row":
                if len(node.args) == 1 and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                    return _union(lookup(node.args[0].value), frozenset({"none"}))
                return None
            if isinstance(func, ast.Name):
                if func.id == "len":
                    return frozenset({"int"})
                if func.id in {"lower", "upper", "strip", "casefold"}:
                    return frozenset({"str"})
                if func.id == "abs" and len(node.args) == 1:
                    argument = infer(node.args[0])
                    if argument is None:
                        return None
                    return frozenset("float" if kind == "float" else "int" for kind in argument if kind in _NUMERIC)
            return None
        if isinstance(node, ast.Compare):
            return frozenset({"bool"})
        if isinstance(node, ast.UnaryOp):
            if isinstance(node.op, ast.Not):
                return frozenset({"bool"})
            argument = infer(node.operand)
            if argument is None:
                return None
            return frozenset("float" if kind == "float" else "int" for kind in argument if kind in _NUMERIC)
        if isinstance(node, (ast.BoolOp, ast.IfExp)):
            arms = node.values if isinstance(node, ast.BoolOp) else [node.body, node.orelse]
            return _union(*(infer(arm) for arm in arms))
        if isinstance(node, ast.BinOp):
            return _binary(node.op, infer(node.left), infer(node.right))
        if isinstance(node, ast.List):
            return frozenset({"list"})
        if isinstance(node, ast.Tuple):
            return frozenset({"tuple"})
        if isinstance(node, ast.Dict):
            return frozenset({"dict"})
        return None

    return infer(parser._ast.body)


def declared_result(kinds: ResultKinds) -> tuple[SchemaFieldType, bool]:
    """Collapse runtime kinds to the schema vocabulary and nullability."""
    if kinds is None:
        return "any", True
    non_null = kinds - {"none"}
    field_type: SchemaFieldType
    if non_null == {"int"}:
        field_type = "int"
    elif non_null and non_null <= {"int", "float"}:
        field_type = "float"
    elif non_null == {"str"}:
        field_type = "str"
    elif non_null == {"bool"}:
        field_type = "bool"
    else:
        field_type = "any"
    return field_type, field_type == "any" or "none" in kinds
