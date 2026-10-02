"""Governance harness for ADR-050 Decision 2 — the operator's declaration of a CARRIED field reaches the emitted contract.

An operator who types a field the transform passes through
(``schema: {mode: flexible, fields: ["name: str"]}`` after an observed
source) makes an output declaration: the node's ``_output_schema_config``
carries it, and the ADR-014 metadata check
(``engine/executors/schema_config_mode``) compares every emitted row's
contract to it by exact type, ``required`` and ``nullable``. The ONE stamp
(``BaseTransform._apply_declared_output_field_contracts``) is what writes
that declaration onto the emitted contract. A transform that emits its input
row's contract without the stamp keeps the observed source's metadata
(``required=False``, the inferred type), and the ADR-014 check then ends the
run with a Tier-1 ``SchemaConfigModeViolation`` on a VALID row: the strict
input check admitted the value, and nothing about the row is wrong.

This sweep builds every registered transform from its ``probe_config()``
with an operator schema declaring two carried probe fields — a ``str`` over
an observed optional ``str`` and a ``float`` over an ``int`` (ruling C3: an
``int`` satisfies a ``float`` declaration) — runs the ADR-009 probe, and
requires every emission to (1) pass the ADR-014 check, (2) carry each
declared field as ``source="declared"`` with the declared type and
requiredness, and (3) pass the engine's value check. Every registered
transform keeps an output declaration: one with ``_output_schema_config is
None`` fails the sweep, because the DAG builder then projects the operator's
schema onto its outgoing edge anyway, so a typed edge would sit over an
emitted contract that is still the input's inference (the two Azure
guardrails, until S7 fix round 1). Every other transform's output
declaration must carry the operator's fields. Only the reductive batch
outputs named in ``_REDUCTIVE_OUTPUTS`` are outside the sweep: the set is
named in this file rather than read from the plugin's own output config, so a
carrying transform cannot drop the operator's declaration and skip itself
(S7 fix round 2).

Control: ``test_an_unstamped_pass_through_ends_the_run`` keeps the failure
the sweep exists to catch — an in-file transform that emits its input
contract without the stamp — and ``test_a_dropped_required_field_still_aborts``
keeps the genuine Tier-1 case the stamp must not mask: a plugin that drops a
field its declaration guarantees still ends the run.
"""

from __future__ import annotations

from typing import Any

import pytest

from elspeth.contracts import Determinism, PluginSchema, TransformResult
from elspeth.contracts.contexts import TransformContext
from elspeth.contracts.errors import SchemaConfigModeViolation
from elspeth.contracts.schema_contract import FieldContract, PipelineRow, SchemaContract
from elspeth.engine.executors.declared_output_types import verify_created_output_types, verify_produced_output_types
from elspeth.engine.executors.schema_config_mode import verify_schema_config_mode
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.plugins.infrastructure.config_base import TransformDataConfig
from tests.invariants.test_pass_through_invariants import (
    _emitted_rows_from_result,
    _probe_context,
    _probe_instantiate,
    _registered_transform_classes,
    _UnprobeableTransform,
)

# (field, declaration, probe value): the value is what an observed source
# infers — optional, typed from the value — and each value satisfies its
# declaration under the one admission rule.
_CARRIED = (
    ("s7_text", "str", "kept"),
    ("s7_amount", "float", 5),
)


def pytest_generate_tests(metafunc: pytest.Metafunc) -> None:
    if "_transform_cls" in metafunc.fixturenames:
        plugins = _registered_transform_classes()
        assert plugins, "Expected at least 1 registered transform; plugin registration may have failed."
        metafunc.parametrize("_transform_cls", plugins, ids=lambda c: c.__name__)


def _observed_row(data: dict[str, Any]) -> PipelineRow:
    """A row as an observed source emits it: every field inferred from its value, optional."""
    fields = tuple(FieldContract.inferred(name, name, value) for name, value in data.items())
    return PipelineRow(data=data, contract=SchemaContract(mode="OBSERVED", fields=fields, locked=True))


# The option naming the field a plugin reads, for the plugins whose config
# requires that field to be named in an explicit schema ("must use the
# normalized field name when schema declares ... explicit fields").
_FIELD_OPTION_THE_SCHEMA_MUST_NAME = {"json_explode": "array_field", "line_explode": "source_field"}

