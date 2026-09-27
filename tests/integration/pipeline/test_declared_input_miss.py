"""A declared-input miss the build could not prove routes; a proven or divergent one aborts (elspeth-5887fb7928 R2).

ADR-013 Amendment 2026-09-27. A transform's option-derived input declaration
(type_coerce ``conversions[].field``, web_scrape ``url_field``, field_mapper
``mapping`` sources, …) names a column. Behind an ``observed`` upstream the
build cannot prove the column exists, so it admits the pipeline and the engine
settles each row before ``process()``:

- the field is absent and unproven → a fact about the row, ROUTED via
  ``on_error`` with one value-free, row-invariant ``missing_field`` reason
  (before R2: every such run ended at the first row, exit 4, for 14 plugins,
  and type_coerce too on the lane — CENSUS-R2);
- the build PROVED the field present (every live predecessor's presence vote
  lists it, OPEN or CLOSED) → our bug, Tier 1, unchanged;
- the row's payload carries the field but its contract lost it → our bug,
  Tier 1, unchanged.

Every case is a real CLI run read back through the exit code, the sink files
and the Landscape. The Tier-1 cases need a row that should not exist; they
inject the engine defect they stand for (a broken upstream val or a
contract-propagation loss) in-process, which is the only way a live graph
reaches them.
"""

from __future__ import annotations

import dataclasses
import json
import sqlite3
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest
import yaml

from elspeth.contracts.schema_contract import PipelineRow
from elspeth.engine.executors.transform import TransformExecutor

SENTINEL = "SENTINEL_R2_ROW_VALUE_4c1e"
SENTINEL_KEY = "sentinel_row_key_9d2f"
_OBSERVED: dict[str, Any] = {"mode": "observed"}
_HTTP = {"abuse_contact": "r2@example.com", "fetch_reason": "r2 regression", "allowed_hosts": ["93.184.216.34/32"]}
_WS_HTTP = {"abuse_contact": "r2@example.com", "scraping_reason": "r2 regression", "allowed_hosts": ["93.184.216.34/32"]}
_AZ = {"endpoint": "https://r2-probe.cognitiveservices.azure.com", "api_key": "r2-not-a-key"}  # secret-scan: allow-this-line

# (id, plugin, options naming `b`, present row usable without a network call or blob)
_CENSUS: list[tuple[str, str, dict[str, Any], bool]] = [
    ("type_coerce-conversions", "type_coerce", {"conversions": [{"field": "b", "to": "int"}]}, True),
    ("field_mapper-strict_false", "field_mapper", {"mapping": {"b": "target"}, "strict": False}, True),
    ("field_mapper-strict_true", "field_mapper", {"mapping": {"b": "target"}, "strict": True}, True),
    (
        "reference_join-key_field",
        "reference_join",
        {
            "reference_file": "ref.csv",
            "reference_format": "csv",
            "key_field": "b",
            "reference_key_name": "sku",
            "output": {"desc": "ref['desc']"},
        },
        True,
    ),
    ("blob_csv_expand-text_field", "blob_csv_expand", {"source": "field", "text_field": "b", "columns": ["x", "y"]}, False),
    ("blob_json_expand-text_field", "blob_json_expand", {"source": "field", "text_field": "b", "format": "json", "fields": ["x"]}, False),
    ("blob_text_expand-blob_ref_field", "blob_text_expand", {"blob_ref_field": "b"}, False),
    ("pdf_rasterize-blob_ref_field", "pdf_rasterize", {"blob_ref_field": "b"}, False),
    # json_explode's array_field became a declared input in R2 (was a raw KeyError that ended the run).
    ("json_explode-array_field", "json_explode", {"array_field": "b"}, False),
    ("blob_fetch-url_field", "blob_fetch", {"url_field": "b", "http": _HTTP}, False),
    ("web_scrape-url_field", "web_scrape", {"url_field": "b", "content_field": "pc", "fingerprint_field": "pf", "http": _WS_HTTP}, False),
    (
        "azure_document_intelligence-source_field",
        "azure_document_intelligence",
        {"model_id": "prebuilt-read", "source_mode": "url", "source_field": "b", "content_field": "dc", **_AZ},
        False,
    ),
    (
        "aws_textract_document_analysis-key_field",
        "aws_textract_document_analysis",
        {
            "region": "ap-southeast-2",
            "auth_mode": "default_chain",
            "bucket": "r2-bucket",
            "key_field": "b",
            "feature_types": ["FORMS"],
            "text_field": "tt",
        },
        False,
    ),
    (
        "aws_textract_inline_analysis-blob_ref_field",
        "aws_textract_inline_analysis",
        {
            "region": "ap-southeast-2",
            "auth_mode": "default_chain",
            "blob_ref_field": "b",
            "document_format": "png",
            "feature_types": ["FORMS"],
            "text_field": "tt",
        },
        False,
    ),
]


