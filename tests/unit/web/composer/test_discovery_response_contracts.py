"""Selected discovery responses admit owned values before serialization."""

from __future__ import annotations

import json
from dataclasses import fields, replace
from pathlib import Path
from types import MappingProxyType
from unittest.mock import MagicMock

import pytest
from pydantic import BaseModel

from elspeth.contracts.errors import FrameworkBugError
from elspeth.web.catalog.protocol import CatalogService
from elspeth.web.composer.guided.planning import guided_redacted_current_state_context
from elspeth.web.composer.pipeline_planner import _ParsedToolCall, _serialize_provider_discovery_result
from elspeth.web.composer.pipeline_proposal import PlannerSurface
from elspeth.web.composer.state import CompositionState, PipelineMetadata
from elspeth.web.composer.tools import ToolContext, ToolResult
from elspeth.web.composer.tools._registry import _REGISTERED_TOOLS
from elspeth.web.composer.tools.declarations import ToolDeclaration, ToolKind
from elspeth.web.plugin_policy.models import PluginAvailabilitySnapshot

TOOLS = ("get_expression_grammar", "get_audit_info")
GOLDEN = Path(__file__).parent / "fixtures" / "discovery_response_scaffold.json"


def _declaration(tool_name: str) -> ToolDeclaration:
    return next(item for item in _REGISTERED_TOOLS if item.name == tool_name)


def _assert_discovery_response_closure(declarations: tuple[ToolDeclaration, ...]) -> None:
    discovery_kinds = {ToolKind.DISCOVERY, ToolKind.BLOB_DISCOVERY, ToolKind.SECRET_DISCOVERY}
    assert declarations
    for declaration in declarations:
        assert (declaration.response_contract is not None) == (declaration.kind in discovery_kinds), declaration.name


def test_live_discovery_declarations_have_exact_response_contract_closure() -> None:
    _assert_discovery_response_closure(_REGISTERED_TOOLS)


def test_response_contract_closure_rejects_missing_discovery_contract() -> None:
    from elspeth.contracts.freeze import deep_thaw

    _assert_discovery_response_closure(_REGISTERED_TOOLS)
    witness = next(item for item in _REGISTERED_TOOLS if item.kind is ToolKind.DISCOVERY)
    broken = tuple(
        replace(item, response_contract=None, json_schema=deep_thaw(item.json_schema)) if item is witness else item
        for item in _REGISTERED_TOOLS
    )
    with pytest.raises(AssertionError, match=witness.name):
        _assert_discovery_response_closure(broken)


def _produce(tool_name: str) -> ToolResult:
    state = CompositionState(source=None, nodes=(), edges=(), outputs=(), metadata=PipelineMetadata(), version=1)
    context = ToolContext(
        catalog=MagicMock(spec=CatalogService),
        plugin_snapshot=MagicMock(spec=PluginAvailabilitySnapshot),
    )
    return _declaration(tool_name).handler({}, state, context)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_selected_discoveries_declare_response_contract(tool_name: str) -> None:
    declaration = _declaration(tool_name)
    assert "response_contract" in {field.name for field in fields(declaration)}
    assert declaration.response_contract is not None


@pytest.mark.parametrize("tool_name", TOOLS)
def test_actual_frozen_producer_payload_roundtrips(tool_name: str) -> None:
    result = _produce(tool_name)
    assert isinstance(result.data, MappingProxyType)
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    admitted = contract.admit(result.data)
    assert admitted.to_wire() == dict(result.data)
    assert admitted.to_wire() is not result.data


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.parametrize("bad_value", [None, [], "not a mapping", 17, {}, {"extra": "private"}])
def test_selected_contract_rejects_wrong_or_missing_root(tool_name: str, bad_value: object) -> None:
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    with pytest.raises(FrameworkBugError):
        contract.admit(bad_value)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_selected_contract_rejects_extra_root(tool_name: str) -> None:
    result = _produce(tool_name)
    raw = dict(result.data)
    raw["private_extra"] = "must not escape"
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    with pytest.raises(FrameworkBugError):
        contract.admit(MappingProxyType(raw))


