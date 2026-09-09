"""Tests for composer elspeth-lints rules."""

from __future__ import annotations

import ast
from importlib import import_module
from pathlib import Path

from elspeth_lints.core.protocols import Finding, RuleContext
from elspeth_lints.rules.composer.catch_order import RULE as CATCH_ORDER_RULE
from elspeth_lints.rules.composer.catch_order.rule import _BROAD_SUPERTYPES, _SUBCLASS_TO_SUPERCLASSES
from elspeth_lints.rules.composer.exception_channel import RULE as EXCEPTION_CHANNEL_RULE


def test_exception_channel_accepts_tool_argument_error(tmp_path: Path) -> None:
    source = (
        "from elspeth.web.composer.protocol import ToolArgumentError\n"
        "def f(x):\n"
        "    if not isinstance(x, str):\n"
        "        raise ToolArgumentError(argument='x', expected='a string', actual_type=type(x).__name__)\n"
    )
    findings = _exception_channel_findings(tmp_path, source)

    assert findings == []


def test_exception_channel_reports_bare_type_error(tmp_path: Path) -> None:
    source = "def f(x):\n    if not isinstance(x, str):\n        raise TypeError('bad')\n"

    findings = _exception_channel_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CEC1"]
    assert "TypeError" in findings[0].message


def test_exception_channel_reports_bare_value_error(tmp_path: Path) -> None:
    source = "def f():\n    raise ValueError('bad')\n"

    findings = _exception_channel_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CEC1"]
    assert findings[0].file_path == "web/composer/tools/blobs.py"
    assert findings[0].line == 2
    assert "ValueError" in findings[0].message


def test_exception_channel_reports_qualified_builtins_value_error(tmp_path: Path) -> None:
    source = "import builtins\n\ndef f():\n    raise builtins.ValueError('bad')\n"

    findings = _exception_channel_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CEC1"]
    assert "ValueError" in findings[0].message


def test_exception_channel_reports_assigned_value_error_alias(tmp_path: Path) -> None:
    source = "BadValue = ValueError\n\ndef f():\n    raise BadValue('bad')\n"

    findings = _exception_channel_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CEC1"]
    assert "ValueError" in findings[0].message


def test_exception_channel_reports_imported_value_error_alias(tmp_path: Path) -> None:
    source = "from builtins import ValueError as BadValue\n\ndef f():\n    raise BadValue('bad')\n"

    findings = _exception_channel_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CEC1"]
    assert "ValueError" in findings[0].message


def test_exception_channel_raise_caught_locally_and_returned_as_failure_result_is_contained(tmp_path: Path) -> None:
    """The rule's own SUGGESTION sanctions 'catch locally and return _failure_result'; the channel is intact."""
    source = (
        "def _failure_result(state, msg): return msg\n"
        "def f(x, state):\n"
        "    try:\n"
        "        raise ValueError('bad')\n"
        "    except ValueError as exc:\n"
        "        return _failure_result(state, str(exc))\n"
    )

    assert _exception_channel_findings(tmp_path, source) == []


def test_exception_channel_ignores_implicit_raise_from_coercion(tmp_path: Path) -> None:
    source = (
        "def _failure_result(state, msg): return msg\n"
        "def f(x, state):\n"
        "    try:\n"
        "        int(x)\n"
        "    except ValueError as exc:\n"
        "        return _failure_result(state, str(exc))\n"
    )

    findings = _exception_channel_findings(tmp_path, source)

    assert findings == []


