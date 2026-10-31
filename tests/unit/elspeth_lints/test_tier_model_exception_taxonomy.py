"""Nominal exception-kind proofs through the existing R5 engine."""

from __future__ import annotations

import ast
import hashlib
import json
from collections import Counter
from collections.abc import Sequence
from dataclasses import asdict
from pathlib import Path
from textwrap import dedent

import pytest

from elspeth_lints.core.ast_walker import iter_python_files
from elspeth_lints.core.protocols import RuleContext
from elspeth_lints.rules.trust_tier.tier_model import rule as rule_module
from elspeth_lints.rules.trust_tier.tier_model.exception_taxonomy import ExceptionTaxonomy
from elspeth_lints.rules.trust_tier.tier_model.rule import Finding, TierModelRule, TierModelVisitor, scan_directory, scan_file


def _findings(source: str, modules: dict[str, str] | None = None) -> list[Finding]:
    source = dedent(source)
    tree = ast.parse(source, filename="subject.py")
    trees = {name: ast.parse(dedent(body), filename=name) for name, body in (modules or {}).items()}
    trees["subject.py"] = tree
    before = {name: ast.dump(value, include_attributes=True) for name, value in trees.items()}
    visitor = TierModelVisitor(
        "subject.py", source.splitlines(), hashlib.sha256(source.encode()).hexdigest(), exception_taxonomy=ExceptionTaxonomy(trees)
    )
    visitor.visit(tree)
    assert {name: ast.dump(value, include_attributes=True) for name, value in trees.items()} == before
    return visitor.findings


def _r5(source: str, modules: dict[str, str] | None = None) -> list[Finding]:
    return [finding for finding in _findings(source, modules) if finding.rule_id == "R5"]


def _ordered_r5(subject: str, companions: dict[str, str], reverse_inputs: bool) -> list[Finding]:
    sources = [*companions.items(), ("subject.py", subject)]
    if reverse_inputs:
        sources.reverse()
    trees = {name: ast.parse(body, filename=name) for name, body in sources}
    before = {name: ast.dump(tree, include_attributes=True) for name, tree in trees.items()}
    visitor = TierModelVisitor(
        "subject.py", subject.splitlines(), hashlib.sha256(subject.encode()).hexdigest(), exception_taxonomy=ExceptionTaxonomy(trees)
    )
    visitor.visit(trees["subject.py"])
    assert before == {name: ast.dump(tree, include_attributes=True) for name, tree in trees.items()}
    return [finding for finding in visitor.findings if finding.rule_id == "R5"]


@pytest.mark.parametrize(
    "source",
    [
        "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "class Parent(RuntimeError): pass\nclass Child(Parent): pass\ndef f(exc): return isinstance(exc, Child)",
        "def f(exc): return isinstance(exc, (Exception, BaseException, RuntimeError))",
        "from sqlalchemy.exc import SQLAlchemyError\ndef f(exc): return isinstance(exc, SQLAlchemyError)",
        "from sqlalchemy.exc import IntegrityError as NativeError\ndef f(exc): return isinstance(exc, NativeError)",
        "import sqlalchemy.exc as errors\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "from sqlalchemy import exc as errors\ndef f(exc): return isinstance(exc, errors.IntegrityError)",
        "import sqlalchemy.exc\ndef f(exc): return isinstance(exc, sqlalchemy.exc.IntegrityError)",
        "import builtins as native\ndef f(exc): return isinstance(exc, native.Exception)",
        "from builtins import RuntimeError as Root\nclass Refusal(Root): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "from sqlalchemy.exc import IntegrityError\ndef f(exc): return isinstance(exc, (RuntimeError, (Exception, IntegrityError)))",
        "def f(exc):\n    class Local(Exception): pass\n    return isinstance(exc, Local)",
        "def f(exc):\n    from sqlalchemy.exc import IntegrityError as E\n    return isinstance(exc, E)",
    ],
    ids=[
        "local-owned",
        "runtime-error-ancestry",
        "builtin-roots",
        "sqlalchemy-root",
        "sqlalchemy-import-as",
        "qualified-module-alias",
        "from-module-alias",
        "qualified-module",
        "builtin-module",
        "builtin-base-alias",
        "all-exception-nested-tuple",
        "function-local-class",
        "function-local-import",
    ],
)
def test_proven_exception_kind_is_outside_r5(source: str) -> None:
    assert _r5(source) == []


@pytest.mark.parametrize(
    "source",
    [
        "class OrdinaryError: pass\ndef f(exc): return isinstance(exc, OrdinaryError)",
        "def f(value): return isinstance(value, dict)",
        "def f(value): return isinstance(value, (list, str))",
        "def f(exc): return isinstance(exc, (Exception, str))",
        "def f(exc): return isinstance(exc, (Exception, Unknown))",
        "def f(exc): return isinstance(exc, ())",
        "def f(exc): return isinstance(exc, Exception | RuntimeError)",
        "def f(exc): return isinstance(exc, make_exception())",
        "def f(exc, types): return isinstance(exc, (Exception, *types))",
        "def f(exc, subjects): return isinstance(*subjects, Exception)",
        "def f(exc): return isinstance(exc, class_or_tuple=Exception)",
        "Alias = Exception\ndef f(exc): return isinstance(exc, Alias)",
        "Types = (Exception, RuntimeError)\ndef f(exc): return isinstance(exc, Types)",
        "from unrelated import SQLAlchemyError\ndef f(exc): return isinstance(exc, SQLAlchemyError)",
        "from sqlalchemy.exc import UnknownError\ndef f(exc): return isinstance(exc, UnknownError)",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc, E): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\nE = str\ndef f(exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc): return isinstance(exc, E)\nE = str",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc): return isinstance(exc, E)\ndel E",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc): return isinstance(exc, E)\nE: type = str",
        "from sqlalchemy.exc import IntegrityError as E\ndef outer():\n    from sqlalchemy.exc import IntegrityError as E\n    def inner(exc): return isinstance(exc, E)\n    E = str\n    return inner",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc):\n    global E\n    return isinstance(exc, E)",
        "def outer():\n    from sqlalchemy.exc import IntegrityError as E\n    def inner(exc):\n        nonlocal E\n        return isinstance(exc, E)\n    return inner",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc):\n    try: raise ValueError()\n    except ValueError as E: return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\ndef f(exc, value):\n    match value:\n        case {'error': E}: return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\ndef f[E](exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\ndef f[E: isinstance(value, E)](exc): pass",
        "from sqlalchemy.exc import IntegrityError as E\ntype Alias[E] = isinstance(value, E)",
        "from sqlalchemy.exc import IntegrityError as E\nif flag: E = str\ndef f(exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\nfrom unknown import *\ndef f(exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\nclass Holder:\n    E = str\n    result = isinstance(value, E)",
        "class Refusal(Exception): pass\nRefusal = str\ndef f(exc): return isinstance(exc, Refusal)",
        "@replace\nclass Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception, metaclass=CustomMeta): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(*(Exception,)): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception, Unknown): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception):\n    def __init_subclass__(cls): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "from typing import Protocol, runtime_checkable\n@runtime_checkable\nclass Refusal(Protocol): pass\ndef f(exc): return isinstance(exc, Refusal)",
        "import sqlalchemy.exc as errors\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)\nerrors.SQLAlchemyError = str",
        "import sqlalchemy.exc as errors\ndef mutate(): errors.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nimport sqlalchemy.exc as other\nother.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nsetattr(errors, 'SQLAlchemyError', str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nerrors.__dict__['SQLAlchemyError'] = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "class Refusal(Exception): pass\nRefusal.__bases__ = (str,)\ndef f(exc): return isinstance(exc, Refusal)",
        "import builtins\nbuiltins.Exception = str\ndef f(exc): return isinstance(exc, Exception)",
        "import builtins\nbuiltins.isinstance = custom\ndef f(exc): return isinstance(exc, Exception)",
        "def isinstance(value, kind): return True\ndef f(exc): return isinstance(exc, Exception)",
        "def f(exc, isinstance): return isinstance(exc, Exception)",
        "def f(exc): return isinstance(exc, Exception)\nException = str",
        "class First(Second): pass\nclass Second(First): pass\ndef f(exc): return isinstance(exc, First)",
        "def f(exc): assert isinstance(exc, Exception)",
    ],
)
def test_unsupported_or_shadowed_kind_retains_r5(source: str) -> None:
    assert len(_r5(source)) == 1


_OWNED = {
    "pkg/base.py": "class Base(RuntimeError): pass",
    "pkg/errors.py": "from pkg.base import Base\nclass Refusal(Base): pass",
    "unrelated/errors.py": "class Refusal: pass",
}


@pytest.mark.parametrize(
    "source",
    [
        "from pkg.errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)",
        "from pkg.errors import Refusal as E\ndef f(exc): return isinstance(exc, E)",
        "import pkg.errors as errors\ndef f(exc): return isinstance(exc, errors.Refusal)",
        "from pkg.errors import Refusal\ndef f(exc): return isinstance(exc, (Refusal, RuntimeError))",
    ],
)
def test_owned_cross_module_ancestry_uses_finite_supplied_sources(source: str) -> None:
    assert _r5(source, _OWNED) == []


def test_reviewed_composer_exception_ancestry_uses_actual_source() -> None:
    root = Path(__file__).resolve().parents[3]
    paths = ("web/composer/protocol.py", "web/composer/prompt_guard.py")
    modules = {f"elspeth/{path}": (root / "src/elspeth" / path).read_text() for path in paths}
    source = """
        from elspeth.web.composer.protocol import ComposerContextUnsupported
        from elspeth.web.composer.prompt_guard import PromptGuardRefused
        from sqlalchemy.exc import IntegrityError, SQLAlchemyError
        def classify(exc):
            return isinstance(exc, (ComposerContextUnsupported, PromptGuardRefused, IntegrityError, SQLAlchemyError))
    """
    assert _r5(source, modules) == []


def test_same_spelling_in_another_module_is_not_exception_evidence() -> None:
    assert len(_r5("from unrelated.errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)", _OWNED)) == 1


def test_missing_owned_definition_remains_unresolved() -> None:
    assert len(_r5("from pkg.errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)")) == 1


def test_source_module_shadow_of_external_model_is_not_trusted() -> None:
    assert (
        len(
            _r5(
                "from sqlalchemy.exc import SQLAlchemyError\ndef f(exc): return isinstance(exc, SQLAlchemyError)",
                {"sqlalchemy/exc.py": "class SQLAlchemyError: pass"},
            )
        )
        == 1
    )


def test_exported_class_rebinding_invalidates_imported_ancestry() -> None:
    modules = {**_OWNED, "pkg/errors.py": "from pkg.base import Base\nclass Refusal(Base): pass\nRefusal = str"}
    assert len(_r5("from pkg.errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)", modules)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "from sqlalchemy.exc import IntegrityError as E\nclass Holder[E]:\n    def f(self, exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\nclass Holder[**E]:\n    def f(self, exc): return isinstance(exc, E)",
        "from sqlalchemy.exc import IntegrityError as E\nclass Holder[*E]:\n    def f(self, exc): return isinstance(exc, E)",
        "class Holder[Exception]:\n    def f(self, exc): return isinstance(exc, Exception)",
        "from sqlalchemy.exc import IntegrityError as E\nclass Holder[E]:\n    def f(self):\n        def inner(exc): return isinstance(exc, E)\n        return inner",
        "import sqlalchemy.exc as errors\nalias = errors\nalias.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = errors\nb = a\nb.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na: object = errors\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = errors if flag else object()\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = object()\nif flag: a = errors\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = transfer(errors)\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = factory()\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\na = b\nb = a\na.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "class Refusal(Exception): pass\nalias = Refusal\nalias.__bases__ = (str,)\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\nglobals()['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\nnamespace = globals()\nnamespace['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\nglobals()[name] = str\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\nlocals()['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\nnamespace = locals()\nnamespace.update(Refusal=str)\ndef f(exc): return isinstance(exc, Refusal)",
        "import sqlalchemy.exc as errors\nerrors.__dict__.update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nvars(errors)['SQLAlchemyError'] = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nnamespace = vars(errors)\nnamespace.update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nfrom builtins import vars as recover\nrecover(errors).update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "class Refusal(Exception): pass\nrecover = globals\nnamespace = recover()\nnamespace.update(Refusal=str)\ndef f(exc): return isinstance(exc, Refusal)",
        "import sqlalchemy.exc as errors\nfrom sqlalchemy.exc import __dict__ as namespace\nnamespace.update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
    ],
)
def test_reviewed_proof_counterexamples_remain_r5(source: str) -> None:
    assert len(_r5(source)) == 1


def test_namespace_assignment_alias_is_not_a_positive_type_alias() -> None:
    assert len(_r5("from sqlalchemy.exc import IntegrityError\nAlias = IntegrityError\ndef f(exc): return isinstance(exc, Alias)")) == 1


def test_cross_supplied_unit_namespace_alias_mutation_vetoes_external_fact() -> None:
    modules = {"bridge.py": "import sqlalchemy.exc as errors\nalias = errors"}
    source = "from bridge import alias\nimport sqlalchemy.exc as errors\nalias.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source, modules)) == 1


def test_qualified_supplied_unit_namespace_alias_mutation_vetoes_external_fact() -> None:
    modules = {"bridge.py": "import sqlalchemy.exc as errors\nalias = errors"}
    source = "import bridge\nimport sqlalchemy.exc as errors\nbridge.alias.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source, modules)) == 1


def test_supplied_unit_namespace_function_alias_withdraws_owned_proof() -> None:
    modules = {"bridge.py": "recover = globals"}
    source = "from bridge import recover\nclass Refusal(Exception): pass\nrecover()['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, modules)) == 1


@pytest.mark.parametrize(
    "modules",
    [
        {"bridge.py": "import bridge\nalias = bridge.alias.child"},
        {"bridge.py": "import other\nalias = other.alias.child", "other.py": "import bridge\nalias = bridge.alias.child"},
    ],
    ids=["same-unit-growing-tail", "two-unit-growing-tail"],
)
def test_growing_qualified_alias_cycle_returns_unknown_veto(modules: dict[str, str]) -> None:
    source = "from bridge import alias\nimport sqlalchemy.exc as errors\nalias.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source, modules)) == 1


def test_deep_assignment_origin_closure_exhaustion_is_unknown() -> None:
    aliases = [f"alias_{index} = alias_{index + 1}" for index in range(80)]
    source = "\n".join(
        [
            "import sqlalchemy.exc as errors",
            *aliases,
            "alias_80 = errors",
            "alias_0.SQLAlchemyError = str",
            "def f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        ]
    )
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "class Refusal(Exception): pass\ndef helper(): pass\nhelper.__globals__['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\ndef helper(): pass\nnamespace = helper.__globals__\nnamespace.update(Refusal=str)\ndef f(exc): return isinstance(exc, Refusal)",
        "def helper(): pass\nhelper.__builtins__['Exception'] = str\ndef f(exc): return isinstance(exc, Exception)",
        "__builtins__['Exception'] = str\ndef f(exc): return isinstance(exc, Exception)",
        "class Refusal(Exception): pass\nexec('Refusal = str')\ndef f(exc): return isinstance(exc, Refusal)",
        "class Refusal(Exception): pass\neval(code)\ndef f(exc): return isinstance(exc, Refusal)",
        "from builtins import exec as run\nclass Refusal(Exception): pass\nrun(code)\ndef f(exc): return isinstance(exc, Refusal)",
        "run = eval\nclass Refusal(Exception): pass\nrun(code)\ndef f(exc): return isinstance(exc, Refusal)",
        "import sqlalchemy.exc as errors\ngetattr(errors, '__dict__').update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nfrom builtins import getattr as reflect\nreflect(errors, '__dict__').update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nreflect = getattr\nreflect(errors, selector).update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nerrors.__getattribute__('__dict__').update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nreflect = errors.__getattribute__\nreflect('__dict__').update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nreflect = object.__getattribute__\nreflect(errors, '__dict__').update(SQLAlchemyError=str)\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "def identity(value): return value\nimport sqlalchemy.exc as errors\nidentity(errors).SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
        "import sqlalchemy\ngetattr(sqlalchemy, 'exc').SQLAlchemyError = str\ndef f(exc): return isinstance(exc, sqlalchemy.exc.SQLAlchemyError)",
        "import sqlalchemy.exc as errors\nfactory().SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)",
    ],
)
def test_namespace_recovery_and_expression_writes_withdraw_r5_proof(source: str) -> None:
    assert len(_r5(source)) == 1


