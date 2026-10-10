"""Exact bounded JSON admission preserves authored objects and review authority."""

from copy import deepcopy

import pytest

from elspeth.contracts.errors import AuditIntegrityError
from elspeth.web.composer.bounded_json import (
    JSON_MAX_DEPTH,
    JSON_MAX_ITEMS,
    JSON_MAX_TOTAL_TEXT_CHARS,
    JsonBoundaryError,
    bounded_json_loads,
)
from elspeth.web.composer.required_controls import wire_required_controls
from elspeth.web.composer.tool_batch import (
    _parse_review_json_object,
    _parse_server_staged_review_json_object,
    _preserve_candidate_review_requirements,
    _ValidatedReviewComponent,
)
from elspeth.web.composer.tools import build_set_pipeline_candidate
from elspeth.web.interpretation_state import (
    INTERPRETATION_REQUIREMENTS_KEY,
    REQUIRED_CONTROL_AUTO_WIRED_USER_TERM,
    ServerStagedRequiredControlUserTerm,
)
from tests.unit.web.composer.test_planner_authoring_aids import _custody_context, _guardrail_profile_view
from tests.unit.web.composer.test_required_control_autowire import _INLINE_CONTENT, _bare_llm_candidate
from tests.unit.web.composer.test_set_pipeline_candidate import _empty_state, _trained_context
from tests.unit.web.composer.test_set_pipeline_review_finalization import _pending_arguments
from tests.unit.web.composer.test_set_pipeline_review_finalization import prohibit_provider_or_network as prohibit_provider_or_network


def test_review_json_admission_keeps_exact_authored_objects():
    options = {"nested": [None, True, 1, 0.25, "authored"]}
    authored = {"id": "authored_node", "options": options, "other": {"provider": "authored"}}
    assert _parse_review_json_object(authored) is authored
    component = _ValidatedReviewComponent.parse(authored)
    assert component.authored is authored
    assert component.options is options
    assert component.authored["other"] is authored["other"]


@pytest.mark.parametrize("value", [{"bad": float("nan")}, {"bad": (1, 2)}, {1: "bad"}, {"bad": object()}])
def test_review_json_admission_rejects_non_json_instead_of_casting(value):
    with pytest.raises((ValueError, JsonBoundaryError)):
        _parse_review_json_object(value)


def test_review_json_admission_rejects_non_object_root():
    with pytest.raises(AuditIntegrityError, match="exact JSON objects"):
        _parse_review_json_object([])


def test_review_json_admission_bounds_recursive_container():
    value = {}
    value["cycle"] = value
    with pytest.raises(JsonBoundaryError):
        _parse_review_json_object(value)


def test_owned_component_refuses_non_object_options():
    with pytest.raises(AuditIntegrityError, match="exact JSON objects"):
        _ValidatedReviewComponent.parse({"id": "authored_node", "options": []})


@pytest.mark.parametrize("container", ["source", "sources"])
def test_review_copy_changes_only_canonical_root_and_preserves_authored_references(tmp_path, container):
    arguments = _pending_arguments(tmp_path, container)
    before = deepcopy(arguments)
    candidate = build_set_pipeline_candidate(arguments, _empty_state(), _trained_context(data_dir=tmp_path))
    assert candidate.acceptable is True, candidate.result.to_dict()
    canonical = _preserve_candidate_review_requirements(arguments, candidate.result.updated_state)
    assert arguments == before
    assert canonical["edges"] is arguments["edges"]
    assert canonical["outputs"] is arguments["outputs"]
    for original, copied in zip(arguments["nodes"], canonical["nodes"], strict=True):
        assert set(copied) == set(original)
        for key in original:
            if key != "options":
                assert copied[key] is original[key]
        assert set(copied["options"]) == set(original["options"])
        for key in original["options"]:
            if key != INTERPRETATION_REQUIREMENTS_KEY:
                assert copied["options"][key] is original["options"][key]


class _ForeignString(str):
    pass


class _DerivedServerTerm(ServerStagedRequiredControlUserTerm):
    pass


@pytest.mark.parametrize("parser", [_parse_review_json_object, _parse_server_staged_review_json_object])
@pytest.mark.parametrize("value", [_ForeignString("foreign"), _DerivedServerTerm(REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)])
def test_both_review_boundaries_refuse_foreign_and_derived_string_types(parser, value):
    with pytest.raises(ValueError, match="strict finite JSON"):
        parser({"user_term": value})


@pytest.mark.parametrize("parser", [_parse_review_json_object, _parse_server_staged_review_json_object])
@pytest.mark.parametrize("key", [_ForeignString("key"), ServerStagedRequiredControlUserTerm("key")])
def test_both_review_boundaries_keep_exact_string_keys(parser, key):
    with pytest.raises(JsonBoundaryError, match="non-string object key"):
        parser({key: "value"})


