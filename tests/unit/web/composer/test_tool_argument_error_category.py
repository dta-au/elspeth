"""Every argument rejection carries an honest class and a closed category.

``error_class`` records the exception class actually raised; the closed
:class:`ToolArgumentErrorCategory` records why the arguments were rejected.
These tests pin the category at construction and at every producing site
(plan ``docs/plans/2026-09-23-composer-strict-tool-contracts.md`` §4 S0, §5.2).
"""

from __future__ import annotations

import ast
import json
from pathlib import Path
from typing import Any, cast

import pytest
from jsonschema.exceptions import ValidationError as JsonSchemaValidationError

from elspeth.contracts.composer_audit import ComposerToolStatus, ToolArgumentErrorCategory
from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.composer._compose_loop_carriers import AdvisorArgumentRejection
from elspeth.web.composer.protocol import ToolArgumentError
from elspeth.web.composer.service import ComposerServiceImpl, composer_loop_tool_definitions
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import _dispatch
from elspeth.web.composer.tools._dispatch import (
    WIRE_KEYWORD_ALLOWLIST,
    _closed_root_schema,
    _validate_tool_arguments,
    get_tool_definitions,
    require_schema_valid_arguments,
)
from tests.helpers.tree_gate import iter_gate_files

from .conftest import (
    _fake_llm_response,
    _FakeChoice,
    _FakeComposeLLM,
    _FakeFunction,
    _FakeLLMResponse,
    _FakeMessage,
    _FakeToolCall,
)


def _error(*, code: str | None = None, category: object = None) -> ToolArgumentError:
    # ``category`` is typed ``object`` so the tests can hand the constructor
    # values outside the closed enum; the constructor is the boundary under test.
    return ToolArgumentError(argument="plugin", expected="a string", actual_type="int", code=code, category=cast(Any, category))


class TestConstruction:
    def test_no_code_and_no_category_is_a_semantic_rule(self) -> None:
        assert _error().category is ToolArgumentErrorCategory.SEMANTIC_RULE

    @pytest.mark.parametrize(
        ("code", "category"),
        [
            ("DISCOVERY_ONLY", ToolArgumentErrorCategory.DISCOVERY_ONLY),
            ("DUPLICATE_RESOLVED_INTERPRETATION", ToolArgumentErrorCategory.DUPLICATE_RESOLVED_INTERPRETATION),
            ("RATE_CAP_PER_SESSION_DAY", ToolArgumentErrorCategory.RATE_CAP_PER_SESSION_DAY),
            ("RATE_CAP_PER_TERM", ToolArgumentErrorCategory.RATE_CAP_PER_TERM),
        ],
    )
    def test_value_codes_carry_their_category_one_to_one(self, code: str, category: ToolArgumentErrorCategory) -> None:
        assert _error(code=code).category is category
        assert _error(code=code, category=category).category is category

    def test_explicit_category_is_kept(self) -> None:
        exc = _error(category=ToolArgumentErrorCategory.MODEL_VALIDATION)
        assert exc.category is ToolArgumentErrorCategory.MODEL_VALIDATION

    @pytest.mark.parametrize("category", [ToolArgumentErrorCategory.SCHEMA_SHAPE, ToolArgumentErrorCategory.SCHEMA_BOUND])
    def test_schema_validation_code_takes_shape_or_bound(self, category: ToolArgumentErrorCategory) -> None:
        assert _error(code="SCHEMA_VALIDATION", category=category).category is category

    def test_schema_validation_code_without_a_split_category_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="category"):
            _error(code="SCHEMA_VALIDATION")
        with pytest.raises(ValueError, match="category"):
            _error(code="SCHEMA_VALIDATION", category=ToolArgumentErrorCategory.MODEL_VALIDATION)

    @pytest.mark.parametrize(
        ("code", "category"),
        [
            ("DISCOVERY_ONLY", ToolArgumentErrorCategory.SEMANTIC_RULE),
            (None, ToolArgumentErrorCategory.DISCOVERY_ONLY),
            (None, ToolArgumentErrorCategory.SCHEMA_SHAPE),
            (None, ToolArgumentErrorCategory.RATE_CAP_PER_TERM),
        ],
    )
    def test_category_disagreeing_with_code_is_rejected(self, code: str | None, category: ToolArgumentErrorCategory) -> None:
        with pytest.raises(ValueError, match="category"):
            _error(code=code, category=category)

    @pytest.mark.parametrize("category", ["semantic_rule", "not_a_category", 3])
    def test_unknown_category_raises_at_construction(self, category: object) -> None:
        with pytest.raises(ValueError, match="category"):
            _error(category=category)

    def test_category_is_frozen(self) -> None:
        exc = _error()
        with pytest.raises(AttributeError):
            exc.category = ToolArgumentErrorCategory.MODEL_VALIDATION
        assert exc.category is ToolArgumentErrorCategory.SEMANTIC_RULE

    def test_corrupt_private_category_raises_rather_than_misclassifying(self) -> None:
        exc = _error()
        BaseException.__setattr__(exc, "_safe_category", "semantic_rule")
        with pytest.raises(FrameworkBugError, match="category"):
            _ = exc.category


