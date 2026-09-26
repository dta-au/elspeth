# tests/integration/pipeline/test_output_declaration_routing.py
"""Operator-level proof of ADR-050: transform outputs DECLARE, sources infer-and-lock.

Every case is a real ``elspeth run --execute`` over a settings file, read back
through the CLI exit code, the sink files and the Landscape. The shapes are the
ones the elspeth-5887fb7928 R2 unit and the S1 specialist panel measured
(``.claude/lanes/5887-batch-row/PANEL-S1-S3-synthesis.md`` §S1.5 tests):

- T1: row-to-row type variance at a transform output under a pure observed
  pipeline no longer aborts the run (before ADR-050: exit 4,
  ``ContractMergeError`` at the node-contract merge), and the node's recorded
  output contract is BYTE-IDENTICAL after row 1 and after row N — the tripwire
  that separates "declared before row 1" from "widened as rows arrive".
- T2/T3: a concrete operator type is ENFORCED on the emitted value — the
  violating row (or the parent of an exploded row) is routed with a
  value-free reason recording ``declared_by: operator`` (before: ``page: int,
  declared`` recorded while a str was delivered, exit 0).
- T4: an explicit ``any`` on a value_transform target is honoured.
- T5: value_transform as a TYPED CONSUMER of a carried field passes a valid
  row and routes the wrong-typed one (before: Tier-1
  ``SchemaConfigModeViolation`` on a VALID row).
- T6: nulls — ``int?`` accepts, ``int`` routes, json_explode records
  ``nullable`` truthfully, null-first and null-later under ``any`` both pass.
- T8: two arrival orders and ``max_workers > 1`` record byte-identical
  contracts.
- T10: the SOURCE seam is unchanged (infer-and-lock, value-validated).
- T11: two observed sources disagreeing on a column share one sink
  (before: exit 4, ``FrameworkBugError`` at the sink batch merge).
- T12: an ``any`` field reaching a typed downstream consumer routes there.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from pathlib import Path
from typing import Any, ClassVar

import pytest
import yaml

SENTINEL = "SENTINEL_ADR050_ROW_VALUE"


def _write_jsonl(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text("".join(json.dumps(row) + "\n" for row in rows))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def _json_source(
    path: Path, *, on_success: str = "rows", schema: dict[str, Any] | None = None, on_validation_failure: str = "discard"
) -> dict[str, Any]:
    return {
        "plugin": "json",
        "on_success": on_success,
        "options": {
            "path": str(path),
            "format": "jsonl",
            "on_validation_failure": on_validation_failure,
            "schema": schema or {"mode": "observed"},
        },
    }


def _json_sink(path: Path) -> dict[str, Any]:
    return {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(path), "format": "jsonl", "schema": {"mode": "observed"}},
    }


def _settings(
    tmp_path: Path,
    *,
    sources: dict[str, dict[str, Any]],
    transforms: list[dict[str, Any]] = (),
    aggregations: list[dict[str, Any]] = (),
    max_workers: int = 1,
    quarantine: bool = True,
) -> Path:
    sinks = {"out": _json_sink(tmp_path / "out.jsonl")}
    if quarantine:
        sinks["quarantine"] = _json_sink(tmp_path / "q.jsonl")
    settings: dict[str, Any] = {
        "sources": sources,
        "concurrency": {"max_workers": max_workers},
        "sinks": sinks,
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    if transforms:
        settings["transforms"] = list(transforms)
    if aggregations:
        settings["aggregations"] = list(aggregations)
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _run(settings_path: Path) -> Any:
    from typer.testing import CliRunner

    from elspeth.cli import app

    runner = CliRunner()
    validated = runner.invoke(app, ["validate", "-s", str(settings_path)])
    assert validated.exit_code == 0, validated.output
    result = runner.invoke(app, ["run", "-s", str(settings_path), "--execute"])
    assert "Traceback" not in result.output, result.output
    return result


def _transform_node_contracts(tmp_path: Path) -> dict[str, str]:
    """``nodes.output_contract_json`` per transform node id, as recorded (raw text)."""
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return dict(con.execute("select node_id, output_contract_json from nodes where node_id like 'transform_%'").fetchall())
    finally:
        con.close()


def _outcomes(tmp_path: Path) -> dict[tuple[str, str], int]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return {(outcome, path): n for outcome, path, n in con.execute("select outcome, path, count(*) from token_outcomes group by 1, 2")}
    finally:
        con.close()


def _transform_error_reasons(tmp_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return [json.loads(text) for (text,) in con.execute("select error_details_json from transform_errors order by rowid")]
    finally:
        con.close()


def _audit_cells_containing(tmp_path: Path, needle: str) -> list[tuple[str, str]]:
    """Every (table, column) of the Landscape whose text contains ``needle`` (R1 ``scan.py``)."""
    con = sqlite3.connect(tmp_path / "audit.db")
    hits: list[tuple[str, str]] = []
    try:
        for (table,) in con.execute("select name from sqlite_master where type='table'"):
            for column in [row[1] for row in con.execute(f'pragma table_info("{table}")')]:
                (count,) = con.execute(f'select count(*) from "{table}" where cast("{column}" as text) like ?', (f"%{needle}%",)).fetchone()
                if count:
                    hits.append((table, column))
    finally:
        con.close()
    return hits


def _vt(
    *, schema: dict[str, Any], operations: list[dict[str, str]], name: str = "lift", input_conn: str = "rows", on_success: str = "out"
) -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "value_transform",
        "input": input_conn,
        "on_success": on_success,
        "on_error": "quarantine",
        "options": {"schema": schema, "operations": operations},
    }


def _explode(*, schema: dict[str, Any], name: str = "explode", on_success: str = "out") -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "json_explode",
        "input": "rows",
        "on_success": on_success,
        "on_error": "quarantine",
        "options": {"array_field": "pages", "output_field": "page", "schema": schema},
    }


def _fm(*, schema: dict[str, Any], mapping: dict[str, str], name: str = "lift", input_conn: str = "rows") -> dict[str, Any]:
    return {
        "name": name,
        "plugin": "field_mapper",
        "input": input_conn,
        "on_success": "out",
        "on_error": "quarantine",
        "options": {"schema": schema, "mapping": mapping},
    }


OBSERVED = {"mode": "observed"}

# The six R2 shapes plus the field_mapper dotted extraction (shape 7) and the
# overwrite shape (SA probe B). Each is (rows, transform); the first row alone
# is the "after row 1" run.
_VARIANCE_SHAPES: dict[str, tuple[list[dict[str, Any]], dict[str, Any]]] = {
    "vt_str": (
        [{"id": 1, "meta": {"copies": 2}}, {"id": 2, "meta": {"copies": SENTINEL}}, {"id": 3, "meta": {"copies": 3}}],
        _vt(schema=OBSERVED, operations=[{"target": "copies", "expression": "row['meta']['copies']"}]),
    ),
    # The float is COMPUTED by the transform (the source seam would lock
    # ``score: int`` on row 1 and quarantine a later 4.5 — that is T10).
    "vt_float": (
        [{"id": 1, "score": 5}, {"id": 2, "score": 9}, {"id": 3, "score": 7}],
        _vt(schema=OBSERVED, operations=[{"target": "lifted", "expression": "row['score'] / 2 if row['id'] == 2 else row['score']"}]),
    ),
    "vt_nullfirst": (
        [{"id": 1, "score": None}, {"id": 2, "score": 4}, {"id": 3, "score": 7}],
        _vt(schema=OBSERVED, operations=[{"target": "lifted", "expression": "row['score']"}]),
    ),
    "vt_nulllater": (
        [{"id": 1, "score": 4}, {"id": 2, "score": None}, {"id": 3, "score": 7}],
        _vt(schema=OBSERVED, operations=[{"target": "lifted", "expression": "row['score']"}]),
    ),
    "explode_str": (
        [{"doc": "D1", "pages": [1, 2]}, {"doc": "D2", "pages": [3, SENTINEL]}, {"doc": "D3", "pages": [4]}],
        _explode(schema=OBSERVED),
    ),
    "explode_null": (
        [{"doc": "D1", "pages": [1, 2]}, {"doc": "D2", "pages": [3, None]}, {"doc": "D3", "pages": [4]}],
        _explode(schema=OBSERVED),
    ),
    "fm_dotted": (
        [{"id": 1, "meta": {"copies": 2}}, {"id": 2, "meta": {"copies": SENTINEL}}, {"id": 3, "meta": {"copies": 3}}],
        _fm(schema=OBSERVED, mapping={"meta.copies": "copies"}),
    ),
    "overwrite": (
        [{"id": 1, "score": 4, "half": False}, {"id": 2, "score": 4, "half": True}, {"id": 3, "score": 5, "half": False}],
        _vt(schema=OBSERVED, operations=[{"target": "score", "expression": "row['score'] * 0.5 if row['half'] else row['score']"}]),
    ),
}


@pytest.mark.parametrize("shape", sorted(_VARIANCE_SHAPES))
def test_type_variance_at_a_transform_output_never_aborts_and_the_record_is_fixed_from_row_one(shape: str, tmp_path: Path) -> None:
    """T1: exit 0, every token terminal, and the node contract after row N is byte-identical to the one after row 1."""
    rows, transform = _VARIANCE_SHAPES[shape]

    first = tmp_path / "first"
    first.mkdir()
    _write_jsonl(first / "in.jsonl", rows[:1])
    result_first = _run(_settings(first, sources={"src": _json_source(first / "in.jsonl")}, transforms=[transform]))
    assert result_first.exit_code == 0, result_first.output

    full = tmp_path / "full"
    full.mkdir()
    _write_jsonl(full / "in.jsonl", rows)
    result_full = _run(_settings(full, sources={"src": _json_source(full / "in.jsonl")}, transforms=[transform]))
    assert result_full.exit_code == 0, result_full.output

    # Every row reached the sink: no routing, no abandonment.
    assert len(_read_jsonl(full / "out.jsonl")) >= len(rows)
    assert _read_jsonl(full / "q.jsonl") == []
    outcomes = _outcomes(full)
    assert all(outcome in {"success", "transient"} for outcome, _ in outcomes), outcomes

    # The WIDEN-by-another-name tripwire: the record did not change shape as rows arrived.
    after_row_one = _transform_node_contracts(first)
    after_row_n = _transform_node_contracts(full)
    assert set(after_row_one) == set(after_row_n) and after_row_one, after_row_n
    for node_id, recorded in after_row_n.items():
        assert recorded == after_row_one[node_id], f"{shape}: the node contract moved between row 1 and row N"
        fields = {field["normalized_name"]: field for field in json.loads(recorded)["fields"]}
        created = (
            "page"
            if shape.startswith("explode")
            else ("copies" if shape in {"vt_str", "fm_dotted"} else ("score" if shape == "overwrite" else "lifted"))
        )
        assert fields[created]["python_type"] == "object"
        assert fields[created]["nullable"] is True
        assert fields[created]["source"] == "declared"
        assert json.loads(recorded)["locked"] is True


@pytest.mark.parametrize(
    ("shape", "transform", "created", "expected_reason", "routed_by"),
    [
        pytest.param(
            "vt_str",
            _vt(
                schema={"mode": "flexible", "fields": ["id: int", "copies: int"]},
                operations=[{"target": "copies", "expression": "row['meta']['copies']"}],
            ),
            "copies",
            "type_mismatch",
            "plugin",
            id="value_transform-pins-its-target",
        ),
        pytest.param(
            "explode_str",
            _explode(schema={"mode": "flexible", "fields": ["doc: any", "pages: any", "page: int"]}),
            "page",
            "contract_violation",
            "engine",
            id="json_explode-declared-int-enforced-on-the-element",
        ),
        pytest.param(
            "fm_dotted",
            _fm(schema={"mode": "flexible", "fields": ["id: int", "copies: int"]}, mapping={"meta.copies": "copies"}),
            "copies",
            "contract_violation",
            "engine",
            id="field_mapper-declared-int-enforced-on-the-extraction",
        ),
    ],
)
def test_a_concrete_operator_type_routes_the_violating_row_value_free(
    shape: str, transform: dict[str, Any], created: str, expected_reason: str, routed_by: str, tmp_path: Path
) -> None:
    """T2/T3: the row (or the exploded parent) is routed; the value appears only where the row itself is stored."""
    rows, _ = _VARIANCE_SHAPES[shape]
    _write_jsonl(tmp_path / "in.jsonl", rows)
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    # PARTIAL: some rows succeeded, one was routed.
    assert result.exit_code == 1, result.output
    delivered = _read_jsonl(tmp_path / "out.jsonl")
    assert all(row[created] != SENTINEL for row in delivered)
    assert all(isinstance(row[created], int) for row in delivered), delivered
    quarantined = _read_jsonl(tmp_path / "q.jsonl")
    assert len(quarantined) == 1
    outcomes = _outcomes(tmp_path)
    assert outcomes[("failure", "on_error_routed")] == 1

    [reason] = _transform_error_reasons(tmp_path)
    assert reason["reason"] == expected_reason
    assert reason["field"] == created
    assert reason["expected"] == "int"
    assert reason["actual"] == "str"
    if routed_by == "engine":
        # The OPERATOR declared the type and the transform created the field
        # from the row's own data (the array element, the dotted leaf): a data
        # fault against the pipeline's declaration, never attributed to the
        # plugin (review-S1a-declare-r1 A1).
        assert (reason["declared_by"], reason["authorship"]) == ("operator", "computed")
        assert "declared int by the pipeline's schema (the transform created the field)" in reason["error"]
        assert "fix the transform" not in reason["error"]
        assert reason["emitted_index"] == (1 if shape == "explode_str" else 0)
    assert SENTINEL not in json.dumps(reason)

    # The value is the row's own data and lives only where the row is stored.
    assert set(_audit_cells_containing(tmp_path, SENTINEL)) == {("transform_errors", "row_data_json")}


def test_an_explicit_any_on_a_value_transform_target_is_honoured(tmp_path: Path) -> None:
    """T4 (panel B7): ``copies: any`` declared by the operator stores an int and a str alike, exit 0."""
    rows, _ = _VARIANCE_SHAPES["vt_str"]
    _write_jsonl(tmp_path / "in.jsonl", rows)
    transform = _vt(
        schema={"mode": "flexible", "fields": ["id: any", "copies: any"]},
        operations=[{"target": "copies", "expression": "row['meta']['copies']"}],
    )
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert [row["copies"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [2, SENTINEL, 3]
    [recorded] = _transform_node_contracts(tmp_path).values()
    copies = next(field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "copies")
    assert (copies["python_type"], copies["nullable"], copies["source"]) == ("object", True, "declared")


class TestValueTransformAsATypedConsumer:
    """T5 (panel shape 8): a declared CARRIED field is stamped, so a valid row passes and a wrong-typed one routes."""

    _TRANSFORM = _vt(
        schema={"mode": "flexible", "fields": ["score: int?"]}, operations=[{"target": "doubled", "expression": "row['score']"}]
    )

    def test_a_valid_row_passes(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "score": 4}, {"id": 2, "score": 7}])
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[self._TRANSFORM]))
        assert result.exit_code == 0, result.output
        assert "SchemaConfigModeViolation" not in result.output
        assert [row["doubled"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [4, 7]

    def test_the_wrong_typed_row_is_routed_at_the_consumer(self, tmp_path: Path) -> None:
        _write_jsonl(
            tmp_path / "in.jsonl", [{"id": 1, "score": None}, {"id": 2, "score": 4}, {"id": 3, "score": SENTINEL}, {"id": 4, "score": 7}]
        )
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[self._TRANSFORM]))
        assert result.exit_code == 1, result.output
        assert "SchemaConfigModeViolation" not in result.output
        assert [row["id"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [1, 2, 4]
        assert [row["id"] for row in _read_jsonl(tmp_path / "q.jsonl")] == [3]
        [reason] = _transform_error_reasons(tmp_path)
        assert reason["reason"] == "contract_violation"
        assert "score: [int_type]" in reason["error"]
        assert SENTINEL not in json.dumps(reason)


def test_a_field_mapper_rename_of_a_declared_field_is_not_re_adjudicated(tmp_path: Path) -> None:
    """A rename whose target inherits the source's declaration carries the admitted value: ``int`` under ``float`` is delivered.

    ``carried_output_fields()`` names are never produced (``declared_output_types``):
    the projected ``total: float`` declaration and the exact-type value check
    would otherwise route a valid row as a transform fault (measured on
    ad01df079: exit 2, both rows quarantined; pre-ADR-050 2c41e123e: exit 0).
    """
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "amount": 5}, {"id": 2, "amount": 7}])
    transform = _fm(schema={"mode": "flexible", "fields": ["id: int", "amount: float"]}, mapping={"amount": "total"})
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == [{"id": 1, "total": 5}, {"id": 2, "total": 7}]
    assert _transform_error_reasons(tmp_path) == []
    assert _outcomes(tmp_path) == {("success", "default_flow"): 2}


def test_a_rename_declared_only_by_its_target_name_is_value_checked(tmp_path: Path) -> None:
    """The operator's ``b: int`` on a rename TARGET is enforced: the str row is routed, the int row delivered.

    No input check ever held ``a``'s value to a declaration written against
    ``b`` (the source is observed and undeclared here), so the target is not a
    ``carried_output_fields()`` name and its value is checked like a created
    field's (review-S1a-r1 F1: on 864602c4d the str was delivered under a
    recorded ``b: int declared``, exit 0).
    """
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": SENTINEL}, {"id": 2, "a": "x2"}])
    transform = _fm(schema={"mode": "flexible", "fields": ["b: int"]}, mapping={"a": "b"})
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    assert result.exit_code == 2, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == []
    assert len(_read_jsonl(tmp_path / "q.jsonl")) == 2
    assert _outcomes(tmp_path) == {("failure", "on_error_routed"): 2}

    reasons = _transform_error_reasons(tmp_path)
    assert len(reasons) == 2
    for reason in reasons:
        assert (reason["reason"], reason["field"], reason["expected"], reason["actual"]) == ("contract_violation", "b", "int", "str")
        assert (reason["declared_by"], reason["authorship"]) == ("operator", "computed")
        assert SENTINEL not in json.dumps(reason)


def test_a_rename_declared_only_by_its_target_name_delivers_a_matching_value(tmp_path: Path) -> None:
    """The same target-only ``b: int`` delivers an int row: the check routes a wrong type, not the rename."""
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 5}, {"id": 2, "a": 7}])
    transform = _fm(schema={"mode": "flexible", "fields": ["b: int"]}, mapping={"a": "b"})
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == [{"id": 1, "b": 5}, {"id": 2, "b": 7}]
    assert _transform_error_reasons(tmp_path) == []
    [recorded] = _transform_node_contracts(tmp_path).values()
    [b_contract] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "b"]
    assert (b_contract["python_type"], b_contract["source"]) == ("int", "declared")


def test_an_undeclared_observed_rename_keeps_the_source_fields_contract(tmp_path: Path) -> None:
    """A rename nobody declared still carries the source's locked contract, not a stamped ``any``.

    The control that separates "not carried when the author declared only the
    target" from the broader "carried only when the source is declared": under
    the broader rule an observed-mode rename target would be stamped
    ``object``/``declared`` over the source's inferred ``str``.
    """
    _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": "x1"}, {"id": 2, "a": "x2"}])
    transform = _fm(schema=OBSERVED, mapping={"a": "b"})
    result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == [{"id": 1, "b": "x1"}, {"id": 2, "b": "x2"}]
    [recorded] = _transform_node_contracts(tmp_path).values()
    [b_contract] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "b"]
    assert (b_contract["python_type"], b_contract["source"]) == ("str", "inferred")


@pytest.mark.parametrize(
    ("header", "mapping", "select_only", "expected"),
    [
        pytest.param(
            "ID,Name",
            {"ID": "ID", "Name": "Name"},
            True,
            [{"ID": "1", "Name": "Ann"}, {"ID": "2", "Name": "Bob"}],
            id="select-by-case-variant-header",
        ),
        pytest.param(
            "id,First Name",
            {"First Name": "First Name"},
            False,
            [{"First Name": "Ann", "id": "1"}, {"First Name": "Bob", "id": "2"}],
            id="identity-by-messy-header",
        ),
    ],
)
def test_an_identity_mapping_by_original_header_delivers_under_the_source_contract(
    header: str, mapping: dict[str, str], select_only: bool, expected: list[dict[str, str]], tmp_path: Path
) -> None:
    """Keeping a column by its CSV header spelling completes, as it did before ADR-050 (review-S1a-r2 F1).

    ``process`` writes the literal header key, which the input row (keyed by
    normalized name) never had. On cf9750351 the completeness contract read
    it as an undeclared created field: ``UndeclaredOutputFieldsViolation``,
    exit 4, run failed. The key carries the source field's inferred contract.
    """
    (tmp_path / "in.csv").write_text(f"{header}\n1,Ann\n2,Bob\n")
    source = {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": str(tmp_path / "in.csv"), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
    }
    transform = _fm(schema=OBSERVED, mapping=mapping)
    transform["options"]["select_only"] = select_only
    result = _run(_settings(tmp_path, sources={"src": source}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == expected
    assert _transform_error_reasons(tmp_path) == []
    assert _outcomes(tmp_path) == {("success", "default_flow"): 2}
    [recorded] = _transform_node_contracts(tmp_path).values()
    by_name = {field["normalized_name"]: field for field in json.loads(recorded)["fields"]}
    for key in mapping:
        assert (by_name[key]["python_type"], by_name[key]["source"]) == ("str", "inferred")


def test_a_rename_spelled_by_original_header_never_guesses_the_source_declaration(tmp_path: Path) -> None:
    """A header-spelled source is not matched to a declaration by its normalized spelling (review-S1a-r2 F2).

    ``First Name`` resolves through the source's ``field_mapping`` to
    ``given_name``, not to ``first_name``: which field it names is known only
    from the row's contract. Reading the operator's ``first_name: int?`` as
    the source's declaration would carry ``given`` under ``int`` with no value
    check. Measured on a mutant that did so: a str delivered under a recorded
    ``given: int declared``, exit 0. The target's own ``given: str`` is the
    declaration that stands, and it is checked.
    """
    (tmp_path / "in.csv").write_text("id,First Name\n1,Ann\n2,Bob\n")
    source = {
        "plugin": "csv",
        "on_success": "rows",
        "options": {
            "path": str(tmp_path / "in.csv"),
            "on_validation_failure": "discard",
            "field_mapping": {"first_name": "given_name"},
            "schema": {"mode": "observed"},
        },
    }
    transform = _fm(schema={"mode": "flexible", "fields": ["first_name: int?", "given: str"]}, mapping={"First Name": "given"})
    result = _run(_settings(tmp_path, sources={"src": source}, transforms=[transform]))

    assert result.exit_code == 0, result.output
    assert _read_jsonl(tmp_path / "out.jsonl") == [{"given": "Ann", "id": "1"}, {"given": "Bob", "id": "2"}]
    [recorded] = _transform_node_contracts(tmp_path).values()
    [given] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "given"]
    assert (given["python_type"], given["source"]) == ("str", "declared")


_ANN_BOB_CSV = "id,First Name\n1,Ann\n2,Bob\n"
_ID_NAME_CSV = "ID,Name\n1,Ann\n2,Bob\n"
_ID_NAME_SENTINEL_CSV = f"ID,Name\n1,{SENTINEL}\n2,Bob\n"
_A_STR_JSONL = f'{{"id":1,"a":"{SENTINEL}"}}\n{{"id":2,"a":"x2"}}\n'
_ANN_BOB = [{"given": "Ann", "id": "1"}, {"given": "Bob", "id": "2"}]
_FIRST_NAME_ANN_BOB = [{"First Name": "Ann", "id": "1"}, {"First Name": "Bob", "id": "2"}]


@dataclasses.dataclass(frozen=True)
class _MapperShape:
    """One reviewed field_mapper run: its input, its node, and the outcome it must keep.

    ``delivered`` is the sink's rows, or ``None`` when every row is routed;
    ``reasons`` is the set of routed ``(reason, field, expected, actual,
    declared_by, authorship)``; ``recorded`` is the node record's
    ``(python_type, source)`` per mapping target.
    """

    input_name: str
    text: str
    mapping: dict[str, str]
    fields: tuple[str, ...] | None = None
    select_only: bool = False
    source_schema: dict[str, Any] = dataclasses.field(default_factory=lambda: OBSERVED)
    source_field_mapping: dict[str, str] | None = None
    delivered: list[dict[str, Any]] | None = None
    reasons: frozenset[tuple[str | None, ...]] = frozenset()
    recorded: dict[str, tuple[str, str]] = dataclasses.field(default_factory=dict)


def _routed(name: str) -> frozenset[tuple[str | None, ...]]:
    """The one value-free reason a str under an operator's ``int`` routes with."""
    return frozenset({("contract_violation", name, "int", "str", "operator", "computed")})


