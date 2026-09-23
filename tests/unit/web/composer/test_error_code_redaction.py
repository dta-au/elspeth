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
from tests.helpers.tree_gate import iter_gate_files

_SRC = Path(__file__).resolve().parents[4] / "src" / "elspeth"
_WEB = _SRC / "web"
# The census covers every web module whose ``error_code`` can reach a composer
# tool response: the composer itself, and the plugin-policy findings and
# execution validation it embeds (``validate_composition_state`` ->
# ``validate_authored_composition_state`` and ``ToolResult.runtime_preflight``).
# It walks all of ``web/`` and excludes only what cannot, each with its reason.
_CENSUS_EXCLUDED: dict[tuple[str, ...], str] = {
    ("composer", "guided"): "guided mode is being removed; out of scope for the strict-contract campaign",
    ("_acceptance_common",): "deployment acceptance HTTP client; its codes are probe results, never a tool response",
    ("_aws_ecs_acceptance",): "deployment acceptance client (AWS ECS)",
    ("_azure_container_apps_acceptance",): "deployment acceptance client (Azure Container Apps)",
    ("aws_ecs_acceptance.py",): "deployment acceptance entry point (AWS ECS)",
    ("azure_container_apps_acceptance.py",): "deployment acceptance entry point (Azure Container Apps)",
}
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
        ("composer/tools/_common.py", "_failure_result", "error_code"),
        ("composer/tools/_common.py", "_prepend_rejection_entry", "error_code"),
        ("composer/tools/_common.py", "_rejection_only_validation", "error_code"),
        ("composer/tools/sessions.py", "_failure_result", "error_code"),
        ("composer/pipeline_planner.py", "_candidate_policy_rejection", "error_code"),
        ("composer/state.py", "add", "code"),
        # PluginUnavailableReason members; the registry holds every value.
        ("composer/tools/_common.py", "_plugin_policy_failure", "violation.error_code.value"),
        ("composer/tools/_common.py", "_validate_plugin_name", "PluginUnavailableReason.NOT_INSTALLED"),
        ("composer/tools/_common.py", "_validate_plugin_name", "PluginUnavailableReason.LOCAL_REQUIREMENT_MISSING"),
        ("composer/tools/_common.py", "_validate_plugin_name", "reason"),
        # A code read back from a state-validation entry, directly or through
        # ``_post_mutation_invariant_error`` / ``_row_union_node_contract_error``
        # (both return an entry's own ``error_code``), or a local chosen from
        # literals (``transforms.py`` / ``sessions.py`` ``"interpretation_
        # requirements_invalid" if ... else None``; ``review_contract_code``).
        ("composer/tools/outputs.py", "_execute_set_output", "error_code"),
        ("composer/tools/sessions.py", "build_set_pipeline_candidate", "error_code"),
        ("composer/tools/sessions.py", "build_set_pipeline_candidate", "review_contract_code"),
        ("composer/tools/transforms.py", "_execute_upsert_node", "error_code"),
        ("composer/tools/transforms.py", "_execute_upsert_edge", "error_code"),
        ("composer/tools/transforms.py", "_execute_patch_node_options", "error_code"),
        ("composer/tools/transforms.py", "_prepare_transform_candidate", "error_code"),
        ("composer/service.py", "_state_payload_for_compose_turn", "error.error_code"),
        ("composer/pipeline_planner.py", "_build_valid_pipeline_plan", "exc.error_code"),
        # The imported ``ADVISOR_SIGNOFF_BLOCKED_CODE`` (pinned below).
        ("composer/service.py", "_advisor_signoff_fully_blocking_validation", "_ADVISOR_SIGNOFF_BLOCKED_CODE"),
        # Planner / discovery feedback that projects an already-produced code,
        # a closed ``ToolArgumentError.code`` or a closed category value.
        ("composer/pipeline_planner.py", "_allowlisted_candidate_feedback", "code"),
        ("composer/pipeline_planner.py", "_binding_rejection_feedback", "rejection.error_code"),
        ("composer/pipeline_planner.py", "_plan_pipeline_inner", "entry.error_code or 'validation_error'"),
        ("composer/pipeline_planner.py", "_allowlisted_argument_error_entry", "error.code or 'argument_error'"),
        ("composer/pipeline_planner.py", "execute_one_discovery", "exc.category.value"),
        ("composer/pipeline_commit.py", "prepare_pipeline_proposal_commit", "exc.category.value"),
        ("composer/provider_discovery_response.py", "to_wire", "code"),
        ("composer/provider_discovery_response.py", "to_wire", "self.error_code"),
        ("composer/tools/generation.py", "_execute_explain_validation_error", "code"),
        # Not a producer: the redaction allowlist entry for the field itself.
        ("composer/redaction.py", "<module>", "REGISTERED_ERROR_CODES"),
        # --- Outside web/composer ---
        # Plugin-policy findings: the finding's own (censused) code, and the
        # PluginUnavailableReason members the registry holds.
        ("plugin_policy/validation.py", "_validation_entry", "finding.error_code"),
        ("plugin_policy/validation.py", "validate_plugin_policy", "reason.value"),
        ("sessions/routes/composer/state.py", "_reject_imported_plugin_policy", "reason.value"),
        # Execution validation: a policy finding's code, a local chosen from two
        # registered literals, imported interpretation-state constants, the
        # settings-reframe table, the closed inline-blob category, and the
        # closed source-proof blocker set (each pinned below).
        ("execution/_validation_authoring.py", "lower_plugin_policy", "item.error_code"),
        ("execution/_validation_authoring.py", "validate_web_network_policy", "error_code"),
        ("execution/_validation_authoring.py", "review_interpretations", "INTERPRETATION_REVIEW_PENDING_CODE"),
        ("execution/validation.py", "_interpretation_review_drift_failure", "INTERPRETATION_REVIEW_DRIFT_CODE"),
        ("execution/_validation_diagnostics.py", "_reframe_settings_missing_parts", "_SETTINGS_MISSING_PART_REFRAMES[part][0]"),
        ("execution/_validation_materialization.py", "_blob_inline_validation_error", "f'{violation.category}_inline_blob_content'"),
        ("execution/service.py", "_merge_authoritative_proof_diagnostics", "code"),
        # Route projections and persistence of codes produced (and censused)
        # elsewhere; none of them authors a code.
        ("execution/routes.py", "execute_pipeline", "err.error_code"),
        ("coordination/repository.py", "record_composition_rejection", "error_code"),
        ("sessions/protocol.py", "decode_stored_composition_validation_errors", "error_code"),
        ("sessions/protocol.py", "serialize_composition_validation_error", "value.error_code"),
        ("sessions/routes/_helpers.py", "_composer_persisted_validation", "error.error_code"),
        ("sessions/routes/_helpers.py", "_message_response", "rejection_record.error_code"),
        ("sessions/routes/_helpers.py", "_validation_entry_responses", "e.error_code"),
        ("sessions/routes/workflow/approvals.py", "request_approval", "error.error_code"),
        ("sessions/service.py", "_sync", "error.error_code"),
        ("sessions/service.py", "_validate_patched_composition_state", "error.error_code"),
        ("sessions/service.py", "list_composition_rejection_events", "row.error_code"),
        ("sessions/service.py", "persist_compose_turn", "record.error_code"),
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


