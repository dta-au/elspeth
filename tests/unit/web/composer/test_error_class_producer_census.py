"""No ``error_class`` writer names a class that was not raised (plan S0 acceptance 1).

``error_class`` is audit evidence: it must name the exception class actually
raised, caught or constructed at the writing site. Before S0 several sites
wrote hand labels (``"TypeError"``, ``"ValueError"``, ``"MissingRequiredPaths"``,
``"TimeoutError"``) for failures that raised nothing of the kind.

The census is an AST walk, not a grep: every string-literal label written
through an ``error_class=`` keyword, an ``"error_class":`` dict key or an
``error_class = ...`` assignment under ``src/elspeth/web/composer`` (guided
excluded) must be backed by the enclosing function, or be on the short,
named out-of-scope list below.

A second census pins ``redaction._SAFE_ARG_ERROR_CLASSES`` to the classes the
ARG_ERROR producers actually raise, measured by triggering each producer.
"""

from __future__ import annotations

import ast
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path

from elspeth.core.canonical import canonical_json
from elspeth.web.composer.bounded_json import bounded_json_loads
from elspeth.web.composer.redaction import _SAFE_ARG_ERROR_CLASSES

_COMPOSER = Path(__file__).resolve().parents[4] / "src" / "elspeth" / "web" / "composer"

# Planner-facing labels and redaction echoes that are not audit ``error_class``
# fields, keyed by (file under web/composer, enclosing function, label).
_OUT_OF_SCOPE: dict[tuple[str, str, str], str] = {
    ("pipeline_planner.py", "_allowlisted_candidate_feedback", "ValidationError"): (
        "planner feedback entry label for a closed validation code; sent to the planner, never an audit field"
    ),
    ("pipeline_planner.py", "_binding_rejection_feedback", "ValidationError"): (
        "planner feedback entry label for a binding rejection; sent to the planner, never an audit field"
    ),
    ("pipeline_planner.py", "_deferred_intent_claim_feedback", "DeferredIntentClaimError"): (
        "planner feedback entry label; sent to the planner, never an audit field"
    ),
    ("pipeline_planner.py", "_canonical_schema_feedback", "SchemaValidationError"): (
        "planner feedback entry label for a canonical-schema rejection; sent to the planner, never an audit field"
    ),
    ("provider_discovery_response.py", "to_wire", "ToolArgumentError"): (
        "_ArgumentErrorResponse is built only from a ToolArgumentError (_argument_error_response); planner-facing projection"
    ),
    ("redaction.py", "redact_failure_response", "CancelledError"): (
        "echoes the input only when it already equals CancelledError; otherwise a fixed sentinel"
    ),
}


@dataclass(frozen=True, slots=True)
class _Label:
    path: str
    function: str
    label: str
    backed: bool


def _labels(expression: ast.expr) -> Iterator[str]:
    """String labels an ``error_class`` expression can evaluate to."""
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        yield expression.value
    elif isinstance(expression, ast.IfExp):
        yield from _labels(expression.body)
        yield from _labels(expression.orelse)
    elif isinstance(expression, ast.BoolOp):
        for value in expression.values:
            yield from _labels(value)


def _names_class(expression: ast.expr | None, label: str) -> bool:
    if expression is None:
        return False
    text = ast.unparse(expression)
    return text == label or text.endswith(f".{label}")


def _handler_catches(handler: ast.ExceptHandler, label: str) -> bool:
    if handler.type is None:
        return False
    caught = handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type]
    return any(_names_class(element, label) for element in caught)


def _parameter_typed(function: ast.AST | None, label: str) -> bool:
    if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
        return False
    arguments = [*function.args.posonlyargs, *function.args.args, *function.args.kwonlyargs]
    return any(_names_class(argument.annotation, label) for argument in arguments)


def _error_class_labels(relative_path: str, source: str) -> list[_Label]:
    """Every literal ``error_class`` label and whether its site backs it.

    A label is backed only when it sits lexically inside an ``except`` clause
    that catches that class, or in a function whose parameter is annotated with
    it. Function-wide evidence is deliberately not enough: ``run_tool_batch``
    is thousands of lines long and catches ``TypeError`` for one site only.
    """
    labels: list[_Label] = []

    def visit(node: ast.AST, function: ast.AST | None, function_name: str, handlers: tuple[ast.ExceptHandler, ...]) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function, function_name, handlers = node, node.name, ()
        if isinstance(node, ast.ExceptHandler):
            handlers = (*handlers, node)
        expressions: list[ast.expr] = []
        if isinstance(node, ast.keyword) and node.arg == "error_class":
            expressions.append(node.value)
        elif isinstance(node, ast.Dict):
            expressions.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "error_class"
            )
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(target, ast.Name) and target.id == "error_class" for target in targets):
                expressions.append(node.value)
        for expression in expressions:
            for label in _labels(expression):
                if label.startswith("<redacted-"):
                    continue  # a redaction sentinel, not a class name
                backed = any(_handler_catches(handler, label) for handler in handlers) or _parameter_typed(function, label)
                labels.append(_Label(relative_path, function_name, label, backed))
        for child in ast.iter_child_nodes(node):
            visit(child, function, function_name, handlers)

    visit(ast.parse(source), None, "<module>", ())
    return labels