# Every field_mapper and original-header shape the S1a reviews ran as real CLI
# runs (review-S1a-r1/r2/r3 ``cfg/``), keyed by the reviewer's config name.
_MAPPER_SHAPES: dict[str, _MapperShape] = {
    # review-S1a-r3 F1: a rename TARGET spelled as the source's header is a
    # created field. Before the fix the first two delivered a str under a
    # recorded ``Name: int declared`` with exit 0: ``Name`` resolved to the
    # input field ``name`` through its original_name and read as unchanged.
    "r3_norm_to_hdr_int": _MapperShape("in.csv", _ID_NAME_SENTINEL_CSV, {"name": "Name"}, ("Name: int",), reasons=_routed("Name")),
    "r3_norm_to_hdr_intq": _MapperShape("in.csv", _ID_NAME_SENTINEL_CSV, {"name": "Name"}, ("Name: int?",), reasons=_routed("Name")),
    "hdr_name_intq": _MapperShape("in.csv", _ID_NAME_CSV, {"Name": "Name"}, ("Name: int?",), reasons=_routed("Name")),
    # Same root cause: this created field was recorded ``authorship: carried``.
    "r3_id_to_hdr_int": _MapperShape(
        "in.csv", _ID_NAME_SENTINEL_CSV, {"id": "Name"}, ("Name: int",), select_only=True, reasons=_routed("Name")
    ),
    # The spelling control: the same rename to a target that is no header.
    "r3_norm_to_other_int": _MapperShape("in.csv", _ID_NAME_SENTINEL_CSV, {"name": "given"}, ("given: int",), reasons=_routed("given")),
    # A REQUIRED header literal is missing from the normalized input row: the input check routes it.
    "hdr_name_int": _MapperShape(
        "in.csv", _ID_NAME_CSV, {"Name": "Name"}, ("Name: int",), reasons=frozenset({("contract_violation", None, None, None, None, None)})
    ),
    # An undeclared identity by header is carried under the source's contract (review-S1a-r2 F1).
    "hdr_caseid_select": _MapperShape(
        "in.csv",
        _ID_NAME_CSV,
        {"ID": "ID", "Name": "Name"},
        select_only=True,
        delivered=[{"ID": "1", "Name": "Ann"}, {"ID": "2", "Name": "Bob"}],
        recorded={"ID": ("str", "inferred"), "Name": ("str", "inferred")},
    ),
    "hdr_identity": _MapperShape(
        "in.csv", _ANN_BOB_CSV, {"First Name": "First Name"}, delivered=_FIRST_NAME_ANN_BOB, recorded={"First Name": ("str", "inferred")}
    ),
    "hdr_identity_decl": _MapperShape(
        "in.csv",
        _ANN_BOB_CSV,
        {"First Name": "First Name"},
        ("first_name: str",),
        delivered=_FIRST_NAME_ANN_BOB,
        recorded={"First Name": ("object", "declared")},
    ),
    "hdr_rename": _MapperShape(
        "in.csv", _ANN_BOB_CSV, {"First Name": "given"}, delivered=_ANN_BOB, recorded={"given": ("str", "inferred")}
    ),
    "hdr_rename_target_int": _MapperShape("in.csv", _ANN_BOB_CSV, {"First Name": "given"}, ("given: int",), reasons=_routed("given")),
    # review-S1a-r2 F2, pinned by design (8bf33f2df): a header-spelled SOURCE is
    # resolved only by the row's contract, which construction does not have, so
    # it never borrows a declaration by its normalized spelling.
    "hdr_rename_both": _MapperShape(
        "in.csv", _ANN_BOB_CSV, {"First Name": "given"}, ("first_name: str", "given: int"), reasons=_routed("given")
    ),
    "hdr_rename_both_normalized": _MapperShape(
        "in.csv",
        _ANN_BOB_CSV,
        {"first_name": "given"},
        ("first_name: str", "given: int"),
        delivered=_ANN_BOB,
        recorded={"given": ("str", "declared")},
    ),
    "hdr_fieldmap_resolve": _MapperShape(
        "in.csv",
        _ANN_BOB_CSV,
        {"First Name": "given"},
        source_field_mapping={"first_name": "given_name"},
        delivered=_ANN_BOB,
        recorded={"given": ("str", "inferred")},
    ),
    "hdr_fieldmap_lie": _MapperShape(
        "in.csv",
        _ANN_BOB_CSV,
        {"First Name": "given"},
        ("first_name: int?", "given: str"),
        source_field_mapping={"first_name": "given_name"},
        delivered=_ANN_BOB,
        recorded={"given": ("str", "declared")},
    ),
    # review-S1a-r1: normalized renames.
    "fm_target_decl": _MapperShape("in.jsonl", _A_STR_JSONL, {"a": "b"}, ("b: int?",), reasons=_routed("b")),
    "fm_target_decl_req": _MapperShape("in.jsonl", _A_STR_JSONL, {"a": "b"}, ("b: int",), reasons=_routed("b")),
    "fm_target_decl_srctyped": _MapperShape(
        "in.jsonl",
        _A_STR_JSONL,
        {"a": "b"},
        ("b: int",),
        source_schema={"mode": "flexible", "fields": ["id: int", "a: str"]},
        reasons=_routed("b"),
    ),
    "fm_target_valid": _MapperShape(
        "in.jsonl",
        '{"id":1,"a":5}\n{"id":2,"a":7}\n{"id":3,"a":9}\n',
        {"a": "b"},
        ("b: int",),
        delivered=[{"b": 5, "id": 1}, {"b": 7, "id": 2}, {"b": 9, "id": 3}],
        recorded={"b": ("int", "declared")},
    ),
    "fm_target_valid_row1": _MapperShape(
        "in.jsonl", '{"id":1,"a":5}\n', {"a": "b"}, ("b: int",), delivered=[{"b": 5, "id": 1}], recorded={"b": ("int", "declared")}
    ),
    "fm_both": _MapperShape(
        "in.jsonl",
        '{"id":1,"a":"s1"}\n{"id":2,"a":"s2"}\n',
        {"a": "b"},
        ("a: str", "b: int"),
        delivered=[{"b": "s1", "id": 1}, {"b": "s2", "id": 2}],
        recorded={"b": ("str", "declared")},
    ),
    "fm_neither": _MapperShape(
        "in.jsonl",
        '{"id":1,"a":"s1"}\n{"id":2,"a":"s2"}\n',
        {"a": "b"},
        ("id: int",),
        delivered=[{"b": "s1", "id": 1}, {"b": "s2", "id": 2}],
        recorded={"b": ("object", "declared")},
    ),
    "fm_rename_float": _MapperShape(
        "in.jsonl",
        '{"id": 1, "amount": 5}\n{"id": 2, "amount": 7}\n',
        {"amount": "total"},
        ("id: int", "amount: float"),
        delivered=[{"id": 1, "total": 5}, {"id": 2, "total": 7}],
        recorded={"total": ("float", "declared")},
    ),
    "fm_rename_hdr": _MapperShape(
        "in.jsonl",
        '{"id":1,"amount":5.5}\n{"id":2,"amount":7.5}\n',
        {"amount": "total"},
        ("id: int", "amount: float"),
        delivered=[{"id": 1, "total": 5.5}, {"id": 2, "total": 7.5}],
        recorded={"total": ("float", "declared")},
    ),
    "fm_str_observed": _MapperShape(
        "in.jsonl",
        f'{{"id": 1, "meta": {{"copies": 2}}}}\n{{"id": 2, "meta": {{"copies": "{SENTINEL}"}}}}\n',
        {"meta.copies": "copies"},
        delivered=[{"copies": 2, "id": 1, "meta": {"copies": 2}}, {"copies": SENTINEL, "id": 2, "meta": {"copies": SENTINEL}}],
        recorded={"copies": ("object", "declared")},
    ),
    "fm_str_observed_row1": _MapperShape(
        "in.jsonl",
        '{"id": 1, "meta": {"copies": 2}}\n',
        {"meta.copies": "copies"},
        delivered=[{"copies": 2, "id": 1, "meta": {"copies": 2}}],
        recorded={"copies": ("object", "declared")},
    ),
}