# The transforms whose output is a new row shape (a batch summary or report),
# so their output declaration carries none of the input's fields and makes no
# claim about them. The set is named here, never computed from the plugin's
# own output config: a carrying transform whose output config dropped the
# operator's fields would otherwise classify itself out of the sweep. Both
# directions are checked: a transform outside the set must carry the fields,
# and one inside it must not (a stale entry fails until it is removed).
_REDUCTIVE_OUTPUTS = frozenset(
    {
        "batch_classifier_metrics",
        "batch_data_quality_report",
        "batch_distribution_profile",
        "batch_drift_compare",
        "batch_effect_size",
        "batch_experiment_compare",
        "batch_paired_preference",
        "batch_stats",
        "batch_threshold_summary",
        "batch_top_k",
        "report_assemble",
    }
)


def _declaring_config(transform_cls: type[BaseTransform]) -> dict[str, Any]:
    """The probe config with an operator schema typing the carried probe fields.

    The fields the plugin reads by name are declared ``any`` beside them, so a
    plugin that requires its input fields to be named in an explicit schema
    accepts the declaration; ``any`` adds no type claim of its own.
    """
    plain = _probe_instantiate(transform_cls)
    config = dict(transform_cls.probe_config())
    read_names = set(plain.declared_input_fields)
    if transform_cls.name in _FIELD_OPTION_THE_SCHEMA_MUST_NAME:
        read_names.add(config[_FIELD_OPTION_THE_SCHEMA_MUST_NAME[transform_cls.name]])
    read_fields = [f"{name}: any" for name in sorted(read_names)]
    config["schema"] = {
        "mode": "flexible",
        "fields": [f"{name}: {declared}" for name, declared, _value in _CARRIED] + read_fields,
    }
    return config


def test_an_operator_declared_carried_field_is_stamped_on_emission(_transform_cls: type[BaseTransform]) -> None:
    try:
        config = _declaring_config(_transform_cls)
    except _UnprobeableTransform as exc:
        pytest.skip(f"{_transform_cls.__name__}: {exc.reason}")
    transform = _transform_cls(config)
    output_schema_config = transform._output_schema_config
    assert output_schema_config is not None, (
        f"{_transform_cls.__name__} keeps no output declaration, so the operator's schema types its edge at build "
        "time while its emitted contract stays the input's inference; set _output_schema_config and stamp every emission"
    )
    declared_names = {definition.name for definition in output_schema_config.fields or ()}
    carried_names = {name for name, _declared, _value in _CARRIED}
    if _transform_cls.name in _REDUCTIVE_OUTPUTS:
        assert not carried_names & declared_names, (
            f"{_transform_cls.__name__} is named a reductive output, but its output declaration carries "
            f"{sorted(carried_names & declared_names)!r}; remove it from _REDUCTIVE_OUTPUTS so the sweep checks it"
        )
        pytest.skip(f"{_transform_cls.__name__}: a reductive output (named in _REDUCTIVE_OUTPUTS) makes no claim about carried fields")
    assert carried_names <= declared_names, (
        f"{_transform_cls.__name__}'s output declaration drops the operator's {sorted(carried_names - declared_names)!r}; "
        "a transform that carries its input must keep the operator's declaration of a carried field (ADR-050 D2)"
    )

    probe = _observed_row({"baseline": "kept"} | {name: value for name, _declared, value in _CARRIED})
    if transform.passes_through_input:
        probe_rows = transform.forward_invariant_probe_rows(probe)
        result = transform.execute_forward_invariant_probe(probe_rows, _probe_context(transform))
    else:
        probe_rows = transform.backward_invariant_probe_rows(probe)
        result = transform.execute_backward_invariant_probe(probe_rows, _probe_context(transform))
    assert result.status == "success", f"{_transform_cls.__name__}: the probe did not emit ({result.status}); nothing to check"
    emitted = _emitted_rows_from_result(result)
    assert emitted, f"{_transform_cls.__name__}: the probe emitted no rows"

    # (1) The ADR-014 check passes: no Tier-1 abort on a valid row.
    verify_schema_config_mode(
        output_schema_config=output_schema_config,
        emitted_rows=emitted,
        plugin_name=transform.name,
        node_id="operator-declared-carried",
        run_id="operator-declared-carried-run",
        row_id="operator-declared-carried-row",
        token_id="operator-declared-carried-token",
    )

    # (2) The declaration is on the emitted contract, not the source's inference.
    for index, row in enumerate(emitted):
        for name, declared, _value in _CARRIED:
            if name not in row.to_dict():
                continue
            field = row.contract.get_field(name)
            assert (field.source, field.python_type.__name__, field.required) == ("declared", declared, True), (
                f"{_transform_cls.__name__} emitted row {index}: {name!r} carries "
                f"{(field.source, field.python_type.__name__, field.required)!r}, not the operator's "
                f"{('declared', declared, True)!r}; route the emitted contract through _apply_declared_output_field_contracts"
            )

    # (3) The engine's value check agrees with the stamp.
    if transform.is_batch_aware:
        verify_created_output_types(transform=transform, emitted_rows=emitted)
    else:
        [probe_input] = probe_rows
        verify_produced_output_types(transform=transform, input_row=probe_input, emitted_rows=emitted)