def test_unknown_element_receiver_does_not_establish_a_namespace_origin() -> None:
    # An arbitrary call could return globals at runtime. That unmodeled
    # storage fact is not a source-proved namespace or binding effect.
    source = "class Refusal(Exception): pass\nnamespace()['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    "module_source",
    [
        "exec(code)",
        "run = eval\nrun(code)",
    ],
)
def test_unsupported_namespace_effect_withdraws_new_proof_across_supplied_graph(module_source: str) -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, {"dynamic.py": module_source})) == 1


@pytest.mark.parametrize(
    "module_source",
    [
        "def helper(): pass\nnamespace = helper.__globals__",
        "def helper(): pass\nnamespace = helper.__builtins__",
        "namespace = globals()",
        "namespace = locals()",
        "namespace = vars()",
    ],
)
def test_passive_namespace_recovery_is_local_to_its_source_context(module_source: str) -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source, {"dynamic.py": module_source}) == []
    assert len(_r5(module_source + "\n" + source)) == 1


@pytest.mark.parametrize(
    "expression",
    [
        "func.__globals__",
        "func.__builtins__",
        "func.__dict__",
        "vars(func)",
        "object.__getattribute__(func, '__builtins__')",
        "getattr(func, '__dict__')",
        "inspect.getattr_static(func, '__dict__')",
    ],
)
def test_passive_read_and_actual_namespace_write_have_distinct_graph_effects(expression: str) -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    passive = f"import inspect\ndef inspect_source(func):\n    namespace = {expression}\n    return namespace"
    assert _r5(source, {"probe.py": passive}) == []
    mutated = f"import inspect\ndef inspect_source(func):\n    namespace = {expression}\n    alias = namespace\n    alias[key] = str"
    assert len(_r5(source, {"probe.py": mutated})) == 1
    updated = f"import inspect\ndef inspect_source(func):\n    namespace = {expression}\n    namespace.update(Refusal=str)"
    assert len(_r5(source, {"probe.py": updated})) == 1


@pytest.mark.parametrize("mutation", ["namespace[key] = str", "namespace.update(Refusal=str)", "namespace.__setitem__(key, str)"])
def test_imported_passive_namespace_alias_still_carries_actual_write_veto(mutation: str) -> None:
    modules = {"bridge.py": "def helper(): pass\nnamespace = helper.__globals__"}
    source = f"from bridge import namespace\nclass Refusal(Exception): pass\n{mutation}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, modules)) == 1


def test_passive_namespace_transfer_into_a_callback_does_not_claim_a_mutation() -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    modules = {"probe.py": "def inspect_source(func):\n    return callback(global_bindings=func.__globals__)"}
    assert _r5(source, modules) == []


@pytest.mark.parametrize("slot", ["__getattribute__", "__setattr__"])
def test_unsupported_slot_read_provenance_is_local_but_write_provenance_is_a_graph_effect(slot: str) -> None:
    arguments = "func, '__builtins__'" if slot == "__getattribute__" else "func, name, value"
    modules = {"probe.py": f"def inspect_source(func, data, name, value):\n    data[name] = value\n    object.{slot}({arguments})"}
    findings = _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)
    assert bool(findings) is (slot == "__setattr__")


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_imported_namespace_get_mutation_is_not_hidden_by_input_order(reverse_inputs: bool) -> None:
    sources = [
        ("bridge.py", "from builtins import globals as recover\nnamespace = recover()"),
        ("probe.py", "from bridge import namespace\nnamespace.get('__builtins__').update(Exception=str)"),
    ]
    modules = dict(reversed(sources) if reverse_inputs else sources)
    assert len(_r5("def f(exc): return isinstance(exc, Exception)", modules)) == 1


@pytest.mark.parametrize("slot", ["__getattribute__", "__setattr__", "__delattr__"])
@pytest.mark.parametrize("operator", ["is", "is not"])
def test_slot_identity_comparison_is_passive_and_source_local(slot: str, operator: str) -> None:
    module = f"def inspect_source(value):\n    return value.{slot} {operator} expected"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(subject, {"probe.py": module}) == []
    local = f"def f(value, exc):\n    same = value.{slot} {operator} expected\n    return isinstance(exc, Exception)"
    assert len(_r5(local)) == 1


@pytest.mark.parametrize("slot", ["__setattr__", "__delattr__"])
def test_identity_comparison_does_not_hide_mutator_invocation_or_escape(slot: str) -> None:
    arguments = "'Refusal', str" if slot == "__setattr__" else "'Refusal'"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    compared = f"def inspect_source(value):\n    return value.{slot} is expected"
    invoked = f"{compared}\ndef mutate(value): value.{slot}({arguments})"
    escaped = f"{compared}\ndef recover(value):\n    replace = value.{slot}\n    return replace"
    assert len(_r5(subject, {"probe.py": invoked})) == 1
    assert len(_r5(subject, {"probe.py": escaped})) == 1


@pytest.mark.parametrize(
    "module",
    [
        "class Wrapper:\n    def getattr(self, obj, name): return super().getattr(obj, name)",
        "def inspect_source(obj, name):\n    read = get_module().getattr_static\n    return read(obj, name)",
        "def inspect_source(name):\n    make = get_module().attrgetter\n    return make(name)",
        "def inspect_source(obj):\n    read = object.__getattribute__\n    return read(obj, '__dict__')",
        "import builtins\nread = getattr(builtins, 'getattr')",
        "import builtins\nread = getattr(builtins, 'globals')\nnamespace = read()",
        "from unknown import __getattribute__ as read\ndef inspect_source(obj): return read(obj, '__dict__')",
    ],
)
def test_unproven_getter_recovery_or_call_does_not_poison_other_sources(module: str) -> None:
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": module}) == []


@pytest.mark.parametrize(
    "module",
    [
        "def mutate(obj):\n    make = get_module().attrgetter\n    namespace = make('__dict__')(obj)\n    namespace.update(Refusal=str)",
        "def mutate(obj):\n    read = object.__getattribute__\n    namespace = read(obj, '__dict__')\n    namespace[key] = str",
        "import builtins\nread = getattr(builtins, 'getattr')\nnamespace = read(builtins, '__dict__')\nnamespace.update(Exception=str)",
        "from unknown import __getattribute__ as read\nreplace = read(obj, '__setattr__')",
        "from unknown import __getattribute__ as read\nnamespace = read(obj, '__dict__')\nnamespace.update(Refusal=str)",
    ],
)
def test_getter_markers_retain_later_namespace_write_vetoes_across_sources(module: str) -> None:
    assert len(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": module})) == 1


@pytest.mark.parametrize(
    "body",
    [
        "return ns.update",
        "update = ns.update\nreturn update",
        "return callback(ns.update)",
        "return ns.clear",
        "return ns.__setitem__",
        "return getattr(ns, 'update')",
        "return ns.__getattribute__('update')",
    ],
)
def test_namespace_mutation_method_escape_withdraws_foreign_proof(body: str) -> None:
    probe = "def recover(func):\n    ns = func.__globals__\n" + "\n".join("    " + line for line in body.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(subject, {"probe.py": probe})) == 1


def test_imported_namespace_mutation_method_escape_keeps_veto_origin() -> None:
    modules = {
        "bridge.py": "def helper(): pass\nnamespace = helper.__globals__",
        "probe.py": "from bridge import namespace\nupdate = namespace.update",
    }
    assert len(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)) == 1


@pytest.mark.parametrize(
    "body",
    [
        "return ns",
        "return callback(ns)",
        "return ns.update is expected",
        "return ns.update is not expected",
        "return getattr(ns, 'update') is expected",
        "return ns.get('update') is expected",
        "return ns.__getattribute__('update') is expected",
        "return ns.get",
    ],
)
def test_passive_namespace_transfer_comparison_or_reader_reference_keeps_foreign_proof(body: str) -> None:
    probe = f"def inspect_source(func):\n    ns = func.__globals__\n    {body}"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize("lookup", ["get", "__getitem__"])
@pytest.mark.parametrize(
    ("member", "use"),
    [
        ("getattr", "read(obj, 'label')"),
        ("getattr_static", "read(obj, 'label')"),
        ("attrgetter", "read('label')(obj)"),
        ("globals", "read()"),
    ],
)
def test_namespace_literal_reader_lookup_preserves_only_veto_kind(lookup: str, member: str, use: str) -> None:
    probe = f"def inspect_source(func, obj):\n    ns = func.__globals__\n    read = ns.{lookup}({member!r})\n    return {use}"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize("lookup", ["get", "__getitem__"])
@pytest.mark.parametrize("member", ["exec", "setattr", "__setattr__", "update"])
def test_namespace_effect_callable_lookup_and_escape_still_withdraws_proof(lookup: str, member: str) -> None:
    probe = f"def recover(func):\n    ns = func.__globals__\n    replace = ns.{lookup}({member!r})\n    return replace"
    assert len(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe})) == 1


@pytest.mark.parametrize("lookup", ["get", "__getitem__"])
def test_namespace_unknown_callable_lookup_does_not_establish_a_graph_effect(lookup: str) -> None:
    probe = f"def recover(func):\n    ns = func.__globals__\n    call = ns.{lookup}(name)\n    return call()"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize("lookup", ["ns.get('getattr', replacement)", "ns.get(*names)", "ns.__getitem__(*names)"])
def test_namespace_reader_lookup_default_and_unknown_shape_stay_source_local(lookup: str) -> None:
    probe = f"def inspect_source(func, obj):\n    ns = func.__globals__\n    read = {lookup}\n    return read(obj, 'label')"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize("operation", ["obj.update", "getattr(obj, 'update')", "obj.clear"])
def test_unrelated_object_mutation_method_recovery_is_not_a_namespace_effect(operation: str) -> None:
    probe = f"def recover(obj): return {operation}"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize(
    ("setup", "expression"),
    [
        ("", "receiver.update"),
        ("", "receiver['update']"),
        ("", "receiver.get('update')"),
        ("", "receiver.__getitem__('update')"),
        ("", "getattr(receiver, 'update')"),
        ("", "inspect.getattr_static(receiver, 'update')"),
        ("ns = func.__globals__\nread = ns.get('getattr')", "read(receiver, 'update')"),
        ("ns = func.__globals__\nread = ns['getattr']", "read(receiver, 'update')"),
        ("", "receiver.__getattribute__('update')"),
        ("", "object.__getattribute__(receiver, 'update')"),
        ("", "operator.attrgetter('update')(receiver)"),
        ("make = operator.attrgetter\nselect = make('update')", "select(receiver)"),
        ("ns = func.__globals__\nmake = ns.get('attrgetter')\nselect = make('update')", "select(receiver)"),
        ("from builtins import getattr as read", "read(receiver, 'update')"),
    ],
    ids=[
        "attribute",
        "subscript",
        "get",
        "getitem",
        "getattr",
        "getattr-static",
        "reader-get",
        "reader-subscript",
        "bound-slot",
        "unbound-slot",
        "factory",
        "factory-alias",
        "internal-factory",
        "getter-import",
    ],
)
@pytest.mark.parametrize("namespace_receiver", [True, False], ids=["namespace", "ordinary"])
@pytest.mark.parametrize("consumption", ["escape", "invoke", "identity"])
def test_namespace_selected_capability_matrix(setup: str, expression: str, namespace_receiver: bool, consumption: str) -> None:
    receiver = "func.__globals__" if namespace_receiver else "obj"
    use = expression + "()" if consumption == "invoke" else f"({expression}) is expected" if consumption == "identity" else expression
    body = [f"receiver = {receiver}", *setup.splitlines(), f"return {use}"]
    probe = "import inspect\nimport operator\ndef recover(func, obj):\n" + "\n".join("    " + line for line in body)
    findings = _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe})
    assert bool(findings) is (namespace_receiver and consumption != "identity")