@pytest.mark.parametrize("shape", sorted(_MAPPER_SHAPES))
def test_every_reviewed_field_mapper_shape_keeps_its_outcome_whatever_the_spelling(shape: str, tmp_path: Path) -> None:
    """Real ``elspeth run`` over every field_mapper and original-header shape of reviews S1a r1 to r3.

    Enforcement never depends on how a name is spelled: the value check reads
    the input and emitted rows by NORMALIZED key only (ADR-050 Decision 5),
    so a target spelled like a source header is a created field exactly as a
    target spelled ``given`` is. A routed reason never carries the row value.
    """
    case = _MAPPER_SHAPES[shape]
    (tmp_path / case.input_name).write_text(case.text)
    is_csv = case.input_name.endswith(".csv")
    options: dict[str, Any] = {"path": str(tmp_path / case.input_name), "on_validation_failure": "discard", "schema": case.source_schema}
    if not is_csv:
        options["format"] = "jsonl"
    if case.source_field_mapping is not None:
        options["field_mapping"] = case.source_field_mapping
    source = {"plugin": "csv" if is_csv else "json", "on_success": "rows", "options": options}
    schema = OBSERVED if case.fields is None else {"mode": "flexible", "fields": list(case.fields)}
    transform = _fm(schema=schema, mapping=case.mapping)
    transform["options"]["select_only"] = case.select_only
    result = _run(_settings(tmp_path, sources={"src": source}, transforms=[transform]))

    rows = len(case.text.splitlines()) - (1 if is_csv else 0)
    reasons = _transform_error_reasons(tmp_path)
    assert {
        (r["reason"], r.get("field"), r.get("expected"), r.get("actual"), r.get("declared_by"), r.get("authorship")) for r in reasons
    } == case.reasons
    assert all(SENTINEL not in json.dumps(reason) for reason in reasons)
    if case.delivered is None:
        assert result.exit_code == 2, result.output
        assert _read_jsonl(tmp_path / "out.jsonl") == []
        assert len(_read_jsonl(tmp_path / "q.jsonl")) == len(reasons) == rows
        assert _outcomes(tmp_path) == {("failure", "on_error_routed"): rows}
    else:
        assert result.exit_code == 0, result.output
        assert _read_jsonl(tmp_path / "out.jsonl") == case.delivered
        assert _outcomes(tmp_path) == {("success", "default_flow"): rows}
    recorded = {
        contract_field["normalized_name"]: (contract_field["python_type"], contract_field["source"])
        for text in _transform_node_contracts(tmp_path).values()
        if text
        for contract_field in json.loads(text)["fields"]
        if contract_field["normalized_name"] in case.mapping.values()
    }
    assert recorded == case.recorded