@pytest.mark.parametrize(
    ("tool_name", "field", "bad_value"),
    [
        ("get_expression_grammar", "grammar", None),
        ("get_expression_grammar", "grammar", 3),
        ("get_audit_info", "enabled", 1),
        ("get_audit_info", "enabled", False),
        ("get_audit_info", "composer_modifiable", 0),
        ("get_audit_info", "composer_modifiable", True),
        ("get_audit_info", "summary", None),
        ("get_audit_info", "audit_export_summary", []),
    ],
)
def test_selected_contract_rejects_wrong_scalar(tool_name: str, field: str, bad_value: object) -> None:
    raw = dict(_produce(tool_name).data)
    raw[field] = bad_value
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    with pytest.raises(FrameworkBugError):
        contract.admit(MappingProxyType(raw))


class _UnrelatedGrammarModel(BaseModel):
    grammar: str


def test_unrelated_pydantic_model_is_not_an_admitted_response() -> None:
    contract = _declaration("get_expression_grammar").response_contract
    assert contract is not None
    with pytest.raises(FrameworkBugError):
        contract.admit(_UnrelatedGrammarModel(grammar="plausible but unowned"))


@pytest.mark.parametrize("tool_name", TOOLS)
def test_successful_absent_data_is_producer_corruption(tool_name: str) -> None:
    from elspeth.web.composer.discovery_response import admit_discovery_result

    result = replace(_produce(tool_name), data=None)
    with pytest.raises(FrameworkBugError):
        admit_discovery_result(tool_name, result)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_failed_absent_data_preserves_absent_envelope(tool_name: str) -> None:
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

    result = replace(_produce(tool_name), success=False, data=None)
    admitted = admit_discovery_result(tool_name, result)
    assert admitted.response is None
    assert admitted.result is result
    assert admitted.contract is _declaration(tool_name).response_contract
    assert "data" not in json.loads(serialize_admitted_discovery_result(admitted))


@pytest.mark.parametrize("tool_name", TOOLS)
def test_admitted_ordinary_bytes_match_immutable_baseline(tool_name: str) -> None:
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

    golden = json.loads(GOLDEN.read_text())
    admitted = admit_discovery_result(tool_name, _produce(tool_name))
    assert serialize_admitted_discovery_result(admitted) == golden["responses"][tool_name]["ordinary"]


@pytest.mark.parametrize("tool_name", TOOLS)
@pytest.mark.parametrize("surface", list(PlannerSurface))
def test_provider_bytes_match_immutable_baseline(tool_name: str, surface: PlannerSurface) -> None:
    from elspeth.web.composer.discovery_response import admit_discovery_result

    golden = json.loads(GOLDEN.read_text())
    call = _ParsedToolCall(call_id="response-golden", name=tool_name, raw_arguments="{}", arguments={})
    admitted = admit_discovery_result(tool_name, _produce(tool_name))
    actual = _serialize_provider_discovery_result(
        call=call,
        result=admitted,
        surface=surface,
        provider_current_state=guided_redacted_current_state_context(admitted.result.updated_state),
    )
    assert actual == golden["responses"][tool_name][surface.value]


@pytest.mark.parametrize("tool_name", TOOLS)
def test_original_data_is_not_serialization_authority_after_admission(tool_name: str) -> None:
    from elspeth.web.composer.discovery_cache import serialize_tool_result
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

    admitted = admit_discovery_result(tool_name, _produce(tool_name))
    expected = serialize_admitted_discovery_result(admitted)
    changed_original = replace(admitted.result, data={"private_extra": "must not escape"})
    changed_envelope = replace(admitted, result=changed_original)
    assert serialize_admitted_discovery_result(changed_envelope) == expected
    persisted = changed_envelope.to_tool_result()
    assert persisted.to_dict() == admitted.to_dict()
    assert serialize_tool_result(persisted) == expected
    golden = json.loads(GOLDEN.read_text())
    call = _ParsedToolCall(call_id="response-golden", name=tool_name, raw_arguments="{}", arguments={})
    for surface in PlannerSurface:
        actual = _serialize_provider_discovery_result(
            call=call,
            result=persisted,
            surface=surface,
            provider_current_state=guided_redacted_current_state_context(persisted.updated_state),
        )
        assert actual == golden["responses"][tool_name][surface.value]