@pytest.mark.parametrize(
    ("bridge", "expression"),
    [
        ("from operator import attrgetter as make\nselect = make('update')", "select(receiver)"),
        ("def helper(): pass\nns = helper.__globals__\nselect = ns.get('getattr')", "select(receiver, 'update')"),
    ],
    ids=["factory-import", "internal-reader-import"],
)
@pytest.mark.parametrize("namespace_receiver", [True, False], ids=["namespace", "ordinary"])
@pytest.mark.parametrize("consumption", ["escape", "invoke", "identity"])
def test_namespace_selected_capability_import_matrix(bridge: str, expression: str, namespace_receiver: bool, consumption: str) -> None:
    receiver = "func.__globals__" if namespace_receiver else "obj"
    use = expression + "()" if consumption == "invoke" else f"({expression}) is expected" if consumption == "identity" else expression
    probe = f"from bridge import select\ndef recover(func, obj):\n    receiver = {receiver}\n    return {use}"
    findings = _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"bridge.py": bridge, "probe.py": probe})
    assert bool(findings) is (namespace_receiver and consumption != "identity")


def _dotted_factory_probe(selectors: tuple[str, ...], form: str, consumption: str) -> dict[str, str]:
    arguments = ", ".join(repr(selector) for selector in selectors)
    modules: dict[str, str] = {}
    imports = "import operator\n"
    setup: list[str] = []
    if form == "canonical":
        expression = f"operator.attrgetter({arguments})(obj)"
    else:
        expression = "select(obj)"
        if form == "alias":
            setup = ["make = operator.attrgetter", f"select = make({arguments})"]
        elif form == "internal":
            setup = ["ns = func.__globals__", "make = ns.get('attrgetter')", f"select = make({arguments})"]
        else:
            assert form == "import"
            modules["bridge.py"] = f"from operator import attrgetter as make\nselect = make({arguments})"
            imports += "from bridge import select\n"
    if consumption == "invoke":
        expression += "()"
    elif consumption == "identity":
        expression = f"({expression}) is expected"
    else:
        assert consumption == "escape"
    modules["probe.py"] = imports + "def recover(func, obj):\n" + "\n".join("    " + line for line in [*setup, f"return {expression}"])
    return modules


@pytest.mark.parametrize("selector", ["__globals__.update", "__dict__.clear", "child.__globals__.update", "__globals__.child.update"])
@pytest.mark.parametrize("form", ["canonical", "alias", "import", "internal"])
@pytest.mark.parametrize("consumption", ["escape", "invoke", "identity"])
def test_dotted_factory_namespace_before_mutator_matrix(selector: str, form: str, consumption: str) -> None:
    modules = _dotted_factory_probe((selector,), form, consumption)
    findings = _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)
    assert bool(findings) is (consumption != "identity")


@pytest.mark.parametrize(
    ("selectors", "same_path"),
    [
        (("__globals__",), False),
        (("update.__globals__",), False),
        (("update.child.__dict__",), False),
        (("update", "__globals__"), False),
        (("__globals__", "update"), False),
        (("__globals__.child", "child.update"), False),
        (("__globals__.update", "label"), True),
        (("label", "__globals__.update"), True),
        (("update", "__globals__.clear"), True),
        (("__dict__", "child.clear"), False),
    ],
)
@pytest.mark.parametrize("consumption", ["escape", "identity"])
def test_dotted_factory_keeps_order_and_each_selector_separate(selectors: tuple[str, ...], same_path: bool, consumption: str) -> None:
    modules = _dotted_factory_probe(selectors, "canonical", consumption)
    findings = _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)
    assert bool(findings) is (same_path and consumption != "identity")


@pytest.mark.parametrize(
    "expression",
    [
        "receiver['__globals__.update']",
        "receiver.get('__globals__.update')",
        "receiver.__getitem__('__globals__.update')",
        "getattr(receiver, '__globals__.update')",
        "object.__getattribute__(receiver, '__globals__.update')",
    ],
)
@pytest.mark.parametrize("namespace_receiver", [True, False])
def test_literal_member_grammars_do_not_acquire_factory_dotted_traversal(expression: str, namespace_receiver: bool) -> None:
    receiver = "func.__globals__" if namespace_receiver else "obj"
    probe = f"def recover(func, obj):\n    receiver = {receiver}\n    return {expression}"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


@pytest.mark.parametrize("selector", ["__globals__.update", "__dict__.clear"])
def test_dotted_factory_construction_has_no_application_receiver(selector: str) -> None:
    probe = f"import operator\ndef recover(): return operator.attrgetter({selector!r})"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"probe.py": probe}) == []


def test_actual_composer_ancestry_with_actual_runtime_manifest_source() -> None:
    root = Path(__file__).resolve().parents[3] / "src/elspeth"
    paths = ("web/composer/protocol.py", "web/composer/prompt_guard.py", "contracts/runtime_val_manifest.py")
    modules = {f"elspeth/{path}": (root / path).read_text() for path in paths}
    source = """
        from elspeth.web.composer.protocol import ComposerContextUnsupported
        from elspeth.web.composer.prompt_guard import PromptGuardRefused
        from sqlalchemy.exc import IntegrityError, SQLAlchemyError
        def classify(exc):
            return isinstance(exc, (ComposerContextUnsupported, PromptGuardRefused, IntegrityError, SQLAlchemyError))
    """
    assert _r5(source, modules) == []


@pytest.mark.parametrize("source_order", ["normal", "reversed"])
def test_complete_actual_source_scan_preserves_input_fidelity_and_nonexception_findings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, source_order: str
) -> None:
    root = Path(__file__).resolve().parents[3] / "src/elspeth"
    nominal = {
        "web/composer/service.py": (
            "isinstance(refused, ComposerContextUnsupported)",
            "isinstance(p4_direct_failure, IntegrityError)",
            "isinstance(previous_cause, SQLAlchemyError)",
            "isinstance(dispatch.advisor_failure, ComposerContextUnsupported)",
            "isinstance(dispatch.advisor_failure, (PromptGuardRefused, ComposerContextUnsupported))",
            "isinstance(advisor_refusal, ComposerContextUnsupported)",
            "isinstance(early_checkpoint_failure, (PromptGuardRefused, ComposerContextUnsupported))",
        ),
        "web/composer/tool_batch.py": ("isinstance(first_party_exc, ComposerContextUnsupported)",),
        "web/sessions/composer_operation_errors.py": ("isinstance(leaf, ComposerContextUnsupported)",),
    }
    sites: dict[str, set[tuple[str, int, int]]] = {"nominal": set(), "nonexception": set()}
    expected_groups = {"nominal": nominal, "nonexception": {"contracts/runtime_val_manifest.py": ("isinstance(value, str)",)}}
    for kind, files in expected_groups.items():
        for relative, expressions in files.items():
            original = ast.parse((root / relative).read_text(), filename=relative)
            for expression in expressions:
                expected = ast.dump(ast.parse(expression, mode="eval").body)
                matches = [node for node in ast.walk(original) if isinstance(node, ast.Call) and ast.dump(node) == expected]
                assert len(matches) == 1, (relative, expression)
                sites[kind].add((relative, matches[0].lineno, matches[0].col_offset))
    # Each bounded case makes one complete scan. Root must also compare the two
    # equal-graph reports with _assert_complete_input_order_parity; separate
    # case passes do not establish that metamorphic relation.
    findings = _complete_source_report(root, tmp_path, monkeypatch, source_order)
    actual = {(finding.file_path, finding.line, finding.col) for finding in findings if finding.rule_id == "R5"}
    (tmp_path / "actual-r5-sites.txt").write_text("\n".join(f"{path}:{line}:{col}" for path, line, col in sorted(actual)) + "\n")
    (tmp_path / "remaining-nominal-sites.txt").write_text(
        "\n".join(f"{path}:{line}:{col}" for path, line, col in sorted(sites["nominal"] & actual)) + "\n"
    )
    (tmp_path / "nominal-site-continuity.json").write_text(
        json.dumps({kind: sorted(locations) for kind, locations in sites.items()}, indent=2) + "\n", encoding="utf-8"
    )
    # Nominal locations are continuity observations. Explicit unconfined effects
    # can withdraw proof in this larger graph; their presence/absence is not a
    # new whitelist or expected-current-findings snapshot.
    assert sites["nonexception"] <= actual


def test_imported_dynamic_code_alias_withdraws_new_proof() -> None:
    source = "from bridge import run\nclass Refusal(Exception): pass\nrun(code)\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, {"bridge.py": "run = exec"})) == 1


def test_imported_function_globals_recovery_withdraws_new_proof() -> None:
    source = "from bridge import helper\nclass Refusal(Exception): pass\nhelper.__globals__['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, {"bridge.py": "def helper(): pass"})) == 1


@pytest.mark.parametrize(
    "expression",
    [
        "getattr(builtins, 'exec')",
        "getattr(builtins, 'eval')",
        "getattr(builtins, 'globals')",
        "reflect(builtins, 'exec')",
        "builtins.__getattribute__('eval')",
        "getattr(builtins, 'getattr')",
    ],
)
def test_reflected_namespace_effect_callable_recovery_is_unsupported(expression: str) -> None:
    source = f"import builtins\nfrom builtins import getattr as reflect\nclass Refusal(Exception): pass\nrun = {expression}\nrun(code)\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "namespace['__builtins__'].update(Exception=str)",
        "run = namespace['exec']\nrun(code)",
        "namespace.get('__builtins__').update(Exception=str)",
    ],
)
def test_escaped_supplied_module_namespace_withdraws_builtin_and_callable_proofs(operation: str) -> None:
    source = f"from bridge import namespace\n{operation}\ndef f(exc): return isinstance(exc, Exception)"
    assert len(_r5(source, {"bridge.py": "from builtins import exec\nnamespace = globals()"})) == 1


def test_explicit_recovered_builtin_subscript_withdraws_new_proof() -> None:
    source = "from unknown import namespace\nnamespace['__builtins__'].update(Exception=str)\ndef f(exc): return isinstance(exc, Exception)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "import inspect\ninspect.getattr_static(errors, '__dict__').update(SQLAlchemyError=str)",
        "from inspect import getattr_static as reflect\nreflect(errors, '__dict__').update(SQLAlchemyError=str)",
        "import inspect\nreflect = inspect.getattr_static\nreflect(errors, selector).update(SQLAlchemyError=str)",
        "import inspect\nimport builtins\nrun = inspect.getattr_static(builtins, 'exec')\nrun(code)",
        "import operator\noperator.attrgetter('__dict__')(errors).update(SQLAlchemyError=str)",
        "from operator import attrgetter as make_getter\nmake_getter('__dict__')(errors).update(SQLAlchemyError=str)",
        "import operator\nmake_getter = operator.attrgetter\nmake_getter('child.__dict__')(errors).update(SQLAlchemyError=str)",
        "import operator\noperator.attrgetter(selector)(errors).update(SQLAlchemyError=str)",
        "import operator\nimport builtins\nrun = operator.attrgetter('exec')(builtins)\nrun(code)",
        "run = __import__('builtins').exec\nrun(code)",
    ],
)
def test_canonical_reflection_and_unknown_dynamic_effect_recovery_is_unsupported(operation: str) -> None:
    source = f"import sqlalchemy.exc as errors\n{operation}\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "import inspect\ninspect.getattr_static(exc, 'label')",
        "import operator\noperator.attrgetter('label')(exc)",
        "from unrelated import attrgetter\nattrgetter('__dict__')",
    ],
)
def test_literal_non_namespace_reads_and_noncanonical_api_spelling_keep_proof(operation: str) -> None:
    source = f"class Refusal(Exception): pass\n{operation}\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    "operation",
    [
        "import operator\noperator = unknown\noperator.attrgetter('label')(exc)",
        "import operator\noperator.attrgetter = unknown\noperator.attrgetter('label')(exc)",
        "import operator\nmake = operator.attrgetter if flag else unknown\nmake('label')(exc)",
        "import operator\nselect = operator.attrgetter('label')\nselect = unknown\nselect(exc)",
        "import operator\noperator.attrgetter(selector)(exc)",
        "import operator\noperator.attrgetter('label', default=value)(exc)",
    ],
    ids=["shadowed-module", "rebound-api", "mixed-maker", "rebound-selection", "unknown-selector", "unknown-shape"],
)
def test_passive_factory_application_keeps_actual_provenance_uncertainty(operation: str) -> None:
    source = f"class Refusal(Exception): pass\n{operation}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_passive_factory_fact_does_not_prove_its_result_as_a_checked_type() -> None:
    source = (
        "import operator\nclass Refusal(Exception): pass\n"
        "Alias = operator.attrgetter('Refusal')(module)\ndef f(exc): return isinstance(exc, Alias)"
    )
    assert len(_r5(source)) == 1


def test_passive_factory_read_context_stays_local_to_its_supplied_source() -> None:
    modules = {"probe.py": "import operator\ndef read(exc, selector): return operator.attrgetter(selector)(exc)"}
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules) == []