class _PassThroughProbe(BaseTransform):
    """A pass-through transform whose emission the controls choose."""

    name = "operator_declared_carried_probe"
    determinism = Determinism.DETERMINISTIC
    input_schema = PluginSchema
    output_schema = PluginSchema
    plugin_version = "1.0.0"
    passes_through_input = True

    def __init__(self, config: dict[str, Any], *, stamp: bool, drop: str | None = None) -> None:
        super().__init__(config)
        cfg = TransformDataConfig.from_dict(config, plugin_name=self.name)
        self._initialize_declared_input_fields(cfg)
        self._schema_config = cfg.schema_config
        self._output_schema_config = self._build_output_schema_config(cfg.schema_config)
        self._stamp = stamp
        self._drop = drop

    def process(self, row: PipelineRow, ctx: TransformContext) -> TransformResult:
        data = {key: value for key, value in row.to_dict().items() if key != self._drop}
        contract = SchemaContract(
            mode=row.contract.mode,
            fields=tuple(field for field in row.contract.fields if field.normalized_name != self._drop),
            locked=row.contract.locked,
        )
        if self._stamp:
            contract = self._apply_declared_output_field_contracts(contract)
        return TransformResult.success(PipelineRow(data, self._align_output_contract(contract)), success_reason={"action": "passthrough"})

    def close(self) -> None:
        pass


def _emit_through(transform: _PassThroughProbe) -> list[PipelineRow]:
    result = transform.process(_observed_row({"name": "Ann", "amount": 5}), _probe_context(transform))
    return _emitted_rows_from_result(result)


def _check(transform: _PassThroughProbe, emitted: list[PipelineRow]) -> None:
    assert transform._output_schema_config is not None
    verify_schema_config_mode(
        output_schema_config=transform._output_schema_config,
        emitted_rows=emitted,
        plugin_name=transform.name,
        node_id="n",
        run_id="r",
        row_id="x",
        token_id="t",
    )


_DECLARING = {"schema": {"mode": "flexible", "fields": ["name: str", "amount: float"]}}


def test_the_field_option_table_names_registered_plugins() -> None:
    registered = {transform_cls.name for transform_cls in _registered_transform_classes()}
    assert set(_FIELD_OPTION_THE_SCHEMA_MUST_NAME) <= registered
    assert registered >= _REDUCTIVE_OUTPUTS


def test_an_unstamped_pass_through_ends_the_run() -> None:
    """The failure the sweep catches: the input contract emitted as-is breaks the ADR-014 check on a valid row."""
    unstamped = _PassThroughProbe(_DECLARING, stamp=False)
    with pytest.raises(SchemaConfigModeViolation, match="field metadata mismatches for \\['name', 'amount'\\]"):
        _check(unstamped, _emit_through(unstamped))

    stamped = _PassThroughProbe(_DECLARING, stamp=True)
    _check(stamped, _emit_through(stamped))


def test_a_dropped_required_field_still_aborts() -> None:
    """The stamp types what is emitted; it never supplies a declared field the plugin dropped (Tier 1)."""
    dropping = _PassThroughProbe(_DECLARING, stamp=True, drop="name")
    with pytest.raises(SchemaConfigModeViolation, match="missing required fields \\['name'\\]"):
        _check(dropping, _emit_through(dropping))