# ---------------------------------------------------------------------------
# S gate: schema_shape vs schema_bound, decided by the failing keyword.
# ---------------------------------------------------------------------------

# Plan §3.3 rule 4: the keywords a strict provider grammar keeps.
_PLAN_ALLOWLIST = frozenset(
    {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "anyOf",
        "description",
        "pattern",
        "format",
        "minimum",
        "maximum",
        "exclusiveMinimum",
        "exclusiveMaximum",
        "multipleOf",
        "minItems",
        "maxItems",
    }
)
# Keywords the plan moves to the ledger: no grammar enforces them.
_LEDGERED_KEYWORDS = ("minLength", "maxLength", "not", "oneOf", "uniqueItems", "propertyNames")


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _schema_error(tool_name: str, arguments: dict[str, Any]) -> ToolArgumentError:
    with pytest.raises(ToolArgumentError) as caught:
        _validate_tool_arguments(tool_name, arguments, _empty_state(), raise_on_error=True)
    return caught.value


class TestSchemaShapeBoundSplit:
    def test_allowlist_is_the_plan_rule_4_keyword_set(self) -> None:
        assert frozenset(WIRE_KEYWORD_ALLOWLIST) == _PLAN_ALLOWLIST

    @pytest.mark.parametrize("keyword", sorted(_PLAN_ALLOWLIST))
    def test_every_allowlisted_keyword_is_a_shape_failure(self, keyword: str) -> None:
        error = JsonSchemaValidationError("m", validator=keyword)
        assert _dispatch._schema_error_category(error) is ToolArgumentErrorCategory.SCHEMA_SHAPE

    @pytest.mark.parametrize("keyword", _LEDGERED_KEYWORDS)
    def test_every_ledgered_keyword_is_a_bound_failure(self, keyword: str) -> None:
        error = JsonSchemaValidationError("m", validator=keyword)
        assert _dispatch._schema_error_category(error) is ToolArgumentErrorCategory.SCHEMA_BOUND

    def test_schema_error_carries_code_and_split_category(self) -> None:
        exc = _schema_error("set_metadata", {"patch": {}, "stray": 1})
        assert exc.code == "SCHEMA_VALIDATION"
        assert exc.category is ToolArgumentErrorCategory.SCHEMA_SHAPE

    def test_extra_key_on_a_live_tool_is_schema_shape(self) -> None:
        exc = _schema_error("set_metadata", {"patch": {}, "stray": 1})
        assert exc.category is ToolArgumentErrorCategory.SCHEMA_SHAPE

    def test_wrong_type_on_a_live_tool_is_schema_shape(self) -> None:
        exc = _schema_error("set_metadata", {"patch": "not an object"})
        assert exc.category is ToolArgumentErrorCategory.SCHEMA_SHAPE

    def test_length_cap_on_a_live_tool_is_schema_bound(self) -> None:
        exc = _schema_error("clear_source", {"source_name": ""})
        assert exc.category is ToolArgumentErrorCategory.SCHEMA_BOUND

    def test_reserved_identifier_on_a_live_tool_is_schema_bound(self) -> None:
        arguments = {
            "predecessor_id": "source",
            "successor_id": "sink",
            "node": {"id": "fork", "plugin": "passthrough", "options": {}},
        }
        exc = _schema_error("splice_transform", arguments)
        assert exc.category is ToolArgumentErrorCategory.SCHEMA_BOUND