def test_passive_factory_does_not_trust_a_supplied_module_with_the_canonical_name() -> None:
    modules = {"operator.py": "def attrgetter(name): return unknown"}
    source = (
        "import operator\nclass Refusal(Exception): pass\noperator.attrgetter('label')(exc)\ndef f(exc): return isinstance(exc, Refusal)"
    )
    assert len(_r5(source, modules)) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "import operator\nmake = getattr(operator, 'attrgetter')\nmake('__dict__')(errors).update(SQLAlchemyError=str)",
        "import inspect\nreflect = getattr(inspect, 'getattr_static')\nreflect(errors, '__dict__').update(SQLAlchemyError=str)",
        "import operator\noperator.attrgetter('inspect.getattr_static')",
        "make = get_module().attrgetter\nmake('__dict__')(errors).update(SQLAlchemyError=str)",
        "reflect = get_module().getattr_static\nreflect(errors, '__dict__').update(SQLAlchemyError=str)",
        "errors.__setattr__('SQLAlchemyError', str)",
        "errors.__delattr__('SQLAlchemyError')",
        "replace = errors.__setattr__\nreplace('SQLAlchemyError', str)",
        "remove = errors.__delattr__\nremove('SQLAlchemyError')",
        "setattr(errors, '__setattr__', replace)",
        "getattr(errors, '__setattr__')('SQLAlchemyError', str)",
    ],
)
def test_composed_reflection_and_namespace_mutation_dunders_are_unsupported(operation: str) -> None:
    source = f"import sqlalchemy.exc as errors\n{operation}\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


def test_proven_unrelated_qualified_getter_spelling_keeps_proof() -> None:
    source = "import unrelated\nclass Refusal(Exception): pass\nmake = unrelated.attrgetter\nmake('__dict__')\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    "operation",
    [
        "object.__setattr__(instance, 'label', 'context')",
        "object.__delattr__(instance, 'label')",
        "BaseException.__setattr__(instance, 'label', 'context')",
        "BaseException.__delattr__(instance, 'label')",
        "BaseException.__getattribute__(instance, '_safe_argument')",
        "instance.__setattr__('label', 'context')",
        "instance.__delattr__('label')",
        "instance.__getattribute__('label')",
        "setattr(instance, 'label', 'context')",
        "delattr(instance, 'label')",
        "from builtins import object as Native\nNative.__setattr__(instance, 'label', 'context')",
        "from builtins import BaseException as Native\nNative.__getattribute__(instance, '_safe_argument')",
        "from builtins import setattr as put\nput(instance, 'label', 'context')",
    ],
)
def test_declared_policy_ordinary_slot_member_does_not_poison_nominal_kind(operation: str) -> None:
    # V5's exact object/label strict-only plant migrates here under the
    # approved declared-member policy. This asserts no runtime setter purity.
    source = f"import sqlalchemy.exc as errors\n{operation}\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    "method",
    [
        "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)",
        "def put(receiver, name):\n    object.__delattr__(receiver, name)",
        "def put(receiver, name, value):\n    setattr(receiver, name, value)",
        "def put(receiver, name):\n    delattr(receiver, name)",
        "def __setattr__(receiver, name, value):\n    super().__setattr__(name, value)",
        "def __delattr__(receiver, name):\n    super().__delattr__(name)",
    ],
)
def test_declared_immediate_instance_role_supports_dynamic_forwarding(method: str) -> None:
    source = "class Refusal(Exception): pass\nclass Holder:\n" + "\n".join("    " + line for line in method.splitlines())
    source += "\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


def test_final_owner_with_subclass_hook_keeps_other_instance_method_role() -> None:
    source = """
        from typing import final
        class Refusal(Exception): pass
        @final
        class Holder(Exception):
            def __init_subclass__(cls): pass
            def __setattr__(receiver, name, value):
                super().__setattr__(name, value)
        def f(exc): return isinstance(exc, Refusal)
    """
    assert _r5(source) == []


def test_other_selected_module_instance_work_does_not_poison_kind() -> None:
    modules = {"ordinary.py": "class Holder:\n    def put(receiver, name, value): object.__setattr__(receiver, name, value)"}
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules) == []


@pytest.mark.parametrize(
    "operation",
    [
        "object.__setattr__(instance, 'SQLAlchemyError', str)",
        "object.__setattr__(instance, '__bases__', (str,))",
        "object.__setattr__(instance, '__dict__', namespace)",
        "object.__setattr__(instance, member, value)",
        "object.__setattr__(errors, 'SQLAlchemyError', str)",
        "BaseException.__setattr__(errors, 'SQLAlchemyError', str)",
        "object.__setattr__(errors, member, value)",
        "setattr(errors, member, value)",
        "delattr(errors, 'SQLAlchemyError')",
        "object.__getattribute__(errors, '__dict__')",
        "BaseException.__getattribute__(instance, '__dict__')",
        "type.__setattr__(errors.SQLAlchemyError, '__bases__', (str,))",
        "object.__setattr__(instance, name='label', value='value')",
        "object.__setattr__(*arguments)",
        "object.__getattribute__(instance)",
        "write = object.__setattr__\nwrite(instance, 'label', 'context')",
        "BaseException = replacement\nBaseException.__setattr__(instance, 'label', 'context')",
        "from unrelated import object\nobject.__setattr__(instance, 'label', 'context')",
    ],
)
def test_direct_namespace_sensitive_member_and_unsupported_slot_shapes_stay_r5(operation: str) -> None:
    source = f"import sqlalchemy.exc as errors\n{operation}\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "owner, method, suffix",
    [
        ("class Holder:", "@staticmethod\ndef put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "@classmethod\ndef put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "@decorate\ndef put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put[T](receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def __new__(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def __init_subclass__(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def __class_getitem__(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put(receiver=namespace, name=None, value=None):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    receiver = other\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    def nested(): object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    return [object.__setattr__(receiver, name, value) for item in items]", ""),
        ("class Holder[T]:", "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder(metaclass=Meta):", "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder(Unknown):", "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder(type):", "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    super(Holder, receiver).__setattr__(name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    __class__ = Other\n    super().__setattr__(name, value)", ""),
        ("class Holder:", "def put(receiver, name, value):\n    super().__setattr__(name, value)", "\nsuper = replacement"),
        ("class Holder:", "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)", "\nHolder = replacement"),
        (
            "class Holder:",
            "def put(receiver, name, value):\n    object.__setattr__(receiver, name, value)",
            "\nimport builtins\nbuiltins.object = replacement",
        ),
    ],
)
def test_unsupported_or_rebound_declaration_does_not_supply_instance_role(owner: str, method: str, suffix: str) -> None:
    source = "class Refusal(Exception): pass\n" + owner + "\n" + "\n".join("    " + line for line in method.splitlines())
    source += suffix + "\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize("reverse", [False, True])
def test_instance_role_withdrawal_observes_later_candidate_effects(reverse: bool) -> None:
    forwarding = "class Forwarder:\n    def put(receiver, name, value): super().__setattr__(name, value)"
    invalidating = "class Other:\n    def put(receiver, value): BaseException.__setattr__(receiver, 'Forwarder', value)"
    declarations = [forwarding, invalidating]
    if reverse:
        declarations.reverse()
    source = "class Refusal(Exception): pass\n" + "\n".join(declarations) + "\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_cross_module_builtin_mutation_withdraws_super_role() -> None:
    source = "class Refusal(Exception): pass\nclass Holder:\n    def put(receiver, name, value): super().__setattr__(name, value)\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, {"mutation.py": "import builtins\nbuiltins.super = replacement"})) == 1


def test_final_owner_role_does_not_grant_decorated_checked_kind() -> None:
    source = "from typing import final\n@final\nclass Holder(Exception):\n    def put(receiver, name, value): super().__setattr__(name, value)\ndef f(exc): return isinstance(exc, Holder)"
    assert len(_r5(source)) == 1


def test_namespace_type_ancestry_does_not_supply_instance_role() -> None:
    source = "from types import ModuleType\nclass Refusal(Exception): pass\nclass Holder(ModuleType):\n    def put(receiver, name, value): object.__setattr__(receiver, name, value)\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_rebound_final_marker_does_not_supply_instance_role() -> None:
    source = "from typing import final\n@final\nclass Holder(Exception):\n    def put(receiver, name, value): super().__setattr__(name, value)\nfinal = replacement\ndef f(exc): return isinstance(exc, Exception)"
    assert len(_r5(source)) == 1


def test_selected_source_shadow_of_final_module_does_not_supply_instance_role() -> None:
    source = "from typing import final\n@final\nclass Holder(Exception):\n    def put(receiver, name, value): super().__setattr__(name, value)\ndef f(exc): return isinstance(exc, Exception)"
    assert len(_r5(source, {"typing.py": "def final(value): return replacement"})) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "type.__setattr__(receiver, name, value)",
        "type.__delattr__(receiver, name)",
        "Meta.__setattr__(receiver, name, value)",
        "Meta.__delattr__(receiver, name)",
    ],
)
def test_canonical_type_slots_never_acquire_instance_forwarding_role(operation: str) -> None:
    source = f"from builtins import type as Meta\nclass Refusal(Exception): pass\nclass Holder:\n    def work(receiver, name, value): {operation}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "replacement",
    [
        "put = classmethod(put)",
        "put = staticmethod(put)",
        "put = put",
        "def put(receiver, name, value): pass",
        "locals()['put'] = classmethod(put)",
    ],
)
def test_replaced_original_method_binding_does_not_supply_instance_role(replacement: str) -> None:
    source = f"class Refusal(Exception): pass\nclass Holder:\n    def put(receiver, name, value): setattr(receiver, name, value)\n    {replacement}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_cross_unit_qualified_method_effect_invalidates_instance_role() -> None:
    modules = {"owner.py": "class Holder:\n    def put(receiver, name, value): object.__setattr__(receiver, name, value)"}
    source = "from owner import Holder\nHolder.put = replacement\ndef f(exc): return isinstance(exc, Exception)"
    assert len(_r5(source, modules)) == 1


@pytest.mark.parametrize(
    "declaration, operation, alias",
    [
        ("from builtins import object as Native", "Native.__setattr__(receiver, name, value)", "Native"),
        ("from builtins import super as Sup", "Sup().__setattr__(name, value)", "Sup"),
        ("from builtins import setattr as put", "put(receiver, name, value)", "put"),
        ("from builtins import object as Native", "Native.__setattr__(receiver, 'label', value)", "Native"),
        ("from builtins import BaseException as Native", "Native.__getattribute__(receiver, '_safe_argument')", "Native"),
        ("from builtins import setattr as put", "put(receiver, 'label', value)", "put"),
    ],
)
def test_named_namespace_effect_revalidates_original_canonical_alias(declaration: str, operation: str, alias: str) -> None:
    prefix = f"from builtins import globals as namespace\n{declaration}\nclass Refusal(Exception): pass\nclass Holder:\n    def work(receiver, name, value): {operation}\n"
    suffix = "def f(exc): return isinstance(exc, Refusal)"
    assert _r5(prefix + suffix) == []
    assert len(_r5(prefix + f"namespace()['{alias}'] = replacement\n" + suffix)) == 1


@pytest.mark.parametrize(
    "operation",
    [
        "type Alias[object] = object.__setattr__(receiver, 'label', value)",
        "type Alias[Native] = Native.__setattr__(receiver, 'label', value)",
        "type Alias[T] = receiver.__setattr__('label', value)",
    ],
)
def test_unsupported_annotation_scope_cannot_establish_effect_provenance(operation: str) -> None:
    source = (
        f"from builtins import object as Native\nclass Refusal(Exception): pass\n{operation}\ndef f(exc): return isinstance(exc, Refusal)"
    )
    assert len(_r5(source)) == 1


def test_visible_callback_mutation_remains_direct_source_evidence() -> None:
    source = "import sqlalchemy.exc as errors\nclass Holder:\n    def put(receiver, name, value): object.__setattr__(receiver, name, value)\ndef callback(): errors.SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


def test_unrelated_literal_attribute_read_does_not_withdraw_new_proof() -> None:
    source = "class Refusal(Exception): pass\ndef f(exc):\n    label = getattr(exc, 'label')\n    return isinstance(exc, Refusal)"
    assert _r5(source) == []


def test_unrelated_instance_dictionary_membership_does_not_withdraw_ancestry() -> None:
    source = "class Refusal(Exception): pass\nclass Reader:\n    def contains(self, name): return name in self.__dict__\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


def test_ordinary_instance_write_does_not_make_its_class_a_namespace_alias() -> None:
    source = "class Refusal(Exception): pass\ndef f(exc):\n    exc.note = 'context'\n    return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    ("declaration", "write"),
    [
        ("data = {}", "data['note'] = context"),
        ("data = [None]", "data[0] = context"),
        ("data: dict[str, object] = {}", "data['note'] = context"),
        ("data = {'error': Refusal}", "data['note'] = context"),
        ("data = [Refusal, None]", "data[1] = context"),
        ("data = {'note': context}", "del data['note']"),
        ("data = [context]", "del data[0]"),
    ],
)
def test_local_data_element_writes_do_not_withdraw_exception_ancestry(declaration: str, write: str) -> None:
    source = (
        f"class Refusal(Exception): pass\ndef update(context):\n    {declaration}\n    {write}\ndef f(exc): return isinstance(exc, Refusal)"
    )
    assert _r5(source) == []


def test_ordinary_prompt_request_data_write_preserves_supplied_exception_declaration() -> None:
    modules = {
        "errors.py": """
            class Refusal(Exception): pass
            def request(parts, thinking):
                exact = {'messages': [part.text for part in parts]}
                if thinking:
                    exact['thinking_parts'] = [{'text': part.text} for part in thinking]
                return exact
        """
    }
    assert _r5("from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)", modules) == []


