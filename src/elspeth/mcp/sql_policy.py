"""Closed PostgreSQL 16 syntax admission for the audit query APIs.

This is only the grammar half of admission. The executing connection must also
pass ``postgres_profile``: a raw parse tree does not resolve executable objects.
The optional PostgreSQL parser is imported only when this boundary is invoked.
"""

from __future__ import annotations

import json
import math
from collections.abc import Callable, Mapping, Set
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from typing import cast

from sqlalchemy import text
from sqlalchemy.dialects.postgresql.base import PGDialect
from sqlalchemy.dialects.postgresql.psycopg import PGDialect_psycopg
from sqlalchemy.dialects.postgresql.psycopg2 import PGDialect_psycopg2

from elspeth.core.landscape.schema import metadata

SQL_BYTES = 64 * 1024
AST_NODES = 8192
AST_DEPTH = 64
PARAMETER_BYTES = 1024 * 1024

SCALAR_TYPES = frozenset(
    {
        "bool",
        "int2",
        "int4",
        "int8",
        "float4",
        "float8",
        "numeric",
        "text",
        "varchar",
        "bpchar",
        "date",
        "time",
        "timestamp",
        "timestamptz",
        "interval",
        "bytea",
    }
)
SCALAR_FUNCTIONS = frozenset({"lower", "upper", "length", "abs", "round"})
AGGREGATES = frozenset({"count", "sum", "avg", "min", "max", "bool_and", "bool_or"})
OPERATORS = frozenset({"=", "<>", "<", ">", "<=", ">=", "+", "-", "*", "/", "%", "||"})

# SQLAlchemy exposes these concrete constructors without annotations. These
# callable types describe only the invocation shapes used below; runtime class
# identity and native dialect compilation remain unchanged.
_numeric_dialect = cast(Callable[..., PGDialect], PGDialect)
_psycopg_dialect = cast(Callable[[], PGDialect_psycopg], PGDialect_psycopg)
_psycopg2_dialect = cast(Callable[[], PGDialect_psycopg2], PGDialect_psycopg2)


class SQLPolicyError(ValueError):
    """Caller SQL or an execution dependency is outside the qualified policy."""


@dataclass(frozen=True, slots=True)
class AdmittedSQL:
    """Syntax evidence, never authorization to execute without profile admission."""

    sql: str
    positional_sql: str
    parameter_names: tuple[str, ...]
    relations: frozenset[str]
    functions: frozenset[str]


def _bounded_sql(sql: str) -> None:
    if type(sql) is not str:
        raise SQLPolicyError("PostgreSQL SQL must be a string")
    if not sql or len(sql) > SQL_BYTES or "\x00" in sql:
        raise SQLPolicyError("PostgreSQL SQL is empty, contains NUL, or exceeds its text budget")
    try:
        size = len(sql.encode("utf-8"))
    except UnicodeEncodeError as exc:
        raise SQLPolicyError("PostgreSQL SQL is not valid UTF-8 text") from exc
    if size > SQL_BYTES:
        raise SQLPolicyError("PostgreSQL SQL exceeds its text budget")


def _check_parameters(params: Mapping[str, object] | None) -> None:
    if params is None:
        return
    if type(params) is not dict:
        raise SQLPolicyError("PostgreSQL parameters must be a plain dictionary")
    remaining = PARAMETER_BYTES
    for name, value in params.items():
        if type(name) is not str or not name or len(name) > SQL_BYTES:
            raise SQLPolicyError("Unsupported PostgreSQL parameter name")
        remaining -= len(name) * 4 + 1
        if type(value) in (str, bytes):
            remaining -= len(value) * 4
        elif type(value) is time:
            if value.tzinfo is not None:
                raise SQLPolicyError("PostgreSQL time-with-timezone parameters are not supported")
            remaining -= 64
        elif value is None or type(value) in (bool, date, datetime, timedelta):
            remaining -= 64
        elif type(value) is int:
            remaining -= max(1, value.bit_length())
        elif type(value) is float:
            if not math.isfinite(value):
                raise SQLPolicyError("Nonfinite PostgreSQL parameter")
            remaining -= 64
        elif type(value) is Decimal:
            if not value.is_finite():
                raise SQLPolicyError("Nonfinite PostgreSQL parameter")
            remaining -= len(value.as_tuple().digits) + 64
        else:
            raise SQLPolicyError("Unsupported PostgreSQL parameter type; driver adapters are not admitted")
        if remaining < 0:
            raise SQLPolicyError("PostgreSQL parameters exceed their input budget")