class TestClosedRootLookup:
    def test_every_advertised_tool_resolves_to_a_closed_root(self) -> None:
        names = [definition["name"] for definition in get_tool_definitions()]
        assert len(names) == 42
        for name in names:
            schema = _closed_root_schema(name)
            assert schema["type"] == "object"
            assert schema["additionalProperties"] is False, name

    def test_lookup_holds_exactly_the_advertised_tools(self) -> None:
        advertised = {definition["name"] for definition in get_tool_definitions()}
        assert set(_dispatch._TOOL_SCHEMA_BY_NAME) == advertised

    @pytest.mark.parametrize(
        ("tool_name", "arguments"),
        [
            (
                "request_advisor_hint",
                {
                    "trigger": "proactive_security_safety",
                    "problem_summary": "stuck",
                    "recent_errors": [],
                    "attempted_actions": [],
                    "full_context": "extra",
                },
            ),
            (
                "request_interpretation_review",
                {"affected_node_id": "n1", "kind": "vague_term", "user_term": "term", "stray": True},
            ),
        ],
    )
    def test_carve_out_extra_key_fails_schema_shape_before_pydantic(self, tool_name: str, arguments: dict[str, Any]) -> None:
        with pytest.raises(ToolArgumentError) as caught:
            require_schema_valid_arguments(tool_name, arguments)
        assert caught.value.code == "SCHEMA_VALIDATION"
        assert caught.value.category is ToolArgumentErrorCategory.SCHEMA_SHAPE
        # Raised by the S gate itself, not wrapped from a pydantic cause.
        assert caught.value.__cause__ is None


# ---------------------------------------------------------------------------
# Pydantic-wrap census: a model rejection must not default to semantic_rule.
# ---------------------------------------------------------------------------


_COMPOSER_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"
_SCANNED_ROOTS = (_COMPOSER_SRC / "web" / "composer", _COMPOSER_SRC / "composer_mcp")


def _pydantic_wraps_without_category(source: str) -> list[int]:
    """Lines of ``ToolArgumentError(...)`` built in an ``except PydanticValidationError`` without ``category=``."""
    missing: list[int] = []
    for handler in ast.walk(ast.parse(source)):
        if not isinstance(handler, ast.ExceptHandler) or handler.type is None:
            continue
        caught = [ast.unparse(node) for node in (handler.type.elts if isinstance(handler.type, ast.Tuple) else [handler.type])]
        if "PydanticValidationError" not in caught:
            continue
        for node in ast.walk(handler):
            if (
                isinstance(node, ast.Call)
                and ast.unparse(node.func) == "ToolArgumentError"
                and not any(keyword.arg == "category" for keyword in node.keywords)
            ):
                missing.append(node.lineno)
    return missing


def _scanned_files() -> list[Path]:
    return [path for root in _SCANNED_ROOTS for path in iter_gate_files(root) if "guided" not in path.relative_to(root).parts]


class TestPydanticWrapCensus:
    def test_instrument_finds_a_planted_uncategorised_wrap(self) -> None:
        planted = (
            "try:\n    Model.model_validate(x)\n"
            "except PydanticValidationError as exc:\n"
            "    raise ToolArgumentError(argument='a', expected='b', actual_type='c') from exc\n"
        )
        assert _pydantic_wraps_without_category(planted) == [4]

    def test_instrument_passes_a_categorised_wrap(self) -> None:
        categorised = (
            "try:\n    Model.model_validate(x)\n"
            "except PydanticValidationError as exc:\n"
            "    raise ToolArgumentError(argument='a', expected='b', actual_type='c', category=C) from exc\n"
        )
        assert _pydantic_wraps_without_category(categorised) == []

    def test_instrument_sees_real_wrap_sites(self) -> None:
        # Known positive: the shared mutation-model wrapper is one such site.
        source = (_COMPOSER_SRC / "web" / "composer" / "tools" / "_common.py").read_text()
        tree = ast.parse(source)
        wraps = [
            node
            for handler in ast.walk(tree)
            if isinstance(handler, ast.ExceptHandler)
            and handler.type is not None
            and "PydanticValidationError" in ast.unparse(handler.type)
            for node in ast.walk(handler)
            if isinstance(node, ast.Call) and ast.unparse(node.func) == "ToolArgumentError"
        ]
        assert wraps, "the census instrument no longer sees _common._validate_mutation_arguments"

    def test_every_pydantic_wrap_names_its_category(self) -> None:
        offenders = {
            str(path.relative_to(_COMPOSER_SRC)): lines
            for path in _scanned_files()
            if (lines := _pydantic_wraps_without_category(path.read_text()))
        }
        assert offenders == {}


# ---------------------------------------------------------------------------
# Producing sites in the compose loop: honest class + closed category, on the
# audit invocation, on the P3 outcome carrier, and in the persisted P4 row.
# ---------------------------------------------------------------------------


def _raw_tool_call_llm(*, name: str, raw_arguments: str) -> _FakeComposeLLM:
    first = _FakeLLMResponse(
        choices=[
            _FakeChoice(
                message=_FakeMessage(
                    content=None,
                    tool_calls=[_FakeToolCall(id="call_raw", function=_FakeFunction(name=name, arguments=raw_arguments))],
                )
            )
        ]
    )
    return _FakeComposeLLM((first, _fake_llm_response(content="Done.")))