@pytest.mark.parametrize(
    "body",
    [
        "def update(data): data['Refusal'] = str",
        "def update():\n    data = dict()\n    data['Refusal'] = str",
        "def update():\n    data = {}\n    alias = data\n    alias['Refusal'] = str",
        "def update():\n    data = {}\n    data = other\n    data['Refusal'] = str",
        "def update(data):\n    data = {}\n    data['Refusal'] = str",
        "def update():\n    if flag: data = {}\n    data['Refusal'] = str",
        "def update():\n    global data\n    data = {}\n    data['Refusal'] = str",
        "def update():\n    data = {}\n    globals()['data'] = replacement\n    data['Refusal'] = str",
        "def update():\n    for item in items:\n        data = {}\n        data[item] = value",
        "def update(data):\n    del data[index]",
    ],
)
def test_unresolved_data_element_receiver_does_not_claim_binding_effects(body: str) -> None:
    source = f"class Refusal(Exception): pass\n{body}\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize(
    "body",
    [
        "from builtins import globals as namespace\ndef update():\n    data = {}\n    namespace()['data'] = replacement\n    data['Refusal'] = str",
        "from builtins import locals as namespace\ndef update():\n    data = {}\n    data['Refusal'] = str\n    namespace()['data'] = replacement",
        "from builtins import vars as namespace\ndef update():\n    data = {}\n    namespace()['data'] = replacement\n    data['Refusal'] = str",
    ],
)
def test_unresolved_or_rebound_data_receiver_keeps_namespace_conservatism(body: str) -> None:
    source = f"class Refusal(Exception): pass\n{body}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "body",
    [
        "def outer():\n    data = {}\n    def update():\n        nonlocal data\n        data['Refusal'] = str",
        "data = {}\ndef update(): data['Refusal'] = str",
    ],
)
def test_outer_data_element_writes_do_not_rebind_the_module_namespace(body: str) -> None:
    source = f"class Refusal(Exception): pass\n{body}\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize("write", ["globals()['cache'] = value", "del globals()['cache']"])
def test_direct_own_globals_write_preserves_an_unrelated_same_module_binding(write: str) -> None:
    source = f"class Refusal(Exception): pass\n{write}\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []


@pytest.mark.parametrize("write", ["globals()['Refusal'] = str", "globals()[name] = value", "del globals()[name]"])
def test_own_globals_binding_effects_follow_only_actual_module_dependencies(write: str) -> None:
    affected = f"class Refusal(Exception): pass\n{write}"
    modules = {"affected.py": affected, "bridge.py": "from affected import Refusal"}
    independent = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    dependent = (
        "from affected import Refusal\ndef f(exc): return isinstance(exc, Refusal)",
        "import affected\ndef f(exc): return isinstance(exc, affected.Refusal)",
        "from affected import Refusal\nclass Child(Refusal): pass\ndef f(exc): return isinstance(exc, Child)",
        "from bridge import Refusal\ndef f(exc): return isinstance(exc, Refusal)",
    )
    assert _r5(independent, modules) == []
    assert _r5("def f(exc): return isinstance(exc, Exception)", modules) == []
    for source in dependent:
        assert len(_r5(source, modules)) == 1
        assert len(_r5(source, dict(reversed(tuple(modules.items()))))) == 1


@pytest.mark.parametrize("name", ["Exception", "isinstance"])
def test_direct_own_globals_write_still_withdraws_local_builtin_dependencies(name: str) -> None:
    source = f"class Refusal(Exception): pass\nglobals()[{name!r}] = unknown\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_direct_globals_owner_does_not_survive_a_rebound_builtin_supplier() -> None:
    modules = {"writer.py": "import builtins\nbuiltins.globals = replacement"}
    source = "class Refusal(Exception): pass\nglobals()['cache'] = value\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, modules)) == 1


@pytest.mark.parametrize(
    "write",
    [
        "namespace = globals()\nnamespace[name] = value",
        "namespace = globals() if flag else data\nnamespace[name] = value",
        "globals()['errors'].SQLAlchemyError = str",
        "globals()['__builtins__']['Exception'] = str",
        "func.__globals__[name] = value",
        "globals = unknown\nglobals()[name] = value",
    ],
)
def test_recovered_mixed_or_nested_namespace_write_keeps_graph_refusal(write: str) -> None:
    modules = {"writer.py": write}
    assert len(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("bridge", "write"),
    [
        ("", "globals()['errors'].SQLAlchemyError = str"),
        ("", "selected = globals()['errors']\nselected.SQLAlchemyError = str"),
        ("selected = globals()['errors']", "from bridge import selected\nselected.SQLAlchemyError = str"),
        ("", "selected, spare = globals()['errors'], None\nselected.SQLAlchemyError = str"),
        ("", "selected = globals()['errors'] if flag else ordinary\nselected.SQLAlchemyError, spare = str, None"),
        ("", "object.__setattr__(globals()['errors'], 'SQLAlchemyError', str)"),
        ("", "selected = globals()['errors']\nreplace = selected.update"),
    ],
)
def test_nested_namespace_member_targets_keep_vetoes_through_original_aliases(bridge: str, write: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"bridge.py": bridge, "writer.py": write}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "value = globals()['label']\nvalue.strip()",
        "value = globals()['label']\nvalue['status'] = 'ok'",
        "value, spare = globals()['label'], None\nvalue['status'] = 'ok'",
        "value = getattr(obj, 'label', sentinel)\nvalue.note = context",
    ],
)
def test_nested_namespace_read_results_keep_ordinary_consumers_source_local(body: str, reverse_inputs: bool) -> None:
    writer = "def inspect_source(obj, context):\n" + "\n".join("    " + line for line in body.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, {"writer.py": writer}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("dictionary", "direct"),
    [
        ("receiver.__dict__['label'] = context", "receiver.label = context"),
        ("receiver.__dict__.pop('label')", "del receiver.label"),
        ("receiver.__dict__.pop('label', None)", "del receiver.label"),
        ("value = receiver.__dict__.pop('label', None)\nvalue['status'] = 'ok'", "del receiver.label"),
    ],
)
def test_declared_literal_instance_dictionary_effects_match_direct_member_inventory(
    dictionary: str, direct: str, reverse_inputs: bool
) -> None:
    # Literal declared effects do not require a runtime ancestry/storage claim.
    prefix = "from foreign import Base\nclass Holder(Base):\n    def patch(receiver, context):\n"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    for body in (dictionary, direct):
        writer = prefix + "\n".join("        " + line for line in body.splitlines())
        assert _ordered_r5(subject, {"writer.py": writer}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "method",
    [
        "@staticmethod\ndef patch(receiver, context): receiver.__dict__['label'] = context",
        "@classmethod\ndef patch(receiver, context): receiver.__dict__.pop('label', None)",
        "@decorate\ndef patch(receiver, context): receiver.__dict__.pop('label', None)",
        "def patch(receiver=foreign, context=None): receiver.__dict__['label'] = context",
        "def patch(receiver, context):\n    receiver = foreign\n    receiver.__dict__.pop('label', None)",
        "def patch(receiver, context):\n    dictionary = receiver.__dict__\n    dictionary.pop('label', None)",
        "def patch(receiver, context): foreign.__dict__['label'] = context",
        "def patch(receiver, context): Holder.__dict__.pop('label', None)",
        "def patch(receiver, context): receiver.__dict__[name] = context",
        "def patch(receiver, context): receiver.__dict__.pop(name, None)",
        "def patch(receiver, context):\n    remove = receiver.__dict__.pop\n    remove('label', None)",
        "def patch(receiver, context): receiver.__dict__.update(label=context)",
        "def patch(receiver, context): receiver.__dict__.clear()",
        "def patch(receiver, context): receiver.__dict__.setdefault('label', context)",
        "def patch(receiver, context): receiver.__dict__.pop('label', default=None)",
    ],
)
def test_dictionary_effect_category_rejects_noninstance_alias_dynamic_and_other_mutator_shapes(method: str, reverse_inputs: bool) -> None:
    writer = "class Holder:\n" + "\n".join("    " + line for line in method.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "receiver.__dict__['Refusal'] = str",
        "receiver.__dict__.pop('Refusal', None)",
        "receiver.__dict__['__bases__'] = context",
        "receiver.__dict__.pop('__dict__', None)",
        "receiver.__dict__['label'] = getattr(obj, '__dict__')",
        "receiver.__dict__['label'] = setattr",
        "value = receiver.__dict__.pop('label', globals())\nvalue['Refusal'] = str",
        "value = receiver.__dict__.pop('label', foreign.__dict__.update)\nreturn value",
        "value = receiver.__dict__.pop('label', setattr)\nvalue(obj, 'Refusal', str)",
    ],
)
def test_dictionary_member_replacement_and_explicit_capability_defaults_keep_vetoes(body: str, reverse_inputs: bool) -> None:
    writer = "class Holder:\n    def patch(receiver, context, obj):\n" + "\n".join("        " + line for line in body.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "mutation", ["from writer import Holder\nHolder.patch = replacement", "import writer\nwriter.Holder = replacement"]
)
def test_dictionary_member_category_rechecks_accumulated_owner_and_method_effects(mutation: str, reverse_inputs: bool) -> None:
    writer = "class Holder:\n    def patch(receiver, context):\n        receiver.__dict__['label'] = context\n        return receiver.__dict__.pop('label', None)"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    companions = {"writer.py": writer, "mutation.py": mutation}
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_dictionary_pop_result_never_supplies_a_favorable_checked_class(reverse_inputs: bool) -> None:
    subject = "class Holder:\n    def check(receiver, exc):\n        Alias = receiver.__dict__.pop('kind', None)\n        return isinstance(exc, Alias)"
    assert len(_ordered_r5(subject, {"other.py": "class Other(Exception): pass"}, reverse_inputs)) == 1
    owner = "from foreign import Base\nclass Holder(Base):\n    def patch(receiver, context): receiver.__dict__['label'] = context"
    imported = "from writer import Holder\ndef f(exc): return isinstance(exc, Holder)"
    assert len(_ordered_r5(imported, {"writer.py": owner}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("bridge", "writer"),
    [
        ("", "globals()['errors'].update(Refusal=str)"),
        ("", "selected = globals()['errors']\nselected.update(Refusal=str)"),
        ("selected = globals()['errors']", "from bridge import selected\nselected.update(Refusal=str)"),
        ("", "selected = globals()['errors']\nselected.clear()"),
        ("", "globals()['errors'].__setitem__('Refusal', str)"),
    ],
)
def test_immediate_nested_namespace_mutator_invocations_retain_selected_effects(bridge: str, writer: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"bridge.py": bridge, "writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("writer", "namespace_effect"),
    [
        ("selected = globals()['errors']\nreplace = selected.update", True),
        ("selected = globals()['errors']\nselected.update is expected", False),
        ("globals()['label'].strip()", False),
        ("value = globals()['label']\nvalue['status'] = 'ok'", False),
        ("def mutate(obj): obj.update(Refusal=str)", False),
        ("class Holder:\n    def take(receiver): return receiver.__dict__.pop('label', None)", False),
    ],
)
def test_immediate_namespace_mutator_opposition_preserves_passive_and_declared_dictionary_reads(
    writer: str, namespace_effect: bool, reverse_inputs: bool
) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    findings = _ordered_r5(subject, {"writer.py": writer}, reverse_inputs)
    if namespace_effect:
        assert len(findings) == 1
    else:
        assert findings == []


@pytest.mark.parametrize(
    ("value", "use"),
    [
        ("globals()", "ns['Refusal'] = str"),
        ("globals", "ns()['Refusal'] = str"),
        ("getattr", "ns(foreign, '__dict__').clear()"),
        ("foreign.__dict__", "ns.update(Refusal=str)"),
        ("foreign.__dict__.update", "ns(Refusal=str)"),
        ("getattr(foreign, '__dict__')", "del ns['Refusal']"),
        ("operator.attrgetter('__dict__')", "ns(foreign).update(Refusal=str)"),
        ("{'namespace': globals()}", "ns['namespace'].clear()"),
    ],
)
def test_own_global_capability_installation_keeps_a_separate_importing_query_refused(value: str, use: str) -> None:
    writer = f"import foreign\nimport operator\nclass Refusal(Exception): pass\nglobals()['ns'] = {value}\n"
    subject = "from writer import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    # Conservatively veto installation itself; no favorable dynamic alias is
    # fabricated, even before the later explicit namespace use is considered.
    assert len(_r5(subject, {"writer.py": writer})) == 1
    assert len(_r5(subject, {"writer.py": writer + use})) == 1


@pytest.mark.parametrize("value", ["None", "data", "{'note': data}"])
def test_own_global_plain_data_installation_retains_local_binding_scope(value: str) -> None:
    writer = f"class Refusal(Exception): pass\nglobals()['ns'] = {value}\nns['note'] = data"
    assert _r5("from writer import Refusal\ndef f(exc): return isinstance(exc, Refusal)", {"writer.py": writer}) == []


@pytest.mark.parametrize(
    "write",
    [
        "globals()['ns'], unused = globals(), None",
        "[globals()['ns'], unused] = [foreign.__dict__, None]",
        "*globals()['ns'], unused = [foreign.__dict__.update, None]",
    ],
)
def test_compound_own_global_capability_installation_refuses_an_independent_importing_query(write: str) -> None:
    writer = f"import foreign\nclass Refusal(Exception): pass\n{write}"
    subject = "from writer import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(subject, {"writer.py": writer})) == 1
    assert len(_r5(subject, {"writer.py": writer + "\nns['Refusal'] = str"})) == 1


@pytest.mark.parametrize(
    "write",
    [
        "globals()['ns'], unused = data, None",
        "[globals()['ns'], unused] = [data, None]",
        "*globals()['ns'], unused = [data, None]",
        "del (globals()['ns'], globals()['unused'])",
        "globals()['ns']: object",
    ],
)
def test_compound_own_global_plain_data_and_deletion_keep_local_binding_scope(write: str) -> None:
    writer = f"class Refusal(Exception): pass\n{write}"
    assert _r5("from writer import Refusal\ndef f(exc): return isinstance(exc, Refusal)", {"writer.py": writer}) == []


@pytest.mark.parametrize(
    "write",
    [
        "for globals()['ns'] in [globals()]: pass",
        "for globals()['ns'], unused in [(globals(), None)]: pass",
        "for globals()['ns'] in [data]: pass",
        "with manager as globals()['ns']: pass",
        "async def install():\n    async for globals()['ns'] in stream: pass",
        "async def install():\n    async with manager as globals()['ns']: pass",
    ],
)
def test_unsupported_own_global_installation_context_keeps_graph_refusal(write: str) -> None:
    writer = f"class Refusal(Exception): pass\n{write}"
    subject = "from writer import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(subject, {"writer.py": writer})) == 1
    assert len(_r5(subject, {"writer.py": writer + "\nns['Refusal'] = str"})) == 1


@pytest.mark.parametrize(
    "forward",
    [
        "Exception.__setattr__(instance, name, value)",
        "BaseException.__delattr__(instance, name)",
        "ValueError.__setattr__(instance, name, value)",
    ],
)
def test_canonical_builtin_exception_slot_abi_supports_a_free_forwarding_helper(forward: str) -> None:
    # No annotation or declared method supplies a runtime receiver/MRO proof.
    writer = f"def forward(instance, name, value):\n    {forward}"
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"writer.py": writer}) == []