def _cli(*args: str) -> Any:
    from typer.testing import CliRunner

    from elspeth.cli import app

    return CliRunner().invoke(app, list(args))


def _json_sink(path: Path) -> dict[str, Any]:
    return {"plugin": "json", "on_write_failure": "discard", "options": {"path": str(path), "format": "jsonl", "schema": _OBSERVED}}


def _settings(
    tmp_path: Path,
    rows: list[dict[str, Any]],
    *,
    plugin: str,
    options: dict[str, Any],
    source_schema: dict[str, Any] | None = None,
    on_error: str = "quarantine",
    csv_text: str | None = None,
) -> Path:
    if csv_text is not None:
        (tmp_path / "in.csv").write_text(csv_text)
        source: dict[str, Any] = {
            "plugin": "csv",
            "on_success": "rows",
            "options": {"path": str(tmp_path / "in.csv"), "on_validation_failure": "discard", "schema": source_schema or _OBSERVED},
        }
    else:
        (tmp_path / "in.jsonl").write_text("".join(json.dumps(row) + "\n" for row in rows))
        source = {
            "plugin": "json",
            "on_success": "rows",
            "options": {
                "path": str(tmp_path / "in.jsonl"),
                "format": "jsonl",
                "on_validation_failure": "discard",
                "schema": source_schema or _OBSERVED,
            },
        }
    options = dict(options)
    if "reference_file" in options:
        (tmp_path / "ref.csv").write_text("sku,desc\n2,two\n1,one\n")
        options["reference_file"] = str(tmp_path / "ref.csv")
    settings: dict[str, Any] = {
        "sources": {"src": source},
        "concurrency": {"max_workers": 1},
        "transforms": [
            {
                "name": "t0",
                "plugin": plugin,
                "input": "rows",
                "on_success": "out",
                "on_error": on_error,
                "options": {"schema": _OBSERVED, **options},
            }
        ],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl")}
        | ({} if on_error == "discard" else {on_error: _json_sink(tmp_path / "q.jsonl")}),
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _query(tmp_path: Path, sql: str) -> list[tuple[Any, ...]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def _terminal_outcomes(tmp_path: Path) -> dict[str, int]:
    """Every token's single completed outcome, keyed ``outcome/path``; ``NONTERMINAL`` for a token without one."""
    counts: dict[str, int] = {}
    for (key,) in _query(
        tmp_path,
        "select coalesce(o.outcome || '/' || o.path, 'NONTERMINAL') from tokens t "
        "left join token_outcomes o on o.token_id = t.token_id and o.completed = 1",
    ):
        counts[key] = counts.get(key, 0) + 1
    return counts


def _routed_reasons(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(text) for (text,) in _query(tmp_path, "select error_details_json from transform_errors order by rowid")]


def _missing_rows(n: int) -> list[dict[str, Any]]:
    return [{"a": SENTINEL, SENTINEL_KEY: f"{SENTINEL}-{index}"} for index in range(n)]


@pytest.mark.parametrize(("case_id", "plugin", "options", "present_row_is_local"), _CENSUS, ids=[case[0] for case in _CENSUS])
def test_an_unproven_absent_declared_input_routes_value_free(
    tmp_path: Path, case_id: str, plugin: str, options: dict[str, Any], present_row_is_local: bool
) -> None:
    """Every census plugin: N rows without the field -> N routed rows, ONE reason, ONE error_hash, no row value or key."""
    rows = _missing_rows(3)
    if present_row_is_local:
        rows.insert(0, {"a": "1", "b": "2"})
    settings = _settings(tmp_path, rows, plugin=plugin, options=options)
    assert _cli("validate", "-s", str(settings)).exit_code == 0

    result = _cli("run", "-s", str(settings), "--execute")

    assert "Traceback" not in result.output
    assert result.exit_code == (1 if present_row_is_local else 2), result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert "NONTERMINAL" not in outcomes, outcomes
    assert outcomes.get("failure/on_error_routed") == 3, outcomes
    reasons = _routed_reasons(tmp_path)
    assert len(reasons) == 3
    assert {(reason["reason"], tuple(reason["fields"])) for reason in reasons} == {("missing_field", ("b",))}
    assert len({json.dumps(reason, sort_keys=True) for reason in reasons}) == 1
    assert all(SENTINEL not in json.dumps(reason) and SENTINEL_KEY not in json.dumps(reason) for reason in reasons)
    hashes = _query(tmp_path, "select distinct error_hash from token_outcomes where path = 'on_error_routed'")
    assert len(hashes) == 1, hashes


def test_on_error_discard_records_the_routed_miss_as_a_failure(tmp_path: Path) -> None:
    """B3: a discarded row still reaches one recorded terminal outcome."""
    settings = _settings(tmp_path, _missing_rows(2), plugin="type_coerce", options=_CENSUS[0][2], on_error="discard")

    result = _cli("run", "-s", str(settings), "--execute")

    assert "Traceback" not in result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert "NONTERMINAL" not in outcomes, outcomes
    assert sum(count for key, count in outcomes.items() if key.startswith("failure/")) == 2, outcomes


def test_following_the_remedy_a_source_column_is_refused_at_the_source(tmp_path: Path) -> None:
    """Review-R2 r2 F1: the routed text's source-column remedy yields a working pipeline.

    Declaring b in the source's schema fields moves the refusal of a row
    without b to the source (on_validation_failure), and the row carrying b
    still reaches the sink; nothing routes as missing_field at the transform.
    """
    rows = [{"a": "1", "b": "2"}, *_missing_rows(2)]
    settings = _settings(
        tmp_path, rows, plugin="type_coerce", options=_CENSUS[0][2], source_schema={"mode": "flexible", "fields": ["b: str"]}
    )

    result = _cli("run", "-s", str(settings), "--execute")

    assert "Traceback" not in result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert "NONTERMINAL" not in outcomes, outcomes
    # on_validation_failure: discard records each refused row as a source
    # validation error (no token is created for it).
    assert outcomes == {"success/default_flow": 1}, outcomes
    assert _query(tmp_path, "select count(*) from validation_errors") == [(2,)]
    assert _routed_reasons(tmp_path) == []
    assert [row["b"] for row in _output_rows(tmp_path)] == [2]


_PA_PASSTHROUGH: dict[str, Any] = {
    "name": "ta",
    "plugin": "passthrough",
    "input": "pa",
    "on_success": "da",
    "on_error": "q",
    "options": {"schema": _OBSERVED},
}


def _lost_branch_settings(tmp_path: Path, pa_transform: dict[str, Any]) -> Path:
    """fork → (pa: ``pa_transform`` | pb: value_transform creating y) → best_effort union → type_coerce on y.

    The row whose pb branch fails (a=3: floor division by zero) merges as the
    pa branch alone.
    """
    (tmp_path / "in.jsonl").write_text('{"a": 1, "b": "2"}\n{"a": 3, "b": "4"}\n')
    settings: dict[str, Any] = {
        "sources": {
            "src": {
                "plugin": "json",
                "on_success": "rows",
                "options": {"path": str(tmp_path / "in.jsonl"), "format": "jsonl", "on_validation_failure": "discard", "schema": _OBSERVED},
            }
        },
        "concurrency": {"max_workers": 1},
        "gates": [{"name": "g", "input": "rows", "condition": "True", "routes": {"true": "fork", "false": "out"}, "fork_to": ["pa", "pb"]}],
        "transforms": [
            pa_transform,
            {
                "name": "tb",
                "plugin": "value_transform",
                "input": "pb",
                "on_success": "db",
                "on_error": "q",
                "options": {"schema": _OBSERVED, "operations": [{"target": "y", "expression": "1 // (row['a'] - 3)"}]},
            },
            {
                "name": "tail",
                "plugin": "type_coerce",
                "input": "m",
                "on_success": "out",
                "on_error": "q",
                "options": {"schema": _OBSERVED, "conversions": [{"field": "y", "to": "str"}]},
            },
        ],
        "coalesce": [{"name": "m", "branches": {"pa": "da", "pb": "db"}, "policy": "best_effort", "timeout_seconds": 5, "merge": "union"}],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "q": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _output_rows(tmp_path: Path) -> list[dict[str, Any]]:
    return [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]


def test_a_field_only_a_lost_branch_created_routes_after_a_best_effort_union(tmp_path: Path) -> None:
    """Review-R2 r1 F1: a best-effort union's lost branch is a row fact, never a proven miss.

    The a=3 row merges as the pa pass-through branch alone, without y. The
    build does not prove y (the pa branch abstains), so that row routes as
    missing_field; before the fix the coalesce published y as guaranteed and
    the run ended (exit 4).

    Review-R2 r2 F1: the routed text's remedy must hold for a field a
    transform CREATES. It used to say only "declare the field(s) as required
    in the source's schema fields" — followed here, that discards every row at
    the source (no source row carries y) and the run reports success with 0
    rows. The full text is pinned; the remedy it names for this shape is proved
    by test_following_the_remedy_every_branch_guarantees_the_field.
    """
    result = _cli("run", "-s", str(_lost_branch_settings(tmp_path, _PA_PASSTHROUGH)), "--execute")

    assert "Traceback" not in result.output
    assert result.exit_code == 1, result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert "NONTERMINAL" not in outcomes, outcomes
    assert outcomes.get("failure/on_error_routed") == 2, outcomes
    [missing] = [reason for reason in _routed_reasons(tmp_path) if reason["reason"] == "missing_field"]
    assert missing == {
        "reason": "missing_field",
        "fields": ["y"],
        "error": (
            "Transform 'type_coerce' requires input field(s) ['y'] that the arriving row does not carry. The build proves "
            "a field only when every path into this node guarantees it, and it could not prove these, so the row is "
            "routed instead of processed. Where every row should carry them: declare a column the source reads in the "
            "source's schema fields (a source row lacking it is then handled by the source's on_validation_failure); a "
            "field a transform creates must be created on every row and guaranteed by that transform's output schema; "
            "after a merge: union coalesce whose policy can lose a branch (best_effort, first, or a quorum below the "
            "branch count), every branch must guarantee it."
        ),
    }


def test_following_the_remedy_every_branch_guarantees_the_field(tmp_path: Path) -> None:
    """Review-R2 r2 F1: the routed text's coalesce remedy yields a working pipeline.

    Following it, the pa branch creates y on every row too (value_transform
    guarantees its target), so every branch guarantees y, the build proves it
    at the tail, and nothing routes as missing_field: the a=3 row's pb failure
    is still routed by tb (its own error), and the pa-only merged row — now
    carrying y — reaches the sink with the a=1 row.
    """
    pa_creates_y = {
        "name": "ta",
        "plugin": "value_transform",
        "input": "pa",
        "on_success": "da",
        "on_error": "q",
        "options": {"schema": _OBSERVED, "operations": [{"target": "y", "expression": "0"}]},
    }
    # The build's proof of y at the tail for this shape is pinned by
    # TestLostBranchIsARowFact.test_every_branch_guaranteeing_the_field_proves_it.
    result = _cli("run", "-s", str(_lost_branch_settings(tmp_path, pa_creates_y)), "--execute")

    assert "Traceback" not in result.output
    assert result.exit_code == 1, result.output
    outcomes = _terminal_outcomes(tmp_path)
    assert "NONTERMINAL" not in outcomes, outcomes
    assert outcomes.get("failure/on_error_routed") == 1, outcomes
    assert [reason["reason"] for reason in _routed_reasons(tmp_path)] == ["invalid_input"]
    assert sorted((row["a"], row["y"]) for row in _output_rows(tmp_path)) == [(1, "-1"), (3, "0")]


def _inject_before_transform(monkeypatch: pytest.MonkeyPatch, rewrite: Callable[[PipelineRow], PipelineRow]) -> None:
    """Stand in for an engine defect that hands the transform a row other than the one upstream produced."""
    original = TransformExecutor.execute_transform

    def execute_transform(self: TransformExecutor, transform: Any, token: Any, ctx: Any, *, attempt: int) -> Any:
        return original(self, transform, dataclasses.replace(token, row_data=rewrite(token.row_data)), ctx, attempt=attempt)

    monkeypatch.setattr(TransformExecutor, "execute_transform", execute_transform)


def _drop(field: str, *, from_payload: bool) -> Callable[[PipelineRow], PipelineRow]:
    def rewrite(row: PipelineRow) -> PipelineRow:
        contract = dataclasses.replace(row.contract, fields=tuple(fc for fc in row.contract.fields if fc.normalized_name != field))
        data = {key: value for key, value in row.to_dict().items() if not (from_payload and key == field)}
        return PipelineRow(data, contract)

    return rewrite


def _assert_tier_one(tmp_path: Path, result: Any) -> None:
    assert result.exit_code == 4, result.output
    assert "DeclaredRequiredInputFieldsViolation" in result.output
    assert _routed_reasons(tmp_path) == []
    assert ("failure", "unrouted") in _query(tmp_path, "select outcome, path from token_outcomes where completed = 1")


class TestProvenMissIsTierOne:
    """A miss of a field the build PROVED present is our bug: the unchanged ADR-013 contract aborts (T1, systems C3)."""

    def test_open_participating_upstream(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """observed + guaranteed_fields [b] is OPEN and proves b; the row reaches the transform without it."""
        _inject_before_transform(monkeypatch, _drop("b", from_payload=True))
        settings = _settings(
            tmp_path,
            [{"a": "1", "b": "2"}],
            plugin="type_coerce",
            options=_CENSUS[0][2],
            source_schema={"mode": "observed", "guaranteed_fields": ["b"]},
        )

        _assert_tier_one(tmp_path, _cli("run", "-s", str(settings), "--execute"))

    def test_closed_upstream(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """A fixed source declaring b is CLOSED and proves b."""
        _inject_before_transform(monkeypatch, _drop("b", from_payload=True))
        settings = _settings(
            tmp_path,
            [],
            plugin="type_coerce",
            options=_CENSUS[0][2],
            source_schema={"mode": "fixed", "fields": ["a: str", "b: str"]},
            csv_text="a,b\n1,2\n",
        )

        _assert_tier_one(tmp_path, _cli("run", "-s", str(settings), "--execute"))

    def test_the_same_row_without_the_proof_routes(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control: the identical injected row behind an abstaining upstream is an unproven miss and routes."""
        _inject_before_transform(monkeypatch, _drop("b", from_payload=True))
        settings = _settings(tmp_path, [{"a": "1", "b": "2"}], plugin="type_coerce", options=_CENSUS[0][2])

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code == 2, result.output
        assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 1}


def test_a_payload_the_contract_lost_is_tier_one_even_unproven(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contract/payload divergence (architect A4, T3): the data has b, the contract lost it — our bug, never routed."""
    _inject_before_transform(monkeypatch, _drop("b", from_payload=False))
    settings = _settings(tmp_path, [{"a": "1", "b": "2"}], plugin="type_coerce", options=_CENSUS[0][2])

    _assert_tier_one(tmp_path, _cli("run", "-s", str(settings), "--execute"))


def _aggregation_settings(tmp_path: Path, *, source_schema: dict[str, Any]) -> Path:
    (tmp_path / "in.jsonl").write_text("".join(json.dumps(row) + "\n" for row in ({"id": 1, "v": 1}, {"id": 2, "v": 2})))
    settings: dict[str, Any] = {
        "sources": {
            "src": {
                "plugin": "json",
                "on_success": "batch_in",
                "options": {
                    "path": str(tmp_path / "in.jsonl"),
                    "format": "jsonl",
                    "on_validation_failure": "discard",
                    "schema": source_schema,
                },
            }
        },
        "aggregations": [
            {
                "name": "thresholds",
                "plugin": "batch_threshold_summary",
                "input": "batch_in",
                "on_success": "out",
                "on_error": "quarantine",
                "trigger": {"count": 2},
                "output_mode": "transform",
                "options": {"value_field": "v", "thresholds": [{"name": "hi", "operator": ">=", "value": 1}], "schema": _OBSERVED},
            }
        ],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl"), "quarantine": _json_sink(tmp_path / "q.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
        "concurrency": {"max_workers": 1},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _inject_before_batch_preflight(monkeypatch: pytest.MonkeyPatch, rewrite: Callable[[PipelineRow], PipelineRow]) -> None:
    """Stand in for an engine defect that hands the aggregation's input check rows other than the buffered ones."""
    from elspeth.engine.executors.aggregation import AggregationExecutor

    original = AggregationExecutor._validate_batch_inputs

    def validate(self: AggregationExecutor, node_id: Any, transform: Any, rows: Any) -> None:
        original(self, node_id, transform, [rewrite(row) for row in rows])

    monkeypatch.setattr(AggregationExecutor, "_validate_batch_inputs", validate)


class TestBatchSeamParity:
    """The aggregation input seam classifies a miss with the transform seam's rule (architect A5, T4)."""

    def test_a_proven_batch_field_missing_is_tier_one_with_every_buffered_token_failed_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inject_before_batch_preflight(monkeypatch, _drop("v", from_payload=True))
        settings = _aggregation_settings(tmp_path, source_schema={"mode": "observed", "guaranteed_fields": ["v"]})

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code == 4, result.output
        assert "BatchDeclaredInputFieldsViolation" in result.output
        assert _routed_reasons(tmp_path) == []
        # Never outcomeless: both buffered tokens are FAILED before the abort.
        assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2}
        contexts = [json.loads(text) for (text,) in _query(tmp_path, "select context_json from token_outcomes where completed = 1")]
        assert {(context["exception_type"], context["failure_kind"], tuple(context["missing"])) for context in contexts} == {
            ("BatchDeclaredInputFieldsViolation", "proven_field_absent", ("v",))
        }

    def test_a_batch_payload_the_contract_lost_is_tier_one(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_before_batch_preflight(monkeypatch, _drop("v", from_payload=False))
        settings = _aggregation_settings(tmp_path, source_schema=_OBSERVED)

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code == 4, result.output
        assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2}

    def test_the_same_unproven_absence_routes_the_batch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Control (B2 kept): behind an abstaining upstream the identical rows fail the batch through on_error."""
        _inject_before_batch_preflight(monkeypatch, _drop("v", from_payload=True))
        settings = _aggregation_settings(tmp_path, source_schema=_OBSERVED)

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code != 4, result.output
        assert "Traceback" not in result.output
        assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 2}
        assert {(reason["reason"], tuple(reason["fields"])) for reason in _routed_reasons(tmp_path)} == {("missing_field", ("v",))}


def _collector_settings(tmp_path: Path, *, create_reading: bool) -> Path:
    """One document exploded into two pages and closed by a batch_stats collector on ``reading``.

    With ``create_reading`` a value_transform creates ``reading`` on every page,
    so the build proves it at the collector; without it the pages pass through
    an observed pass-through and the collector's required field is unproven.
    """
    (tmp_path / "in.jsonl").write_text(json.dumps({"doc_id": "D", "pages": [{"reading": 1}, {"reading": 2}]}) + "\n")
    middle: dict[str, Any] = (
        {
            "name": "read_value",
            "plugin": "value_transform",
            "input": "page_in",
            "on_success": "pages",
            "on_error": "discard",
            "options": {"schema": _OBSERVED, "operations": [{"target": "reading", "expression": "row['page']['reading'] + 0"}]},
        }
        if create_reading
        else {
            "name": "read_value",
            "plugin": "passthrough",
            "input": "page_in",
            "on_success": "pages",
            "on_error": "discard",
            "options": {"schema": _OBSERVED},
        }
    )
    settings: dict[str, Any] = {
        "sources": {
            "docs": {
                "plugin": "json",
                "on_success": "rows",
                "options": {"path": str(tmp_path / "in.jsonl"), "format": "jsonl", "on_validation_failure": "discard", "schema": _OBSERVED},
            }
        },
        "concurrency": {"max_workers": 1},
        "transforms": [
            {
                "name": "explode_pages",
                "plugin": "json_explode",
                "input": "rows",
                "on_success": "page_in",
                "on_error": "discard",
                "options": {"array_field": "pages", "output_field": "page", "schema": _OBSERVED},
            },
            middle,
        ],
        "collectors": [
            {
                "name": "page_stitcher",
                "plugin": "batch_stats",
                "input": "pages",
                "on_success": "out",
                "options": {"value_field": "reading", "schema": _OBSERVED},
            }
        ],
        "scopes": [{"name": "document_pages", "opener": "explode_pages", "closer": "page_stitcher", "policy": "require_all"}],
        "sinks": {"out": _json_sink(tmp_path / "out.jsonl")},
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _inject_before_collector_preflight(monkeypatch: pytest.MonkeyPatch, rewrite: Callable[[PipelineRow], PipelineRow]) -> None:
    """Stand in for an engine defect that hands the collector's input check rows other than the buffered ones."""
    import elspeth.engine.executors.collector as collector_module

    original = collector_module.validate_batch_inputs

    def validate(transform: Any, rows: Any, **kwargs: Any) -> None:
        original(transform, [rewrite(row) for row in rows], **kwargs)

    monkeypatch.setattr(collector_module, "validate_batch_inputs", validate)


def _collector_member_hold_states(tmp_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    for (status,) in _query(
        tmp_path,
        "select ns.status from node_states ns join nodes n on n.node_id = ns.node_id and n.run_id = ns.run_id "
        "where n.plugin_name = 'batch_stats'",
    ):
        counts[status] = counts.get(status, 0) + 1
    return counts


def _add_payload_field(field: str) -> Callable[[PipelineRow], PipelineRow]:
    def rewrite(row: PipelineRow) -> PipelineRow:
        return PipelineRow({**row.to_dict(), field: 1}, row.contract)

    return rewrite


class TestCollectorSeamParity:
    """The collector input seam: same classifier as the aggregation seam, every member FAILED before a Tier-1 abort.

    Review-R2 r1 F2: a proven miss at a collector used to end the run with every
    member token outcomeless (the WS3 settle seam never runs on a Tier-1 path).
    """

    def test_a_proven_field_missing_is_tier_one_with_every_member_failed_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _inject_before_collector_preflight(monkeypatch, _drop("reading", from_payload=True))
        settings = _collector_settings(tmp_path, create_reading=True)

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code == 4, result.output
        assert "BatchDeclaredInputFieldsViolation" in result.output
        # Never outcomeless: both page members FAILED; the opener's parent is transient.
        assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2, "transient/expand_parent": 1}
        contexts = [
            json.loads(text)
            for (text,) in _query(tmp_path, "select context_json from token_outcomes where completed = 1 and outcome = 'failure'")
        ]
        assert {(context["exception_type"], context["failure_kind"], tuple(context["missing"])) for context in contexts} == {
            ("BatchDeclaredInputFieldsViolation", "proven_field_absent", ("reading",))
        }
        # Every member's accept-time hold is closed FAILED (none left open), beside the failed flush state.
        assert _collector_member_hold_states(tmp_path) == {"failed": 3}

    def test_a_payload_the_contract_lacks_is_tier_one_even_unproven(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        _inject_before_collector_preflight(monkeypatch, _add_payload_field("reading"))
        settings = _collector_settings(tmp_path, create_reading=False)

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code == 4, result.output
        assert "BatchDeclaredInputFieldsViolation" in result.output
        assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2, "transient/expand_parent": 1}
        assert _collector_member_hold_states(tmp_path) == {"failed": 3}

    def test_the_unproven_absence_fails_the_group_without_ending_the_run(self, tmp_path: Path) -> None:
        """Control (B2 kept): pages behind an abstaining pass-through lack ``reading``; the group fails, the run goes on."""
        settings = _collector_settings(tmp_path, create_reading=False)

        result = _cli("run", "-s", str(settings), "--execute")

        assert result.exit_code != 4, result.output
        assert "Traceback" not in result.output
        outcomes = _terminal_outcomes(tmp_path)
        assert "NONTERMINAL" not in outcomes, outcomes
        assert "BatchDeclaredInputFieldsViolation" not in result.output
