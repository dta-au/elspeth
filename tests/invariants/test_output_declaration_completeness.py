"""Governance harness for ADR-050 — every created field is declared before the first row.

Two sweeps over the live transform registry, built from each plugin's
``probe_config()`` exactly as the ADR-009 harness does:

- **Static** (``test_every_created_field_has_a_declared_contract``): the set
  of fields a transform states it creates — ``declared_output_fields``, the
  names in ``created_output_fields()`` and every output-config guarantee —
  is covered by the declaration stamp table
  (``_stamped_output_field_contracts``), minus the ``carried_output_fields``
  whose contract is an input field's. A name outside the table would be
  typed per emission from the row's value.

- **Dynamic** (``test_every_emitted_created_field_carries_a_declared_contract``):
  for every probeable single-row transform, run the forward invariant probe
  and assert every emitted key absent from the probe input carries a
  ``source="declared"`` contract — the same predicate the runtime
  ``OutputDeclarationCompletenessContract`` enforces per row, applied here
  across the roster so a plugin that bypasses the stamp is caught in CI, not
  in a run. Batch-aware transforms run through the same probe: their
  reductive outputs carry the stamp via ``_batch_output_contract`` and their
  passthrough outputs stamp the created fields, so every emitted created key
  must be ``declared`` there too. The same emission then goes through the
  engine's value check for the seam the transform runs behind (per-row or
  batch flush), so a declared type the probe's own computation breaks is
  caught here as well.

Controls: web_scrape (a plugin that promotes observed to a typed flexible
output) passes; the predicate itself is exercised against an in-file plugin
that declares its created field and one that emits an undeclared one.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts import Determinism, PluginSchema, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.contract_propagation import propagate_contract
from elspeth.contracts.schema import FieldDefinition
from elspeth.contracts.schema_contract import PipelineRow
from elspeth.engine.executors.declared_output_types import verify_created_output_types, verify_produced_output_types
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from elspeth.testing import make_pipeline_row
from tests.invariants.test_pass_through_invariants import (
    _emitted_rows_from_result,
    _probe_context,
    _probe_instantiate,
    _registered_transform_classes,
    _UnprobeableTransform,
)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "_transform_cls" in metafunc.fixturenames:
        plugins = _registered_transform_classes()
        assert plugins, "Expected at least 1 registered transform; plugin registration may have failed."
        metafunc.parametrize("_transform_cls", plugins, ids=lambda c: c.__name__)


def _created_names(transform: BaseTransform) -> frozenset[str]:
    """Every field the transform STATES it creates, by name."""
    created = set(transform.declared_output_fields) | {definition.name for definition in transform.created_output_fields()}
    output_schema_config = transform._output_schema_config
    if output_schema_config is not None:
        created |= set(output_schema_config.get_effective_guaranteed_fields())
    return frozenset(created)


def undeclared_created_fields(
    transform: BaseTransform, probe_rows: list[PipelineRow], emitted: list[PipelineRow]
) -> dict[str, frozenset[str]]:
    """Per emitted key created by ``transform``, the reason it is not declared (empty when all are).

    The roster predicate: a created key (absent from every probe input row,
    not carried) must be in the emitted contract with ``source="declared"``.
    """
    inputs: set[str] = set()
    for probe in probe_rows:
        inputs |= set(probe.to_dict())
    carried = transform.carried_output_fields()
    problems: dict[str, set[str]] = {}
    for row in emitted:
        contract_fields = {fc.normalized_name: fc for fc in row.contract.fields}
        for key in row.to_dict():
            if key in inputs or key in carried:
                continue
            if key not in contract_fields:
                problems.setdefault(key, set()).add("absent from the emitted contract")
            elif contract_fields[key].source != "declared":
                problems.setdefault(key, set()).add(f"source={contract_fields[key].source!r}")
    return {key: frozenset(reasons) for key, reasons in problems.items()}


def test_every_created_field_has_a_declared_contract(_transform_cls: type[BaseTransform]) -> None:
    try:
        transform = _probe_instantiate(_transform_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_transform_cls.__name__}: {exc.reason}")
    stamped = frozenset(transform._stamped_output_field_contracts())
    missing = sorted((_created_names(transform) - transform.carried_output_fields()) - stamped)
    assert not missing, (
        f"{_transform_cls.__name__} creates {missing!r} but declares no contract for them (ADR-050): "
        "name them in created_output_fields() (with the type the plugin fixes, or 'any') or in declared_output_fields."
    )


def test_every_emitted_created_field_carries_a_declared_contract(_transform_cls: type[BaseTransform]) -> None:
    try:
        transform = _probe_instantiate(_transform_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_transform_cls.__name__}: {exc.reason}")
    # The ADR-009 harness's own selection: the forward probe for a pass-through
    # transform, the backward probe otherwise — a reductive batch transform
    # grafts the value fields its process() reads onto the backward probe rows.
    probe = make_pipeline_row({"baseline": "kept"})
    if transform.passes_through_input:
        probe_rows = transform.forward_invariant_probe_rows(probe)
        result = transform.execute_forward_invariant_probe(probe_rows, _probe_context(transform))
    else:
        probe_rows = transform.backward_invariant_probe_rows(probe)
        result = transform.execute_backward_invariant_probe(probe_rows, _probe_context(transform))
    assert result.status == "success", f"{_transform_cls.__name__}: the probe did not emit ({result.status}); nothing to check"
    emitted = _emitted_rows_from_result(result)
    assert emitted, f"{_transform_cls.__name__}: the forward probe emitted no rows"

    problems = undeclared_created_fields(transform, probe_rows, emitted)
    assert not problems, (
        f"{_transform_cls.__name__} emitted created fields without a declared contract: "
        f"{ {key: sorted(reasons) for key, reasons in problems.items()}!r} (ADR-050). The runtime completeness contract "
        "would end the run on the first row; declare the field or route it through the stamp."
    )

    # The declared TYPES hold on the probe's emission too: the engine's value
    # check, at the seam this transform runs behind, passes on it.
    if transform.is_batch_aware:
        verify_created_output_types(transform=transform, emitted_rows=emitted)
    else:
        [probe_input] = probe_rows
        verify_produced_output_types(transform=transform, input_row=probe_input, emitted_rows=emitted)


class _Probe(BaseTransform):
    name = "declaration_completeness_probe"
    determinism = Determinism.DETERMINISTIC
    input_schema = PluginSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"

    def __init__(self, config: dict[str, Any], *, declare: bool, stamp: bool) -> None:
        super().__init__(config)
        cfg = TransformDataConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._stamp = stamp
        self.declared_output_fields = frozenset({"created"}) if declare else frozenset()
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)

    def created_output_fields(self) -> tuple[FieldDefinition, ...]:
        return ()

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        output = {**row.to_dict(), "created": 1}
        contract = propagate_contract(row.contract, output, transform_adds_fields=True)
        if self._stamp:
            contract = self._apply_declared_output_field_contracts(contract)
        return TransformResult.success(PipelineRow(output, self._align_output_contract(contract)), success_reason={"action": "probe"})

    def close(self) -> None:
        pass


def _emit(transform: _Probe) -> tuple[list[PipelineRow], list[PipelineRow]]:
    probe_rows = [make_pipeline_row({"baseline": "kept"})]
    result = transform.process(probe_rows[0], _probe_context(transform))
    return probe_rows, _emitted_rows_from_result(result)


def test_predicate_controls() -> None:
    """The roster predicate passes a declared-and-stamped field and flags the two ways of bypassing the stamp."""
    declared = _Probe({"schema": {"mode": "observed"}}, declare=True, stamp=True)
    assert undeclared_created_fields(declared, *_emit(declared)) == {}
    assert "created" in declared._stamped_output_field_contracts()

    unstamped = _Probe({"schema": {"mode": "observed"}}, declare=True, stamp=False)
    assert undeclared_created_fields(unstamped, *_emit(unstamped)) == {"created": frozenset({"source='inferred'"})}

    unnamed = _Probe({"schema": {"mode": "observed"}}, declare=False, stamp=True)
    assert undeclared_created_fields(unnamed, *_emit(unnamed)) == {"created": frozenset({"source='inferred'"})}
    assert "created" not in unnamed._stamped_output_field_contracts()


# Batch plugins write some created keys only when a row was skipped or a pair
# was incomplete. Those keys are not in ``declared_output_fields`` (the
# guarantee surface), so neither sweep above reaches them from the plugin's
# probe rows: each plugin names them in ``created_output_fields()`` and this
# case makes it emit them. (plugin class, config, rows, the conditional keys
# the rows must make it write)
_CONDITIONAL_CREATED_CASES: list[tuple[str, dict[str, Any], list[dict[str, Any]], frozenset[str]]] = [
    (
        "batch_stats",
        {"schema": {"mode": "observed"}, "value_field": "v"},
        [{"v": 1}, {"v": None}, {"v": float("nan")}],
        frozenset({"skipped_missing", "skipped_missing_indices", "skipped_non_finite", "skipped_non_finite_indices"}),
    ),
    (
        "batch_distribution_profile",
        {"schema": {"mode": "observed"}, "value_field": "v"},
        [{"v": 1}, {"v": 2}, {"v": None}, {"v": float("nan")}],
        frozenset({"missing_indices", "non_finite_indices"}),
    ),
    (
        "batch_classifier_metrics",
        {"schema": {"mode": "observed"}, "actual_field": "a", "predicted_field": "p"},
        [{"a": "x", "p": "x"}, {"a": None, "p": "x"}],
        frozenset({"missing_indices"}),
    ),
    (
        "batch_effect_size",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [
            {"g": "b", "s": 1.0},
            {"g": "b", "s": 2.0},
            {"g": "b", "s": None},
            {"g": "b", "s": float("nan")},
            {"g": "c", "s": 3.0},
            {"g": "c", "s": 5.0},
            {"g": "c", "s": None},
            {"g": "c", "s": float("nan")},
        ],
        frozenset({"baseline_missing_indices", "baseline_non_finite_indices", "variant_missing_indices", "variant_non_finite_indices"}),
    ),
    (
        "batch_experiment_compare",
        {"schema": {"mode": "observed"}, "variant_field": "g", "score_field": "s"},
        [
            {"g": "b", "s": 1.0},
            {"g": "b", "s": 2.0},
            {"g": "b", "s": None},
            {"g": "b", "s": float("nan")},
            {"g": "c", "s": 3.0},
            {"g": "c", "s": 5.0},
            {"g": "c", "s": None},
            {"g": "c", "s": float("nan")},
        ],
        frozenset({"baseline_missing_indices", "baseline_non_finite_indices", "variant_missing_indices", "variant_non_finite_indices"}),
    ),
    (
        "batch_paired_preference",
        {"schema": {"mode": "observed"}, "pair_field": "k", "variant_field": "g", "score_field": "s"},
        [{"k": 1, "g": "b", "s": 1.0}, {"k": 1, "g": "c", "s": 2.0}, {"k": 2, "g": "b", "s": 1.0}],
        frozenset({"incomplete_pairs"}),
    ),
]


@pytest.mark.parametrize(
    ("plugin_name", "config", "rows", "conditional"), _CONDITIONAL_CREATED_CASES, ids=[c[0] for c in _CONDITIONAL_CREATED_CASES]
)
def test_conditional_batch_created_fields_carry_a_declared_contract(
    plugin_name: str, config: dict[str, Any], rows: list[dict[str, Any]], conditional: frozenset[str]
) -> None:
    [transform_cls] = [cls for cls in _registered_transform_classes() if cls.name == plugin_name]
    transform = transform_cls(config)
    input_rows = [make_pipeline_row(row) for row in rows]
    result = transform.process(input_rows, _probe_context(transform))
    assert result.status == "success", result.reason
    emitted = _emitted_rows_from_result(result)
    written = set().union(*(row.to_dict().keys() for row in emitted))
    assert conditional <= written, f"{plugin_name}: the case did not make it write {sorted(conditional - written)!r}; nothing is armed"
    assert undeclared_created_fields(transform, input_rows, emitted) == {}
    for row in emitted:
        contract_fields = {fc.normalized_name: fc for fc in row.contract.fields}
        for name in conditional & set(row.to_dict()):
            assert (contract_fields[name].source, contract_fields[name].required) == ("declared", False), name