class TestNulls:
    """T6: None is presence, not a type — the declaration decides."""

    def test_a_nullable_declaration_accepts_null(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 10}, {"id": 2, "a": 11}])
        transform = _vt(schema={"mode": "flexible", "fields": ["id: int", "a: int?"]}, operations=[{"target": "a", "expression": "None"}])
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        assert [row["a"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [None, None]

    def test_a_non_nullable_declaration_routes_null(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 10}, {"id": 2, "a": 11}])
        transform = _vt(schema={"mode": "flexible", "fields": ["id: int", "a: int"]}, operations=[{"target": "a", "expression": "None"}])
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 2, result.output
        assert _read_jsonl(tmp_path / "out.jsonl") == []
        assert len(_read_jsonl(tmp_path / "q.jsonl")) == 2
        for reason in _transform_error_reasons(tmp_path):
            assert (reason["reason"], reason["field"], reason["expected"], reason["actual"]) == ("type_mismatch", "a", "int", "NoneType")

    def test_json_explode_records_a_null_element_as_nullable(self, tmp_path: Path) -> None:
        rows, transform = _VARIANCE_SHAPES["explode_null"]
        _write_jsonl(tmp_path / "in.jsonl", rows)
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        assert [row["page"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [1, 2, 3, None, 4]
        [recorded] = _transform_node_contracts(tmp_path).values()
        page = next(field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "page")
        assert (page["python_type"], page["nullable"], page["source"]) == ("object", True, "declared")


def test_two_arrival_orders_and_a_worker_pool_record_byte_identical_contracts(tmp_path: Path) -> None:
    """T8: the record depends on the declaration, not on which row arrived first or on scheduling."""
    rows, transform = _VARIANCE_SHAPES["vt_str"]
    recorded: list[str] = []
    for name, ordered_rows, workers in (("forward", rows, 1), ("reversed", list(reversed(rows)), 1), ("pool", rows, 4)):
        lane = tmp_path / name
        lane.mkdir()
        _write_jsonl(lane / "in.jsonl", ordered_rows)
        result = _run(_settings(lane, sources={"src": _json_source(lane / "in.jsonl")}, transforms=[transform], max_workers=workers))
        assert result.exit_code == 0, result.output
        [contract] = _transform_node_contracts(lane).values()
        recorded.append(contract)
    assert recorded[0] == recorded[1] == recorded[2]
    assert json.loads(recorded[0])["version_hash"] == json.loads(recorded[1])["version_hash"] == json.loads(recorded[2])["version_hash"]


class TestTheSourceSeamIsUnchanged:
    """T10: a source still infers on the first valid row, locks, and validates every later VALUE."""

    def test_a_later_row_of_another_type_is_quarantined_at_the_source(self, tmp_path: Path) -> None:
        _write_jsonl(
            tmp_path / "in.jsonl", [{"id": 1, "score": 5}, {"id": 2, "score": 4.5}, {"id": 3, "score": None}, {"id": 4, "score": 7}]
        )
        settings = _settings(
            tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl", on_success="out", on_validation_failure="quarantine")}
        )
        result = _run(settings)
        assert result.exit_code == 1, result.output
        assert [row["id"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [1, 3, 4]
        assert [row["id"] for row in _read_jsonl(tmp_path / "q.jsonl")] == [2]

    def test_a_null_first_field_locks_object_and_accepts_every_later_type(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "score": None}, {"id": 2, "score": 4}, {"id": 3, "score": SENTINEL}])
        settings = _settings(
            tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl", on_success="out", on_validation_failure="quarantine")}
        )
        result = _run(settings)
        assert result.exit_code == 0, result.output
        assert len(_read_jsonl(tmp_path / "out.jsonl")) == 3

    def test_a_field_locked_float_admits_a_later_int_but_never_a_bool(self, tmp_path: Path) -> None:
        """Ruling 2026-09-25 C3 at the source seam: an int value satisfies the locked ``float``; a bool does not.

        Before the one admission rule the int row was quarantined too (exit 1,
        two quarantined): the locked-contract check compared exact types.
        """
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "score": 4.5}, {"id": 2, "score": 5}, {"id": 3, "score": True}])
        settings = _settings(
            tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl", on_success="out", on_validation_failure="quarantine")}
        )
        result = _run(settings)
        assert result.exit_code == 1, result.output
        assert _read_jsonl(tmp_path / "out.jsonl") == [{"id": 1, "score": 4.5}, {"id": 2, "score": 5}]
        assert [row["id"] for row in _read_jsonl(tmp_path / "q.jsonl")] == [3]