def _object(value: object, fields: set[str], required: Set[str] = frozenset()) -> dict[str, object]:
    if type(value) is not dict or not required <= value.keys() or not value.keys() <= fields:
        raise SQLPolicyError("Unsupported PostgreSQL syntax shape")
    return value


def _list(value: object) -> list[object]:
    if type(value) is not list:
        raise SQLPolicyError("Unsupported PostgreSQL syntax list")
    return value


def _string(value: object) -> str:
    if type(value) is not str:
        raise SQLPolicyError("Unsupported PostgreSQL identifier")
    return value


def _names(value: object) -> tuple[str, ...]:
    names = []
    for item in _list(value):
        node = _object(item, {"String"}, {"String"})
        body = _object(node["String"], {"sval"}, {"sval"})
        names.append(_string(body["sval"]))
    return tuple(names)


def _qualified(value: object) -> str:
    names = _names(value)
    if len(names) == 1:
        return names[0]
    if len(names) == 2 and names[0] == "pg_catalog":
        return names[1]
    raise SQLPolicyError("Only qualified builtin PostgreSQL expression names are supported")


def _bounded_tree(value: object) -> None:
    pending = [(value, 0)]
    remaining = AST_NODES
    while pending:
        item, depth = pending.pop()
        remaining -= 1
        if remaining < 0 or depth > AST_DEPTH:
            raise SQLPolicyError("PostgreSQL syntax exceeds its node or depth budget")
        if type(item) is dict:
            pending.extend((child, depth + 1) for child in item.values())
        elif type(item) is list:
            pending.extend((child, depth + 1) for child in item)


class _Columns:
    def __init__(self, initial: tuple[str, ...] = ()) -> None:
        self.qualified: dict[str, tuple[str, ...]] = {}
        self.unqualified = initial