_FLAT_PIPELINE = {"source": {"plugin": "csv", "on_success": "rows"}, "nodes": [], "edges": [], "outputs": []}


@pytest.mark.parametrize(
    ("tool_name", "raw_arguments", "error_class", "category"),
    [
        pytest.param("get_pipeline_state", "{", "JSONDecodeError", ToolArgumentErrorCategory.WIRE_JSON_INVALID, id="json-invalid"),
        pytest.param(
            "get_pipeline_state",
            "[" * 2_000 + "0" + "]" * 2_000,
            "JsonBoundaryError",
            ToolArgumentErrorCategory.WIRE_JSON_BOUNDS,
            id="json-bounds",
        ),
        pytest.param(
            "get_pipeline_state", json.dumps([1, 2, 3]), "ToolArgumentError", ToolArgumentErrorCategory.WIRE_NOT_OBJECT, id="not-object"
        ),
        pytest.param(
            "set_pipeline", json.dumps(_FLAT_PIPELINE), "ToolArgumentError", ToolArgumentErrorCategory.WIRE_ENVELOPE, id="envelope"
        ),
        # An integer outside the I-JSON range decodes but does not
        # canonicalise (non-finite constants never get this far: the bounded
        # decoder rejects them). A top-level one takes the non-object gate's
        # canonicalisation arm, a nested one the object path's. Both record
        # the class canonical_json raised.
        pytest.param(
            "set_metadata",
            "9007199254740993",
            "IntegerDomainError",
            ToolArgumentErrorCategory.CANONICALIZATION,
            id="canonicalization-scalar",
        ),
        pytest.param(
            "set_metadata",
            '{"patch": {"name": 9007199254740993}}',
            "IntegerDomainError",
            ToolArgumentErrorCategory.CANONICALIZATION,
            id="canonicalization-object",
        ),
        pytest.param(
            "set_metadata", '{"patch": {"name": NaN}}', "ValueError", ToolArgumentErrorCategory.WIRE_JSON_INVALID, id="non-finite"
        ),
        pytest.param(
            "set_source", json.dumps({}), "ToolArgumentError", ToolArgumentErrorCategory.MISSING_REQUIRED_PATH, id="required-paths"
        ),
        pytest.param(
            "set_metadata",
            json.dumps({"patch": {}, "stray": 1}),
            "ToolArgumentError",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
            id="schema-shape",
        ),
        pytest.param(
            "clear_source", json.dumps({"source_name": ""}), "ToolArgumentError", ToolArgumentErrorCategory.SCHEMA_BOUND, id="schema-bound"
        ),
        pytest.param(
            "request_advisor_hint",
            json.dumps(
                {
                    "trigger": "proactive_security_safety",
                    "problem_summary": "stuck",
                    "recent_errors": [],
                    "attempted_actions": [],
                    "full_context": "extra",
                }
            ),
            "ToolArgumentError",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
            id="advisor-extra-key",
        ),
        pytest.param(
            "request_advisor_hint",
            json.dumps(
                {"trigger": "proactive_security_safety", "problem_summary": "stuck", "recent_errors": [], "attempted_actions": "oops"}
            ),
            "ToolArgumentError",
            ToolArgumentErrorCategory.SCHEMA_SHAPE,
            id="advisor-wrong-type",
        ),
    ],
)
@pytest.mark.asyncio
async def test_compose_loop_site_records_honest_class_and_category(
    fake_composer_service: ComposerServiceImpl,
    result_session_id: str,
    tool_name: str,
    raw_arguments: str,
    error_class: str,
    category: ToolArgumentErrorCategory,
) -> None:
    llm = _raw_tool_call_llm(name=tool_name, raw_arguments=raw_arguments)

    result = await fake_composer_service._run_one_turn_for_test(llm=llm, session_id=result_session_id)

    assert len(result.tool_invocations) == 1
    invocation = result.tool_invocations[0]
    assert invocation.status is ComposerToolStatus.ARG_ERROR
    assert (invocation.error_class, invocation.error_category) == (error_class, category)
    outcome = result.tool_outcomes[0]
    assert (outcome.error_class, outcome.error_category) == (error_class, category)
    persisted = json.loads(fake_composer_service._phase3_last_redacted_tool_rows[-1].content)
    assert persisted["_redaction_status"] == "arg_error"
    assert persisted["error_category"] == category.value
    assert persisted["error_class"] == error_class


