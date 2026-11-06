"""Static assertion-shape fixtures; specimen bodies are parsed, not executed."""

import json
from pathlib import Path

import pytest
from tests.unit.elspeth_lints.rules.trust_boundary.test_tests_rule import _analyze_at, _test_fingerprint, _write_test_file

from elspeth_lints.core.cli import main


def _analyze_body(
    root: Path,
    body: str,
    *,
    invariant: str = "raises ValueError on malformed data",
    asynchronous: bool = False,
) -> list[str]:
    ref = "tests/test_boundary.py::test_rejects"
    declaration = "async def" if asynchronous else "def"
    source = (
        f"import contextlib\nimport pytest\nimport unittest\n\n{declaration} test_rejects():\n"
        + "\n".join("    " + line for line in body.splitlines())
        + "\n"
    )
    _write_test_file(root, "tests/test_boundary.py", source)
    fingerprint = _test_fingerprint(root, ref)
    findings = _analyze_at(
        f'''
        @trust_boundary(
            tier=3,
            source="fixture",
            source_param="data",
            suppresses=("R1",),
            invariant={invariant!r},
            test_ref="{ref}",
            test_fingerprint="{fingerprint}",
        )
        def foo(data):
            return len(data["items"])
        ''',
        repo_root=root,
    )
    return [finding.rule_id for finding in findings]


