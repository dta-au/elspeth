"""Source-bound DATA receipts with independently calibrated structural checks."""

from __future__ import annotations

import ast
import hashlib
from dataclasses import dataclass
from functools import cache
from pathlib import Path

import pytest
from tests.helpers import web_reserve_batch_data_transfers as helper

from elspeth_lints.core.ast_dump import stable_ast_dump


@dataclass(frozen=True)
class _Source:
    path: str
    source: str
    tree: ast.Module


def _parse(path: str, text: str) -> _Source:
    return _Source(path, text, ast.parse(text, filename=path))


@cache
def _units() -> tuple[_Source, ...]:
    root = Path(__file__).resolve().parents[3]
    return tuple(_parse(path, (root / path).read_text()) for path in helper.REQUIRED_PATHS)


def _change(path: str, before: str, after: str) -> tuple[_Source, ...]:
    original = next(unit for unit in _units() if unit.path == path)
    assert original.source.count(before) == 1
    changed = _parse(path, original.source.replace(before, after))
    return tuple(changed if unit.path == path else unit for unit in _units())


def _rebind_body(monkeypatch, units, path, symbol):
    unit = next(unit for unit in units if unit.path == path)
    scope = unit.tree.body
    for part in symbol.split("."):
        selected = [node for node in scope if isinstance(node, (ast.ClassDef, ast.FunctionDef)) and node.name == part]
        assert len(selected) == 1
        node = selected[0]
        scope = node.body
    bindings = dict(helper.BODY_BINDINGS)
    bindings[path, symbol] = hashlib.sha256(stable_ast_dump(node).encode()).hexdigest()
    monkeypatch.setattr(helper, "BODY_BINDINGS", bindings)


def test_current_batch_data_receipt_has_actual_source_sites_and_separate_import_roles():
    units = _units()
    failures, receipt = helper.batch_data_local_contracts(units)
    assert not failures, failures
    actual = {(unit.path, id(node)) for unit in units for node in ast.walk(unit.tree)}
    for key in helper.SITE_KEYS:
        assert receipt[key] <= actual, key
    assert receipt["additional_local_imports"] == receipt["type_only_imports"] | receipt["application_imports"]
    assert receipt["type_only_imports"]
    assert receipt["application_imports"]
    assert not receipt["type_only_imports"] & receipt["application_imports"]
    assert receipt["constructor_calls"]
    assert receipt["nominal_uses"]
    detached = tuple(_parse(unit.path, unit.source) for unit in units)
    other_failures, other = helper.batch_data_local_contracts(detached)
    assert not other_failures, other_failures
    for key in helper.SITE_KEYS:
        assert not receipt[key] & other[key], key


@pytest.mark.parametrize(
    ("symbol", "before", "after", "diagnostic"),
    [
        (
            "_members",
            'return "SchemaSecretRequirement", {"field": value.field, "candidates": value.candidates}',
            'return "SchemaSecretRequirement", {"field": value.field, "candidates": foreign_candidates}',
            "schema field projection changed SchemaSecretRequirement",
        ),
        (
            "_construct",
            'return AdmittedPluginSchemaResponse(members["snapshot"], PLUGIN_SCHEMA_RESPONSE_CONTRACT)',
            'return AdmittedPluginSchemaResponse(members["snapshot"], foreign_contract)',
            "schema constructor changed AdmittedPluginSchemaResponse",
        ),
        (
            "_joint",
            "result_tools[id(response)] = outcome.call.function.name",
            'result_tools[id(response)] = "foreign_tool"',
            "result contract identity transfer changed",
        ),
        (
            "_encode",
            "if prepared is not None or memory_completion or application.selected is not data:",
            "if prepared is not None or memory_completion or application.selected != data:",
            "application selected cut changed",
        ),
        (
            "_decode_graph",
            'members["audit"] = None',
            'members["audit"] = foreign_audit',
            "portable audit separation changed",
        ),
        (
            "_check_prepared",
            'expected_phase = "application_pending" if memory_completion else "prepared"',
            'expected_phase = "prepared"',
            "prepared memory phase changed",
        ),
        (
            "encode_memory_completion_batch_data",
            "encoded = _encode(RestoredBatchData(batch, accumulator, calls), env_ref_names, (retained, dialect), memory_completion=True)",
            "encoded = _encode(RestoredBatchData(batch, accumulator, calls), env_ref_names, (retained, dialect), memory_completion=False)",
            "memory codec phase/caller changed",
        ),
        (
            "require_selected_memory_completion",
            "or selected_pre_state is not call.pre_state",
            "or selected_pre_state != call.pre_state",
            "selected memory root identity changed",
        ),
        (
            "_encode",
            "    identities: dict[int, int] = {}",
            "    from foreign.module import unexpected_supplier\n    identities: dict[int, int] = {}",
            "local import role inventory changed",
        ),
        (
            "_decode_graph",
            "    application: bool = False,",
            "    application: bool = True,",
            "exact formal/default transfer changed _decode_graph",
        ),
    ],
)
def test_data_structural_checks_survive_only_the_mutated_body_binding(monkeypatch, symbol, before, after, diagnostic):
    positive_failures, positive = helper.batch_data_local_contracts(_units())
    assert not positive_failures, positive_failures
    assert positive
    changed = _change(helper.DATA, before, after)
    _rebind_body(monkeypatch, changed, helper.DATA, symbol)
    failures, receipt = helper.batch_data_local_contracts(changed)
    assert any(diagnostic in failure for failure in failures), failures
    assert not any("selected body changed" in failure for failure in failures), failures
    assert not receipt