def test_advisor_prompt_budget_is_its_own_category() -> None:
    from tests.unit.web.composer._helpers import _make_settings, _mock_catalog

    settings = _make_settings(composer_advisor_max_prompt_tokens=1)
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=settings)
    rejection = service._validate_advisor_arguments(
        {"trigger": "proactive_security_safety", "problem_summary": "x" * 100, "recent_errors": [], "attempted_actions": []}
    )
    assert type(rejection) is AdvisorArgumentRejection
    assert rejection.category is ToolArgumentErrorCategory.PROMPT_BUDGET
    assert rejection.error_class == "ToolArgumentError"


def test_advisor_model_rejection_after_schema_is_model_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    from tests.unit.web.composer._helpers import _make_settings, _mock_catalog

    # The flat schema and the pydantic model agree today, so skip S to reach
    # the model's own rejection arm, which must stay honestly labelled.
    monkeypatch.setattr("elspeth.web.composer.service.require_schema_valid_arguments", lambda _name, _arguments: None)
    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    rejection = service._validate_advisor_arguments({"trigger": "not-a-trigger"})
    assert type(rejection) is AdvisorArgumentRejection
    assert rejection.category is ToolArgumentErrorCategory.MODEL_VALIDATION
    assert rejection.error_class == "ValidationError"


def test_mutation_model_rejection_is_model_validation() -> None:
    from elspeth.web.composer.tools._common import _validate_mutation_arguments
    from elspeth.web.composer.tools.transforms import _UpsertNodeArgumentsModel

    with pytest.raises(ToolArgumentError) as caught:
        _validate_mutation_arguments(_UpsertNodeArgumentsModel, {"id": 3}, "upsert_node arguments")
    assert caught.value.category is ToolArgumentErrorCategory.MODEL_VALIDATION


# ---------------------------------------------------------------------------
# Pipeline planner: category values are rejection codes and are not collapsed.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("category", list(ToolArgumentErrorCategory), ids=lambda category: category.value)
def test_every_category_survives_planner_rejection_code_closure(category: ToolArgumentErrorCategory) -> None:
    from elspeth.contracts.composer_planner_audit import _safe_token
    from elspeth.web.composer.pipeline_planner import _closed_planner_rejection_codes

    assert _closed_planner_rejection_codes((category.value,)) == (category.value,)
    # The attempt record admits only bounded lowercase tokens.
    assert _safe_token(category.value)


def test_planner_rejection_closure_still_collapses_unknown_codes() -> None:
    from elspeth.web.composer.pipeline_planner import _closed_planner_rejection_codes

    assert _closed_planner_rejection_codes(("zz_not_a_known_code", "SCHEMA_VALIDATION")) == ("validation_error",)


# ---------------------------------------------------------------------------
# service.py session_id premise (plan S0): the tool-list builder filters nothing.
# ---------------------------------------------------------------------------


def test_tool_list_builder_does_not_filter_session_aware_tools() -> None:
    names = {tool["function"]["name"] for tool in composer_loop_tool_definitions()}
    assert "request_interpretation_review" in names


@pytest.mark.asyncio
async def test_missing_session_id_invariant_names_the_real_guard() -> None:
    """The unreachable no-session branch must not claim a tool-list filter exists."""
    from unittest.mock import MagicMock

    from elspeth.web.catalog.policy_view import PolicyCatalogView
    from elspeth.web.composer.anti_anchor import AntiAnchorTracker
    from elspeth.web.composer.audit import BufferingRecorder, DispatchAudit
    from tests.unit.web.composer._helpers import _make_settings, _mock_catalog

    service = ComposerServiceImpl.for_trained_operator(catalog=_mock_catalog(), settings=_make_settings())
    with pytest.raises(RuntimeError) as caught:
        await service._dispatch_session_aware_tool(
            tool_name="request_interpretation_review",
            tool_call_id="call_1",
            arguments={},
            state=_empty_state(),
            audit=MagicMock(spec=DispatchAudit),
            recorder=MagicMock(spec=BufferingRecorder),
            session_id=None,
            current_state_id=None,
            composer_model_version="m",
            llm_messages=[],
            anti_anchor=MagicMock(spec=AntiAnchorTracker),
            policy_catalog=MagicMock(spec=PolicyCatalogView),
        )
    message = str(caught.value)
    assert "composer_loop_tool_definitions" not in message
    assert "COMPOSE session authority" in message


def test_every_closed_code_has_a_category_rule() -> None:
    """A new ToolArgumentError code must be given its category, not hit a KeyError."""
    from elspeth.web.composer.protocol import _TOOL_ARGUMENT_CATEGORY_BY_CODE, _TOOL_ARGUMENT_ERROR_CODES

    assert {"SCHEMA_VALIDATION", *_TOOL_ARGUMENT_CATEGORY_BY_CODE} == _TOOL_ARGUMENT_ERROR_CODES