@pytest.mark.parametrize(
    "body",
    [
        'with pytest.raises(TypeError):\n    foo(data={"items": None})\nwith pytest.raises(ValueError):\n    int("bad")',
        'pytest.raises(TypeError, foo, data={"items": None})\npytest.raises(ValueError, int, "bad")',
        'case = unittest.TestCase()\nwith case.assertRaises(TypeError):\n    foo(data={"items": None})\nwith case.assertRaises(ValueError):\n    int("bad")',
        'case = unittest.TestCase()\ncase.assertRaises(TypeError, foo, data={"items": None})\ncase.assertRaises(ValueError, int, "bad")',
        'case = unittest.TestCase()\nwith case.assertRaisesRegex(TypeError, "object"):\n    foo(data={"items": None})\nwith case.assertRaisesRegex(ValueError, "invalid"):\n    int("bad")',
        'with pytest.raises(ValueError):\n    with pytest.raises(TypeError):\n        foo(data={"items": None})\n    int("bad")',
    ],
    ids=["pytest-context", "pytest-callable", "unittest-context", "unittest-callable", "unittest-regex-context", "nested-unrelated-outer"],
)
def test_unrelated_exception_assertion_cannot_authorize_subject(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_INVARIANT_MISMATCH"]


@pytest.mark.parametrize(
    "body",
    [
        'with pytest.raises(ValueError):\n    foo(data={"items": None})\nwith pytest.raises(TypeError):\n    len(None)',
        'pytest.raises(ValueError, foo, data={"items": None})\npytest.raises(TypeError, len, None)',
        'case = unittest.TestCase()\nwith case.assertRaises(ValueError):\n    foo(data={"items": None})',
        'case = unittest.TestCase()\ncase.assertRaises(ValueError, foo, data={"items": None})',
        'case = unittest.TestCase()\nwith case.assertRaisesRegex(ValueError, "bad"):\n    foo(data={"items": None})',
        'with pytest.raises(TypeError):\n    with pytest.raises(ValueError):\n        foo(data={"items": None})\n    len(None)',
    ],
    ids=["pytest-context", "pytest-callable", "unittest-context", "unittest-callable", "unittest-regex-context", "nested-subject-inner"],
)
def test_subject_assertion_keeps_its_own_exception_evidence(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == []


@pytest.mark.parametrize(
    "body",
    [
        'with pytest.raises(ValueError):\n    pytest.raises(TypeError, foo, data={"items": None})\n    int("bad")',
        # Multi-manager cases pin structural association, not exception propagation.
        'with pytest.raises(ValueError), pytest.raises(TypeError):\n    foo(data={"items": None})',
        'with pytest.raises(ValueError):\n    int("bad")\nwith pytest.raises(TypeError):\n    foo(data={"items": None})',
        'pytest.raises(exception_type(), foo, data={"items": None})\npytest.raises(ValueError, int, "bad")',
        'with pytest.raises(exception_type()):\n    foo(data={"items": None})\nwith pytest.raises(ValueError):\n    int("bad")',
        'with pytest.raises(ValueError):\n    case = unittest.TestCase()\n    case.assertRaises(TypeError, foo, data={"items": None})\n    int("bad")',
    ],
    ids=[
        "nested-pytest-callable",
        "multiple-contexts-nearest-wrong",
        "sibling-order-reversed",
        "unknown-callable-exception",
        "unknown-context-exception",
        "nested-unittest-callable",
    ],
)
def test_exception_evidence_respects_nearest_assertion_owner(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_INVARIANT_MISMATCH"]


@pytest.mark.parametrize(
    "body",
    [
        # This is the rightmost-body convention, not a runtime honesty claim.
        'with pytest.raises(TypeError), pytest.raises(ValueError):\n    foo(data={"items": None})',
        'with pytest.raises(ValueError):\n    with contextlib.nullcontext():\n        if ready:\n            foo(data={"items": None})',
        'with pytest.raises(TypeError):\n    foo(data={"items": None})\nwith pytest.raises(ValueError):\n    foo(data={"items": None})',
        'with pytest.raises((TypeError, ValueError)):\n    foo(data={"items": None})',
        'with pytest.raises(errors.ValueError):\n    foo(data={"items": None})',
        'with pytest.raises(ValueError):\n    with pytest.raises(TypeError):\n        unrelated()\n    foo(data={"items": None})',
        'pytest.raises(exception_type(), foo, data={"items": None})\npytest.raises(ValueError, foo, data={"items": None})',
    ],
    ids=[
        "multiple-contexts-nearest-correct",
        "nonraising-wrapper-and-if",
        "one-matching-subject-assertion-suffices",
        "exception-alternatives",
        "qualified-exception",
        "outer-direct-subject",
        "unknown-plus-matching-subject",
    ],
)
def test_existing_subject_evidence_forms_remain_accepted(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == []


@pytest.mark.parametrize("asynchronous_with", [True, False], ids=["async-with-static-shape", "ordinary-with"])
def test_async_test_functions_keep_raising_shape_visibility(tmp_path: Path, asynchronous_with: bool) -> None:
    keyword = "async with" if asynchronous_with else "with"
    assert _analyze_body(tmp_path, f'{keyword} pytest.raises(ValueError):\n    foo(data={{"items": None}})', asynchronous=True) == []


def test_invariant_without_named_exception_keeps_existing_policy(tmp_path: Path) -> None:
    assert _analyze_body(tmp_path, 'with pytest.raises(TypeError):\n    foo(data={"items": None})', invariant="reject malformed data") == []


@pytest.mark.parametrize(
    "body",
    [
        "with pytest.raises(ValueError):\n    foo()",
        'with pytest.raises(ValueError):\n    other(data={"items": None})',
        'with pytest.raises(ValueError):\n    def helper():\n        foo(data={"items": None})',
        'with pytest.raises(ValueError):\n    thunk = lambda: foo(data={"items": None})',
    ],
    ids=["missing-source-argument", "different-subject", "nested-helper-subject", "lambda-subject"],
)
def test_unrelated_assertions_keep_irrelevant_input_diagnostic(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_IRRELEVANT_INPUT"]


@pytest.mark.parametrize(
    "body",
    [
        'with pytest.raises(ValueError):\n    pytest.raises(TypeError, int, foo(data={"items": None}))',
        'with pytest.raises(ValueError):\n    with pytest.raises(TypeError, match=foo(data={"items": None})):\n        pass',
        'with pytest.raises(ValueError):\n    with contextlib.nullcontext(foo(data={"items": None})), pytest.raises(TypeError):\n        pass',
        'with pytest.raises(ValueError):\n    foo(data={"items": None}).assertRaises(TypeError, int, "bad")',
        'destinations = {}\nwith pytest.raises(ValueError):\n    with contextlib.nullcontext() as destinations[foo(data={"items": None})], pytest.raises(TypeError):\n        pass',
    ],
    ids=[
        "call-argument-before-inner-assertion",
        "context-keyword-before-inner-entry",
        "context-before-first-inner-raiser",
        "receiver-before-inner-call",
        "assignment-before-first-inner-raiser",
    ],
)
def test_outer_assertion_retains_eager_subject_argument(tmp_path: Path, body: str) -> None:
    assert _analyze_body(tmp_path, body) == []


def test_later_context_creation_does_not_borrow_outer_exception(tmp_path: Path) -> None:
    body = (
        "with pytest.raises(ValueError):\n"
        '    with pytest.raises(TypeError), contextlib.nullcontext(foo(data={"items": None})):\n'
        "        pass"
    )
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_IRRELEVANT_INPUT"]


def test_later_context_assignment_does_not_borrow_outer_exception(tmp_path: Path) -> None:
    body = (
        "destinations = {}\nwith pytest.raises(ValueError):\n"
        '    with pytest.raises(TypeError), contextlib.nullcontext() as destinations[foo(data={"items": None})]:\n'
        "        pass"
    )
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_IRRELEVANT_INPUT"]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        (
            'with pytest.raises(ValueError):\n    pytest.raises(TypeError, list, (foo(data={"items": None}) for _ in "x"))\n    int("bad")',
            ["R_TB_TESTS_IRRELEVANT_INPUT"],
        ),
        (
            'with pytest.raises(ValueError):\n    pytest.raises(TypeError, list, (item for item in foo(data={"items": None})))',
            [],
        ),
    ],
    ids=["deferred-element-does-not-belong-to-outer", "first-iterable-evaluates-before-inner"],
)
def test_generator_evaluation_boundary(tmp_path: Path, body: str, expected: list[str]) -> None:
    assert _analyze_body(tmp_path, body) == expected


def test_nested_callable_lambda_does_not_authorize_outer_assertion(tmp_path: Path) -> None:
    body = 'with pytest.raises(ValueError):\n    pytest.raises(TypeError, lambda: foo(data={"items": None}))'
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_IRRELEVANT_INPUT"]


def test_nested_async_with_keeps_exception_evidence_with_inner_assertion(tmp_path: Path) -> None:
    body = (
        'async with pytest.raises(ValueError):\n    async with pytest.raises(TypeError):\n        foo(data={"items": None})\n    int("bad")'
    )
    assert _analyze_body(tmp_path, body, asynchronous=True) == ["R_TB_TESTS_INVARIANT_MISMATCH"]


def test_callable_unittest_regex_keeps_existing_unsupported_shape(tmp_path: Path) -> None:
    body = 'case = unittest.TestCase()\ncase.assertRaisesRegex(ValueError, "bad", foo, data={"items": None})'
    assert _analyze_body(tmp_path, body) == ["R_TB_TESTS_IRRELEVANT_INPUT"]


@pytest.mark.parametrize("unrelated", [True, False], ids=["unrelated-exception-refused", "subject-exception-accepted"])
def test_ci_facing_honesty_trio_checks_associated_exception(tmp_path: Path, capsys: pytest.CaptureFixture[str], unrelated: bool) -> None:
    body = (
        'with pytest.raises(TypeError):\n    foo(data={"items": None})\nwith pytest.raises(ValueError):\n    int("bad")'
        if unrelated
        else 'with pytest.raises(ValueError):\n    foo(data={"items": None})'
    )
    _analyze_body(tmp_path, body)
    fingerprint = _test_fingerprint(tmp_path, "tests/test_boundary.py::test_rejects")
    function_body = (
        '    return len(data.get("items"))\n'
        if unrelated
        else '    value = data.get("items")\n    if value is None:\n        raise ValueError("bad")\n    return len(value)\n'
    )
    decorated = (
        "from elspeth.contracts import trust_boundary\n"
        "@trust_boundary(tier=3, source='fixture', source_param='data', suppresses=('R1',), "
        "invariant='raises ValueError on malformed data', test_ref='tests/test_boundary.py::test_rejects', "
        f"test_fingerprint={fingerprint!r})\n"
        "def foo(data):\n" + function_body
    )
    target = _write_test_file(tmp_path, "src/elspeth/boundary.py", decorated)
    exit_code = main(
        [
            "check",
            "--rules",
            "trust_boundary.tests,trust_boundary.scope,trust_boundary.tier",
            "--root",
            str(target.parent),
            "--repo-root",
            str(tmp_path),
            "--format",
            "json",
            "--fail-on-inert",
        ]
    )
    findings = json.loads(capsys.readouterr().out)
    assert exit_code == (1 if unrelated else 0)
    assert [finding["rule_id"] for finding in findings] == (["R_TB_TESTS_INVARIANT_MISMATCH"] if unrelated else [])