def _live_labels() -> list[_Label]:
    labels: list[_Label] = []
    for path in sorted(_COMPOSER.rglob("*.py")):
        relative = path.relative_to(_COMPOSER)
        if "guided" in relative.parts:
            continue
        labels.extend(_error_class_labels(relative.as_posix(), path.read_text()))
    return labels


def _dishonest(labels: list[_Label]) -> set[tuple[str, str, str]]:
    return {(label.path, label.function, label.label) for label in labels if not label.backed} - set(_OUT_OF_SCOPE)


class TestErrorClassProducerCensus:
    def test_instrument_flags_a_planted_hand_label(self) -> None:
        planted = "def site(audit):\n    return finish_arg_error(audit, error_class='TypeError', error_message='m')\n"
        assert _dishonest(_error_class_labels("planted.py", planted)) == {("planted.py", "site", "TypeError")}

    def test_instrument_flags_a_planted_dict_label_and_assignment(self) -> None:
        planted = "def site():\n    error_class = 'MissingRequiredPaths'\n    return {'error_class': 'TimeoutError'}\n"
        assert _dishonest(_error_class_labels("planted.py", planted)) == {
            ("planted.py", "site", "MissingRequiredPaths"),
            ("planted.py", "site", "TimeoutError"),
        }

    def test_instrument_accepts_a_caught_class(self) -> None:
        honest = "async def site():\n    try:\n        pass\n    except TimeoutError:\n        error_class = 'TimeoutError'\n"
        assert _dishonest(_error_class_labels("honest.py", honest)) == set()

    def test_instrument_rejects_a_label_outside_the_catching_clause(self) -> None:
        # A long function that catches TypeError at one site must not launder a
        # hand-written "TypeError" at another.
        planted = (
            "def site(raw):\n    try:\n        decode(raw)\n    except TypeError:\n        pass\n    record(error_class='TypeError')\n"
        )
        assert _dishonest(_error_class_labels("planted.py", planted)) == {("planted.py", "site", "TypeError")}

    def test_instrument_sees_the_live_tree(self) -> None:
        # Known positives: the LLM-call ``except TimeoutError`` sites in service.py.
        labels = _live_labels()
        assert any(label.path == "service.py" and label.label == "TimeoutError" and label.backed for label in labels)

    def test_every_error_class_label_names_a_raised_class(self) -> None:
        assert _dishonest(_live_labels()) == set()

    def test_out_of_scope_entries_all_still_exist(self) -> None:
        live = {(label.path, label.function, label.label) for label in _live_labels()}
        assert set(_OUT_OF_SCOPE) - live == set()


# ---------------------------------------------------------------------------
# _SAFE_ARG_ERROR_CLASSES: exactly the classes the ARG_ERROR producers raise.
# ---------------------------------------------------------------------------


def _raised_class_name(action: Callable[[], object]) -> str:
    """The exact class a producer raises; the class itself is the measurement."""
    try:
        action()
    except Exception as exc:
        return type(exc).__name__
    raise AssertionError("the producer under measurement did not raise")


def _produced_arg_error_classes() -> set[str]:
    """Trigger every web ARG_ERROR producer with input that can reach it.

    Provider arguments are admitted as ``str`` (``_AdmittedToolFunction``), so
    the wire gate only ever sees text; the canonicalization gate only ever sees
    what ``bounded_json_loads`` decoded. Unreachable classes (``TypeError`` for
    non-text, ``FloatDomainError`` / bare ``CanonicalizationError`` for values
    JSON cannot carry) are therefore not produced, and must not be allowlisted.
    """
    from pydantic import BaseModel, ConfigDict

    from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
    from elspeth.web.composer.protocol import ToolArgumentError

    class _Strict(BaseModel):
        model_config = ConfigDict(extra="forbid")
        value: int

    return {
        # tool_batch wire gate: bounded_json_loads over provider text.
        _raised_class_name(lambda: bounded_json_loads("{", label="a")),
        _raised_class_name(lambda: bounded_json_loads("[" * 2_000 + "]" * 2_000, label="a")),
        _raised_class_name(lambda: bounded_json_loads("[NaN]", label="a")),
        _raised_class_name(lambda: bounded_json_loads("[1e999]", label="a")),
        # Canonicalization gate over decoded arguments: a JSON integer outside
        # the canonical domain.
        _raised_class_name(lambda: canonical_json(bounded_json_loads('{"a": 100000000000000000000000}', label="a"))),
        # Every other gate records a ToolArgumentError it raised or built.
        type(ToolArgumentError(argument="a", expected="b", actual_type="c", category=ToolArgumentErrorCategory.SEMANTIC_RULE)).__name__,
        # The advisor's pydantic model rejection records pydantic's class.
        _raised_class_name(lambda: _Strict.model_validate({"value": "x", "extra": 1})),
    }


def test_safe_arg_error_classes_are_exactly_the_produced_classes() -> None:
    produced = _produced_arg_error_classes()
    assert json.dumps(sorted(produced)) == json.dumps(sorted(_SAFE_ARG_ERROR_CLASSES))


def test_producer_census_detects_a_stale_allowlist_entry() -> None:
    stale = {*_SAFE_ARG_ERROR_CLASSES, "MissingRequiredPaths"}
    assert stale != _produced_arg_error_classes()
