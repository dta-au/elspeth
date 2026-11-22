"""The composer's two field-type resolvers, compared field by field on the same graphs.

``web/composer/state.py`` works out "what type does this producer give this
field" in two separate walks. One serves a ``value_transform`` that reads the
field in an expression (``field_type`` inside
``_bind_composer_value_transforms``). The other serves a step that declares the
field's type (``_resolved_producer_field_type`` in its ``edge`` mode). Nothing
compared them: each is checked only through its own final verdict.

Each case here builds one upstream and attaches each kind of reader to it in
turn, records what each walk answered for every producer and field it was
asked about, and requires the same answer wherever both were asked.

Two things are deliberately left out:

* Fields declared ``any``. What a producer's ``any`` means to a typed reader is
  under review (``docs/development/composer-any-type-divergence-2026-11-22.md``);
  these cases declare none.
* Renames. The two walks answer differently behind a ``field_mapper`` rename,
  and so do the two runtime walks they copy: an expression's read is typed
  through the rename, a declared type is not checked through it. The second
  class below holds the composer to the runtime on that, by verdict.

The walks are nested functions with no handle a test can call, so the answers
are read with a profile hook keyed on their names. Each case names the answers
it expects to see, so a rename of either function fails the case instead of
leaving it comparing nothing.
"""

from __future__ import annotations

import sys
from pathlib import Path
from types import FrameType
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
from tests.integration.pipeline.test_composer_runtime_agreement_value_transform_types import (
    _OBSERVED,
    _rename,
    _typed_total,
    _untyped_value_transform,
)

_TYPED_SOURCE: dict[str, Any] = {"mode": "fixed", "fields": ["name: str", "amount: int"]}
_Answers = dict[tuple[str, str], str | None]


def _recorded_answers(pipeline_yaml: str) -> tuple[_Answers, _Answers]:
    """Validate the pipeline in the composer; return what each walk answered, by (producer, field)."""
    expression_walk: _Answers = {}
    declaration_walk: _Answers = {}

    def on_return(frame: FrameType, event: str, returned: Any) -> None:
        if event != "return" or not frame.f_code.co_filename.endswith("web/composer/state.py"):
            return
        if frame.f_code.co_name == "field_type":
            key = (frame.f_locals["producer"].producer_id, frame.f_locals["name"])
            # Anything that is not a type name is this walk's "cannot prove".
            expression_walk[key] = returned if isinstance(returned, str) else None
        elif frame.f_code.co_name == "_resolved_producer_field_type" and frame.f_locals["mode"] == "edge":
            key = (frame.f_locals["producer"].producer_id, frame.f_locals["field_name"])
            declaration_walk[key] = None if returned is None else returned.field_type

    sys.setprofile(on_return)
    try:
        _composer(pipeline_yaml)
    finally:
        sys.setprofile(None)
    return expression_walk, declaration_walk


def _both_readers(
    tmp_path: Path,
    steps: list[dict[str, Any]],
    *,
    source_schema: dict[str, Any] = _TYPED_SOURCE,
    fields: tuple[str, str] = ("name", "amount"),
    gates: list[dict[str, Any]] | None = None,
    csv_source: bool = False,
) -> tuple[str, str]:
    """The same upstream twice: read by an expression, and read by a step that declares the fields' types."""
    text, number = fields
    reads = _typed_total("ready", f"row['{number}'] + 1")
    reads["options"]["operations"].append({"target": "seen", "expression": f"row['{text}']"})
    declares = _passthrough("check", "ready", "out", {"mode": "flexible", "fields": [f"{text}: str", f"{number}: int"]})
    source = _source(tmp_path, "rows", "raw", source_schema)
    if csv_source:
        source = {
            "plugin": "csv",
            "on_success": "raw",
            "options": {"path": str(tmp_path / "rows.csv"), "on_validation_failure": "discard", "schema": source_schema},
        }

    def document(reader: dict[str, Any]) -> str:
        body: dict[str, Any] = {"sources": {"rows": source}, "transforms": [*steps, reader], "sinks": {"out": _sink(tmp_path)}}
        if gates:
            body["gates"] = gates
        return _dump(body)

    return document(reads), document(declares)


_GUARANTEED: dict[str, Any] = {"mode": "observed", "guaranteed_fields": ["name", "amount"]}