class _Policy:
    def __init__(
        self, tables: frozenset[str], parameters: int, *, stored_expression: bool = False, stored_columns: frozenset[str] = frozenset()
    ) -> None:
        self.tables = tables
        self.parameters = parameters
        self.parameter_locations: dict[int, int] = {}
        self.relations: set[str] = set()
        self.functions: set[str] = set()
        self.scopes: list[_Columns] = []
        self.stored_columns = tuple(sorted(stored_columns))
        self.stored_expression = stored_expression

    @staticmethod
    def named_columns(columns: tuple[str | None, ...], aliases: object | None = None) -> tuple[str, ...]:
        renamed = list(columns)
        if aliases is not None:
            names = _names(aliases)
            if len(names) > len(renamed):
                raise SQLPolicyError("PostgreSQL column alias count exceeds the projection")
            renamed[: len(names)] = names
        result = []
        for name in renamed:
            if name is None:
                raise SQLPolicyError("Derived PostgreSQL expressions require explicit column aliases")
            result.append(name)
        return tuple(result)

    def select(self, raw: object, ctes: Mapping[str, tuple[str, ...]] | None = None) -> tuple[str | None, ...]:
        self.scopes.append(_Columns(self.stored_columns if not self.scopes else ()))
        try:
            return self.select_body(raw, {} if ctes is None else dict(ctes))
        finally:
            self.scopes.pop()

    def select_body(self, raw: object, ctes: dict[str, tuple[str, ...]]) -> tuple[str | None, ...]:
        node = _object(raw, {"SelectStmt"}, {"SelectStmt"})
        body = _object(
            node["SelectStmt"],
            {
                "targetList",
                "fromClause",
                "whereClause",
                "groupClause",
                "havingClause",
                "sortClause",
                "limitOffset",
                "limitCount",
                "limitOption",
                "op",
                "withClause",
                "distinctClause",
            },
            {"targetList", "op"},
        )
        if body["op"] != "SETOP_NONE":
            raise SQLPolicyError("PostgreSQL set operations are not supported")
        if "limitOption" in body and body["limitOption"] not in ("LIMIT_OPTION_DEFAULT", "LIMIT_OPTION_COUNT"):
            raise SQLPolicyError("Unsupported PostgreSQL limit mode")
        local = dict(ctes)
        if "withClause" in body:
            with_body = _object(body["withClause"], {"ctes", "location"}, {"ctes"})
            for item in _list(with_body["ctes"]):
                cte = _object(item, {"CommonTableExpr"}, {"CommonTableExpr"})
                definition = _object(
                    cte["CommonTableExpr"],
                    {
                        "ctename",
                        "aliascolnames",
                        "ctematerialized",
                        "ctequery",
                        "location",
                    },
                    {"ctename", "ctequery"},
                )
                name = _string(definition["ctename"])
                if name in local:
                    raise SQLPolicyError("Duplicate or shadowing PostgreSQL CTE name")
                if "ctematerialized" in definition and definition["ctematerialized"] not in (
                    "CTEMaterializeDefault",
                    "CTEMaterializeAlways",
                    "CTEMaterializeNever",
                ):
                    raise SQLPolicyError("Unsupported PostgreSQL CTE mode")
                projected = self.select(definition["ctequery"], local)
                aliases = definition["aliascolnames"] if "aliascolnames" in definition else None
                local[name] = self.named_columns(projected, aliases)
        scope = local
        if "fromClause" in body:
            for item in _list(body["fromClause"]):
                self.scopes[-1].unqualified += self.relation(item, scope)
        projected_columns: list[str | None] = []
        for item in _list(body["targetList"]):
            target = _object(item, {"ResTarget"}, {"ResTarget"})
            target_body = _object(target["ResTarget"], {"val", "name", "location"}, {"val"})
            if "name" in target_body:
                _string(target_body["name"])
            self.expression(target_body["val"], scope, star=True)
            if "name" in target_body:
                projected_columns.append(_string(target_body["name"]))
            else:
                projected_columns.extend(self.projection_columns(target_body["val"]))
        for key in ("whereClause", "havingClause", "limitOffset", "limitCount"):
            if key in body:
                self.expression(body[key], scope)
        for key in ("groupClause", "sortClause"):
            if key in body:
                for item in _list(body[key]):
                    self.expression(item, scope, output_names=frozenset(name for name in projected_columns if name is not None))
        if "distinctClause" in body and body["distinctClause"] != [{}]:
            raise SQLPolicyError("PostgreSQL DISTINCT ON is not supported")
        return tuple(projected_columns)

    def projection_columns(self, raw: object) -> tuple[str | None, ...]:
        node = _object(
            raw,
            {
                "ColumnRef",
                "FuncCall",
                "A_Const",
                "A_Expr",
                "TypeCast",
                "CaseExpr",
                "CoalesceExpr",
                "SubLink",
                "ParamRef",
                "NullTest",
                "BooleanTest",
                "BoolExpr",
            },
        )
        if "ColumnRef" in node:
            body = _object(node["ColumnRef"], {"fields", "location"}, {"fields"})
            fields = _list(body["fields"])
            if fields[-1] == {"A_Star": {}}:
                if len(fields) == 1:
                    return self.scopes[-1].unqualified
                qualifier = _names(fields[:-1])[0]
                for scope in reversed(self.scopes):
                    if qualifier in scope.qualified:
                        return scope.qualified[qualifier]
                raise SQLPolicyError("Unknown PostgreSQL column qualifier")
            return (_names(fields)[-1],)
        if "FuncCall" in node:
            body = _object(
                node["FuncCall"], {"funcname", "args", "agg_star", "agg_distinct", "agg_filter", "funcformat", "location"}, {"funcname"}
            )
            return (_qualified(body["funcname"]),)
        return (None,)

    def relation(self, raw: object, ctes: Mapping[str, tuple[str, ...]]) -> tuple[str, ...]:
        node = _object(raw, {"RangeVar", "JoinExpr", "RangeSubselect"})
        if len(node) != 1:
            raise SQLPolicyError("Unsupported PostgreSQL relation")
        kind, value = next(iter(node.items()))
        if kind == "RangeVar":
            body = _object(value, {"schemaname", "relname", "inh", "relpersistence", "alias", "location"}, {"relname"})
            name = _string(body["relname"])
            if "schemaname" in body and body["schemaname"] != "public":
                raise SQLPolicyError("Only canonical public Landscape relations are supported")
            if "relpersistence" in body and body["relpersistence"] != "p":
                raise SQLPolicyError("Temporary PostgreSQL relations are not supported")
            if "schemaname" in body or name not in ctes:
                if name not in self.tables or name not in metadata.tables:
                    raise SQLPolicyError("PostgreSQL relation is outside the Landscape query scope")
                self.relations.add(name)
                columns = tuple(column.name for column in metadata.tables[name].columns)
            else:
                columns = ctes[name]
            qualifier: str | None = name
        elif kind == "RangeSubselect":
            body = _object(value, {"subquery", "alias"}, {"subquery"})
            projected = self.select(body["subquery"], ctes)
            aliases = None
            if "alias" in body:
                alias = _object(body["alias"], {"aliasname", "colnames"}, {"aliasname"})
                aliases = alias["colnames"] if "colnames" in alias else None
            columns = self.named_columns(projected, aliases)
            qualifier = None
        else:
            body = _object(value, {"jointype", "larg", "rarg", "quals", "usingClause", "alias", "isNatural"}, {"jointype", "larg", "rarg"})
            if body["jointype"] not in ("JOIN_INNER", "JOIN_LEFT", "JOIN_RIGHT", "JOIN_FULL"):
                raise SQLPolicyError("Unsupported PostgreSQL join")
            saved_aliases = dict(self.scopes[-1].qualified)
            saved_columns = self.scopes[-1].unqualified
            left = self.relation(body["larg"], ctes)
            right = self.relation(body["rarg"], ctes)
            self.scopes[-1].unqualified = saved_columns + left + right
            if "quals" in body:
                self.expression(body["quals"], ctes)
            self.scopes[-1].unqualified = saved_columns
            using: tuple[str, ...] = ()
            if "usingClause" in body:
                using = tuple(_names(body["usingClause"]))
            if "isNatural" in body:
                if type(body["isNatural"]) is not bool:
                    raise SQLPolicyError("Unsupported PostgreSQL natural join")
                if body["isNatural"]:
                    using = tuple(name for name in left if name in right)
            if len(set(using)) != len(using) or any(left.count(name) != 1 or right.count(name) != 1 for name in using):
                raise SQLPolicyError("Unsupported PostgreSQL join column binding")
            columns = using + tuple(name for name in left if name not in using) + tuple(name for name in right if name not in using)
            qualifier = None
            if "alias" in body:
                self.scopes[-1].qualified = saved_aliases
        if "alias" in body:
            alias = _object(body["alias"], {"aliasname", "colnames"}, {"aliasname"})
            qualifier = _string(alias["aliasname"])
            if "colnames" in alias and kind != "RangeSubselect":
                columns = self.named_columns(columns, alias["colnames"])
        if qualifier is not None:
            if qualifier in self.scopes[-1].qualified:
                raise SQLPolicyError("Duplicate PostgreSQL relation qualifier")
            self.scopes[-1].qualified[qualifier] = columns
        return columns

    def expression(
        self, raw: object, ctes: Mapping[str, tuple[str, ...]], *, star: bool = False, output_names: frozenset[str] = frozenset()
    ) -> None:
        if type(raw) is not dict or len(raw) != 1:
            raise SQLPolicyError("Unsupported PostgreSQL expression")
        kind, value = next(iter(raw.items()))
        if kind == "ColumnRef":
            body = _object(value, {"fields", "location"}, {"fields"})
            fields = _list(body["fields"])
            if not 1 <= len(fields) <= 2:
                raise SQLPolicyError("Unsupported PostgreSQL column qualification")
            for index, field in enumerate(fields):
                if field == {"A_Star": {}} and star and index == len(fields) - 1:
                    continue
                _names([field])
            wildcard = fields[-1] == {"A_Star": {}}
            names = _names(fields[:-1] if wildcard else fields)
            if len(fields) == 1:
                if wildcard:
                    if not self.scopes[-1].unqualified:
                        raise SQLPolicyError("PostgreSQL wildcard requires an admitted source")
                elif names[0] not in output_names:
                    for column_scope in reversed(self.scopes):
                        count = column_scope.unqualified.count(names[0])
                        if count:
                            if count != 1:
                                raise SQLPolicyError("Ambiguous PostgreSQL column reference")
                            break
                    else:
                        raise SQLPolicyError("Unresolved PostgreSQL column or whole-row reference")
            else:
                for column_scope in reversed(self.scopes):
                    if names[0] in column_scope.qualified:
                        if not wildcard and column_scope.qualified[names[0]].count(names[1]) != 1:
                            raise SQLPolicyError("Unresolved PostgreSQL qualified column reference")
                        break
                else:
                    raise SQLPolicyError("Unknown PostgreSQL column qualifier")
        elif kind == "A_Const":
            body = _object(value, {"ival", "fval", "sval", "boolval", "isnull", "location"})
            if len(body.keys() - {"location"}) != 1:
                raise SQLPolicyError("Unsupported PostgreSQL constant")
        elif kind == "ParamRef":
            body = _object(value, {"number", "location"}, {"number", "location"})
            number = body["number"]
            if type(number) is not int or not 1 <= number <= self.parameters:
                raise SQLPolicyError("PostgreSQL positional parameters must originate from named binds")
            location = body["location"]
            if type(location) is not int or location < 0 or location in self.parameter_locations:
                raise SQLPolicyError("Unsupported PostgreSQL bind location")
            self.parameter_locations[location] = number
        elif kind == "FuncCall":
            body = _object(value, {"funcname", "args", "agg_star", "agg_distinct", "agg_filter", "funcformat", "location"}, {"funcname"})
            name = _qualified(body["funcname"])
            functions = SCALAR_FUNCTIONS | AGGREGATES
            if self.stored_expression:
                functions = functions | {"btrim"}
            if name not in functions:
                raise SQLPolicyError("PostgreSQL function is outside the reviewed scalar/aggregate policy")
            formats = {"COERCE_EXPLICIT_CALL"}
            if self.stored_expression and name == "btrim":
                formats.add("COERCE_SQL_SYNTAX")
            if "funcformat" in body and body["funcformat"] not in formats:
                raise SQLPolicyError("Unsupported PostgreSQL function syntax")
            if name in SCALAR_FUNCTIONS and body.keys() & {"agg_star", "agg_distinct", "agg_filter"}:
                raise SQLPolicyError("Unsupported PostgreSQL scalar-call modifier")
            if "agg_star" in body and (name != "count" or body["agg_star"] is not True or "args" in body):
                raise SQLPolicyError("Only count(*) admits an aggregate star")
            self.functions.add(name)
            if "args" in body:
                args = _list(body["args"])
                if len(args) not in ((1, 2) if name == "round" else (1,)):
                    raise SQLPolicyError("Unsupported PostgreSQL function arity")
                for arg in args:
                    self.expression(arg, ctes)
            elif name != "count" or "agg_star" not in body:
                raise SQLPolicyError("Missing PostgreSQL function argument")
            if "agg_filter" in body:
                self.expression(body["agg_filter"], ctes)
        elif kind == "A_Expr":
            body = _object(value, {"kind", "name", "lexpr", "rexpr", "location"}, {"kind", "name", "rexpr"})
            name = _qualified(body["name"])
            mode = body["kind"]
            if mode == "AEXPR_OP":
                allowed = name in OPERATORS or (self.stored_expression and name == "~")
            elif mode in ("AEXPR_IN", "AEXPR_NULLIF", "AEXPR_DISTINCT", "AEXPR_NOT_DISTINCT"):
                allowed = name in ("=", "<>")
            elif mode in ("AEXPR_BETWEEN", "AEXPR_NOT_BETWEEN"):
                allowed = name in ("BETWEEN", "NOT BETWEEN")
            elif mode in ("AEXPR_LIKE", "AEXPR_ILIKE"):
                allowed = name in ("~~", "!~~", "~~*", "!~~*")
            elif mode == "AEXPR_OP_ANY" and self.stored_expression:
                allowed = name in ("=", "<>")
            else:
                allowed = False
            if not allowed:
                raise SQLPolicyError("Unsupported PostgreSQL operator expression")
            if "lexpr" in body:
                self.expression(body["lexpr"], ctes)
            if mode == "AEXPR_OP_ANY" and self.stored_expression:
                self.stored_array(body["rexpr"], ctes)
            else:
                self.expression(body["rexpr"], ctes)
        elif kind == "TypeCast":
            body = _object(value, {"arg", "typeName", "location"}, {"arg", "typeName"})
            typename = _object(body["typeName"], {"names", "typemod", "location"}, {"names"})
            if _qualified(typename["names"]) not in SCALAR_TYPES:
                raise SQLPolicyError("Unsupported PostgreSQL cast type")
            if "typemod" in typename and typename["typemod"] != -1:
                raise SQLPolicyError("PostgreSQL cast modifiers are not supported")
            self.expression(body["arg"], ctes)
        elif kind in ("BoolExpr", "CoalesceExpr", "List"):
            operand_fields = {"args", "location"} if kind != "List" else {"items"}
            if kind == "BoolExpr":
                operand_fields.add("boolop")
            body = _object(value, operand_fields)
            if kind == "BoolExpr" and ("boolop" not in body or body["boolop"] not in ("AND_EXPR", "OR_EXPR", "NOT_EXPR")):
                raise SQLPolicyError("Unsupported PostgreSQL boolean operation")
            key = "items" if kind == "List" else "args"
            if key not in body:
                raise SQLPolicyError("Missing PostgreSQL expression operands")
            for arg in _list(body[key]):
                self.expression(arg, ctes)
        elif kind in ("NullTest", "BooleanTest"):
            key = "nulltesttype" if kind == "NullTest" else "booltesttype"
            body = _object(value, {"arg", key, "location"}, {"arg", key})
            choices = (
                ("IS_NULL", "IS_NOT_NULL")
                if kind == "NullTest"
                else (
                    "IS_TRUE",
                    "IS_NOT_TRUE",
                    "IS_FALSE",
                    "IS_NOT_FALSE",
                    "IS_UNKNOWN",
                    "IS_NOT_UNKNOWN",
                )
            )
            if body[key] not in choices:
                raise SQLPolicyError("Unsupported PostgreSQL truth test")
            self.expression(body["arg"], ctes)
        elif kind in ("CaseExpr", "CaseWhen"):
            case_fields = {"arg", "args", "defresult", "location"} if kind == "CaseExpr" else {"expr", "result", "location"}
            body = _object(value, case_fields)
            for key, child in body.items():
                if key == "location":
                    continue
                if key == "args":
                    for arg in _list(child):
                        self.expression(arg, ctes)
                else:
                    self.expression(child, ctes)
        elif kind == "SortBy":
            body = _object(value, {"node", "sortby_dir", "sortby_nulls", "location"}, {"node"})
            if "sortby_dir" in body and body["sortby_dir"] not in ("SORTBY_DEFAULT", "SORTBY_ASC", "SORTBY_DESC"):
                raise SQLPolicyError("Unsupported PostgreSQL ordering operator")
            if "sortby_nulls" in body and body["sortby_nulls"] not in (
                "SORTBY_NULLS_DEFAULT",
                "SORTBY_NULLS_FIRST",
                "SORTBY_NULLS_LAST",
            ):
                raise SQLPolicyError("Unsupported PostgreSQL null ordering")
            self.expression(body["node"], ctes, output_names=output_names)
        elif kind == "SubLink":
            body = _object(value, {"subLinkType", "subselect", "location"}, {"subLinkType", "subselect"})
            if body["subLinkType"] not in ("EXISTS_SUBLINK", "EXPR_SUBLINK"):
                raise SQLPolicyError("Unsupported PostgreSQL subquery expression")
            projected = self.select(body["subselect"], ctes)
            if body["subLinkType"] == "EXPR_SUBLINK" and len(projected) != 1:
                raise SQLPolicyError("PostgreSQL scalar subqueries require one projected column")
        else:
            raise SQLPolicyError("Unsupported PostgreSQL expression family")

    def stored_array(self, raw: object, ctes: Mapping[str, tuple[str, ...]]) -> None:
        """Only canonical scalar-ANY literal arrays, never general array SQL."""
        node = _object(raw, {"TypeCast", "A_ArrayExpr"})
        if len(node) != 1:
            raise SQLPolicyError("Stored PostgreSQL ANY requires one literal array")
        if "TypeCast" in node:
            cast_body = _object(node["TypeCast"], {"arg", "typeName", "location"}, {"arg", "typeName"})
            typename = _object(cast_body["typeName"], {"names", "typemod", "location", "arrayBounds"}, {"names", "arrayBounds"})
            if (
                typename["arrayBounds"] != [{"Integer": {}}]
                or _qualified(typename["names"]) not in SCALAR_TYPES
                or ("typemod" in typename and typename["typemod"] != -1)
            ):
                raise SQLPolicyError("Unsupported stored PostgreSQL literal array cast")
            node = _object(cast_body["arg"], {"A_ArrayExpr"}, {"A_ArrayExpr"})
        body = _object(node["A_ArrayExpr"], {"elements", "location"}, {"elements"})
        for item in _list(body["elements"]):
            literal = _object(item, {"TypeCast", "A_Const"})
            if len(literal) != 1:
                raise SQLPolicyError("Stored PostgreSQL arrays require scalar literals")
            if "TypeCast" in literal:
                literal_cast = _object(literal["TypeCast"], {"arg", "typeName", "location"}, {"arg", "typeName"})
                _object(literal_cast["arg"], {"A_Const"}, {"A_Const"})
            self.expression(item, ctes)