def _walk_outside_fstrings(expression: ast.expr) -> list[ast.AST]:
    """``ast.walk`` that does not descend into f-strings.

    An f-string's constant fragments are not codes (``f"{category}_suffix"``
    would otherwise report ``"_suffix"``); the whole f-string is a non-literal
    site and must be a reviewed forwarder.
    """
    nodes: list[ast.AST] = []
    pending: list[ast.AST] = [expression]
    while pending:
        node = pending.pop()
        nodes.append(node)
        if not isinstance(node, ast.JoinedStr):
            pending.extend(ast.iter_child_nodes(node))
    return nodes


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
                for sub in _walk_outside_fstrings(expression)
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


def _is_excluded(relative: Path) -> bool:
    return any(relative.parts[: len(prefix)] == prefix for prefix in _CENSUS_EXCLUDED)


def _live_code_sites() -> list[_CodeSite]:
    sites: list[_CodeSite] = []
    for path in iter_gate_files(_WEB):
        relative = path.relative_to(_WEB)
        if _is_excluded(relative):
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
        # Producers outside ``web/composer`` whose codes reach tool responses.
        assert "profile_alias_used_as_bucket" in codes
        assert "fabricated_secret" in codes

    def test_exclusions_skip_only_what_they_name(self) -> None:
        paths = {site.path for site in _live_code_sites()}
        assert not any(path.startswith(("composer/guided/", "_acceptance_common/")) for path in paths)
        assert "plugin_policy/validation.py" in paths
        assert "execution/_validation_authoring.py" in paths
        assert _is_excluded(Path("composer/guided/x.py"))
        assert not _is_excluded(Path("composer/guided_x.py"))

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

    def test_forwarded_execution_code_sources_are_registered(self) -> None:
        """The closed sources behind the execution forwarders above."""
        from typing import get_args

        from elspeth.contracts.blobs_inline import BlobInlineValidationCategory
        from elspeth.web.execution._validation_diagnostics import _SETTINGS_MISSING_PART_REFRAMES
        from elspeth.web.interpretation_state import INTERPRETATION_REVIEW_DRIFT_CODE, INTERPRETATION_REVIEW_PENDING_CODE

        assert {INTERPRETATION_REVIEW_PENDING_CODE, INTERPRETATION_REVIEW_DRIFT_CODE} <= REGISTERED_ERROR_CODES
        assert {reframe[0] for reframe in _SETTINGS_MISSING_PART_REFRAMES.values()} <= REGISTERED_ERROR_CODES
        assert {f"{category}_inline_blob_content" for category in get_args(BlobInlineValidationCategory)} <= REGISTERED_ERROR_CODES
        assert {"web_scrape_private_network_not_allowed", "web_fetch_private_network_not_allowed"} <= REGISTERED_ERROR_CODES

    def test_every_blocking_proof_diagnostic_code_is_registered(self) -> None:
        from elspeth.web.composer.tools.generation import _BLOCKING_DIAGNOSTIC_CODES

        assert _BLOCKING_DIAGNOSTIC_CODES <= REGISTERED_ERROR_CODES

    def test_instrument_does_not_read_fstring_fragments_as_codes(self) -> None:
        planted = "def f(state, c):\n    return _failure_result(state, 'm', error_code=f'{c}_zz_suffix')\n"
        sites = _code_sites("planted.py", planted)
        assert _unregistered_literals(sites) == set()
        assert _unreviewed_forwarders(sites) == {("planted.py", "f", "f'{c}_zz_suffix'")}

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
        ("patch_source_options", "plugin_options_invalid"),
        ("set_source_from_blob", "plugin_options_invalid"),
        ("set_source_from_blobs", "plugin_options_invalid"),
        ("splice_transform", "plugin_options_invalid"),
        ("patch_output_options", "plugin_options_invalid"),
        ("patch_node_options", "plugin_options_invalid"),
        ("patch_node_options", "prompt_template_parts_required"),
        # Plugin-policy and execution-validation codes (produced outside
        # web/composer) on option tools.
        ("upsert_node", "profile_alias_used_as_bucket"),
        ("upsert_node", "required_control_unavailable"),
        ("set_pipeline", "required_control_coverage"),
        ("patch_node_options", "llm_base_url_not_allowed"),
        ("set_source", "fabricated_secret"),
    ],
)
def test_registered_code_survives_response_redaction(tool_name: str, error_code: str) -> None:
    entry = _persisted_first_error(tool_name, error_code)
    assert entry["error_code"] == error_code
    # The message stays redacted: only the closed code crosses.
    assert entry["message"] != "Invalid option 'secretvalue'"


# The 10 option-bearing tools (plan §1.2): R1 is decided on their rejection codes.
_OPTION_TOOLS = (
    "set_source",
    "patch_source_options",
    "set_source_from_blob",
    "set_source_from_blobs",
    "set_pipeline",
    "upsert_node",
    "splice_transform",
    "patch_node_options",
    "set_output",
    "patch_output_options",
)


@pytest.mark.parametrize("tool_name", _OPTION_TOOLS)
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
