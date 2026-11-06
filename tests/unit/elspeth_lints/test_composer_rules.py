"""Tests for composer elspeth-lints rules."""

from __future__ import annotations

import ast
import copy
from importlib import import_module
from pathlib import Path

import pytest

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


@pytest.mark.parametrize("supertype", ["ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_reports_shadowed_admission_refusal(tmp_path: Path, supertype: str) -> None:
    source = (
        f"def f():\n    try:\n        pass\n    except {supertype}:\n        pass\n    except ComposerAdmissionRefused:\n        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]
    assert "ComposerAdmissionRefused" in findings[0].message


@pytest.mark.parametrize("supertype", ["ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_accepts_admission_refusal_before_supertype(tmp_path: Path, supertype: str) -> None:
    source = (
        f"def f():\n    try:\n        pass\n    except ComposerAdmissionRefused:\n        pass\n    except {supertype}:\n        pass\n"
    )

    assert _catch_order_findings(tmp_path, source) == []


@pytest.mark.parametrize("supertype", ["ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_reports_shadowed_mutation_authority_revocation(tmp_path: Path, supertype: str) -> None:
    source = f"def f():\n    try:\n        pass\n    except {supertype}:\n        pass\n    except ComposerMutationAuthorityRevoked:\n        pass\n"

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]
    assert "ComposerMutationAuthorityRevoked" in findings[0].message


@pytest.mark.parametrize("supertype", ["ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_accepts_mutation_authority_revocation_before_supertype(tmp_path: Path, supertype: str) -> None:
    source = f"def f():\n    try:\n        pass\n    except ComposerMutationAuthorityRevoked:\n        pass\n    except {supertype}:\n        pass\n"

    assert _catch_order_findings(tmp_path, source) == []


@pytest.mark.parametrize("supertype", ["ComposerAdmissionRefused", "ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_reports_shadowed_credential_material_refusal(tmp_path: Path, supertype: str) -> None:
    source = (
        f"def f():\n    try:\n        pass\n    except {supertype}:\n        pass\n    except CredentialMaterialRefused:\n        pass\n"
    )

    findings = _catch_order_findings(tmp_path, source)

    assert [finding.rule_id for finding in findings] == ["CCO1"]
    assert "CredentialMaterialRefused" in findings[0].message


@pytest.mark.parametrize("supertype", ["ComposerAdmissionRefused", "ComposerServiceError", "Exception", "BaseException"])
def test_catch_order_accepts_credential_material_refusal_before_supertype(tmp_path: Path, supertype: str) -> None:
    source = (
        f"def f():\n    try:\n        pass\n    except CredentialMaterialRefused:\n        pass\n    except {supertype}:\n        pass\n"
    )

    assert _catch_order_findings(tmp_path, source) == []


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
    import_module("elspeth.web.credential_guard")

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


_OWNED_GUARD_PREAMBLE = (
    "from dataclasses import dataclass\n"
    "from collections.abc import Callable\n"
    "from typing import Any, final\n"
    "@final\n@dataclass(frozen=True, slots=True)\nclass Owned:\n    field: str\n"
    "@dataclass(frozen=True)\nclass Other:\n    field: str\n"
)


@pytest.mark.parametrize(
    "body",
    [
        "    if type(value) is not Owned:\n        raise TypeError('owned')\n",
        "    if type(value) not in (Owned, Other):\n        raise TypeError('owned')\n",
        "    if value is not None and type(value) not in (Owned, Other):\n        raise TypeError('owned')\n",
        "    resolved = resolver('key')\n    if resolved is None or type(resolved) is Other:\n        return None\n"
        "    if type(resolved) is not Owned:\n        raise TypeError('owned')\n",
    ],
)
def test_exception_channel_accepts_proven_internal_nominal_guard(tmp_path: Path, body: str) -> None:
    source = _OWNED_GUARD_PREAMBLE + "def handler(value: Owned | Other | None, resolver: Callable[[str], Owned | Other | None]):\n" + body
    assert _exception_channel_findings(tmp_path, source) == []


@pytest.mark.parametrize(
    "body",
    [
        "    if type(value) is not str:\n        raise TypeError('guard')\n",
        "    if type(value) not in (Owned, str):\n        raise TypeError('guard')\n",
        "    if type(value) is Owned:\n        raise TypeError('guard')\n",
        "    if value is None or type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    if arguments is not None and type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    if type(None) is not Owned:\n        raise TypeError('guard')\n",
        "    if type(arguments['payload']) is not Owned:\n        raise TypeError('guard')\n",
        "    local = arguments['payload']\n    if type(local) is not Owned:\n        raise TypeError('guard')\n",
        "    value = arguments['payload']\n    if type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    resolved = resolver('key')\n    resolved = arguments['payload']\n"
        "    if type(resolved) is not Owned:\n        raise TypeError('guard')\n",
        "    resolved = resolver('key')\n    alias = resolved\n    if type(alias) is not Owned:\n        raise TypeError('guard')\n",
        "    get = resolver\n    resolved = get('key')\n    if type(resolved) is not Owned:\n        raise TypeError('guard')\n",
        "    resolver = arguments['resolver']\n    resolved = resolver('key')\n"
        "    if type(resolved) is not Owned:\n        raise TypeError('guard')\n",
        "    type = arguments['type']\n    if type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    TypeError = ValueError\n    if type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    class Owned:\n        pass\n    if type(value) is not Owned:\n        raise TypeError('guard')\n",
        "    if arguments:\n        if type(value) is not Owned:\n            raise TypeError('guard')\n",
        "    if type(value) is not Owned:\n        pass\n    else:\n        raise TypeError('guard')\n",
        "    if type(value) is not Owned:\n        def later():\n            raise TypeError('guard')\n        return later\n",
        "    if type(value) is not Owned:\n        raise ValueError('guard')\n",
        "    if type(value) is not Owned:\n        observe(value)\n        raise TypeError('guard')\n",
        "    if value is not None and type(value) is not Owned and arguments:\n        raise TypeError('guard')\n",
        "    if type(value) is not Owned:\n        raise TypeError\n",
        "    raise TypeError('guard')\n",
    ],
)
def test_exception_channel_keeps_unproved_nominal_sites_red(tmp_path: Path, body: str) -> None:
    source = (
        _OWNED_GUARD_PREAMBLE
        + ("def handler(value: Owned | Other | None, resolver: Callable[[str], Owned | Other | None], arguments):\n")
        + body
    )
    expected = [node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Raise)]
    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == sorted(expected)


@pytest.mark.parametrize("annotation", ["", ": Any", ": object", ": dict[str, object]"])
def test_exception_channel_requires_closed_subject_contract(tmp_path: Path, annotation: str) -> None:
    source = (
        _OWNED_GUARD_PREAMBLE + f"def handler(value{annotation}):\n    if type(value) is not Owned:\n        raise TypeError('guard')\n"
    )
    assert len(_exception_channel_findings(tmp_path, source)) == 1


@pytest.mark.parametrize(
    ("old", "new"),
    [
        ("frozen=True", "frozen=False"),
        ("frozen=True", "frozen=1"),
        ("from dataclasses import dataclass", "from elsewhere import dataclass"),
        ("from dataclasses import dataclass", "from dataclasses import dataclass as renamed\ndataclass = renamed"),
        ("from dataclasses import dataclass", "if available:\n    from dataclasses import dataclass"),
        ("from collections.abc import Callable", "if available:\n    from collections.abc import Callable"),
        ("from typing import Any, final", "if available:\n    from typing import Any, final"),
        ("from typing import Any, final", "from typing import Any\nfrom elsewhere import final"),
        ("class Owned:", "class Owned(Protocol):"),
        ("@final", "@unknown\n@final"),
        ("@dataclass(frozen=True, slots=True)", "@dataclass(frozen=False)\n@dataclass(frozen=True)"),
        ("def handler", "Owned = Other\ndef handler"),
        ("def handler", "class Owned:\n    pass\ndef handler"),
        ("def handler", "if flag:\n    class Owned:\n        pass\ndef handler"),
        ("def handler", "from elsewhere import Owned\ndef handler"),
        ("def handler", "type = custom_type\ndef handler"),
        ("def handler", "TypeError = ValueError\ndef handler"),
        ("def handler", "dataclass = replacement\ndef handler"),
        ("def handler", "final = replacement\ndef handler"),
        ("def handler", "Callable = replacement\ndef handler"),
        ("def handler", "def change():\n    global Owned\n    Owned = Other\ndef handler"),
        ("def handler", "@unknown\ndef handler"),
        ("resolver: Callable[[str], Owned]", "resolver: Callable[[str], Any]"),
        ("resolver: Callable[[str], Owned]", "resolver: Callable[[str], Owned | object]"),
        ("resolver: Callable[[str], Owned]", "resolver: Callable"),
        ("resolver: Callable[[str], Owned]", "resolver: object"),
        ("resolver: Callable[[str], Owned]", "resolver: Any"),
        ("resolver: Callable[[str], Owned]", "resolver: Callable[[str], Owned], type=object"),
        ("resolver: Callable[[str], Owned]", "resolver: Callable[[str], Owned], TypeError=ValueError"),
        ("value: Owned", "value: External"),
        ("value: Owned", "value: Owned | object"),
    ],
)
def test_exception_channel_rejects_ambiguous_nominal_bindings(tmp_path: Path, old: str, new: str) -> None:
    source = _OWNED_GUARD_PREAMBLE + (
        "def handler(value: Owned, resolver: Callable[[str], Owned]):\n"
        "    resolved = resolver('key')\n    if type(resolved) is not Owned:\n        raise TypeError('guard')\n"
    )
    if old == "value: Owned":
        source = source.replace("type(resolved)", "type(value)")
    source = source.replace(old, new)
    assert len(_exception_channel_findings(tmp_path, source)) == 1


@pytest.mark.parametrize(
    "definition",
    [
        "def helper(default=(value := arguments['payload'])):\n        pass",
        "async def helper(*, default=(value := arguments['payload'])):\n        pass",
        "@(value := arguments['payload'])\n    def helper():\n        pass",
        "class Helper((value := arguments['payload'])):\n        pass",
        "class Helper(metaclass=(value := arguments['payload'])):\n        pass",
        "@(value := arguments['payload'])\n    class Helper:\n        pass",
        "helper = lambda default=(value := arguments['payload']): None",
    ],
)
def test_exception_channel_reports_subject_rebound_in_definition_header(tmp_path: Path, definition: str) -> None:
    source = _OWNED_GUARD_PREAMBLE + (
        "def handler(value: Owned, arguments):\n"
        f"    {definition}\n"
        "    if type(value) is not Owned:\n        raise TypeError('raw argument')\n"
    )
    compile(source, "<definition-header-control>", "exec")
    expected = next(node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Raise))

    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == [expected]


@pytest.mark.parametrize("name, replacement", [("Owned", "str"), ("type", "custom_type"), ("TypeError", "ValueError")])
def test_exception_channel_reports_module_binding_rebound_in_definition_header(tmp_path: Path, name: str, replacement: str) -> None:
    source = _OWNED_GUARD_PREAMBLE + (
        f"def helper(default=({name} := {replacement})):\n    pass\n"
        "def handler(value: Owned):\n"
        "    if type(value) is not Owned:\n        raise TypeError('rebound binding')\n"
    )
    compile(source, "<module-definition-header-control>", "exec")
    expected = next(node.lineno for node in ast.walk(ast.parse(source)) if isinstance(node, ast.Raise))

    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == [expected]


@pytest.mark.parametrize(
    "definition",
    [
        "def helper(default=value):\n        pass",
        "def helper(value: Owned, type=None):\n        value = type",
        "class Helper:\n        value = None\n        type = None",
        "helper = lambda value, type=None: (value := type)",
        "def helper(default=lambda: (value := arguments['payload'])):\n        pass",
    ],
)
def test_exception_channel_keeps_definition_headers_and_child_namespaces_distinct(tmp_path: Path, definition: str) -> None:
    source = _OWNED_GUARD_PREAMBLE + (
        f"def handler(value: Owned, arguments):\n    {definition}\n    if type(value) is not Owned:\n        raise TypeError('owned')\n"
    )
    compile(source, "<child-namespace-control>", "exec")

    assert _exception_channel_findings(tmp_path, source) == []


def test_exception_channel_nominal_guard_does_not_exempt_other_raises(tmp_path: Path) -> None:
    source = _OWNED_GUARD_PREAMBLE + (
        "def handler(value: Owned, arguments):\n"
        "    if type(value) is not Owned:\n        raise TypeError('owned')\n"
        "    if type(arguments['payload']) is not str:\n        raise TypeError('external')\n"
        "    raise ValueError('other')\n"
    )
    expected = [
        node.lineno
        for node in ast.walk(ast.parse(source))
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and node.exc.args
        and isinstance(node.exc.args[0], ast.Constant)
        and node.exc.args[0].value != "owned"
    ]
    assert [finding.line for finding in _exception_channel_findings(tmp_path, source)] == sorted(expected)


def test_exception_channel_actual_generation_guards_and_mutated_contracts(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[3]
    target = root / "src/elspeth/web/composer/tools/generation.py"
    tree = ast.parse(target.read_text(), filename=str(target))
    context = RuleContext(root=root / "src/elspeth")
    assert EXCEPTION_CHANNEL_RULE.analyze(tree, target, context) == []
    targets = {
        "_compute_proof_diagnostics_for_source": "blob_resolver",
        "compute_proof_diagnostics": "context",
    }
    for function_name, parameter_name in targets.items():
        for mutation in ("contract", "builtin_guard", "unrelated_guard"):
            mutated = copy.deepcopy(tree)
            function = next(node for node in mutated.body if isinstance(node, ast.FunctionDef) and node.name == function_name)
            guard_raise = next(
                node
                for node in ast.walk(function)
                if isinstance(node, ast.Raise)
                and isinstance(node.exc, ast.Call)
                and isinstance(node.exc.func, ast.Name)
                and node.exc.func.id == "TypeError"
            )
            if mutation == "contract":
                parameter = next(
                    arg for arg in (*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs) if arg.arg == parameter_name
                )
                parameter.annotation = ast.Name(id="object", ctx=ast.Load())
            else:
                guard = next(node for node in function.body if isinstance(node, ast.If) and guard_raise in node.body)
                subject = "resolved_blob" if parameter_name == "blob_resolver" else "context"
                guard.test = (
                    ast.parse(f"type({subject}) is not str", mode="eval").body if mutation == "builtin_guard" else ast.Constant(True)
                )
            findings = EXCEPTION_CHANNEL_RULE.analyze(mutated, target, context)
            assert guard_raise.lineno in {finding.line for finding in findings}


def test_exception_channel_existing_native_fixtures_remain_exact() -> None:
    from elspeth_lints.core.fixture_harness import assert_fixture_case, discover_rule_fixture_cases, find_fixture_inventory_errors

    assert find_fixture_inventory_errors([EXCEPTION_CHANNEL_RULE]) == []
    for case in discover_rule_fixture_cases(EXCEPTION_CHANNEL_RULE):
        assert_fixture_case(case)


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
