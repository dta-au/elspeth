"""Composer/runtime agreement on the input types a ``value_transform`` reads through other steps.

A ``value_transform`` that declares its target's type is refused when its
expression computes another type. To decide that, each surface has to work
out the type of every field the expression reads, and the field is often
declared several steps upstream: behind a pass-through, a rename, a routing
gate, or another ``value_transform``. The runtime build resolves it by walking
the DAG; the composer resolves it in ``_bind_composer_value_transforms``
(``web/composer/state.py``), a separate copy of that walk.

Each case builds one small pipeline, feeds the SAME YAML to both surfaces and
asserts one verdict: both refuse, or both accept. Where neither surface can
prove the type (an observed source, a field a step removed, a
``value_transform`` that declares nothing), both stay permissive and the
case pins that they do so together.

The harness is the one in ``test_composer_runtime_agreement_fan_in_walk.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.integration.pipeline.test_composer_runtime_agreement_fan_in_walk import (
    _composer,
    _dump,
    _passthrough,
    _runtime_refusal,
    _sink,
    _source,
)

_TYPED_SOURCE: dict[str, Any] = {"mode": "fixed", "fields": ["name: str", "amount: int"]}
_OBSERVED: dict[str, Any] = {"mode": "observed"}
_REFUSAL = "target 'total' declares int, but its expression computes str"


def _typed_total(input_connection: str, expression: str) -> dict[str, Any]:
    """A ``value_transform`` whose target ``total`` is declared ``int`` and computed by ``expression``."""
    return {
        "name": "calc",
        "plugin": "value_transform",
        "input": input_connection,
        "on_success": "out",
        "on_error": "discard",
        "options": {
            "schema": {"mode": "flexible", "fields": ["total: int"]},
            "operations": [{"target": "total", "expression": expression}],
        },
    }


def _rename(input_connection: str, on_success: str, mapping: dict[str, str], *, select_only: bool = False) -> dict[str, Any]:
    return {
        "name": "rename",
        "plugin": "field_mapper",
        "input": input_connection,
        "on_success": on_success,
        "on_error": "discard",
        "options": {"schema": _OBSERVED, "mapping": mapping, "select_only": select_only},
    }


def _untyped_value_transform(input_connection: str, on_success: str, target: str, expression: str) -> dict[str, Any]:
    return {
        "name": "first",
        "plugin": "value_transform",
        "input": input_connection,
        "on_success": on_success,
        "on_error": "discard",
        "options": {"schema": _OBSERVED, "operations": [{"target": target, "expression": expression}]},
    }


def _pipeline(
    tmp_path: Path,
    transforms: list[dict[str, Any]],
    *,
    source_schema: dict[str, Any] | None = None,
    gates: list[dict[str, Any]] | None = None,
    csv_source: bool = False,
) -> str:
    source = _source(tmp_path, "rows", "raw", source_schema or _TYPED_SOURCE)
    if csv_source:
        source = {
            "plugin": "csv",
            "on_success": "raw",
            "options": {"path": str(tmp_path / "rows.csv"), "on_validation_failure": "discard", "schema": source_schema},
        }
    document: dict[str, Any] = {
        "sources": {"rows": source},
        "transforms": transforms,
        "sinks": {"out": _sink(tmp_path)},
    }
    if gates:
        document["gates"] = gates
    return _dump(document)


def _assert_both_refuse(pipeline_yaml: str) -> None:
    runtime = _runtime_refusal(pipeline_yaml)
    assert runtime is not None and _REFUSAL in runtime
    composer = _composer(pipeline_yaml)
    assert not composer.is_valid
    [error] = [entry for entry in composer.errors if entry.error_code == "value_transform_result_type_incompatible"]
    assert error.component == "node:calc"
    assert _REFUSAL in error.message


def _assert_both_accept(pipeline_yaml: str) -> None:
    assert _runtime_refusal(pipeline_yaml) is None
    composer = _composer(pipeline_yaml)
    assert composer.is_valid, [(entry.error_code, entry.message) for entry in composer.errors]


# name -> (steps between the source and the typed value_transform, the name its
# expression reads for the source's ``name: str``, the name for ``amount: int``).
_Route = tuple[list[dict[str, Any]], str, str]


def _routes() -> dict[str, _Route]:
    return {
        "directly-from-the-source": ([], "name", "amount"),
        "through-a-pass-through": ([_passthrough("mid", "raw", "ready", _OBSERVED)], "name", "amount"),
        "through-two-pass-throughs": (
            [_passthrough("mid_one", "raw", "halfway", _OBSERVED), _passthrough("mid_two", "halfway", "ready", _OBSERVED)],
            "name",
            "amount",
        ),
        "through-a-pass-through-that-declares-another-field": (
            [_passthrough("mid", "raw", "ready", {"mode": "flexible", "fields": ["amount: int"]})],
            "name",
            "amount",
        ),
        "through-a-rename-of-the-field": ([_rename("raw", "ready", {"name": "label", "amount": "quantity"})], "label", "quantity"),
        "through-a-rename-of-a-different-field": ([_rename("raw", "ready", {"amount": "quantity"})], "name", "quantity"),
    }


_ROUTE_NAMES = sorted(_routes())


class TestValueTransformInputTypeBehindForwardingStepsAgreement:
    """The declared type of a field reaches a ``value_transform`` through steps that only carry it."""

    @pytest.mark.parametrize("route", _ROUTE_NAMES)
    def test_both_refuse_a_str_field_computed_into_an_int_target(self, route: str, tmp_path: Path) -> None:
        steps, text_field, _number_field = _routes()[route]
        consumer_input = "ready" if steps else "raw"
        _assert_both_refuse(_pipeline(tmp_path, [*steps, _typed_total(consumer_input, f"row['{text_field}']")]))

    @pytest.mark.parametrize("route", _ROUTE_NAMES)
    def test_both_accept_an_int_field_computed_into_an_int_target(self, route: str, tmp_path: Path) -> None:
        steps, _text_field, number_field = _routes()[route]
        consumer_input = "ready" if steps else "raw"
        _assert_both_accept(_pipeline(tmp_path, [*steps, _typed_total(consumer_input, f"row['{number_field}']")]))

    @pytest.mark.parametrize(("field", "refused"), [("name", True), ("amount", False)], ids=["str-field", "int-field"])
    def test_both_agree_through_a_routing_gate(self, field: str, refused: bool, tmp_path: Path) -> None:
        pipeline_yaml = _pipeline(
            tmp_path,
            [_typed_total("work", f"row['{field}']")],
            gates=[{"name": "route", "input": "raw", "condition": "True", "routes": {"true": "work", "false": "out"}}],
        )
        (_assert_both_refuse if refused else _assert_both_accept)(pipeline_yaml)

    def test_both_refuse_a_str_value_computed_by_an_upstream_value_transform(self, tmp_path: Path) -> None:
        """The upstream step computes ``tag`` from a ``str`` field, so ``tag`` is ``str`` for the step that reads it."""
        _assert_both_refuse(
            _pipeline(tmp_path, [_untyped_value_transform("raw", "ready", "tag", "row['name']"), _typed_total("ready", "row['tag']")])
        )


class TestValueTransformInputTypeNeitherSurfaceCanProveAgreement:
    """Where the field's type is not established upstream, neither surface refuses."""

    def test_both_accept_a_source_field_read_through_an_undeclaring_value_transform(self, tmp_path: Path) -> None:
        _assert_both_accept(
            _pipeline(tmp_path, [_untyped_value_transform("raw", "ready", "extra", "1"), _typed_total("ready", "row['name']")])
        )

    def test_both_accept_a_value_an_upstream_value_transform_computed_from_an_untyped_field(self, tmp_path: Path) -> None:
        """The upstream step copies a field whose type nobody declared, so its result has no provable type either."""
        _assert_both_accept(
            _pipeline(
                tmp_path,
                [_untyped_value_transform("raw", "ready", "tag", "row['name']"), _typed_total("ready", "row['tag']")],
                source_schema={"mode": "observed", "guaranteed_fields": ["name"]},
            )
        )

    def test_both_accept_a_read_of_the_name_a_rename_removed(self, tmp_path: Path) -> None:
        _assert_both_accept(_pipeline(tmp_path, [_rename("raw", "ready", {"name": "label"}), _typed_total("ready", "row['name']")]))

    def test_both_accept_a_read_of_a_field_a_selecting_mapper_dropped(self, tmp_path: Path) -> None:
        _assert_both_accept(
            _pipeline(tmp_path, [_rename("raw", "ready", {"amount": "amount"}, select_only=True), _typed_total("ready", "row['name']")])
        )

    @pytest.mark.parametrize("behind_pass_through", [False, True], ids=["directly", "through-a-pass-through"])
    def test_both_accept_a_field_an_observed_source_only_guarantees(self, behind_pass_through: bool, tmp_path: Path) -> None:
        steps = [_passthrough("mid", "raw", "ready", _OBSERVED)] if behind_pass_through else []
        _assert_both_accept(
            _pipeline(
                tmp_path,
                [*steps, _typed_total("ready" if steps else "raw", "row['name']")],
                source_schema={"mode": "observed", "guaranteed_fields": ["name"]},
            )
        )


class TestValueTransformInputTypeFromAnObservedSourceAgreement:
    """An observed source declares no field types, but a CSV source can only ever produce text.

    A field such a source guarantees is therefore ``str`` on both surfaces; a
    field it does not guarantee has no type on either.
    """

    @pytest.mark.parametrize("behind_pass_through", [False, True], ids=["directly", "through-a-pass-through"])
    def test_both_refuse_a_guaranteed_csv_field_computed_into_an_int_target(self, behind_pass_through: bool, tmp_path: Path) -> None:
        steps = [_passthrough("mid", "raw", "ready", _OBSERVED)] if behind_pass_through else []
        _assert_both_refuse(
            _pipeline(
                tmp_path,
                [*steps, _typed_total("ready" if steps else "raw", "row['name']")],
                source_schema={"mode": "observed", "guaranteed_fields": ["name"]},
                csv_source=True,
            )
        )

    def test_both_accept_a_csv_field_the_source_does_not_guarantee(self, tmp_path: Path) -> None:
        _assert_both_accept(_pipeline(tmp_path, [_typed_total("raw", "row['name']")], source_schema=_OBSERVED, csv_source=True))
