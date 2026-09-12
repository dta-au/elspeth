"""Positive and adversarial controls for diagnostic contract discovery."""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from types import ModuleType

import pytest
from tests.helpers.exception_diagnostics import (
    DiagnosticCase,
    ExceptionShape,
    check_case_partition,
    discover_exception_shapes,
)

from elspeth.contracts.audit_evidence import AuditEvidenceBase


def _exercise() -> None:
    assert str(ValueError("retained")) == "retained"


def _case(exception: type[BaseException], *fields: str) -> DiagnosticCase:
    return DiagnosticCase(exception, frozenset(fields), "message", "Assert actual message retention.", _exercise)


@pytest.mark.parametrize(
    ("shapes", "cases", "message"),
    [
        ((ExceptionShape(ValueError, frozenset({"reason"})),), (_case(ValueError),), "missing fields.*reason"),
        ((ExceptionShape(ValueError, frozenset()),), (_case(ValueError, "removed"),), "stale fields.*removed"),
        (
            (ExceptionShape(ValueError, frozenset()), ExceptionShape(TypeError, frozenset())),
            (_case(ValueError),),
            "missing class.*TypeError",
        ),
        ((ExceptionShape(ValueError, frozenset()),), (_case(ValueError), _case(TypeError)), "stale class.*TypeError"),
        (
            (ExceptionShape(ValueError, frozenset({"reason"})),),
            (_case(ValueError, "reason"), _case(ValueError, "reason")),
            "duplicate fields.*reason",
        ),
        ((ExceptionShape(ValueError, frozenset()),), (DiagnosticCase(ValueError, frozenset(), "message", " ", _exercise),), "explanation"),
        (
            (ExceptionShape(ValueError, frozenset({"reason"})),),
            (DiagnosticCase(ValueError, frozenset({"reason"}), "inherited", "Base renderer.", _exercise),),
            "inherited",
        ),
        (
            (ExceptionShape(ValueError, frozenset({"reason"})),),
            (DiagnosticCase(ValueError, frozenset(), "inherited", "Base renderer.", _exercise),),
            "inherited",
        ),
        ((), (), "empty"),
    ],
)
def test_partition_rejects_incomplete_contracts(
    shapes: tuple[ExceptionShape, ...], cases: tuple[DiagnosticCase, ...], message: str
) -> None:
    with pytest.raises(AssertionError, match=message):
        check_case_partition(shapes, cases)


def test_partition_accepts_disjoint_fields_and_zero_owned_class() -> None:
    check_case_partition(
        (ExceptionShape(ValueError, frozenset({"reason", "code"})), ExceptionShape(TypeError, frozenset())),
        (
            _case(ValueError, "reason"),
            _case(ValueError, "code"),
            DiagnosticCase(TypeError, frozenset(), "inherited", "Base message.", _exercise),
        ),
    )


