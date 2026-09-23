"""Registered ``error_code`` values survive redaction; nothing else does.

A plugin-option rejection is a SUCCESS dispatch whose ``validation.errors[]``
entry carries a closed, server-authored ``error_code``. Before S0 the redactor
summarized that code to ``<redacted-response-text>``, so a persisted rejection
recorded *that* it was refused but not *why* (plan §2.3). The closed registry
``composer/error_codes.py`` is the allowlist; this file pins its membership
against every producer and against the guidance catalogue.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from pathlib import Path

import pytest

from elspeth.contracts.composer_audit import ToolArgumentErrorCategory
from elspeth.web.composer.error_codes import REGISTERED_ERROR_CODES
from elspeth.web.composer.redaction import redact_arg_error_response, redact_tool_call_response
from elspeth.web.composer.redaction_telemetry import NoopRedactionTelemetry
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools._common import _failure_result
from elspeth.web.composer.tools.generation import _VALIDATION_GUIDANCE_BY_CODE
from elspeth.web.execution.schemas import ADVISOR_SIGNOFF_BLOCKED_CODE

_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"
_COMPOSER = _SRC / "web" / "composer"
_REDACTED_TEXT = "<redacted-response-text>"

# ---------------------------------------------------------------------------
# Producer census
# ---------------------------------------------------------------------------

# ``ValidationEntry`` (aliased ``_err`` in state.py) takes ``error_code`` as its
# fourth positional argument.
_POSITIONAL_CODE_CALLS = frozenset({"ValidationEntry", "_err"})
# The session-store rejection record copies an outcome's class or its first
# entry's code; it is not a response producer.
_NOT_A_PRODUCER = frozenset({"RejectionRecord"})

# Sites whose ``error_code`` is not a literal: each forwards a code produced
# (and so censused) elsewhere, or reads a closed enum the registry includes.
# Keyed by (file under web/composer, enclosing function, expression). A new
# non-literal site fails the census until it is reviewed and added here.
_REVIEWED_FORWARDERS: frozenset[tuple[str, str, str]] = frozenset(
    {
        # Parameter forwarding inside the rejection builders.
        ("tools/_common.py", "_failure_result", "error_code"),
        ("tools/_common.py", "_prepend_rejection_entry", "error_code"),
        ("tools/_common.py", "_rejection_only_validation", "error_code"),
        ("tools/sessions.py", "_failure_result", "error_code"),
        ("pipeline_planner.py", "_candidate_policy_rejection", "error_code"),
        ("state.py", "add", "code"),
        # PluginUnavailableReason members; the registry holds every value.
        ("tools/_common.py", "_plugin_policy_failure", "violation.error_code.value"),
        ("tools/_common.py", "_validate_plugin_name", "PluginUnavailableReason.NOT_INSTALLED"),
        ("tools/_common.py", "_validate_plugin_name", "PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING"),
        ("tools/_common.py", "_validate_plugin_name", "reason"),
        # A code read back from a state-validation entry, directly or through
        # ``_post_mutation_invariant_error`` / ``_row_union_node_contract_error``
        # (both return an entry's own ``error_code``), or a local chosen from
        # literals (``transforms.py`` / ``sessions.py`` ``"interpretation_
        # requirements_invalid" if ... else None``; ``review_contract_code``).
        ("tools/outputs.py", "_execute_set_output", "error_code"),
        ("tools/sessions.py", "build_set_pipeline_candidate", "error_code"),
        ("tools/sessions.py", "build_set_pipeline_candidate", "review_contract_code"),
        ("tools/transforms.py", "_execute_upsert_node", "error_code"),
        ("tools/transforms.py", "_execute_upsert_edge", "error_code"),
        ("tools/transforms.py", "_execute_patch_node_options", "error_code"),
        ("tools/transforms.py", "_prepare_transform_candidate", "error_code"),
        ("service.py", "_state_payload_for_compose_turn", "error.error_code"),
        ("pipeline_planner.py", "_build_valid_pipeline_plan", "exc.error_code"),
        # The imported ``ADVISOR_SIGNOFF_BLOCKED_CODE`` (pinned below).
        ("service.py", "_advisor_signoff_fully_blocking_validation", "_ADVISOR_SIGNOFF_BLOCKED_CODE"),
        # Planner / discovery feedback that projects an already-produced code,
        # a closed ``ToolArgumentError.code`` or a closed category value.
        ("pipeline_planner.py", "_allowlisted_candidate_feedback", "code"),
        ("pipeline_planner.py", "_binding_rejection_feedback", "rejection.error_code"),
        ("pipeline_planner.py", "_plan_pipeline_inner", "entry.error_code or 'validation_error'"),
        ("pipeline_planner.py", "_allowlisted_argument_error_entry", "error.code or 'argument_error'"),
        ("pipeline_planner.py", "execute_one_discovery", "exc.category.value"),
        ("pipeline_commit.py", "prepare_pipeline_proposal_commit", "exc.category.value"),
        ("provider_discovery_response.py", "to_wire", "code"),
        ("provider_discovery_response.py", "to_wire", "self.error_code"),
        ("tools/generation.py", "_execute_explain_validation_error", "code"),
        # Not a producer: the redaction allowlist entry for the field itself.
        ("redaction.py", "<module>", "REGISTERED_ERROR_CODES"),
    }
)


@dataclass(frozen=True, slots=True)
class _CodeSite:
    path: str
    function: str
    expression: str
    literals: tuple[str, ...]
    is_literal: bool


def _module_str_constants(tree: ast.Module) -> dict[str, str]:
    constants: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, ast.Assign):
            targets: list[ast.expr] = list(node.targets)
        elif isinstance(node, ast.AnnAssign):
            targets = [node.target]
        else:
            continue
        if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
            for target in targets:
                if isinstance(target, ast.Name):
                    constants[target.id] = node.value.value
    return constants


def _is_literal(expression: ast.expr, constants: dict[str, str]) -> bool:
    if isinstance(expression, ast.Constant):
        return True
    if isinstance(expression, ast.Name):
        return expression.id in constants
    if isinstance(expression, ast.IfExp):
        return _is_literal(expression.body, constants) and _is_literal(expression.orelse, constants)
    return False


def _code_sites(relative_path: str, source: str) -> list[_CodeSite]:
    """Every ``error_code`` value a module passes to a producer."""
    tree = ast.parse(source)
    constants = _module_str_constants(tree)
    sites: list[_CodeSite] = []

    def visit(node: ast.AST, function: str) -> None:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            function = node.name
        expressions: list[ast.expr] = []
        if isinstance(node, ast.Call):
            callee = ast.unparse(node.func).rsplit(".", 1)[-1]
            if callee not in _NOT_A_PRODUCER:
                expressions.extend(keyword.value for keyword in node.keywords if keyword.arg == "error_code")
                if callee in _POSITIONAL_CODE_CALLS and len(node.args) >= 4:
                    expressions.append(node.args[3])
        elif isinstance(node, ast.Dict):
            expressions.extend(
                value
                for key, value in zip(node.keys, node.values, strict=True)
                if isinstance(key, ast.Constant) and key.value == "error_code"
            )
        for expression in expressions:
            literals = [
                sub.value if isinstance(sub, ast.Constant) else constants[sub.id]
                for sub in ast.walk(expression)
                if (isinstance(sub, ast.Constant) and isinstance(sub.value, str)) or (isinstance(sub, ast.Name) and sub.id in constants)
            ]
            sites.append(
                _CodeSite(
                    path=relative_path,
                    function=function,
                    expression=ast.unparse(expression),
                    literals=tuple(literals),
                    is_literal=_is_literal(expression, constants),
                )
            )
        for child in ast.iter_child_nodes(node):
            visit(child, function)

    visit(tree, "<module>")
    return sites


def _live_code_sites() -> list[_CodeSite]:
    sites: list[_CodeSite] = []
    for path in sorted(_COMPOSER.rglob("*.py")):
        relative = path.relative_to(_COMPOSER)
        if "guided" in relative.parts:
            continue
        sites.extend(_code_sites(relative.as_posix(), path.read_text()))
    return sites


def _unregistered_literals(sites: list[_CodeSite]) -> set[tuple[str, str, str]]:
    return {(site.path, site.function, code) for site in sites for code in site.literals if code not in REGISTERED_ERROR_CODES}


def _unreviewed_forwarders(sites: list[_CodeSite]) -> set[tuple[str, str, str]]:
    return {(site.path, site.function, site.expression) for site in sites if not site.is_literal} - _REVIEWED_FORWARDERS


class TestRegistryCensus:
    def test_instrument_sees_known_producers(self) -> None:
        sites = _live_code_sites()
        codes = {code for site in sites for code in site.literals}
        # Keyword producer, positional ``_err`` producer, and dict-key producer.
        assert "plugin_options_invalid" in codes
        assert "pipeline_cycle" in codes
        assert "deferred_intent_claim" in codes

    def test_instrument_flags_a_planted_unregistered_code(self) -> None:
        planted = "def f(state):\n    return _failure_result(state, 'm', error_code='zz_planted_unregistered')\n"
        assert _unregistered_literals(_code_sites("planted.py", planted)) == {("planted.py", "f", "zz_planted_unregistered")}

    def test_instrument_flags_a_planted_positional_code(self) -> None:
        planted = "_err = ValidationEntry\ndef f():\n    return _err('c', 'm', 'high', 'zz_positional_unregistered')\n"
        assert _unregistered_literals(_code_sites("planted.py", planted)) == {("planted.py", "f", "zz_positional_unregistered")}

    def test_instrument_flags_a_planted_unreviewed_forwarder(self) -> None:
        planted = "def f(state, code):\n    return _failure_result(state, 'm', error_code=code)\n"
        assert _unreviewed_forwarders(_code_sites("planted.py", planted)) == {("planted.py", "f", "code")}

    def test_every_emitted_literal_code_is_registered(self) -> None:
        assert _unregistered_literals(_live_code_sites()) == set()

    def test_every_non_literal_code_site_is_a_reviewed_forwarder(self) -> None:
        assert _unreviewed_forwarders(_live_code_sites()) == set()

    def test_reviewed_forwarders_all_still_exist(self) -> None:
        live = {(site.path, site.function, site.expression) for site in _live_code_sites() if not site.is_literal}
        assert _REVIEWED_FORWARDERS - live == set()

    def test_guidance_catalogue_is_a_subset_of_the_registry(self) -> None:
        assert set(_VALIDATION_GUIDANCE_BY_CODE) - REGISTERED_ERROR_CODES == set()

    def test_subset_pin_detects_an_unregistered_catalogue_key(self) -> None:
        catalogue = {*_VALIDATION_GUIDANCE_BY_CODE, "zz_catalogue_only"}
        assert catalogue - REGISTERED_ERROR_CODES == {"zz_catalogue_only"}

    def test_imported_advisor_signoff_code_is_registered(self) -> None:
        assert ADVISOR_SIGNOFF_BLOCKED_CODE in REGISTERED_ERROR_CODES

    def test_every_category_is_registered(self) -> None:
        assert {category.value for category in ToolArgumentErrorCategory} <= REGISTERED_ERROR_CODES

    def test_every_tool_argument_error_code_is_registered(self) -> None:
        # Forwarded as ``error_code`` by the planner and discovery feedback.
        from elspeth.web.composer.protocol import _TOOL_ARGUMENT_ERROR_CODES

        assert _TOOL_ARGUMENT_ERROR_CODES <= REGISTERED_ERROR_CODES

    def test_every_plugin_unavailable_reason_is_registered(self) -> None:
        from elspeth.web.plugin_policy.models import PluginUnavailableReason

        assert {reason.value for reason in PluginUnavailableReason} <= REGISTERED_ERROR_CODES


# ---------------------------------------------------------------------------
# Redaction behaviour
# ---------------------------------------------------------------------------


def _empty_state() -> CompositionState:
    return CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)


def _persisted_first_error(tool_name: str, error_code: str) -> dict[str, object]:
    result = _failure_result(_empty_state(), "Invalid option 'secretvalue'", error_code=error_code)
    redacted = redact_tool_call_response(tool_name, result.to_dict(), telemetry=NoopRedactionTelemetry())
    errors = redacted["validation"]["errors"]
    assert isinstance(errors, list)
    first = errors[0]
    assert isinstance(first, dict)
    return first


@pytest.mark.parametrize(
    ("tool_name", "error_code"),
    [
        ("set_source", "plugin_options_invalid"),
        ("upsert_node", "plugin_options_invalid"),
        ("set_output", "plugin_options_invalid"),
        ("set_pipeline", "plugin_options_invalid"),
        ("patch_node_options", "prompt_template_parts_required"),
    ],
)
def test_registered_code_survives_response_redaction(tool_name: str, error_code: str) -> None:
    entry = _persisted_first_error(tool_name, error_code)
    assert entry["error_code"] == error_code
    # The message stays redacted: only the closed code crosses.
    assert entry["message"] != "Invalid option 'secretvalue'"


@pytest.mark.parametrize("tool_name", ["set_source", "upsert_node", "set_output", "set_pipeline", "patch_node_options"])
def test_unregistered_code_is_still_redacted(tool_name: str) -> None:
    entry = _persisted_first_error(tool_name, "zz_not_a_registered_code")
    assert entry["error_code"] == _REDACTED_TEXT


def test_arg_error_projection_persists_the_category() -> None:
    projection = redact_arg_error_response(
        error_class="ToolArgumentError",
        error_category=ToolArgumentErrorCategory.SCHEMA_SHAPE,
        error_message="m",
    )
    assert projection["error_category"] == "schema_shape"
    assert projection["error_class"] == "ToolArgumentError"