class TestAnIntSatisfiesAFloatDeclaration:
    """Ruling 2026-09-25 C3: the one admission rule, end to end at every transform-output check.

    An ``int`` value satisfies a ``float`` declaration (pydantic strict
    agrees); the value is delivered unconverted and the recorded ``float,
    declared`` is true of it. A ``bool`` never satisfies ``float``. Before the
    rule the int cases routed as ``type_mismatch`` / ``contract_violation``.
    """

    def test_a_typed_float_value_transform_target_delivers_a_computed_int(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 3}, {"id": 2, "a": 4}])
        transform = _vt(
            schema={"mode": "flexible", "fields": ["id: int", "y: float"]}, operations=[{"target": "y", "expression": "row['a'] * 2"}]
        )
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        assert [row["y"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [6, 8]
        [recorded] = _transform_node_contracts(tmp_path).values()
        [y] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "y"]
        assert (y["python_type"], y["source"]) == ("float", "declared")

    def test_a_typed_float_value_transform_target_routes_a_computed_bool(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 3}, {"id": 2, "a": 4}])
        transform = _vt(
            schema={"mode": "flexible", "fields": ["id: int", "y: float"]}, operations=[{"target": "y", "expression": "row['a'] > 3"}]
        )
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 2, result.output
        reasons = _transform_error_reasons(tmp_path)
        assert [(r["reason"], r["field"], r["expected"], r["actual"]) for r in reasons] == [("type_mismatch", "y", "float", "bool")] * 2

    def test_a_json_explode_float_declaration_delivers_int_elements_and_routes_a_bool(self, tmp_path: Path) -> None:
        schema = {"mode": "flexible", "fields": ["doc: any", "pages: any", "page: float"]}
        _write_jsonl(tmp_path / "in.jsonl", [{"doc": "D1", "pages": [1, 2.5]}, {"doc": "D2", "pages": [True]}, {"doc": "D3", "pages": [3]}])
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[_explode(schema=schema)]))
        assert result.exit_code == 1, result.output
        assert [(row["doc"], row["page"]) for row in _read_jsonl(tmp_path / "out.jsonl")] == [("D1", 1), ("D1", 2.5), ("D3", 3)]
        [reason] = _transform_error_reasons(tmp_path)
        assert (reason["reason"], reason["field"], reason["expected"], reason["actual"]) == ("contract_violation", "page", "float", "bool")

    def test_a_forwarded_int_under_an_operator_float_is_delivered_under_a_true_record(self, tmp_path: Path) -> None:
        """review-REBASE2 M1: before, exit 0 with a record whose own validate() named ``x``; now the record is true."""
        _write_jsonl(tmp_path / "in.jsonl", [{"id": "a", "x": 2}, {"id": "b", "x": 3}])
        transform = _vt(schema={"mode": "flexible", "fields": ["x: float"]}, operations=[{"target": "label", "expression": "1"}])
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        delivered = _read_jsonl(tmp_path / "out.jsonl")
        assert [row["x"] for row in delivered] == [2, 3]
        [recorded] = _transform_node_contracts(tmp_path).values()
        from elspeth.contracts.schema_contract import SchemaContract

        contract = SchemaContract.from_checkpoint(json.loads(recorded))
        assert contract.get_field("x").python_type is float
        assert all(contract.validate(row) == [] for row in delivered)