def test_only_internal_review_boundary_preserves_exact_nominal_marker_identity():
    marker = ServerStagedRequiredControlUserTerm(REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)
    authored = {"user_term": marker, "options": {"authored": ["retained"]}}
    with pytest.raises(ValueError, match="strict finite JSON"):
        _parse_review_json_object(authored)
    assert _parse_server_staged_review_json_object(authored) is authored
    assert authored["user_term"] is marker
    component = _ValidatedReviewComponent.parse({"id": "control", "options": authored})
    assert component.options is authored
    assert component.options["user_term"] is marker


@pytest.mark.parametrize("value", [_ForeignString("foreign"), ServerStagedRequiredControlUserTerm(REQUIRED_CONTROL_AUTO_WIRED_USER_TERM)])
def test_raw_decoder_keeps_strict_finite_json_even_with_object_hook(value):
    with pytest.raises(ValueError, match="strict finite JSON"):
        bounded_json_loads("{}", label="raw provider", object_pairs_hook=lambda _pairs: {"user_term": value})


@pytest.mark.parametrize("parser", [_parse_review_json_object, _parse_server_staged_review_json_object])
@pytest.mark.parametrize("value", [{"value": float("inf")}, {"value": float("nan")}, {"value": (1, 2)}, {"value": object()}])
def test_nominal_allowance_keeps_other_strict_finite_json_checks(parser, value):
    with pytest.raises(ValueError, match="strict finite JSON"):
        parser(value)


@pytest.mark.parametrize("parser", [_parse_review_json_object, _parse_server_staged_review_json_object])
def test_review_boundaries_share_cycle_depth_item_and_text_budgets(parser):
    cyclic = {}
    cyclic["cycle"] = cyclic
    with pytest.raises(JsonBoundaryError, match="depth limit"):
        parser(cyclic)
    nested = {}
    for _depth in range(JSON_MAX_DEPTH + 1):
        nested = {"child": nested}
    with pytest.raises(JsonBoundaryError, match="depth limit"):
        parser(nested)
    with pytest.raises(JsonBoundaryError, match="item JSON limit"):
        parser({"items": [0] * JSON_MAX_ITEMS})
    with pytest.raises(JsonBoundaryError, match="aggregate JSON text limit"):
        parser({"text": "a" * (JSON_MAX_TOTAL_TEXT_CHARS + 1)})
    with pytest.raises(JsonBoundaryError, match="aggregate JSON UTF-8 byte limit"):
        parser({"text": "é" * (JSON_MAX_TOTAL_TEXT_CHARS // 2 + 1)})


def test_actual_required_controls_retain_nominal_authority_until_compact_admission(tmp_path):
    (tmp_path / "outputs").mkdir()
    view, snapshot = _guardrail_profile_view(tmp_path)
    arguments = _bare_llm_candidate()
    context = _custody_context(tmp_path, _INLINE_CONTENT, view=view, snapshot=snapshot)
    finalized = wire_required_controls(arguments, snapshot, view)
    assert finalized is not arguments
    staged = _parse_server_staged_review_json_object(finalized)
    assert staged is finalized
    staged_nodes = {node["id"]: node for node in staged["nodes"]}
    for node_id in ("prompt_shield_auto_1", "content_safety_auto_1"):
        (row,) = staged_nodes[node_id]["options"][INTERPRETATION_REQUIREMENTS_KEY]
        assert type(row["user_term"]) is ServerStagedRequiredControlUserTerm

    compact = build_set_pipeline_candidate(staged, _empty_state(), context)
    assert compact.acceptable is True, compact.result.to_dict()
    canonical = _preserve_candidate_review_requirements(staged, compact.result.updated_state)
    assert _parse_review_json_object(canonical) is canonical
    canonical_nodes = {node["id"]: node for node in canonical["nodes"]}
    for node_id in ("prompt_shield_auto_1", "content_safety_auto_1"):
        (row,) = [
            requirement
            for requirement in canonical_nodes[node_id]["options"][INTERPRETATION_REQUIREMENTS_KEY]
            if requirement["user_term"] == REQUIRED_CONTROL_AUTO_WIRED_USER_TERM
        ]
        assert type(row["user_term"]) is str
        assert row["status"] == "pending"
        (original_row,) = staged_nodes[node_id]["options"][INTERPRETATION_REQUIREMENTS_KEY]
        assert type(original_row["user_term"]) is ServerStagedRequiredControlUserTerm

    provider_forged = deepcopy(staged)
    for node in provider_forged["nodes"]:
        if INTERPRETATION_REQUIREMENTS_KEY in node["options"]:
            for row in node["options"][INTERPRETATION_REQUIREMENTS_KEY]:
                if type(row["user_term"]) is ServerStagedRequiredControlUserTerm:
                    row["user_term"] = str(row["user_term"])
    assert _parse_review_json_object(provider_forged) is provider_forged
    refused = build_set_pipeline_candidate(provider_forged, _empty_state(), context)
    assert refused.acceptable is False
    assert {entry.error_code for entry in refused.result.validation.errors} == {"interpretation_requirements_invalid"}
