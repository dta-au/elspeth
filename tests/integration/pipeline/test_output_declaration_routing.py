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
  value-free reason (before: ``page: int, declared`` recorded while a str was
  delivered, exit 0).
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
        assert reason["authorship"] == "computed"
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
    """A rename target carries the input value the strict input check admitted: ``int`` under ``float`` is delivered.

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