# name -> (keyword arguments for ``_both_readers`` apart from the steps, the steps,
# the answers both walks must have given: (producer, field) -> type or None).
_CASES: dict[str, tuple[dict[str, Any], list[dict[str, Any]], _Answers]] = {
    "typed-source-behind-a-pass-through": (
        {},
        [_passthrough("mid", "raw", "ready", _OBSERVED)],
        {("mid", "name"): "str", ("mid", "amount"): "int", ("source:rows", "name"): "str", ("source:rows", "amount"): "int"},
    ),
    "typed-source-behind-two-pass-throughs": (
        {},
        [_passthrough("first_hop", "raw", "half", _OBSERVED), _passthrough("second_hop", "half", "ready", _OBSERVED)],
        {("second_hop", "name"): "str", ("first_hop", "amount"): "int", ("source:rows", "name"): "str"},
    ),
    "pass-through-that-declares-both-fields": (
        {},
        [_passthrough("mid", "raw", "ready", {"mode": "flexible", "fields": ["name: str", "amount: int"]})],
        {("mid", "name"): "str", ("mid", "amount"): "int"},
    ),
    "pass-through-that-declares-only-one-field": (
        {},
        [_passthrough("mid", "raw", "ready", {"mode": "flexible", "fields": ["amount: int"]})],
        {("mid", "name"): "str", ("mid", "amount"): "int", ("source:rows", "name"): "str"},
    ),
    "typed-source-behind-a-routing-gate": (
        {"gates": [{"name": "route", "input": "raw", "condition": "True", "routes": {"true": "ready", "false": "out"}}]},
        [],
        {("source:rows", "name"): "str", ("source:rows", "amount"): "int"},
    ),
    "value-transform-that-declares-nothing": (
        {},
        [_untyped_value_transform("raw", "ready", "extra", "row['name'] + '!'")],
        {("first", "name"): None, ("first", "amount"): None},
    ),
    "value-transform-that-computes-the-field": (
        {"fields": ("made", "amount")},
        [_untyped_value_transform("raw", "ready", "made", "row['name'] + '!'")],
        {("first", "made"): "str", ("first", "amount"): None},
    ),
    "source-that-declares-only-one-field": (
        {"source_schema": {"mode": "flexible", "fields": ["amount: int"]}},
        [_passthrough("mid", "raw", "ready", _OBSERVED)],
        {("mid", "name"): None, ("mid", "amount"): "int", ("source:rows", "name"): None},
    ),
    "observed-source-that-guarantees-the-fields": (
        {"source_schema": _GUARANTEED},
        [_passthrough("mid", "raw", "ready", _OBSERVED)],
        {("mid", "name"): None, ("source:rows", "amount"): None},
    ),
    "csv-source-that-guarantees-the-fields": (
        {"source_schema": _GUARANTEED, "csv_source": True},
        [_passthrough("mid", "raw", "ready", _OBSERVED)],
        {("mid", "name"): "str", ("mid", "amount"): "str", ("source:rows", "name"): "str"},
    ),
    "csv-source-that-guarantees-nothing": (
        {"source_schema": _OBSERVED, "csv_source": True},
        [_passthrough("mid", "raw", "ready", _OBSERVED)],
        {("mid", "name"): None, ("source:rows", "amount"): None},
    ),
}


class TestTheTwoFieldTypeWalksGiveTheSameAnswer:
    @pytest.mark.parametrize("case", sorted(_CASES))
    def test_both_walks_answer_alike_for_every_producer_and_field_both_were_asked_about(self, case: str, tmp_path: Path) -> None:
        arguments, steps, expected = _CASES[case]
        read_by_expression, read_by_declaration = _both_readers(tmp_path, steps, **arguments)

        expression_walk, _ = _recorded_answers(read_by_expression)
        _, declaration_walk = _recorded_answers(read_by_declaration)

        asked_of_both = expression_walk.keys() & declaration_walk.keys()
        assert expected.keys() <= asked_of_both
        assert {key: expression_walk[key] for key in asked_of_both} == {key: declaration_walk[key] for key in asked_of_both}
        assert {key: declaration_walk[key] for key in expected} == expected


class TestADeclaredTypeBehindARenameAgreesWithTheRuntime:
    """The declaration walk does not look through a rename. Whatever the runtime decides there, the composer decides too."""

    @pytest.mark.parametrize("declared", ["int", "str"])
    @pytest.mark.parametrize("select_only", [False, True], ids=["keeping-other-fields", "selecting-only-mapped-fields"])
    def test_composer_and_runtime_give_one_verdict_on_a_renamed_field_declared_downstream(
        self, declared: str, select_only: bool, tmp_path: Path
    ) -> None:
        pipeline_yaml = _dump(
            {
                "sources": {"rows": _source(tmp_path, "rows", "raw", _TYPED_SOURCE)},
                "transforms": [
                    _rename("raw", "ready", {"name": "label"}, select_only=select_only),
                    _passthrough("check", "ready", "out", {"mode": "flexible", "fields": [f"label: {declared}"]}),
                ],
                "sinks": {"out": _sink(tmp_path)},
            }
        )

        runtime_refuses = _runtime_refusal(pipeline_yaml) is not None
        composer = _composer(pipeline_yaml)

        assert (not composer.is_valid) == runtime_refuses, [(entry.error_code, entry.message) for entry in composer.errors]

    def test_the_same_mismatch_without_the_rename_is_refused_by_both(self, tmp_path: Path) -> None:
        """Control: the declaration check does run on this edge, so agreeing behind a rename is not two silent passes."""
        pipeline_yaml = _dump(
            {
                "sources": {"rows": _source(tmp_path, "rows", "raw", _TYPED_SOURCE)},
                "transforms": [
                    _passthrough("mid", "raw", "ready", _OBSERVED),
                    _passthrough("check", "ready", "out", {"mode": "flexible", "fields": ["name: int"]}),
                ],
                "sinks": {"out": _sink(tmp_path)},
            }
        )

        assert _runtime_refusal(pipeline_yaml) is not None
        composer = _composer(pipeline_yaml)
        assert [entry.error_code for entry in composer.errors] == ["edge_field_type_incompatible"]