def test_catch_order_accepts_narrow_before_broad(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
        "    except ComposerServiceError as exc:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert findings == []


def test_catch_order_reports_broad_before_narrow(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ComposerServiceError as exc:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]
    assert findings[0].file_path == "web/sessions/routes.py"
    assert findings[0].line == 6
    assert "ComposerPluginCrashError" in findings[0].message


def test_catch_order_reports_tuple_handler_shadowing_subclass(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except (RuntimeError, ComposerServiceError) as exc:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]


def test_catch_order_reports_attribute_handler_shadowing_subclass(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except protocol.ComposerServiceError as exc:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]


def test_catch_order_ignores_single_handler_and_unrelated_pairs(tmp_path: Path) -> None:
    assert _catch_order_findings(tmp_path, "def f():\n    try:\n        pass\n    except ComposerPluginCrashError:\n        pass\n") == []
    assert _catch_order_findings(tmp_path, "def f():\n    try:\n        pass\n    except ComposerServiceError:\n        pass\n") == []
    assert (
        _catch_order_findings(
            tmp_path, "def f():\n    try:\n        pass\n    except OSError:\n        pass\n    except ValueError:\n        pass\n"
        )
        == []
    )


def test_catch_order_reports_runtime_preflight_shadowing(tmp_path: Path) -> None:
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ComposerServiceError as exc:\n"
        "        pass\n"
        "    except ComposerRuntimePreflightError as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]
    assert "ComposerRuntimePreflightError" in findings[0].message


def test_catch_order_reports_aliased_handler_shadowing(tmp_path: Path) -> None:
    # elspeth-c0c4f49981: an aliased supertype handler still shadows the narrow
    # subclass at runtime and must be flagged.
    source = (
        "CSE = ComposerServiceError\n"
        "CPCE = ComposerPluginCrashError\n"
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except CSE as exc:\n"
        "        pass\n"
        "    except CPCE as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]


def test_catch_order_reports_broad_exception_before_subclass(tmp_path: Path) -> None:
    # elspeth-eb90341cdb: a bare except Exception before a composer crash
    # subclass shadows it (the subclass descends from Exception).
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]


def test_catch_order_accepts_broad_exception_after_subclass(tmp_path: Path) -> None:
    # Correct order: the narrow composer handler precedes the broad except
    # Exception — no finding (guards against over-flagging the common shape).
    source = (
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except ComposerPluginCrashError as crash:\n"
        "        pass\n"
        "    except Exception as exc:\n"
        "        pass\n"
    )

    assert _catch_order_findings(tmp_path, source) == []


def test_catch_order_declared_map_matches_real_composer_exception_mro() -> None:
    import_module("elspeth.web.composer.protocol")
    import_module("elspeth.web.composer.service")

    from elspeth.web.composer.protocol import ComposerServiceError

    composer_family: set[type] = {ComposerServiceError} | _all_subclasses(ComposerServiceError)
    name_to_cls = {cls.__name__: cls for cls in composer_family}
    real_subclasses = {name for name in name_to_cls if name != "ComposerServiceError"}

    assert set(_SUBCLASS_TO_SUPERCLASSES) == real_subclasses
    for sub_name, declared_supers in _SUBCLASS_TO_SUPERCLASSES.items():
        assert "ComposerServiceError" in declared_supers
        cls = name_to_cls[sub_name]
        real_supers = {ancestor.__name__ for ancestor in cls.__mro__[1:] if ancestor in composer_family}
        # Declared = the real composer-family supertypes PLUS the broad
        # Exception/BaseException supertypes (elspeth-eb90341cdb): a bare
        # except Exception also shadows the narrow handler.
        assert declared_supers == frozenset(real_supers) | _BROAD_SUPERTYPES
        # The broad supertypes are genuinely in the class's real MRO.
        assert _BROAD_SUPERTYPES.issubset({ancestor.__name__ for ancestor in cls.__mro__})


def test_exception_channel_contains_raise_caught_in_the_same_function(tmp_path: Path) -> None:
    source = (
        "def handler(state):\n    try:\n        raise ValueError('bad')\n    except ValueError as exc:\n        return (state, str(exc))\n"
    )

    assert _exception_channel_findings(tmp_path, source) == []


def test_exception_channel_contains_helper_raise_when_every_local_call_is_guarded(tmp_path: Path) -> None:
    source = (
        "def _inner(k):\n    if not k:\n        raise ValueError('empty')\n    return k[0]\n"
        "def _outer(k):\n    return _inner(k)\n"
        "def handler(state, k):\n    try:\n        return _outer(k)\n    except (KeyError, ValueError) as exc:\n        return (state, str(exc))\n"
        "def other(state, k):\n    try:\n        return _outer(k)\n    except Exception:\n        return None\n"
    )

    assert _exception_channel_findings(tmp_path, source) == []


def test_exception_channel_reports_helper_raise_reached_through_one_unguarded_call(tmp_path: Path) -> None:
    source = (
        "def _inner(k):\n    if not k:\n        raise ValueError('empty')\n    return k[0]\n"
        "def guarded(state, k):\n    try:\n        return _inner(k)\n    except ValueError as exc:\n        return (state, str(exc))\n"
        "def unguarded(state, k):\n    return _inner(k)\n"
    )

    findings = _exception_channel_findings(tmp_path, source)

    assert [(finding.line, finding.rule_id) for finding in findings] == [(3, "CEC1")]


def test_exception_channel_reports_helper_with_no_local_caller_as_escaping(tmp_path: Path) -> None:
    """A helper reached only from another module: the catch is invisible, so fail closed."""
    source = "def _parse(v):\n    raise ValueError('bad')\n"

    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == [2]


def test_exception_channel_guard_must_name_the_exception_or_a_base_class(tmp_path: Path) -> None:
    wrong_type = "def handler(state):\n    try:\n        raise ValueError('bad')\n    except TypeError:\n        return None\n"
    base_class = "def handler(state):\n    try:\n        raise UnicodeDecodeError('utf-8', b'', 0, 1, 'x')\n    except ValueError:\n        return None\n"
    bare_except = "def handler(state):\n    try:\n        raise TypeError('bad')\n    except:\n        return None\n"
    subclass_only = "def handler(state):\n    try:\n        raise ValueError('bad')\n    except UnicodeError:\n        return None\n"

    assert [finding.line for finding in _exception_channel_findings(tmp_path, wrong_type)] == [3]
    assert _exception_channel_findings(tmp_path, base_class) == []
    assert _exception_channel_findings(tmp_path, bare_except) == []
    assert [finding.line for finding in _exception_channel_findings(tmp_path, subclass_only)] == [3]


def test_exception_channel_raise_inside_except_handler_or_finally_is_not_guarded_by_that_try(tmp_path: Path) -> None:
    source = (
        "def handler(state):\n    try:\n        return state\n    except KeyError:\n        raise ValueError('in handler')\n"
        "    finally:\n        pass\n"
    )

    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == [5]


def test_exception_channel_nested_function_is_its_own_scope(tmp_path: Path) -> None:
    """A try around a nested def does not guard raises executed later when the closure is called."""
    source = (
        "def handler(state):\n    try:\n        def _later():\n            raise ValueError('deferred')\n"
        "        return _later\n    except ValueError:\n        return None\n"
    )

    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == [4]


def test_exception_channel_exempts_post_init_nominal_invariants(tmp_path: Path) -> None:
    source = (
        "from dataclasses import dataclass\n@dataclass(frozen=True)\nclass View:\n    blob_id: str\n"
        "    def __post_init__(self) -> None:\n        if type(self.blob_id) is not str:\n            raise TypeError('nominal')\n"
        "def handler(state):\n    return View(state)\n"
    )

    assert _exception_channel_findings(tmp_path, source) == []


def test_exception_channel_recursive_helpers_do_not_loop(tmp_path: Path) -> None:
    source = (
        "def _walk(node):\n    if node is None:\n        raise ValueError('none')\n    return _walk(node.child)\n"
        "def handler(state, node):\n    try:\n        return _walk(node)\n    except ValueError:\n        return None\n"
    )

    assert _exception_channel_findings(tmp_path, source) == []


def _exception_channel_findings(tmp_path: Path, source: str) -> list[Finding]:
    target = tmp_path / "web" / "composer" / "tools" / "blobs.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    tree = ast.parse(source, filename=str(target))
    return list(EXCEPTION_CHANNEL_RULE.analyze(tree, target, RuleContext(root=tmp_path)))


def _catch_order_findings(tmp_path: Path, source: str) -> list[Finding]:
    target = tmp_path / "web" / "sessions" / "routes.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    tree = ast.parse(source, filename=str(target))
    return list(CATCH_ORDER_RULE.analyze(tree, target, RuleContext(root=tmp_path)))


def _all_subclasses(cls: type) -> set[type]:
    discovered: set[type] = set()
    stack: list[type] = [cls]
    while stack:
        parent = stack.pop()
        for child in parent.__subclasses__():
            if child not in discovered:
                discovered.add(child)
                stack.append(child)
    return discovered