@pytest.mark.parametrize("tool_name", TOOLS)
def test_readmission_preserves_bytes_with_current_contract(tool_name: str) -> None:
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    admitted = contract.admit(_produce(tool_name).data)
    readmitted = admitted.readmit(contract)
    assert readmitted.to_wire() == admitted.to_wire()
    assert readmitted is not admitted


def test_readmission_rejects_a_different_selected_contract() -> None:
    grammar_contract = _declaration("get_expression_grammar").response_contract
    audit_contract = _declaration("get_audit_info").response_contract
    assert grammar_contract is not None
    assert audit_contract is not None
    admitted = grammar_contract.admit(_produce("get_expression_grammar").data)
    with pytest.raises(FrameworkBugError, match="contract changed"):
        admitted.readmit(audit_contract)


@pytest.mark.parametrize(
    ("tool_name", "raw"),
    [("list_secret_refs", []), ("get_expression_grammar", MappingProxyType({"grammar": "safe"}))],
)
def test_cache_readmission_cannot_renormalize_a_replaced_canonical_root(tool_name: str, raw: object) -> None:
    contract = _declaration(tool_name).response_contract
    assert contract is not None
    admitted = contract.admit(raw)
    # These roots are valid at initial producer admission, but are not the
    # immutable canonical T that admission placed in the cached carrier.
    corrupted = replace(admitted, value=raw)
    with pytest.raises(FrameworkBugError, match="canonical root"):
        corrupted.readmit(contract)


def test_readmission_revalidates_corrupt_nominal_response() -> None:
    from elspeth.web.composer.response_contracts import _AdmittedResponse
    from elspeth.web.composer.tools.generation import EXPRESSION_GRAMMAR_RESPONSE_CONTRACT, ExpressionGrammarResponse

    # Deliberate runtime corruption: dataclass annotations do not validate values.
    corrupt = _AdmittedResponse(ExpressionGrammarResponse(grammar=17), EXPRESSION_GRAMMAR_RESPONSE_CONTRACT, ExpressionGrammarResponse)
    with pytest.raises(FrameworkBugError):
        corrupt.readmit(EXPRESSION_GRAMMAR_RESPONSE_CONTRACT)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_failed_response_cannot_smuggle_success_payload(tool_name: str) -> None:
    from elspeth.web.composer.discovery_response import admit_discovery_result

    failed = replace(_produce(tool_name), success=False)
    with pytest.raises(FrameworkBugError):
        admit_discovery_result(tool_name, failed)


@pytest.mark.parametrize("tool_name", TOOLS)
def test_admitted_serialization_never_traverses_original_data(tool_name: str) -> None:
    from unittest.mock import patch

    from elspeth.contracts.freeze import deep_thaw
    from elspeth.web.composer.discovery_cache import serialize_tool_result
    from elspeth.web.composer.discovery_response import admit_discovery_result, serialize_admitted_discovery_result

    result = _produce(tool_name)
    admitted = admit_discovery_result(tool_name, result)
    expected = serialize_admitted_discovery_result(admitted)
    with patch(
        "elspeth.web.composer.tools._common.deep_thaw",
        spec=deep_thaw,
        side_effect=AssertionError("original producer data traversed"),
    ):
        # Positive control proves the instrument intercepts the legacy data walk.
        with pytest.raises(AssertionError, match="original producer data traversed"):
            result.to_dict()
        assert serialize_admitted_discovery_result(admitted) == expected
        persisted = admitted.to_tool_result()
    assert persisted.to_dict() == admitted.to_dict()
    assert serialize_tool_result(persisted) == expected