@pytest.mark.parametrize(
    "operation",
    [
        "Exception.__setattr__(instance, '__dict__', namespace)",
        "Exception.__setattr__(errors, 'SQLAlchemyError', str)",
        "Exception.__setattr__(errors.SQLAlchemyError, '__bases__', (str,))",
        "Exception.__setattr__(func.__globals__, name, value)",
        "instance = unknown if flag else errors\nException.__setattr__(instance, name, value)",
        "Exception = unknown\nException.__setattr__(instance, name, value)",
        "Exception.__setattr__ = unknown\nException.__setattr__(instance, name, value)",
        "write = Exception.__setattr__\nwrite(instance, name, value)",
        "class Exception:\n    def __setattr__(self, name, value): pass\nException.__setattr__(instance, name, value)",
    ],
)
def test_builtin_exception_slot_abi_does_not_admit_namespace_or_counterfeit_effects(operation: str) -> None:
    writer = f"import sqlalchemy.exc as errors\n{operation}"
    subject = "import sqlalchemy.exc as errors\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(subject, {"writer.py": writer})) == 1


def test_supplied_builtin_module_cannot_supply_an_exception_receiver_abi() -> None:
    modules = {
        "builtins.py": "class Exception:\n    def __setattr__(self, name, value): pass",
        "writer.py": "from builtins import Exception\ndef forward(instance, name, value): Exception.__setattr__(instance, name, value)",
    }
    assert len(_r5("import sqlalchemy.exc as errors\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)", modules)) == 1


@pytest.mark.parametrize(
    "read",
    [
        "type.__dict__['__name__'].__get__(type(current))",
        "BaseException.__dict__['__traceback__'].__get__(current)",
        "BaseException.__dict__['__cause__'].__get__(current)",
        "BaseException.__dict__['__context__'].__get__(current)",
        "BaseException.__dict__['__suppress_context__'].__get__(current)",
    ],
)
def test_original_canonical_builtin_data_descriptor_read_is_not_a_graph_write(read: str) -> None:
    modules = {"reader.py": f"def read(current): return {read}"}
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules) == []


@pytest.mark.parametrize(
    ("reader", "graph_effect"),
    [
        ("def read(current): return Custom.__dict__['__name__'].__get__(type(current))", False),
        ("def read(current): return BaseException.__dict__['foreign'].__get__(current)", False),
        ("def read(current, name): return BaseException.__dict__[name].__get__(current)", False),
        ("def write(current, value): BaseException.__dict__['__traceback__'].__set__(current, value)", True),
        ("desc = BaseException.__dict__['__traceback__']\ndef read(current): return desc.__get__(current)", False),
        ("desc = BaseException.__dict__['__traceback__'] if flag else foreign\ndef read(current): return desc.__get__(current)", False),
        ("BaseException = unknown\ndef read(current): return BaseException.__dict__['__traceback__'].__get__(current)", False),
        ("type = unknown\ndef read(current): return type.__dict__['__name__'].__get__(type(current))", False),
        ("import builtins\nbuiltins.type = unknown\ndef read(current): return type.__dict__['__name__'].__get__(type(current))", True),
        ("BaseException.__traceback__ = unknown\ndef read(current): return BaseException.__dict__['__traceback__'].__get__(current)", True),
        ("def read(current): return BaseException.__dict__['__traceback__'].__get__(current, BaseException)", False),
        (
            "class Owner:\n    @property\n    def payload(self): return foreign()\ndef read(current): return Owner.__dict__['payload'].__get__(current)",
            False,
        ),
    ],
)
def test_descriptor_read_and_actual_builtin_mutation_have_distinct_graph_effects(reader: str, graph_effect: bool) -> None:
    assert bool(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", {"reader.py": reader})) is graph_effect


def test_supplied_builtin_module_cannot_supply_a_data_descriptor_read() -> None:
    modules = {
        "builtins.py": "class BaseException:\n    @property\n    def __traceback__(self): return foreign()",
        "reader.py": "from builtins import BaseException\ndef read(current): return BaseException.__dict__['__traceback__'].__get__(current)",
    }
    assert len(_r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules)) == 1