def test_two_observed_sources_disagreeing_on_a_column_share_one_sink(tmp_path: Path) -> None:
    """T11 (panel multisrc_sink): the sink batch contract is the J1 join, so the run completes (before: exit 4 at the sink)."""
    _write_jsonl(tmp_path / "a.jsonl", [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}])
    _write_jsonl(tmp_path / "b.jsonl", [{"id": "K-1", "v": "c"}, {"id": "K-2", "v": "d"}])
    settings = _settings(
        tmp_path,
        sources={
            "src_a": _json_source(tmp_path / "a.jsonl", on_success="out"),
            "src_b": _json_source(tmp_path / "b.jsonl", on_success="out"),
        },
        quarantine=False,
    )
    result = _run(settings)
    assert result.exit_code == 0, result.output
    assert "FrameworkBugError" not in result.output
    delivered = _read_jsonl(tmp_path / "out.jsonl")
    assert sorted(str(row["id"]) for row in delivered) == ["1", "2", "K-1", "K-2"]
    assert _outcomes(tmp_path) == {("success", "default_flow"): 4}


class TestAnAnyFieldReachingATypedConsumer:
    """T12: nothing fails at the producer; the consumer that DECLARES a type routes the row."""

    _EXPLODE = _explode(schema={"mode": "flexible", "fields": ["doc: any", "pages: any"]}, on_success="mid")
    _ROWS: ClassVar[list[dict[str, Any]]] = [
        {"doc": "D1", "pages": [1, 2]},
        {"doc": "D2", "pages": [3, SENTINEL]},
        {"doc": "D3", "pages": [4]},
    ]

    def test_a_per_row_field_mapper_consumer_routes_the_str_row(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", self._ROWS)
        consumer = _fm(schema={"mode": "flexible", "fields": ["page: int"]}, mapping={"doc": "doc2"}, name="consume", input_conn="mid")
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[self._EXPLODE, consumer]))
        assert result.exit_code == 1, result.output
        assert [row["page"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [1, 2, 3, 4]
        assert [row["page"] for row in _read_jsonl(tmp_path / "q.jsonl")] == [SENTINEL]
        [reason] = _transform_error_reasons(tmp_path)
        assert "page: [int_type]" in reason["error"]
        assert SENTINEL not in json.dumps(reason)

    def test_a_per_row_value_transform_consumer_routes_the_str_row(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", self._ROWS)
        consumer = _vt(
            schema={"mode": "flexible", "fields": ["page: int"]},
            operations=[{"target": "p2", "expression": "row['page']"}],
            name="consume",
            input_conn="mid",
        )
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[self._EXPLODE, consumer]))
        assert result.exit_code == 1, result.output
        assert "SchemaConfigModeViolation" not in result.output
        assert [row["p2"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [1, 2, 3, 4]
        assert [row["page"] for row in _read_jsonl(tmp_path / "q.jsonl")] == [SENTINEL]

    def test_a_typed_aggregation_consumer_fails_the_batch(self, tmp_path: Path) -> None:
        _write_jsonl(tmp_path / "in.jsonl", self._ROWS)
        aggregation = {
            "name": "stats",
            "plugin": "batch_stats",
            "input": "mid",
            "on_success": "out",
            "on_error": "quarantine",
            "output_mode": "transform",
            "options": {"schema": {"mode": "flexible", "fields": ["page: int"]}, "value_field": "page"},
        }
        result = _run(
            _settings(
                tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[self._EXPLODE], aggregations=[aggregation]
            )
        )
        assert result.exit_code == 2, result.output
        assert _read_jsonl(tmp_path / "out.jsonl") == []
        assert len(_read_jsonl(tmp_path / "q.jsonl")) == 5
        reasons = _transform_error_reasons(tmp_path)
        assert reasons and all(reason["reason"] == "contract_violation" and "page: [int_type]" in reason["error"] for reason in reasons)
        assert SENTINEL not in json.dumps(reasons)


def test_two_observed_sources_disagreeing_on_a_column_share_one_csv_sink(tmp_path: Path) -> None:
    """D10: the CSV sink writes ``str()`` of each cell, so a J1 ``object`` column is written, not refused."""
    _write_jsonl(tmp_path / "a.jsonl", [{"id": 1, "v": "a"}, {"id": 2, "v": "b"}])
    _write_jsonl(tmp_path / "b.jsonl", [{"id": "K-1", "v": "c"}, {"id": "K-2", "v": "d"}])
    settings_path = _settings(
        tmp_path,
        sources={
            "src_a": _json_source(tmp_path / "a.jsonl", on_success="out"),
            "src_b": _json_source(tmp_path / "b.jsonl", on_success="out"),
        },
        quarantine=False,
    )
    settings = yaml.safe_load(settings_path.read_text())
    settings["sinks"]["out"] = {
        "plugin": "csv",
        "on_write_failure": "discard",
        "options": {"path": str(tmp_path / "out.csv"), "schema": {"mode": "observed"}},
    }
    settings_path.write_text(yaml.safe_dump(settings, sort_keys=False))
    result = _run(settings_path)
    assert result.exit_code == 0, result.output
    lines = (tmp_path / "out.csv").read_text().splitlines()
    assert lines[0] == "id,v"
    assert sorted(lines[1:]) == ["1,a", "2,b", "K-1,c", "K-2,d"]


def _plugin(plugin: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"name": "declare", "plugin": plugin, "input": "rows", "on_success": "out", "on_error": "quarantine", "options": options}


# ADR-050 Decision 2 at every shipped transform that forwards the input row
# under its own output declaration. Each option set leaves ``a`` and ``s``
# untouched (``s`` is truncated only past 11 characters, the filter pattern
# never matches, the coercion is of ``id``), so the operator's declarations of
# those two fields are declarations of CARRIED fields.
_CARRYING_TRANSFORMS: dict[str, tuple[str, dict[str, Any]]] = {
    "passthrough": ("passthrough", {}),
    "truncate": ("truncate", {"fields": {"s": 11}}),
    "keyword_filter": ("keyword_filter", {"fields": ["s"], "blocked_patterns": ["zzz"]}),
    "type_coerce": ("type_coerce", {"conversions": [{"field": "id", "to": "str"}]}),
}


class TestAnOperatorDeclarationOfACarriedFieldIsStamped:
    """S7 (review-S1a-r4 F2): the operator's declaration of a field the transform carries reaches the emitted contract.

    Before, passthrough, truncate, keyword_filter and type_coerce emitted their
    input row's contract unstamped, so an operator who typed a carried field
    after an observed source (``required=False``, the inferred type) ended the
    run with a Tier-1 ``SchemaConfigModeViolation`` on a VALID row (exit 4).
    Each now routes its emitted contract through the one stamp; the value the
    strict input check admitted is delivered unchanged, and the recorded
    declaration is true of it. The registry-wide form of this proof is
    ``tests/invariants/test_operator_declared_carried_fields.py``.
    """

    @pytest.mark.parametrize("shape", sorted(_CARRYING_TRANSFORMS))
    def test_a_valid_row_is_delivered_under_the_operators_declaration(self, shape: str, tmp_path: Path) -> None:
        plugin, options = _CARRYING_TRANSFORMS[shape]
        _write_jsonl(tmp_path / "in.jsonl", [{"id": 1, "a": 5, "s": "hello"}, {"id": 2, "a": 7, "s": "bye"}])
        transform = _plugin(plugin, {**options, "schema": {"mode": "flexible", "fields": ["a: float", "s: str"]}})
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        delivered = _read_jsonl(tmp_path / "out.jsonl")
        assert [(row["a"], row["s"]) for row in delivered] == [(5, "hello"), (7, "bye")]
        assert [outcome for (outcome, _path), n in _outcomes(tmp_path).items() for _ in range(n)] == ["success", "success"]
        [recorded] = _transform_node_contracts(tmp_path).values()
        from elspeth.contracts.schema_contract import SchemaContract

        contract = SchemaContract.from_checkpoint(json.loads(recorded))
        for name, declared in (("a", float), ("s", str)):
            field = contract.get_field(name)
            assert (field.python_type, field.required, field.source) == (declared, True, "declared")
        assert all(contract.validate(row) == [] for row in delivered)

    @pytest.mark.parametrize("shape", sorted(_CARRYING_TRANSFORMS))
    def test_a_row_breaking_the_declaration_is_routed_not_aborted(self, shape: str, tmp_path: Path) -> None:
        """The declaration is still enforced at the input check: a float ``a`` under ``a: int`` and a row without ``a`` route, value-free.

        The observed source locks ``a: float`` on row 1 and admits row 2's
        ``int`` (ruling C3), so both reach the transform; only the int is
        delivered.
        """
        plugin, options = _CARRYING_TRANSFORMS[shape]
        _write_jsonl(
            tmp_path / "in.jsonl",
            [{"id": 1, "a": 5.5, "s": SENTINEL}, {"id": 2, "a": 5, "s": "hello"}, {"id": 3, "s": "no a"}],
        )
        transform = _plugin(plugin, {**options, "schema": {"mode": "flexible", "fields": ["a: int", "s: str"]}})
        result = _run(_settings(tmp_path, sources={"src": _json_source(tmp_path / "in.jsonl")}, transforms=[transform]))
        assert result.exit_code == 1, result.output
        assert [row["id"] for row in _read_jsonl(tmp_path / "out.jsonl")] in ([2], ["2"])
        assert sorted(row["id"] for row in _read_jsonl(tmp_path / "q.jsonl")) == [1, 3]
        reasons = _transform_error_reasons(tmp_path)
        assert [reason["reason"] for reason in reasons] == ["contract_violation", "contract_violation"]
        assert SENTINEL not in json.dumps(reasons)

    def test_a_csv_header_field_declared_by_its_normalized_name(self, tmp_path: Path) -> None:
        """review-S1a-r4 ``s_csv_passthrough_str``: an observed CSV source, ``name: str`` declared on a passthrough."""
        (tmp_path / "in.csv").write_text("id,Name\n1,Ann\n2,Bob\n")
        source = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": str(tmp_path / "in.csv"), "on_validation_failure": "discard", "schema": {"mode": "observed"}},
        }
        transform = _plugin("passthrough", {"schema": {"mode": "flexible", "fields": ["name: str"]}})
        result = _run(_settings(tmp_path, sources={"src": source}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        assert [row["name"] for row in _read_jsonl(tmp_path / "out.jsonl")] == ["Ann", "Bob"]
        [recorded] = _transform_node_contracts(tmp_path).values()
        [name] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "name"]
        assert (name["original_name"], name["python_type"], name["required"], name["source"]) == ("Name", "str", True, "declared")

    def test_a_typed_source_int_forwarded_under_an_operator_float(self, tmp_path: Path) -> None:
        """S6 S7a-TYPED-SOURCE: a fixed ``x: int`` source, ``x: float`` on a passthrough (ruling C3), exit 0."""
        (tmp_path / "in.csv").write_text("id,x\n1,5\n2,7\n")
        source = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {
                "path": str(tmp_path / "in.csv"),
                "on_validation_failure": "discard",
                "schema": {"mode": "fixed", "fields": ["id: int", "x: int"]},
            },
        }
        transform = _plugin("passthrough", {"schema": {"mode": "flexible", "fields": ["x: float"]}})
        result = _run(_settings(tmp_path, sources={"src": source}, transforms=[transform]))
        assert result.exit_code == 0, result.output
        assert [row["x"] for row in _read_jsonl(tmp_path / "out.jsonl")] == [5, 7]
        [recorded] = _transform_node_contracts(tmp_path).values()
        [x] = [field for field in json.loads(recorded)["fields"] if field["normalized_name"] == "x"]
        assert (x["python_type"], x["source"]) == ("float", "declared")