@pytest.fixture
def module_factory(tmp_path: Path) -> Iterator[Callable[[str], ModuleType]]:
    names: list[str] = []

    def build(source: str) -> ModuleType:
        name = f"diagnostic_discovery_control_{len(names)}"
        path = tmp_path / f"{name}.py"
        path.write_text(source)
        spec = importlib.util.spec_from_file_location(name, path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        sys.modules[name] = module
        names.append(name)
        spec.loader.exec_module(module)
        return module

    yield build
    for name in names:
        del sys.modules[name]


def test_discovery_collects_stores_not_reads_or_nested_bodies(module_factory: Callable[[str], ModuleType]) -> None:
    module = module_factory("""
class Ordinary:
    def __init__(self):
        self.ignored = 1
class Trouble(Exception):
    __slots__ = ('reason', 'code', 'left', 'right')
    def __init__(this, other):
        this.reason = 'reason'
        this.code: str = 'code'
        this.left = this.right = 1
        this.first, this.second = (1, 2)
        this.only_annotation: str
        other.ignored = this.reason
        def unused():
            this.nested = 1
        super().__init__(this.reason)
Alias = Trouble
Imported = ValueError
class Empty(Trouble):
    pass
""")
    shapes = discover_exception_shapes((module,))
    assert [(shape.exception.__name__, shape.fields) for shape in shapes] == [
        ("Empty", frozenset()),
        ("Trouble", frozenset({"reason", "code", "left", "right", "first", "second"})),
    ]


@pytest.mark.parametrize(
    "body",
    [
        "alias = self; alias.hidden = 1",
        "setattr(self, 'hidden', 1)",
        "object.__setattr__(self, 'hidden', 1)",
        "self.__dict__['hidden'] = 1",
        "vars(self)['hidden'] = 1",
        "self.__dict__.update(hidden=1)",
        "helper(self)",
        "self.populate()",
        "self.reason += 1",
        "del self.reason",
        "(lambda: self.reason)()",
        "def helper():\n            self.hidden = 1\n        helper()",
    ],
)
def test_discovery_rejects_unsupported_ownership(module_factory: Callable[[str], ModuleType], body: str) -> None:
    module = module_factory(f"class Trouble(Exception):\n    def __init__(self):\n        {body}\n")
    with pytest.raises(AssertionError, match=r"unsupported.*Trouble"):
        discover_exception_shapes((module,))


@pytest.mark.parametrize(
    "source",
    [
        "from dataclasses import dataclass\n@dataclass\nclass Trouble(Exception):\n    reason: str\n",
        "def initializer(self):\n    self.hidden = 1\nclass Trouble(Exception):\n    __init__ = initializer\n",
        "Trouble = type('Trouble', (Exception,), {})\n",
        "def make():\n    class Trouble(Exception): pass\n    return Trouble\nTrouble = make()\n",
        "class Trouble(Exception):\n    def __new__(cls):\n        instance = super().__new__(cls)\n        instance.hidden = 1\n        return instance\n",
        "from dataclasses import dataclass\nfrom elspeth.contracts.audit_evidence import AuditEvidenceBase\n@dataclass\nclass Trouble(AuditEvidenceBase, Exception):\n    reason: str\n    def to_audit_dict(self):\n        return {'reason': self.reason}\n",
    ],
)
def test_discovery_rejects_generated_shapes(module_factory: Callable[[str], ModuleType], source: str) -> None:
    with pytest.raises(AssertionError, match=r"unsupported.*Trouble"):
        discover_exception_shapes((module_factory(source),))


def test_nominal_wrapper_preserves_source_ownership(module_factory: Callable[[str], ModuleType]) -> None:
    module = module_factory("""
from elspeth.contracts.audit_evidence import AuditEvidenceBase
class Trouble(AuditEvidenceBase, Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(self._format_message())
    def _format_message(self):
        return self.reason
    def to_audit_dict(self):
        return {'reason': self.reason}
class Child(Trouble):
    pass
""")
    shapes = discover_exception_shapes((module,))
    assert [(shape.exception.__name__, shape.fields) for shape in shapes] == [("Child", frozenset()), ("Trouble", frozenset({"reason"}))]
    inherited = shapes[0].exception("retained")
    assert isinstance(inherited, AuditEvidenceBase)
    assert str(inherited) == "retained"
    assert inherited.to_audit_dict() == {"reason": "retained"}


def test_empty_discovery_fails(module_factory: Callable[[str], ModuleType]) -> None:
    with pytest.raises(AssertionError, match="empty"):
        discover_exception_shapes((module_factory("class Ordinary: pass\n"),))


def test_discovery_mutations_require_new_field_and_new_class_cases(module_factory: Callable[[str], ModuleType]) -> None:
    module = module_factory("""
class Trouble(Exception):
    def __init__(self):
        self.reason = 'reason'
        self.added = 'new obligation'
class NewWithoutSuffix(Exception):
    pass
""")
    shapes = discover_exception_shapes((module,))
    trouble = next(shape.exception for shape in shapes if shape.exception.__name__ == "Trouble")
    with pytest.raises(AssertionError) as failure:
        check_case_partition(shapes, (_case(trouble, "reason"),))
    assert "missing fields" in str(failure.value) and "added" in str(failure.value)
    assert "missing class" in str(failure.value) and "NewWithoutSuffix" in str(failure.value)


def test_parent_constructor_delegation_does_not_reown_fields(module_factory: Callable[[str], ModuleType]) -> None:
    module = module_factory("""
class Parent(Exception):
    def __init__(self, reason):
        self.reason = reason
class Child(Parent):
    def __init__(self, reason):
        Parent.__init__(self, reason)
""")
    assert [(shape.exception.__name__, shape.fields) for shape in discover_exception_shapes((module,))] == [
        ("Child", frozenset()),
        ("Parent", frozenset({"reason"})),
    ]