@pytest.mark.parametrize("path", ["contracts/blobs.py", "engine/orchestrator/cleanup.py"])
def test_actual_builtin_forwarder_and_descriptor_owner_preserve_an_independent_nominal_query(path: str) -> None:
    root = Path(__file__).resolve().parents[3] / "src/elspeth"
    modules = {f"elspeth/{path}": (root / path).read_text()}
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)", modules) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize("member", ["__cause__", "__context__"])
def test_immutable_builtin_descriptor_identity_survives_unknown_instance_value_writes(member: str, reverse_inputs: bool) -> None:
    companions = {
        "reader.py": f"def read(current): return BaseException.__dict__[{member!r}].__get__(current)",
        "writer.py": f"def attach(exc, value): exc.{member} = value",
    }
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, companions, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "mutation",
    [
        "import builtins\nbuiltins.BaseException = replacement",
        "BaseException.__cause__ = replacement",
        "import builtins\nbuiltins.BaseException.__context__ = replacement",
        "BaseException.__dict__['__cause__'] = replacement",
        "def mutate(obj): obj.__instancecheck__ = replacement",
    ],
)
def test_actual_builtin_descriptor_replacement_and_sensitive_effects_still_refuse(mutation: str, reverse_inputs: bool) -> None:
    companions = {
        "reader.py": "def read(current): return BaseException.__dict__['__cause__'].__get__(current)",
        "writer.py": mutation,
    }
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_builtin_descriptor_identity_category_does_not_weaken_owned_mutable_class_identity(reverse_inputs: bool) -> None:
    companions = {
        "errors.py": "class Refusal(Exception): pass",
        "reader.py": "def read(current): return BaseException.__dict__['__cause__'].__get__(current)",
        "writer.py": "from errors import Refusal\nRefusal.__cause__ = replacement",
    }
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "reader",
    [
        "def read(obj, sentinel):\n    value = getattr(obj, 'id', sentinel)\n    return value.strip()",
        "def read(obj, name):\n    value = getattr(obj, name)\n    raw = {'value': value}\n    raw['status'] = 'ok'",
        "def read(obj, fields):\n    raw = {name: deep_thaw(getattr(obj, name)) for name in fields}\n    raw['status'] = 'ok'",
        "class Wrapper:\n    def getattr(self, obj, name):\n        namespace = super().getattr(obj, name)\n        namespace.update(Refusal=str)",
        "def mutate(obj, name):\n    read = get_module().getattr_static\n    namespace = read(obj, name)\n    namespace[key] = str",
    ],
)
def test_unknown_read_result_compositions_do_not_establish_cross_input_namespace_effects(reader: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, {"reader.py": reader}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_unknown_lazy_export_keeps_independent_proof_but_refuses_opaque_imports(reverse_inputs: bool) -> None:
    facade = (
        "from importlib import import_module\nclass Refusal(Exception): pass\n"
        "descriptor_value = BaseException.__dict__['__cause__'].__get__(current)\n"
        "value = getattr(import_module('foreign'), name)\nglobals()[name] = value"
    )
    companions = {"facade.py": facade}
    independent = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    dependent = "from facade import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(independent, companions, reverse_inputs) == []
    assert len(_ordered_r5(dependent, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "effect",
    [
        "namespace = getattr(obj, '__dict__')\nnamespace['Refusal'] = str",
        "namespace = getattr(obj, '__dict__')\ndel namespace['Refusal']",
        "replace = getattr(obj, '__dict__').update",
        "value = globals()\nglobals()['ns'], unused = value, None\nns['Refusal'] = str",
        "value = getattr(obj, name, globals())\nvalue['Refusal'] = str",
        "value = getattr(obj, 'label', globals())\nglobals()['ns'] = value",
        "value = getattr(obj, name, getattr)\nglobals()['read'] = value",
        "namespace = globals()\nvalue = namespace.get(name, globals())\nvalue.update(Refusal=str)",
        "namespace = globals()\nrecover = namespace.get('globals')\nrecover()['Refusal'] = str",
    ],
)
def test_explicit_namespace_capability_and_getter_defaults_retain_cross_input_vetoes(effect: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": effect}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("read", "use"),
    [
        ("getattr(obj, name, globals(), **{})", "value['Refusal'] = str"),
        ("getattr(obj, '__dict__', **{})", "value['Refusal'] = str"),
        ("obj.__getattribute__('__dict__', **{})", "value['Refusal'] = str"),
        ("object.__getattribute__(obj, '__dict__', **{})", "value['Refusal'] = str"),
        ("getattr(obj, '__set__', **{})", "return value"),
        ("getattr(obj, '__delete__', **{})", "return value"),
        ("obj.__getattribute__('__set__', **{})", "return value"),
        ("obj.__getattribute__('__delete__', **{})", "return value"),
    ],
)
def test_unsupported_reader_shapes_keep_literal_namespace_and_selected_effect_vetoes(read: str, use: str, reverse_inputs: bool) -> None:
    writer = f"def mutate(obj, name):\n    value = {read}\n    {use}"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("read", "use"),
    [
        ("getattr(obj, name, namespace, **{})", "value['Refusal'] = str"),
        ("read(obj, '__dict__', **{})", "value['Refusal'] = str"),
        ("slot(obj, '__dict__', **{})", "value['Refusal'] = str"),
        ("read(obj, '__set__', **{})", "return value"),
        ("slot(obj, '__delete__', **{})", "return value"),
        ("inspect.getattr_static(obj, name, default=namespace)", "value['Refusal'] = str"),
        ("getattr(obj, *parts, namespace, **{})", "value['Refusal'] = str"),
        ("obj.__getattribute__(*parts, namespace, **{})", "value['Refusal'] = str"),
        ("getattr(obj, name, replace, **{})", "value(obj, 'Refusal', str)"),
    ],
)
def test_unsupported_reader_shapes_keep_assigned_imported_default_and_argument_veto_origins(
    read: str, use: str, reverse_inputs: bool
) -> None:
    bridge = "from builtins import getattr as read, setattr as replace\nnamespace = globals()\nslot = object.__getattribute__"
    writer = (
        f"import inspect\nfrom bridge import namespace, read, replace, slot\ndef mutate(obj, name, parts):\n    value = {read}\n    {use}"
    )
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"bridge.py": bridge, "writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("read", "use"),
    [
        ("getattr(obj, name, sentinel, **{})", "value['status'] = 'ok'"),
        ("getattr(obj, 'label', **{})", "return value.strip()"),
        ("obj.__getattribute__('label', **{})", "value['status'] = 'ok'"),
        ("slot(obj, 'label', **{})", "value['status'] = 'ok'"),
        ("read(obj, name, sentinel, **{})", "value['status'] = 'ok'"),
        ("getattr(obj, *parts, sentinel, **{})", "value['status'] = 'ok'"),
        ("inspect.getattr_static(obj, name, default=sentinel)", "value['status'] = 'ok'"),
        ("getattr(obj, name, namespace, **{})", "return value"),
        ("getattr(obj, '__dict__', **{})", "return value"),
    ],
)
def test_unsupported_reader_shapes_leave_ordinary_results_and_passive_namespace_reads_local(
    read: str, use: str, reverse_inputs: bool
) -> None:
    bridge = "from builtins import getattr as read\nnamespace = globals()\nslot = object.__getattribute__"
    writer = (
        "import inspect\nfrom bridge import namespace, read, slot\n"
        f"def inspect_source(obj, name, sentinel, parts):\n    value = {read}\n    {use}"
    )
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, {"bridge.py": bridge, "writer.py": writer}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "recover = inspect.getattr_static(obj, attr='globals')\nnamespace = recover()\nnamespace['Refusal'] = str",
        "read = inspect.getattr_static(obj, attr='getattr')\nnamespace = read(other, '__dict__')\nnamespace['Refusal'] = str",
        "make = inspect.getattr_static(obj, attr='attrgetter')\nnamespace = make('__dict__')(other)\nnamespace['Refusal'] = str",
        "read = inspect.getattr_static(obj, attr='__getattribute__')\nnamespace = read(other, '__dict__')\nnamespace['Refusal'] = str",
        "namespace = globals()\nreplace = inspect.getattr_static(obj=namespace, attr='update')\nreturn replace",
    ],
)
def test_named_reader_capability_and_receiver_provenance_retain_composed_vetoes(body: str, reverse_inputs: bool) -> None:
    writer = "import inspect\ndef mutate(obj, other):\n" + "\n".join("    " + line for line in body.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": writer}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "body",
    [
        "value = inspect.getattr_static(obj, attr='label')\nvalue['status'] = 'ok'",
        "namespace = globals()\nvalue = inspect.getattr_static(obj=namespace, attr='label')\nvalue['status'] = 'ok'",
        "recover = inspect.getattr_static(obj, attr='globals')\nreturn recover",
    ],
)
def test_named_ordinary_reads_and_passive_reader_selection_do_not_propagate_receiver_namespace(body: str, reverse_inputs: bool) -> None:
    writer = "import inspect\ndef inspect_source(obj):\n" + "\n".join("    " + line for line in body.splitlines())
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, {"writer.py": writer}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    ("selector", "use", "namespace_effect"),
    [("__dict__", "value['Refusal'] = str", True), ("label", "value['status'] = 'ok'", False)],
)
def test_unsupported_factory_application_retains_selected_namespace_without_ordinary_result_poison(
    selector: str, use: str, namespace_effect: bool, reverse_inputs: bool
) -> None:
    writer = f"import operator\ndef inspect_source(obj):\n    value = operator.attrgetter({selector!r})(obj, **{{}})\n    {use}"
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert bool(_ordered_r5(subject, {"writer.py": writer}, reverse_inputs)) is namespace_effect


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "reader",
    [
        "slot = Owner.__dict__['payload']\nvalue = slot.__get__(obj, Owner)\nvalue.strip()",
        "slot = Owner.__dict__['payload']\nread = slot.__get__\nvalue = read(obj, Owner)\nvalue['status'] = 'ok'",
        "slot = type.__dict__['__name__']\nvalue = slot.__get__(type(obj), type)",
    ],
)
def test_unproved_descriptor_read_aliases_are_source_local_unknown(reader: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(subject, {"reader.py": reader}, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "effect",
    [
        "slot = Owner.__dict__['payload']\nslot.__set__(obj, value)",
        "slot = Owner.__dict__['payload']\nslot.__delete__(obj)",
        "replace = Owner.__dict__['payload'].__set__",
        "slot = Owner.__dict__['payload']\nreplace = getattr(slot, '__delete__')",
        "setattr(slot, '__set__', replacement)",
        "slot = Owner.__dict__['payload']\nnamespace = obj.__dict__\nnamespace.update(Refusal=str)",
    ],
)
def test_descriptor_mutators_and_explicit_namespace_effects_remain_cross_input_refusals(effect: str, reverse_inputs: bool) -> None:
    subject = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, {"writer.py": effect}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "read",
    ["getattr(obj, name)", "getattr(obj, 'Refusal', fallback)", "Owner.__dict__['payload'].__get__(obj, Owner)"],
)
def test_unknown_read_results_never_supply_a_favorable_checked_class(read: str, reverse_inputs: bool) -> None:
    subject = "from reader import Alias\ndef f(exc): return isinstance(exc, Alias)"
    assert len(_ordered_r5(subject, {"reader.py": f"Alias = {read}"}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_local_builtin_shadow_is_distinct_from_the_canonical_owner_in_other_inputs(reverse_inputs: bool) -> None:
    reader = "BaseException = unknown\ndef read(current): return BaseException.__dict__['__cause__'].__get__(current)"
    independent = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    shadowed = "from reader import BaseException\ndef f(exc): return isinstance(exc, BaseException)"
    assert _ordered_r5(independent, {"reader.py": reader}, reverse_inputs) == []
    assert len(_ordered_r5(shadowed, {"reader.py": reader}, reverse_inputs)) == 1
    supplied = {"builtins.py": "class BaseException: pass", "reader.py": reader}
    assert len(_ordered_r5(independent, supplied, reverse_inputs)) == 1


@pytest.mark.parametrize(
    "namespace",
    [
        "globals()",
        "locals()",
        "vars()",
        "errors.__dict__",
        "vars(errors)",
    ],
)
def test_namespace_receiver_does_not_gain_local_data_identity(namespace: str) -> None:
    source = f"import sqlalchemy.exc as errors\ndef update():\n    data = {namespace}\n    data['SQLAlchemyError'] = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


@pytest.mark.parametrize("namespace", ["globals", "locals", "vars"])
def test_supplied_namespace_callable_rebinding_withdraws_local_data_identity(namespace: str) -> None:
    modules = {"bridge.py": f"from builtins import {namespace} as namespace"}
    source = "from bridge import namespace\nclass Refusal(Exception): pass\ndef update():\n    data = {}\n    namespace()['data'] = replacement\n    data['Refusal'] = str\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source, modules)) == 1


@pytest.mark.parametrize(
    "body",
    [
        "def update(context):\n    data = [Refusal, None]\n    data[1] = context\n    data[0].__bases__ = (str,)",
        "def update(context):\n    data = {'error': Refusal}\n    data['note'] = context\n    data['error'].__bases__ = (str,)",
        "class Holder: data = [Refusal]\ndef update(): Holder.data[0].__bases__ = (str,)",
    ],
)
def test_contained_class_member_mutation_still_withdraws_ancestry(body: str) -> None:
    source = f"class Refusal(Exception): pass\n{body}\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_r5(source)) == 1


def test_local_data_identity_keeps_contained_namespace_mutation_origins() -> None:
    source = "import sqlalchemy.exc as errors\ndef update(context):\n    data = {'errors': errors}\n    data['note'] = context\n    data['errors'].SQLAlchemyError = str\ndef f(exc): return isinstance(exc, errors.SQLAlchemyError)"
    assert len(_r5(source)) == 1


def test_local_data_identity_does_not_prove_a_contained_checked_type() -> None:
    source = (
        "class Refusal(Exception): pass\ndef f(exc):\n    data = [Refusal, None]\n    data[1] = None\n    return isinstance(exc, data[0])"
    )
    assert len(_r5(source)) == 1


def test_generic_class_binder_does_not_hide_an_unrelated_builtin_kind() -> None:
    assert _r5("class Holder[T]:\n    def f(self, exc): return isinstance(exc, Exception)") == []


def test_explicit_selected_files_share_only_supplied_ancestry(tmp_path: Path) -> None:
    errors = tmp_path / "errors.py"
    use = tmp_path / "use.py"
    allowlist = tmp_path / "allowlist.yaml"
    errors.write_text("class Refusal(RuntimeError): pass")
    use.write_text("from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)")
    allowlist.write_text("")
    included = rule_module.collect_check_result(tmp_path, allowlist_path=allowlist, files=[errors, use])
    omitted = rule_module.collect_check_result(tmp_path, allowlist_path=allowlist, files=[use])
    assert not [finding for finding in included.violations if finding.rule_id == "R5"]
    assert len([finding for finding in omitted.violations if finding.rule_id == "R5"]) == 1


def test_excluded_sources_supply_no_class_facts(tmp_path: Path) -> None:
    (tmp_path / "errors.py").write_text("class Refusal(Exception): pass")
    (tmp_path / "use.py").write_text("from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)")
    assert not [finding for finding in scan_directory(tmp_path) if finding.rule_id == "R5"]
    findings = scan_directory(tmp_path, ["errors.py"])
    assert len([finding for finding in findings if finding.rule_id == "R5"]) == 1


def test_directory_reads_selected_sources_once(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    bodies = {
        "base.py": "class Base(RuntimeError): pass",
        "errors.py": "from base import Base\nclass Refusal(Base): pass",
        "use.py": "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)",
    }
    for name, body in bodies.items():
        (tmp_path / name).write_text(body)
    original = rule_module._read_tier_model_source
    reads: list[Path] = []

    def read(path: Path, root: Path) -> rule_module._TierModelSource | None:
        reads.append(path)
        return original(path, root)

    monkeypatch.setattr(rule_module, "_read_tier_model_source", read)
    assert not [finding for finding in scan_directory(tmp_path) if finding.rule_id == "R5"]
    assert sorted(reads) == sorted(tmp_path / name for name in bodies)


@pytest.mark.parametrize("base", ["Exception", "str"])
def test_single_file_directory_and_direct_ast_entrypoints_agree(tmp_path: Path, base: str) -> None:
    source = f"class Refusal({base}): pass\ndef f(exc): return isinstance(exc, Refusal)"
    path = tmp_path / "subject.py"
    path.write_text(source)
    file_findings = [finding for finding in scan_file(path, tmp_path) if finding.rule_id == "R5"]
    directory_findings = [finding for finding in scan_directory(tmp_path) if finding.rule_id == "R5"]
    direct = TierModelRule().analyze(ast.parse(source), path, RuleContext(root=tmp_path))
    direct_r5 = [finding for finding in direct if finding.message.startswith("isinstance() used:")]
    assert len(file_findings) == len(directory_findings) == len(direct_r5) == (0 if base == "Exception" else 1)


def test_direct_ast_uses_supplied_class_body_instead_of_disk(tmp_path: Path) -> None:
    path = tmp_path / "subject.py"
    path.write_text("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)")
    tree = ast.parse("class Refusal(str): pass\ndef f(exc): return isinstance(exc, Refusal)")
    findings = TierModelRule().analyze(tree, path, RuleContext(root=tmp_path))
    assert len([finding for finding in findings if finding.message.startswith("isinstance() used:")]) == 1


def test_direct_visitor_default_retains_local_exception_proof() -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    visitor = TierModelVisitor("subject.py", source.splitlines(), hashlib.sha256(source.encode()).hexdigest())
    visitor.visit(ast.parse(source))
    assert not [finding for finding in visitor.findings if finding.rule_id == "R5"]


def test_ancestry_memo_is_not_shared_between_invocations() -> None:
    assert _r5("class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)") == []
    assert len(_r5("class Refusal(str): pass\ndef f(exc): return isinstance(exc, Refusal)")) == 1


def test_exception_discrimination_preserves_subclass_semantics() -> None:
    class Refusal(Exception):
        pass

    class SpecificRefusal(Refusal):
        pass

    error = SpecificRefusal()
    assert isinstance(error, Refusal)
    assert type(error) is not Refusal
    assert _r5("class Refusal(Exception): pass\nclass Specific(Refusal): pass\ndef f(exc): return isinstance(exc, Refusal)") == []


def test_r4_and_r6_are_not_exception_kind_exemptions() -> None:
    broad = _findings("try: work()\nexcept Exception as exc:\n    if isinstance(exc, RuntimeError): return_value = None")
    specific = _findings("try: work()\nexcept ValueError as exc:\n    if isinstance(exc, RuntimeError): return_value = None")
    assert {finding.rule_id for finding in broad} == {"R4"}
    assert {finding.rule_id for finding in specific} == {"R6"}


def test_disabling_new_proof_restores_positive_finding(monkeypatch: pytest.MonkeyPatch) -> None:
    source = "class Refusal(Exception): pass\ndef f(exc): return isinstance(exc, Refusal)"
    assert _r5(source) == []
    monkeypatch.setattr(ExceptionTaxonomy, "is_exception_kind", lambda *_args: False)
    assert len(_r5(source)) == 1


@pytest.mark.parametrize(
    "source",
    [
        "class OrdinaryError: pass\ndef f(exc): return isinstance(exc, OrdinaryError)",
        "def f(exc): return isinstance(exc, (Exception, str))",
    ],
)
def test_negative_oracle_rejects_overbroad_proof(source: str, monkeypatch: pytest.MonkeyPatch) -> None:
    assert len(_r5(source)) == 1
    monkeypatch.setattr(ExceptionTaxonomy, "is_exception_kind", lambda *_args: True)
    with pytest.raises(AssertionError):
        assert len(_r5(source)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "capture",
    [
        "saved = receiver.__dict__['label']",
        "first = receiver.__dict__['label']\nsaved = first",
        "saved = receiver.label",
    ],
)
def test_declared_dictionary_restore_consumes_the_unknown_saved_value(capture: str, reverse_inputs: bool) -> None:
    body = f"{capture}\nreceiver.__dict__['label'] = context\nreceiver.__dict__['label'] = saved"
    writer = "from foreign import Base\nclass Holder(Base):\n    def patch(receiver, context):\n" + "\n".join(
        "        " + line for line in body.splitlines()
    )
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    companions = {"errors.py": "class Refusal(Exception): pass", "writer.py": writer}
    assert _ordered_r5(subject, companions, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "capture",
    [
        "saved = receiver.__dict__",
        "saved = globals()",
        "saved = foreign.__dict__.update",
        "saved = receiver.__dict__['label']\nsaved = globals()",
        "saved = getattr(obj, 'label', globals())",
        "saved = receiver.__dict__['__globals__']",
    ],
)
def test_dictionary_restore_keeps_explicit_namespace_and_capability_sources(capture: str, reverse_inputs: bool) -> None:
    body = f"{capture}\nreceiver.__dict__['label'] = saved"
    writer = "class Holder:\n    def patch(receiver, obj):\n" + "\n".join("        " + line for line in body.splitlines())
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    companions = {"errors.py": "class Refusal(Exception): pass", "writer.py": writer}
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_restored_unknown_dictionary_value_never_proves_a_checked_class(reverse_inputs: bool) -> None:
    subject = (
        "class Holder:\n"
        "    def check(receiver, exc):\n"
        "        saved = receiver.__dict__['kind']\n"
        "        receiver.__dict__['label'] = saved\n"
        "        return isinstance(exc, saved)"
    )
    assert len(_ordered_r5(subject, {"errors.py": "class Refusal(Exception): pass"}, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize("capture", ["saved = receiver.__dict__['label']", "saved = receiver.label"])
def test_unrelated_init_write_does_not_change_saved_value_provenance(capture: str, reverse_inputs: bool) -> None:
    writer = f"class Holder:\n    def patch(receiver):\n        {capture}\n        receiver.__dict__['label'] = saved"
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    companions = {
        "errors.py": "class Refusal(Exception): pass",
        "writer.py": writer,
        "mutation.py": "def mutate(cls, replacement): cls.__init__ = replacement",
    }
    assert _ordered_r5(subject, companions, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_unrelated_lazy_package_preserves_direct_nominal_import_but_not_opaque_exports(reverse_inputs: bool) -> None:
    facade = (
        "from importlib import import_module\n"
        "from errors import Refusal\n"
        "def __getattr__(name):\n"
        "    value = getattr(import_module('foreign'), name)\n"
        "    globals()[name] = value\n"
        "    return value"
    )
    companions = {"errors.py": "class Refusal(Exception): pass", "facade/__init__.py": facade}
    independent = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert _ordered_r5(independent, companions, reverse_inputs) == []
    dependent = "from facade import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(dependent, companions, reverse_inputs)) == 1
    lazy = "import facade\ndef f(exc): return isinstance(exc, facade.LazyError)"
    assert len(_ordered_r5(lazy, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "value",
    [
        "globals()",
        "getattr(import_module('foreign'), name, globals())",
        "getattr(obj, '__dict__')",
        "obj.__dict__.update",
    ],
)
def test_lazy_package_explicit_namespace_and_capability_installs_keep_global_refusal(value: str, reverse_inputs: bool) -> None:
    facade = (
        f"from importlib import import_module\ndef __getattr__(name):\n    value = {value}\n    globals()[name] = value\n    return value"
    )
    companions = {"errors.py": "class Refusal(Exception): pass", "facade/__init__.py": facade}
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


@pytest.mark.parametrize("reverse_inputs", [False, True])
def test_target_own_dynamic_binding_write_with_lazy_package_keeps_dependent_import_refusal(reverse_inputs: bool) -> None:
    companions = {
        "errors.py": "class Refusal(Exception): pass\ndef replace(name, value): globals()[name] = value",
        "facade/__init__.py": (
            "from importlib import import_module\n"
            "def __getattr__(name):\n"
            "    value = getattr(import_module('foreign'), name)\n"
            "    globals()[name] = value\n"
            "    return value"
        ),
    }
    dependent = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(dependent, companions, reverse_inputs)) == 1
    independent = "class Other(Exception): pass\ndef f(exc): return isinstance(exc, Other)"
    assert _ordered_r5(independent, companions, reverse_inputs) == []


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize("with_facade", [False, True])
def test_free_helper_dynamic_slot_write_refuses_independently_of_lazy_package(with_facade: bool, reverse_inputs: bool) -> None:
    companions = {
        "errors.py": "class Refusal(Exception): pass",
        "writer.py": "def write(instance, name, value): object.__setattr__(instance, name, value)",
    }
    if with_facade:
        companions["facade/__init__.py"] = (
            "from importlib import import_module\n"
            "def __getattr__(name):\n"
            "    value = getattr(import_module('foreign'), name)\n"
            "    globals()[name] = value\n"
            "    return value"
        )
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1


def _complete_source_report(root: Path, output: Path, monkeypatch: pytest.MonkeyPatch, source_order: str) -> list[Finding]:
    # The shared discovery authority defines policy, independently of the rule's
    # selected iterator. No R5-specific source exclusions are permitted here.
    inventory = tuple(iter_python_files(root))
    assert inventory
    expected: dict[str, tuple[str, list[str], str]] = {}
    for path in inventory:
        raw = path.read_bytes()
        source = raw.decode("utf-8")
        original = ast.parse(source, filename=str(path))
        expected[path.relative_to(root).as_posix()] = (
            hashlib.sha256(raw).hexdigest(),
            source.splitlines(),
            hashlib.sha256(ast.dump(original, include_attributes=True).encode()).hexdigest(),
        )
    assert len(expected) == len(inventory)
    reader = rule_module._read_tier_model_source
    factory = rule_module._taxonomy_for_sources
    scan = rule_module._scan_tier_model_source
    reads: dict[str, rule_module._TierModelSource] = {}
    visits: list[str] = []
    constructions: list[ExceptionTaxonomy] = []
    construction_order: list[str] = []
    observations: list[Finding] = []

    def read(path: Path, scan_root: Path) -> rule_module._TierModelSource:
        assert scan_root == root
        relative = path.relative_to(root).as_posix()
        assert relative in expected and relative not in reads, relative
        result = reader(path, scan_root)
        assert result is not None, relative
        fingerprint, lines, ast_fingerprint = expected[relative]
        assert result.relative_path == relative
        assert result.file_fingerprint == fingerprint
        assert result.source_lines == lines
        assert hashlib.sha256(ast.dump(result.tree, include_attributes=True).encode()).hexdigest() == ast_fingerprint
        reads[relative] = result
        return result

    def construct(sources: Sequence[rule_module._TierModelSource], scan_root: Path) -> ExceptionTaxonomy:
        assert scan_root == root and not constructions
        assert [source.relative_path for source in sources] == list(expected)
        assert all(source is reads[source.relative_path] for source in sources)
        assert source_order in {"normal", "reversed"}
        forwarded = sources if source_order == "normal" else tuple(reversed(sources))
        construction_order.extend(source.relative_path for source in forwarded)
        result = factory(forwarded, scan_root)
        assert set(result._files) == set(expected)
        assert all(result._files[source.relative_path].tree is source.tree for source in sources)
        for source in sources:
            parts = list(Path(source.relative_path).with_suffix("").parts)
            if parts[-1] == "__init__":
                parts.pop()
            prefix = ["elspeth"] if root.name == "elspeth" else []
            assert result._files[source.relative_path].name == ".".join([*prefix, *parts])
        constructions.append(result)
        return result

    def visit(source: rule_module._TierModelSource, taxonomy: ExceptionTaxonomy) -> tuple[list[Finding], list[Finding]]:
        assert source is reads[source.relative_path]
        assert len(constructions) == 1 and taxonomy is constructions[0]
        visits.append(source.relative_path)
        result = scan(source, taxonomy)
        observations.extend(result[1])
        return result

    with monkeypatch.context() as recording:
        recording.setattr(rule_module, "_read_tier_model_source", read)
        recording.setattr(rule_module, "_taxonomy_for_sources", construct)
        recording.setattr(rule_module, "_scan_tier_model_source", visit)
        findings = scan_directory(root)
    assert list(reads) == list(expected)
    assert visits == list(expected)
    assert len(constructions) == 1
    for relative, source in reads.items():
        assert hashlib.sha256(ast.dump(source.tree, include_attributes=True).encode()).hexdigest() == expected[relative][2]
    graph = [(relative, expected[relative][0], expected[relative][2]) for relative in expected]

    def rows(records: list[Finding]) -> list[dict[str, object]]:
        return [
            {**asdict(finding), "severity": "note" if finding.rule_id == "R_TB_SUPPRESSED" else rule_module.RULE_METADATA.severity.value}
            for finding in records
        ]

    report = {
        "source_order": source_order,
        "package_prefix": "elspeth" if root.name == "elspeth" else "",
        "input_graph": graph,
        "input_graph_sha256": hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(),
        "read_input_order": list(reads),
        "constructor_input_order": construction_order,
        "original_source_and_ast_identity_proved": True,
        "findings": rows(findings),
        "suppression_observations": rows(observations),
    }
    (output / "complete-source-report.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return findings


def _assert_complete_input_order_parity(reports: list[dict[str, object]]) -> None:
    """Specific final-qualification join; two isolated case passes are insufficient."""
    assert len(reports) == 2
    by_order: dict[str, dict[str, object]] = {}
    for report in reports:
        order = report["source_order"]
        assert isinstance(order, str) and order in {"normal", "reversed"} and order not in by_order
        by_order[order] = report
        graph = report["input_graph"]
        assert isinstance(graph, list) and graph
        assert report["input_graph_sha256"] == hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest()
        paths = [entry[0] for entry in graph]
        assert paths == sorted(set(paths), key=Path)
        assert report["read_input_order"] == paths
        assert report["constructor_input_order"] == (paths if order == "normal" else list(reversed(paths)))
        assert report["original_source_and_ast_identity_proved"] is True
    normal, reversed_report = by_order["normal"], by_order["reversed"]
    assert normal["input_graph"] == reversed_report["input_graph"]
    assert normal["package_prefix"] == reversed_report["package_prefix"]
    for kind in ("findings", "suppression_observations"):
        left, right = normal[kind], reversed_report[kind]
        assert isinstance(left, list) and isinstance(right, list)
        assert Counter(json.dumps(row, sort_keys=True) for row in left) == Counter(json.dumps(row, sort_keys=True) for row in right)


@pytest.mark.parametrize("damage", ["none", "missing", "duplicate", "graph", "finding", "observation", "order"])
def test_complete_input_order_join_refuses_missing_duplicate_and_corrupted_reports(damage: str) -> None:
    graph = [["base.py", "bytes-base", "ast-base"], ["use.py", "bytes-use", "ast-use"]]
    report: dict[str, object] = {
        "source_order": "normal",
        "package_prefix": "",
        "input_graph": graph,
        "input_graph_sha256": hashlib.sha256(json.dumps(graph, sort_keys=True).encode()).hexdigest(),
        "read_input_order": ["base.py", "use.py"],
        "constructor_input_order": ["base.py", "use.py"],
        "original_source_and_ast_identity_proved": True,
        "findings": [{"rule_id": "R5", "file_path": "use.py", "line": 2, "message": "nonexception"}],
        "suppression_observations": [],
    }
    reverse = {**report, "source_order": "reversed", "constructor_input_order": ["use.py", "base.py"]}
    reports = [report, reverse]
    if damage == "missing":
        reports.pop()
    elif damage == "duplicate":
        reports[1] = report
    elif damage == "graph":
        reverse["input_graph"] = graph[:-1]
    elif damage == "finding":
        reverse["findings"] = []
    elif damage == "observation":
        reverse["suppression_observations"] = [{"rule_id": "R_TB_SUPPRESSED"}]
    elif damage == "order":
        reverse["constructor_input_order"] = ["base.py", "use.py"]
    if damage == "none":
        _assert_complete_input_order_parity(reports)
    else:
        with pytest.raises(AssertionError):
            _assert_complete_input_order_parity(reports)


@pytest.mark.parametrize("damage", ["none", "duplicate", "wrong-reverse"])
def test_complete_input_order_join_uses_canonical_component_order_for_actual_source_pair(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    root = tmp_path / "source"
    (root / "a").mkdir(parents=True)
    (root / "a/b.py").write_text("class Refusal(Exception): pass\nresult = isinstance(exc, Refusal)", encoding="utf-8")
    (root / "a.py").write_text("result = isinstance(exc, str)", encoding="utf-8")
    reports = []
    for order in ("normal", "reversed"):
        output = tmp_path / order
        output.mkdir()
        findings = _complete_source_report(root, output, monkeypatch, order)
        assert any(finding.rule_id == "R5" and finding.file_path == "a.py" for finding in findings)
        report = json.loads((output / "complete-source-report.json").read_text(encoding="utf-8"))
        assert report["read_input_order"] == ["a/b.py", "a.py"]
        reports.append(report)
    if damage == "duplicate":
        normal = reports[0]
        normal["input_graph"].insert(1, normal["input_graph"][0])
        normal["input_graph_sha256"] = hashlib.sha256(json.dumps(normal["input_graph"], sort_keys=True).encode()).hexdigest()
        normal["read_input_order"].insert(1, "a/b.py")
        normal["constructor_input_order"].insert(1, "a/b.py")
    elif damage == "wrong-reverse":
        reports[1]["constructor_input_order"] = ["a/b.py", "a.py"]
    if damage == "none":
        _assert_complete_input_order_parity(reports)
    else:
        with pytest.raises(AssertionError):
            _assert_complete_input_order_parity(reports)


@pytest.mark.parametrize("damage", ["none", "omitted", "duplicate", "unreadable", "tree", "constructor", "prefix"])
def test_complete_scan_fidelity_refuses_omitted_duplicate_unreadable_or_replaced_inputs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, damage: str
) -> None:
    (tmp_path / "base.py").write_text("class Refusal(Exception): pass", encoding="utf-8")
    (tmp_path / "use.py").write_text("from base import Refusal\nresult = isinstance(exc, Refusal)", encoding="utf-8")
    paths = tuple(iter_python_files(tmp_path))
    if damage == "none":
        findings = _complete_source_report(tmp_path, tmp_path, monkeypatch, "normal")
        assert not [finding for finding in findings if finding.rule_id == "R5"]
        return
    if damage in {"omitted", "duplicate"}:
        selected = paths[:-1] if damage == "omitted" else (*paths, paths[0])
        monkeypatch.setattr(rule_module, "iter_scannable_python_files", lambda *_args: iter(selected))
    elif damage in {"unreadable", "tree"}:
        original = rule_module._read_tier_model_source

        def read(path: Path, root: Path) -> rule_module._TierModelSource | None:
            source = original(path, root)
            if path == paths[0]:
                if damage == "unreadable":
                    return None
                assert source is not None
                return rule_module._TierModelSource(source.relative_path, source.source_lines, source.file_fingerprint, ast.parse("pass"))
            return source

        monkeypatch.setattr(rule_module, "_read_tier_model_source", read)
    elif damage == "constructor":
        factory = rule_module._taxonomy_for_sources
        monkeypatch.setattr(rule_module, "_taxonomy_for_sources", lambda sources, root: factory(sources[:-1], root))
    else:
        monkeypatch.setattr(
            rule_module,
            "_taxonomy_for_sources",
            lambda sources, _root: ExceptionTaxonomy({source.relative_path: source.tree for source in sources}, package_prefix="wrong"),
        )
    with pytest.raises(AssertionError):
        _complete_source_report(tmp_path, tmp_path, monkeypatch, "normal")


def test_complete_scan_independent_inventory_refuses_unparseable_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "broken.py").write_text("def broken(:", encoding="utf-8")
    with pytest.raises(SyntaxError):
        _complete_source_report(tmp_path, tmp_path, monkeypatch, "normal")


@pytest.mark.parametrize("reverse_inputs", [False, True])
@pytest.mark.parametrize(
    "writer",
    [
        "import errors\ndef write(target, name, value): object.__setattr__(target, name, value)\nwrite(errors, 'Refusal', str)",
        "import errors\nclass Box: pass\nbox = Box()\ndef write(target, name, value): object.__setattr__(target, name, value)\nwrite(box, '__dict__', errors.__dict__)\nwrite(box, 'Refusal', str)",
        "import errors\ndef write(target, name, value): setattr(target, name, value)\nwrite(errors.Refusal, '__bases__', (str,))",
    ],
)
def test_unconfined_helper_module_targets_dictionary_installation_and_ancestry_keep_refusal(writer: str, reverse_inputs: bool) -> None:
    companions = {"errors.py": "class Refusal(Exception): pass", "writer.py": writer}
    subject = "from errors import Refusal\ndef f(exc): return isinstance(exc, Refusal)"
    assert len(_ordered_r5(subject, companions, reverse_inputs)) == 1