def _parse(positional: str) -> object:
    try:
        import pg_query
    except ImportError as exc:
        raise SQLPolicyError("PostgreSQL query policy requires the qualified postgres parser extra") from exc
    try:
        parsed = json.loads(pg_query.query_parse_json(positional))
    except pg_query.PgQueryExc as exc:
        raise SQLPolicyError("PostgreSQL query does not parse under the qualified PG16 grammar") from exc
    except (json.JSONDecodeError, RecursionError) as exc:
        raise SQLPolicyError("PostgreSQL parser output exceeds the supported syntax representation") from exc
    _bounded_tree(parsed)
    envelope = _object(parsed, {"version", "stmts"}, {"version", "stmts"})
    if envelope["version"] != 160001:
        raise SQLPolicyError("Unsupported PostgreSQL parser version")
    statements = _list(envelope["stmts"])
    if len(statements) != 1:
        raise SQLPolicyError("Exactly one PostgreSQL SELECT statement is required")
    statement = _object(statements[0], {"stmt", "stmt_location", "stmt_len"}, {"stmt"})
    return statement["stmt"]


def admit_postgres_sql(sql: str, params: Mapping[str, object] | None, tables: frozenset[str]) -> AdmittedSQL:
    """Admit bounded syntax without querying PostgreSQL or adapting bind values."""
    _bounded_sql(sql)
    _check_parameters(params)
    compiled = text(sql).compile(dialect=_numeric_dialect(paramstyle="numeric_dollar"))
    positional = str(compiled)
    _bounded_sql(positional)
    names = tuple(compiled.positiontup or ())
    if set(names) != (set(params) if params is not None else set()):
        raise SQLPolicyError("PostgreSQL named binds and supplied parameters must match exactly")
    policy = _Policy(tables, len(names))
    policy.select(_parse(positional))
    _check_bind_structure(sql, positional, names, policy.parameter_locations)
    return AdmittedSQL(sql, positional, names, frozenset(policy.relations), frozenset(policy.functions))