@pytest.mark.parametrize(
    ("before", "after", "diagnostic"),
    [
        ('"SchemaSecretRequirement": ("field", "candidates"),', '"SchemaSecretRequirement": ("field",),', "schema registry fields changed"),
        ("if TYPE_CHECKING:\n", "if True:\n", "type-only application import changed"),
        ("from typing import TYPE_CHECKING, Any, NoReturn", "from typing import Any, NoReturn", "complete top-level imports changed"),
    ],
)
def test_data_registry_and_type_only_import_are_independent_structural_roles(before, after, diagnostic):
    normal_failures, normal = helper.batch_data_local_contracts(_units())
    assert not normal_failures, normal_failures
    assert normal
    failures, receipt = helper.batch_data_local_contracts(_change(helper.DATA, before, after))
    assert any(diagnostic in failure for failure in failures), failures
    assert not receipt


def test_data_receipt_refuses_unqualified_extra_schema_constructor():
    normal_failures, normal = helper.batch_data_local_contracts(_units())
    assert not normal_failures, normal_failures
    assert normal
    extra = _parse(
        "src/elspeth/web/foreign_data_constructor.py",
        "from elspeth.web.composer.continuation_batch_data import _construct\n"
        "def foreign(members):\n    return _construct('PluginSchemaSnapshot', members)\n",
    )
    failures, receipt = helper.batch_data_local_contracts((*_units(), extra))
    assert any("constructor entry reference inventory changed" in failure for failure in failures), failures
    assert not receipt


@pytest.mark.parametrize(
    ("path", "before", "after"),
    [
        (
            helper.SCHEMA,
            'frozenset({"null", "boolean", "object", "array", "number", "string", "integer"})',
            'frozenset({"null", "foreign"})',
        ),
        (
            helper.GENERATION,
            "PLUGIN_SCHEMA_RESPONSE_CONTRACT = PluginSchemaResponseContract()",
            "PLUGIN_SCHEMA_RESPONSE_CONTRACT = foreign_contract()",
        ),
        (helper.DISCOVERY, "from dataclasses import dataclass, replace", "from foreign_dataclasses import dataclass, replace"),
        (helper.RECEIPTS, "_INVOCATION = TypeAdapter(ComposerToolInvocation)", "_INVOCATION = TypeAdapter(RetainedToolReceipt)"),
        (helper.APPLICATION, '"application_llm": ComposerLLMCall,', '"application_llm": ComposerPlannerAttempt,'),
        (helper.FAMILIES, "from typing import TYPE_CHECKING, Any", "from foreign_typing import TYPE_CHECKING, Any"),
    ],
)
def test_dependency_header_rejects_supplier_or_registry_substitution(path, before, after):
    normal_failures, normal = helper.batch_data_local_contracts(_units())
    assert not normal_failures, normal_failures
    assert normal
    failures, receipt = helper.batch_data_local_contracts(_change(path, before, after))
    assert any("dependency namespace changed " + path in failure for failure in failures), failures
    assert not receipt


def test_actual_central_batch_data_prerequisite_keeps_native_three_constructor_sites():
    from tests.unit.architecture import test_web_landscape_mutation_fencing as fence

    root = Path(__file__).resolve().parents[3]
    paths = ("src/elspeth/web/composer/tool_batch.py", "src/elspeth/web/composer/bounded_json.py")
    units = (*_units(), *(_parse(path, (root / path).read_text()) for path in paths))
    batch = next(unit for unit in units if unit.path == paths[0])
    classes = {name: fence._reserve_node_at(batch, name) for name in ("ContinuationBatchState", "ContinuationCallState", "_CallWireFacts")}
    assert all(isinstance(declaration, ast.ClassDef) for declaration in classes.values())
    failures, receipt = fence._reserve_continuation_batch_data_receipt(units, classes)
    assert not failures, failures
    actual = {(unit.path, id(node)): node for unit in units for node in ast.walk(unit.tree)}
    assert receipt["calls"] <= actual.keys()
    assert {ast.unparse(actual[site]) for site in receipt["calls"]} == {
        "ContinuationCallState(**members)",
        "_CallWireFacts(**members)",
        "ContinuationBatchState(**members)",
    }
    transfers = receipt["additional_transfers"]
    for key in helper.SITE_KEYS:
        assert transfers[key] <= actual.keys(), key


def test_data_receipt_refuses_missing_or_duplicate_source():
    units = _units()
    for changed in (units[1:], (*units, units[0])):
        failures, receipt = helper.batch_data_local_contracts(changed)
        assert failures
        assert not receipt