def _check_bind_structure(sql: str, positional: str, names: tuple[str, ...], locations: dict[int, int]) -> None:
    """Match driver compilation using only parser-recognized parameter tokens.

    SQLAlchemy text() also substitutes colon names inside quoted strings and
    comments. Such substitutions are not grammar parameters and must never
    acquire driver interpolation. PostgreSQL locations are UTF-8 byte offsets;
    every byte outside a recognized token must match both drivers' compilation.
    """
    encoded = positional.encode("utf-8")
    parts: list[bytes] = []
    previous = 0
    for location, number in sorted(locations.items()):
        token = f"${number}".encode("ascii")
        if location < previous or encoded[location : location + len(token)] != token:
            raise SQLPolicyError("PostgreSQL bind structure does not match parsed token locations")
        parts.append(encoded[previous:location].replace(b"%", b"%%"))
        parts.append(("%(" + names[number - 1] + ")s").encode("utf-8"))
        previous = location + len(token)
    parts.append(encoded[previous:].replace(b"%", b"%%"))
    expected = b"".join(parts)
    for driver in (_psycopg_dialect(), _psycopg2_dialect()):
        actual = str(text(sql).compile(dialect=driver)).encode("utf-8")
        if actual != expected:
            raise SQLPolicyError("PostgreSQL bind structure differs between parser and driver compilation")


def admit_stored_expression(expression: str, columns: frozenset[str]) -> None:
    """Reject execution-bearing custom CHECK/index expressions before planning.

    PostgreSQL deparses canonical IN checks to scalar ANY arrays. This private
    catalog-only path admits those and the canonical lower-hex regex checks; the
    caller-query policy does not acquire array or arbitrary regex expressions.
    """
    sql = "SELECT " + expression
    _bounded_sql(sql)
    policy = _Policy(frozenset(), 0, stored_expression=True, stored_columns=columns)
    policy.select(_parse(sql))
    if policy.relations:
        raise SQLPolicyError("Stored PostgreSQL expression references a relation")
