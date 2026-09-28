# tests/integration/pipeline/test_field_name_spelling_rule.py
"""Operator-level proof of the field-name spelling rule (operator ruling 2026-09-25, elspeth-5887fb7928).

A DECLARATION names a field as rows carry it; a header spelling of a field the
row carries is REFUSED — at build (``elspeth validate`` / ``run``) where a
participating upstream proves it, per row otherwise. Row LOOKUPS keep resolving
either spelling. Every case is a real CLI run read back through the exit code,
the sink files and the Landscape. The shapes are the confirmed sites of
``.claude/lanes/5887-batch-row/systems-spelling-sweep.md`` and the S7 (b)/(c)
shapes of the 2026-09-26 Q4 amendment:

- field_mapper: a schema field spelled by the mapping source's header
  (``{Name: given}`` with ``Name: int?``) no longer records a false
  ``given: int, declared`` over a delivered str (sweep §2.1, CODEX-S1a);
- value_transform and every sink: a header-spelled schema declaration is no
  longer silently inert (sweep §2.2, §2.3), nor a custom ``headers`` key a
  write-time crash that left tokens with no terminal outcome;
- ``required_input_fields``: the verdict names the canonical spelling (§2.4);
- the declared-input siblings (web_scrape ``url_field``, blob_json_expand
  ``blob_ref_field``, rag ``query_field``) and type_coerce's
  ``conversions[].field`` (S7 b): routed, where they were a Tier-1 abort;
- created names (S7 c): a value_transform target or a field_mapper rename
  target spelled as an arriving field's header is refused, where it crashed
  with ``Duplicate original_name`` or silently shadowed the field;
- a batch transform's ``group_by`` spelled by header (sweep §2.8);
- keyword_filter's named scan ``fields`` (``declared_string_input_fields``,
  shared with the guardrails): a header spelling walked past the string-type
  build check, which compares names as written.

Every routed reason is one stable, value-free code carrying config literals and
their canonical names only, and every token reaches exactly one terminal
outcome.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest
import yaml
from sqlalchemy import Column, MetaData, Table, Text, create_engine

from elspeth.plugins.sinks.database_sink import database_effect_ledger_table

SENTINEL = "SENTINEL_SPELLING_ROW_VALUE"
_ID_NAME_CSV = f"ID,Name\n1,{SENTINEL}\n2,Bob\n"
_OBSERVED: dict[str, Any] = {"mode": "observed"}
_FIXED_ID_NAME: dict[str, Any] = {"mode": "fixed", "fields": ["id: str", "name: str"]}
_FLEXIBLE_ID_NAME: dict[str, Any] = {"mode": "flexible", "fields": ["id: str", "name: str"]}
_READ = "declared_field_is_header_spelling"
_CREATE = "target_is_header_spelling"


def _csv_source(tmp_path: Path, text: str = _ID_NAME_CSV, *, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    path = tmp_path / "in.csv"
    path.write_text(text)
    return {
        "plugin": "csv",
        "on_success": "rows",
        "options": {"path": str(path), "on_validation_failure": "discard", "schema": schema or _OBSERVED},
    }


def _json_sink(path: Path, schema: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "plugin": "json",
        "on_write_failure": "discard",
        "options": {"path": str(path), "format": "jsonl", "schema": schema or _OBSERVED},
    }


def _transform(plugin: str, options: dict[str, Any]) -> dict[str, Any]:
    return {"name": "t0", "plugin": plugin, "input": "rows", "on_success": "out", "on_error": "quarantine", "options": options}


def _settings(
    tmp_path: Path,
    *,
    source: dict[str, Any],
    transforms: list[dict[str, Any]] = (),
    aggregations: list[dict[str, Any]] = (),
    out_sink: dict[str, Any] | None = None,
) -> Path:
    sinks = {"out": out_sink or _json_sink(tmp_path / "out.jsonl")}
    if transforms or aggregations:
        sinks["quarantine"] = _json_sink(tmp_path / "q.jsonl")
    settings: dict[str, Any] = {
        "sources": {"src": source},
        "concurrency": {"max_workers": 1},
        "sinks": sinks,
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
        "payload_store": {"backend": "filesystem", "base_path": str(tmp_path / "payloads")},
    }
    if transforms:
        settings["transforms"] = list(transforms)
    if aggregations:
        settings["aggregations"] = list(aggregations)
    if not transforms and not aggregations:
        settings["sources"]["src"]["on_success"] = "out"
    path = tmp_path / "settings.yaml"
    path.write_text(yaml.safe_dump(settings, sort_keys=False))
    return path


def _flat(output: str) -> str:
    """CLI panel text as one line: the error panels wrap long verdicts across boxed lines."""
    return " ".join(" ".join(line.strip().strip("│╭╮╰╯─").split()) for line in output.splitlines()).replace("  ", " ")


def _cli(*args: str) -> Any:
    from typer.testing import CliRunner

    from elspeth.cli import app

    return CliRunner().invoke(app, list(args))


def _run(settings_path: Path) -> Any:
    """Validate (must pass: the build cannot see the spelling) then run."""
    validated = _cli("validate", "-s", str(settings_path))
    assert validated.exit_code == 0, validated.output
    return _cli("run", "-s", str(settings_path), "--execute")


def _refused_at_build(settings_path: Path) -> str:
    """``elspeth validate`` refuses the graph with the rule's own verdict, before any row exists."""
    validated = _cli("validate", "-s", str(settings_path))
    assert validated.exit_code != 0, validated.output
    assert "Traceback" not in validated.output
    output = _flat(validated.output)
    assert "Field name header spelling" in output, validated.output
    return output


def _terminal_outcomes(tmp_path: Path) -> dict[str, int]:
    """Every token's single terminal outcome, keyed ``outcome/path``; ``NONTERMINAL`` for a token without one."""
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        rows = con.execute(
            "select coalesce(o.outcome || '/' || o.path, 'NONTERMINAL') from tokens t "
            "left join token_outcomes o on o.token_id = t.token_id and o.completed = 1"
        ).fetchall()
    finally:
        con.close()
    counts: dict[str, int] = {}
    for (key,) in rows:
        counts[key] = counts.get(key, 0) + 1
    return counts


def _reasons(tmp_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return [json.loads(text) for (text,) in con.execute("select error_details_json from transform_errors order by rowid")]
    finally:
        con.close()


def _batch_failure_reasons(tmp_path: Path) -> list[dict[str, Any]]:
    con = sqlite3.connect(tmp_path / "audit.db")
    try:
        return [json.loads(text) for (text,) in con.execute("select error_json from node_states where error_json is not null")]
    finally:
        con.close()


def _assert_routed(tmp_path: Path, result: Any, *, reason: str, literal: str, canonical: str, rows: int = 2) -> None:
    """Every row routed to on_error with the rule's one value-free reason; every token terminal."""
    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": rows}
    reasons = _reasons(tmp_path)
    assert len(reasons) == rows
    assert {(r["reason"], tuple(r["fields"]), tuple(r["canonical_fields"])) for r in reasons} == {(reason, (literal,), (canonical,))}
    assert all(SENTINEL not in json.dumps(r) for r in reasons)


# ---------------------------------------------------------------------------
# Read declarations (schema fields, required fields, column options)
# ---------------------------------------------------------------------------


def test_a_field_mapper_source_declared_by_header_no_longer_records_a_false_claim(tmp_path: Path) -> None:
    """Sweep §2.1 / CODEX-S1a: ``{Name: given}`` with ``Name: int?`` recorded ``given: int, declared`` over a str, exit 0."""
    mapper = _transform("field_mapper", {"mapping": {"Name": "given"}, "schema": {"mode": "flexible", "fields": ["Name: int?"]}})
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[mapper]))

    _assert_routed(tmp_path, result, reason=_READ, literal="Name", canonical="name")
    assert not (tmp_path / "out.jsonl").exists() or (tmp_path / "out.jsonl").read_text() == ""


def test_the_same_declaration_is_refused_at_build_behind_a_closed_upstream(tmp_path: Path) -> None:
    mapper = _transform("field_mapper", {"mapping": {"Name": "given"}, "schema": {"mode": "flexible", "fields": ["Name: int?"]}})
    output = _refused_at_build(_settings(tmp_path, source=_csv_source(tmp_path, schema=_FIXED_ID_NAME), transforms=[mapper]))

    assert "'Name' is a header spelling of 'name'" in output
    assert "Declare 'name'" in output


def test_a_row_lookup_by_header_keeps_working(tmp_path: Path) -> None:
    """The mapping SOURCE is a lookup: the canonical declaration of the same field delivers."""
    mapper = _transform("field_mapper", {"mapping": {"Name": "given"}, "schema": {"mode": "flexible", "fields": ["name: str?"]}})
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[mapper]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}


def test_a_value_transform_schema_declaration_is_no_longer_inert(tmp_path: Path) -> None:
    """Sweep §2.2: ``Name: int?`` on a value_transform over header ``Name`` had zero effect, exit 0."""
    vt = _transform(
        "value_transform",
        {"operations": [{"target": "doubled", "expression": "row['name']"}], "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
    )
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[vt]))

    _assert_routed(tmp_path, result, reason=_READ, literal="Name", canonical="name")


def _provisioned_sqlite_target(path: Path) -> str:
    """The database sink writes only to a table and effect ledger the operator provisioned."""
    url = f"sqlite:///{path}"
    engine = create_engine(url)
    metadata = MetaData()
    Table("t", metadata, Column("id", Text), Column("name", Text))
    database_effect_ledger_table(metadata, "_elspeth_sink_effects")
    metadata.create_all(engine)
    engine.dispose()
    return url


# The runnable sinks' file suffixes; aws_s3, azure_blob, chroma_sink and
# dataverse cannot run locally and are pinned by
# tests/invariants/test_declared_field_spelling_surfaces.py.
_SINK_SUFFIX = {"json": "jsonl", "csv": "csv", "text": "txt", "document": "docx"}


@pytest.mark.parametrize(
    ("sink_plugin", "options"),
    [
        pytest.param("json", {"format": "jsonl", "schema": {"mode": "flexible", "fields": ["Name: int?"]}}, id="json-schema-field"),
        pytest.param("csv", {"schema": {"mode": "flexible", "fields": ["Name: int?"]}}, id="csv-schema-field"),
        pytest.param("csv", {"schema": _OBSERVED, "headers": {"id": "Ident", "Name": "Full"}}, id="csv-custom-headers-key"),
        pytest.param("text", {"schema": _OBSERVED, "field": "Name"}, id="text-field-option"),
        pytest.param("document", {"schema": _OBSERVED, "field": "Name"}, id="document-field-option"),
        pytest.param(
            "database",
            {
                "table": "t",
                "if_exists": "append",
                "effect_ledger": {"table": "_elspeth_sink_effects", "permissions": ["select", "insert"]},
                "schema": {"mode": "flexible", "fields": ["Name: int?"]},
            },
            id="database-schema-field",
        ),
    ],
)
def test_a_sink_declaration_by_header_ends_the_run_with_every_token_terminal(
    tmp_path: Path, sink_plugin: str, options: dict[str, Any]
) -> None:
    """Sweep §2.3: inert at every sink; a custom headers key crashed the write leaving tokens NON-terminal.

    The sink seam routes no contract violation, so the refusal ends the run the
    way a sink's required-field violation does — but with the rule's actionable
    verdict and every token's terminal recorded.
    """
    options = dict(options)
    if sink_plugin == "database":
        options["url"] = _provisioned_sqlite_target(tmp_path / "out.db")
    else:
        options["path"] = str(tmp_path / f"out.{_SINK_SUFFIX[sink_plugin]}")
    sink = {"plugin": sink_plugin, "on_write_failure": "discard", "options": options}
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), out_sink=sink))

    assert result.exit_code == 4, result.output
    assert "HeaderSpelledDeclarationViolation" in result.output
    assert "'Name' is a header spelling of 'name'" in _flat(result.output)
    assert SENTINEL not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2}


def test_a_sink_declaration_by_header_is_refused_at_build_behind_a_closed_upstream(tmp_path: Path) -> None:
    sink = _json_sink(tmp_path / "out.jsonl", schema={"mode": "flexible", "fields": ["Name: int?"]})
    _refused_at_build(_settings(tmp_path, source=_csv_source(tmp_path, schema=_FIXED_ID_NAME), out_sink=sink))


def test_a_header_spelled_required_input_field_names_its_canonical_form(tmp_path: Path) -> None:
    """Sweep §2.4: the Phase-1 verdict said only 'Missing fields: [Name]' for a producer guaranteeing 'name'."""
    vt = _transform(
        "value_transform",
        {"required_input_fields": ["Name"], "operations": [{"target": "doubled", "expression": "row['name']"}], "schema": _OBSERVED},
    )
    validated = _cli("validate", "-s", str(_settings(tmp_path, source=_csv_source(tmp_path, schema=_FLEXIBLE_ID_NAME), transforms=[vt])))

    assert validated.exit_code != 0
    assert "Header spellings: 'Name' is a header spelling of 'name'" in _flat(validated.output)


def _web_scrape(url_field: str) -> dict[str, Any]:
    return _transform(
        "web_scrape",
        {
            "url_field": url_field,
            "content_field": "page_content",
            "fingerprint_field": "page_fingerprint",
            "http": {"abuse_contact": "test@example.com", "scraping_reason": "spelling rule test", "allowed_hosts": ["127.0.0.0/8"]},
            "schema": _OBSERVED,
        },
    )


def test_a_declared_input_option_by_header_routes_instead_of_aborting(tmp_path: Path) -> None:
    """web_scrape ``url_field: Url``: a Tier-1 DeclaredRequiredInputFieldsViolation (exit 4) before; routed now, no request made."""
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path, "ID,Url\n1,http://127.0.0.1:9/a\n"), transforms=[_web_scrape("Url")]))

    _assert_routed(tmp_path, result, reason=_READ, literal="Url", canonical="url", rows=1)


def test_a_declared_input_option_by_header_is_refused_at_build_behind_a_closed_upstream(tmp_path: Path) -> None:
    source = _csv_source(tmp_path, "ID,Url\n1,http://127.0.0.1:9/a\n", schema={"mode": "fixed", "fields": ["id: str", "url: str"]})
    _refused_at_build(_settings(tmp_path, source=source, transforms=[_web_scrape("Url")]))


def test_a_blob_ref_field_by_header_routes(tmp_path: Path) -> None:
    blob = _transform(
        "blob_json_expand",
        {
            "source": "blob",
            "blob_ref_field": "BlobRef",
            "content_type_field": "blob_content_type",
            "data_key": "docs",
            "fields": ["doc_id"],
            "schema": _OBSERVED,
        },
    )
    source = _csv_source(tmp_path, "ID,BlobRef,blob_content_type\n1,abc,application/json\n")
    result = _run(_settings(tmp_path, source=source, transforms=[blob]))

    # Normalization lowercases; it does not split camelCase: 'BlobRef' names 'blobref'.
    _assert_routed(tmp_path, result, reason=_READ, literal="BlobRef", canonical="blobref", rows=1)


def test_a_rag_query_field_by_header_is_refused_at_build_behind_a_closed_upstream(tmp_path: Path) -> None:
    """Sweep §2.4 sibling / §2.7: the header-spelled ``query_field`` never reaches the query builder's raw dict."""
    rag = _transform(
        "rag_retrieval",
        {
            "query_field": "Question",
            "output_prefix": "sci",
            "provider": "chroma",
            "provider_config": {"collection": "col", "mode": "persistent", "persist_directory": str(tmp_path / "chroma")},
            "schema": _OBSERVED,
        },
    )
    source = _csv_source(tmp_path, "ID,Question\n1,q\n", schema={"mode": "fixed", "fields": ["id: str", "question: str"]})
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[rag]))

    assert "'Question' is a header spelling of 'question'" in output


class TestStringScanFields:
    """keyword_filter's named ``fields`` (and the guardrails' ``fields``): ``declared_string_input_fields``.

    The string-type build validator compares these names to the upstream
    schema as written, so ``fields: [Count]`` over a source that types
    ``count`` int passed ``elspeth validate`` while ``fields: [count]`` was
    refused — and the run then failed every row with ``non_string_field``.
    They are read declarations; a header spelling is refused like any other.
    """

    def _keyword_filter(self, field: str) -> dict[str, Any]:
        return _transform("keyword_filter", {"fields": [field], "blocked_patterns": ["zzz"], "schema": _OBSERVED})

    @pytest.mark.parametrize("count_type", ["int", "str"])
    def test_a_header_spelled_scan_field_is_refused_at_build_behind_a_closed_upstream(self, tmp_path: Path, count_type: str) -> None:
        source = _csv_source(tmp_path, "ID,Count\n1,5\n2,7\n", schema={"mode": "fixed", "fields": ["id: int", f"count: {count_type}"]})
        output = _refused_at_build(_settings(tmp_path, source=source, transforms=[self._keyword_filter("Count")]))

        assert "'Count' is a header spelling of 'count'" in output
        assert "Declare 'count'" in output

    def test_a_header_spelled_scan_field_behind_an_observed_upstream_routes(self, tmp_path: Path) -> None:
        """Before: the lookup resolved ``Name`` and the scan delivered every row; now routed, one value-free reason."""
        result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[self._keyword_filter("Name")]))

        _assert_routed(tmp_path, result, reason=_READ, literal="Name", canonical="name")

    def test_the_canonical_scan_field_delivers(self, tmp_path: Path) -> None:
        result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[self._keyword_filter("name")]))

        assert result.exit_code == 0, result.output
        assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}


class TestTypeCoerceConversionField:
    """S7(b): the conversion field is a declared input (Q4 amendment), so the rule governs it."""

    def _coerce(self, field: str, schema: dict[str, Any]) -> dict[str, Any]:
        return _transform("type_coerce", {"conversions": [{"field": field, "to": "int"}], "schema": schema})

    def test_a_header_spelled_conversion_under_a_declared_schema_routes_instead_of_aborting(self, tmp_path: Path) -> None:
        """Before: exit 4, Tier-1 ``SchemaConfigModeViolation: missing required fields ['Price']``."""
        source = _csv_source(tmp_path, "ID,Price\n1,5\n2,6\n")
        coerce = self._coerce("Price", {"mode": "flexible", "fields": ["price: str"]})
        result = _run(_settings(tmp_path, source=source, transforms=[coerce]))

        _assert_routed(tmp_path, result, reason=_READ, literal="Price", canonical="price")

    def test_the_observed_mode_lookup_is_refused_too(self, tmp_path: Path) -> None:
        """Behaviour change (CHANGELOG): an observed-mode ``field: Price`` worked as a lookup; it now routes."""
        result = _run(
            _settings(tmp_path, source=_csv_source(tmp_path, "ID,Price\n1,5\n2,6\n"), transforms=[self._coerce("Price", _OBSERVED)])
        )

        _assert_routed(tmp_path, result, reason=_READ, literal="Price", canonical="price")

    def test_the_canonical_conversion_delivers(self, tmp_path: Path) -> None:
        result = _run(
            _settings(tmp_path, source=_csv_source(tmp_path, "ID,Price\n1,5\n2,6\n"), transforms=[self._coerce("price", _OBSERVED)])
        )

        assert result.exit_code == 0, result.output
        assert [row["price"] for row in map(json.loads, (tmp_path / "out.jsonl").read_text().splitlines())] == [5, 6]

    def test_a_missing_conversion_field_behind_an_observed_upstream_routes(self, tmp_path: Path) -> None:
        """A conversion naming a field no row carries routes ``missing_field``, as release did (R2, ADR-013 Amendment 2026-09-27).

        As a declared input it is settled before ``process()``: the observed
        upstream proves nothing about ``nope``, so each row's miss is a fact
        about that row and routes with one value-free reason. The spelling-rule
        unit's first cut ended the run here (exit 4) — a lane regression that
        never shipped.
        """
        result = _run(
            _settings(tmp_path, source=_csv_source(tmp_path, "ID,Price\n1,5\n2,6\n"), transforms=[self._coerce("nope", _OBSERVED)])
        )

        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output
        assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 2}
        reasons = _reasons(tmp_path)
        assert {(r["reason"], tuple(r["fields"])) for r in reasons} == {("missing_field", ("nope",))}


class TestJSONExplodeArrayField:
    """R2: json_explode's ``array_field`` is a declared input, so the rule governs it (it was a lookup)."""

    def _explode(self, field: str) -> dict[str, Any]:
        return _transform("json_explode", {"array_field": field, "schema": _OBSERVED})

    def test_a_header_spelled_array_field_behind_an_observed_upstream_routes(self, tmp_path: Path) -> None:
        """Behaviour change (CHANGELOG): the lookup resolved ``Name`` inside ``process()``; it now routes before it."""
        result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[self._explode("Name")]))

        _assert_routed(tmp_path, result, reason=_READ, literal="Name", canonical="name")

    def test_a_missing_array_field_behind_an_observed_upstream_routes(self, tmp_path: Path) -> None:
        """Before R2: ``row[array_field]`` raised a raw KeyError that ended the run (exit 4)."""
        result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[self._explode("nope")]))

        assert result.exit_code == 2, result.output
        assert "Traceback" not in result.output
        assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 2}
        reasons = _reasons(tmp_path)
        assert {(r["reason"], tuple(r["fields"])) for r in reasons} == {("missing_field", ("nope",))}
        assert all(SENTINEL not in json.dumps(r) for r in reasons)


# ---------------------------------------------------------------------------
# Created names (S7 c)
# ---------------------------------------------------------------------------


def _value_transform_target(target: str) -> dict[str, Any]:
    return _transform("value_transform", {"operations": [{"target": target, "expression": "row['name'] + '!'"}], "schema": _OBSERVED})


@pytest.mark.parametrize(
    "csv_text",
    [pytest.param(_ID_NAME_CSV, id="probe-A-header-Name"), pytest.param(f"ID,NAME\n1,{SENTINEL}\n2,Bob\n", id="probe-B-header-NAME")],
)
def test_a_header_spelled_target_is_refused_at_build_behind_a_participating_upstream(tmp_path: Path, csv_text: str) -> None:
    """Probe A crashed ``Duplicate original_name`` (exit 4, token abandoned); probe B silently shadowed ``name`` (exit 0)."""
    source = _csv_source(tmp_path, csv_text, schema=_FLEXIBLE_ID_NAME)
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[_value_transform_target("Name")]))

    assert "'Name' is a header spelling of the arriving field 'name'" in output


def test_a_header_spelled_target_behind_an_observed_upstream_routes(tmp_path: Path) -> None:
    """Probe D: the build cannot see 'name'; the executor routes before the write (was: ValueError, token abandoned)."""
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[_value_transform_target("Name")]))

    _assert_routed(tmp_path, result, reason=_CREATE, literal="Name", canonical="name")


@pytest.mark.parametrize("target", ["Total", "name"], ids=["probe-C-unrelated-created-name", "probe-E-canonical-overwrite"])
def test_other_targets_deliver(tmp_path: Path, target: str) -> None:
    source = _csv_source(tmp_path, schema=_FLEXIBLE_ID_NAME)
    result = _run(_settings(tmp_path, source=source, transforms=[_value_transform_target(target)]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}


def test_targets_spelling_one_another_are_refused_by_config(tmp_path: Path) -> None:
    vt = _transform(
        "value_transform",
        {"operations": [{"target": "total", "expression": "1"}, {"target": "Total", "expression": "2"}], "schema": _OBSERVED},
    )
    validated = _cli("validate", "-s", str(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[vt])))

    assert validated.exit_code != 0
    assert "target 'Total' is a header spelling of target 'total'" in _flat(validated.output)


def test_a_field_mapper_rename_target_spelling_an_arriving_field_routes(tmp_path: Path) -> None:
    """``{id: Name}`` wrote 'Name' beside 'name' (header ``Name``), so ``row['Name']`` downstream read the id, exit 0."""
    mapper = _transform("field_mapper", {"mapping": {"id": "Name"}, "schema": _OBSERVED})
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[mapper]))

    _assert_routed(tmp_path, result, reason=_CREATE, literal="Name", canonical="name")


def test_a_header_source_rename_whose_target_spells_a_kept_field_routes(tmp_path: Path) -> None:
    """``{Name: ID}`` over header ``ID,Name`` wrote ``ID`` beside ``id`` (exit 0).

    The source is an original header, so the executor cannot name what the
    rename removes and abstains; field_mapper checks the target against the
    row it forwards, before the write.
    """
    mapper = _transform("field_mapper", {"mapping": {"Name": "ID"}, "schema": _OBSERVED})
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[mapper]))

    _assert_routed(tmp_path, result, reason=_CREATE, literal="ID", canonical="id")


def test_a_rename_that_restores_the_header_is_not_a_shadow(tmp_path: Path) -> None:
    """``{name: Name}`` removes 'name' and writes 'Name': nothing arriving is shadowed."""
    mapper = _transform("field_mapper", {"mapping": {"name": "Name"}, "schema": _OBSERVED})
    result = _run(_settings(tmp_path, source=_csv_source(tmp_path), transforms=[mapper]))

    assert result.exit_code == 0, result.output
    assert [sorted(json.loads(line)) for line in (tmp_path / "out.jsonl").read_text().splitlines()] == [["Name", "id"], ["Name", "id"]]


def test_a_required_created_name_the_source_cannot_supply_builds_and_routes_each_row(tmp_path: Path) -> None:
    """``{Name: Name}`` + required ``Name: str`` over a source with no ``name`` (P1 review r3 F1).

    ``Name`` is the name the node creates, so neither the runtime nor the
    composer demands it of the input row; the pipeline builds, and each row
    whose lookup finds nothing routes ``missing_field`` — every token terminal,
    no traceback. The Stage-1 half is pinned in the composer/runtime agreement
    file (Shape 31).
    """
    mapper = _transform("field_mapper", {"mapping": {"Name": "Name"}, "schema": {"mode": "flexible", "fields": ["Name: str"]}})
    source = _csv_source(tmp_path, f"ID,Other\n1,{SENTINEL}\n2,Bob\n", schema={"mode": "fixed", "fields": ["id: str", "other: str"]})
    result = _run(_settings(tmp_path, source=source, transforms=[mapper]))

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 2}
    reasons = _reasons(tmp_path)
    assert [r["reason"] for r in reasons] == ["missing_field", "missing_field"]
    assert all(SENTINEL not in json.dumps(r) for r in reasons)


# ---------------------------------------------------------------------------
# Batch transforms (sweep §2.8)
# ---------------------------------------------------------------------------


def test_a_header_spelled_group_by_fails_the_batch_routed(tmp_path: Path) -> None:
    """``group_by: Name`` emitted the output key 'Name' while the field is 'name'; it is a declaration, so it is refused."""
    source = _csv_source(
        tmp_path, "ID,Name,Amount\n1,Ann,10\n2,Ann,20\n3,Bob,30\n", schema={"mode": "flexible", "fields": ["amount: float"]}
    )
    aggregation = {
        "name": "bs",
        "plugin": "batch_stats",
        "input": "rows",
        "on_success": "out",
        "on_error": "quarantine",
        "output_mode": "transform",
        "trigger": {"count": 3},
        "options": {"value_field": "amount", "group_by": "Name", "schema": _OBSERVED},
    }
    result = _run(_settings(tmp_path, source=source, aggregations=[aggregation]))

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 3}
    [failure] = [r for r in _batch_failure_reasons(tmp_path) if r.get("reason") == _READ]
    assert (failure["fields"], failure["canonical_fields"]) == (["Name"], ["name"])


# ---------------------------------------------------------------------------
# A header the source's field_mapping renames (Codex final review, finding 1)
# ---------------------------------------------------------------------------
#
# CSV header ``Name`` under ``field_mapping: {name: b}``: rows carry ``b``, the
# contract records ``Name`` as its original name, and a lookup of ``Name``
# reads ``b``. The predicate compared only ``normalize("Name")`` (``name``,
# absent), so a declaration spelled by the mapped header walked past every
# surface — the field_mapper shape delivered a str under a recorded
# ``given: int`` with exit 0. The declaration is now resolved as the upstream
# resolves it: the row's contract at run time, the sources' renames at build.
# Each surface of the rule gets its source-field_mapping variant here.

_MAPPED: dict[str, str] = {"name": "b"}
_FIXED_ID_B: dict[str, Any] = {"mode": "fixed", "fields": ["id: str", "b: str"]}


def _mapped_source(tmp_path: Path, *, schema: dict[str, Any] | None = None, text: str = _ID_NAME_CSV) -> dict[str, Any]:
    source = _csv_source(tmp_path, text, schema=schema)
    source["options"]["field_mapping"] = dict(_MAPPED)
    return source


def _mapped_surfaces(*, runtime: bool) -> list[Any]:
    """One transform per declaration surface of the rule, each declaring the mapped header ``Name``: (transform, reason).

    ``required_input_fields`` has no run-time residual to exercise: an
    observed upstream guarantees nothing, so the build already refuses the
    requirement as missing (and names the header spelling, below).
    """
    required = [
        pytest.param(
            _transform(
                "value_transform",
                {"required_input_fields": ["Name"], "operations": [{"target": "doubled", "expression": "row['b']"}], "schema": _OBSERVED},
            ),
            _READ,
            id="required_input_fields",
        )
    ]
    return [
        pytest.param(
            _transform(
                "field_mapper",
                {"mapping": {"Name": "given"}, "select_only": True, "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
            ),
            _READ,
            id="field_mapper-schema-field-codex-shape",
        ),
        pytest.param(
            _transform(
                "value_transform",
                {"operations": [{"target": "doubled", "expression": "row['b']"}], "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
            ),
            _READ,
            id="value_transform-schema-field",
        ),
        *([] if runtime else required),
        pytest.param(
            _transform("keyword_filter", {"fields": ["Name"], "blocked_patterns": ["zzz"], "schema": _OBSERVED}),
            _READ,
            id="keyword_filter-scan-field",
        ),
        pytest.param(
            _transform("type_coerce", {"conversions": [{"field": "Name", "to": "int"}], "schema": _OBSERVED}),
            _READ,
            id="type_coerce-conversion-field",
        ),
        pytest.param(_web_scrape("Name"), _READ, id="web_scrape-url_field"),
        pytest.param(
            _transform("value_transform", {"operations": [{"target": "Name", "expression": "row['b'] + '!'"}], "schema": _OBSERVED}),
            _CREATE,
            id="value_transform-target",
        ),
        pytest.param(
            _transform("field_mapper", {"mapping": {"id": "Name"}, "schema": _OBSERVED}), _CREATE, id="field_mapper-rename-target"
        ),
    ]


@pytest.mark.parametrize(("transform", "reason"), _mapped_surfaces(runtime=True))
def test_a_renamed_header_declaration_behind_an_observed_upstream_routes(tmp_path: Path, transform: dict[str, Any], reason: str) -> None:
    """The run time resolves ``Name`` through the row's contract (recorded original of ``b``) and routes every row."""
    result = _run(_settings(tmp_path, source=_mapped_source(tmp_path), transforms=[transform]))

    _assert_routed(tmp_path, result, reason=reason, literal="Name", canonical="b")
    assert not (tmp_path / "out.jsonl").exists() or (tmp_path / "out.jsonl").read_text() == ""


@pytest.mark.parametrize(("transform", "reason"), _mapped_surfaces(runtime=False))
def test_a_renamed_header_declaration_is_refused_at_build_behind_a_closed_upstream(
    tmp_path: Path, transform: dict[str, Any], reason: str
) -> None:
    """The build resolves ``Name`` through the source's rename (``name`` -> ``b``) and refuses before any row exists."""
    output = _refused_at_build(_settings(tmp_path, source=_mapped_source(tmp_path, schema=_FIXED_ID_B), transforms=[transform]))

    if reason == _READ:
        assert "'Name' is a header spelling of 'b': headers are normalized to lowercase identifiers ('Name' -> 'name') and " in output
        assert "the source's field_mapping renames 'name' to 'b'. Declare 'b'" in output
    else:
        assert "'Name' is a header spelling of the arriving field 'b'" in output


def test_a_renamed_header_required_input_field_names_the_rename_target(tmp_path: Path) -> None:
    """Behind a participating but OPEN upstream the requirement is merely missing; the verdict's hint resolves it to ``b``."""
    vt = _transform(
        "value_transform",
        {"required_input_fields": ["Name"], "operations": [{"target": "doubled", "expression": "row['b']"}], "schema": _OBSERVED},
    )
    source = _mapped_source(tmp_path, schema={"mode": "flexible", "fields": ["id: str", "b: str"]})
    validated = _cli("validate", "-s", str(_settings(tmp_path, source=source, transforms=[vt])))

    assert validated.exit_code != 0
    assert "Header spellings: 'Name' is a header spelling of 'b'" in _flat(validated.output)


def test_the_mapping_key_itself_is_refused_at_build(tmp_path: Path) -> None:
    """``name`` is its own normalized form, yet the source renames it: declaring it names ``b`` too."""
    vt = _transform(
        "value_transform",
        {"operations": [{"target": "doubled", "expression": "row['b']"}], "schema": {"mode": "flexible", "fields": ["name: int?"]}},
    )
    output = _refused_at_build(_settings(tmp_path, source=_mapped_source(tmp_path, schema=_FIXED_ID_B), transforms=[vt]))

    assert "'name' is a header spelling of 'b': the source's field_mapping renames 'name' to 'b'. Declare 'b'" in output


def test_the_rename_target_is_the_canonical_declaration_and_delivers(tmp_path: Path) -> None:
    """Control: the codex shape declared by the target ``b`` — the lookup ``Name`` keeps working, the str is delivered as a str."""
    mapper = _transform(
        "field_mapper", {"mapping": {"Name": "given"}, "select_only": True, "schema": {"mode": "flexible", "fields": ["b: str?"]}}
    )
    result = _run(_settings(tmp_path, source=_mapped_source(tmp_path), transforms=[mapper]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}
    assert [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()] == [{"given": SENTINEL}, {"given": "Bob"}]


def test_the_canonical_typed_declaration_of_the_codex_shape_is_refused_by_type(tmp_path: Path) -> None:
    """Control: ``b: int?`` over the fixed ``b: str`` is the type refusal the header spelling used to walk past."""
    mapper = _transform(
        "field_mapper", {"mapping": {"Name": "given"}, "select_only": True, "schema": {"mode": "flexible", "fields": ["b: int?"]}}
    )
    validated = _cli("validate", "-s", str(_settings(tmp_path, source=_mapped_source(tmp_path, schema=_FIXED_ID_B), transforms=[mapper])))

    assert validated.exit_code != 0
    assert "Type mismatches: b (expected int | None, got str)" in _flat(validated.output)


def test_a_renamed_header_sink_declaration_ends_the_run_with_every_token_terminal(tmp_path: Path) -> None:
    sink = _json_sink(tmp_path / "out.jsonl", schema={"mode": "flexible", "fields": ["Name: int?"]})
    result = _run(_settings(tmp_path, source=_mapped_source(tmp_path), out_sink=sink))

    assert result.exit_code == 4, result.output
    assert "'Name' is a header spelling of 'b'" in _flat(result.output)
    assert SENTINEL not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/unrouted": 2}


def test_a_renamed_header_sink_declaration_is_refused_at_build(tmp_path: Path) -> None:
    sink = _json_sink(tmp_path / "out.jsonl", schema={"mode": "flexible", "fields": ["Name: int?"]})
    output = _refused_at_build(_settings(tmp_path, source=_mapped_source(tmp_path, schema=_FIXED_ID_B), out_sink=sink))

    assert "'Name' is a header spelling of 'b'" in output


def test_a_renamed_header_group_by_fails_the_batch_routed(tmp_path: Path) -> None:
    source = _mapped_source(
        tmp_path, schema={"mode": "flexible", "fields": ["amount: float"]}, text="ID,Name,Amount\n1,Ann,10\n2,Ann,20\n3,Bob,30\n"
    )
    aggregation = {
        "name": "bs",
        "plugin": "batch_stats",
        "input": "rows",
        "on_success": "out",
        "on_error": "quarantine",
        "output_mode": "transform",
        "trigger": {"count": 3},
        "options": {"value_field": "amount", "group_by": "Name", "schema": _OBSERVED},
    }
    result = _run(_settings(tmp_path, source=source, aggregations=[aggregation]))

    assert result.exit_code == 2, result.output
    assert "Traceback" not in result.output
    assert _terminal_outcomes(tmp_path) == {"failure/on_error_routed": 3}
    [failure] = [r for r in _batch_failure_reasons(tmp_path) if r.get("reason") == _READ]
    assert (failure["fields"], failure["canonical_fields"]) == (["Name"], ["b"])


# A headerless source renames each column AS WRITTEN (review-C1-alias-bypass-r1 F1)
# ---------------------------------------------------------------------------
#
# ``columns: [id, Name]`` + ``field_mapping: {Name: b}``: resolve_field_names
# keys a headerless source's mapping by the column as written, not by its
# normalized form, so ``Name`` names ``b`` while ``name`` names nothing. The
# build resolution must follow that keying — resolving ``Name`` by its
# normalized form missed the rename and let the declaration past
# ``elspeth validate`` behind a closed upstream (every row then failed).

_HEADERLESS_TEXT = f"1,{SENTINEL}\n2,Bob\n"


def _headerless_source(
    tmp_path: Path, *, columns: list[str], field_mapping: dict[str, str], schema: dict[str, Any] | None = None
) -> dict[str, Any]:
    source = _csv_source(tmp_path, _HEADERLESS_TEXT, schema=schema)
    source["options"]["columns"] = list(columns)
    source["options"]["field_mapping"] = dict(field_mapping)
    return source


@pytest.mark.parametrize(("transform", "reason"), _mapped_surfaces(runtime=False))
def test_a_renamed_headerless_column_declaration_is_refused_at_build_behind_a_closed_upstream(
    tmp_path: Path, transform: dict[str, Any], reason: str
) -> None:
    """The build resolves ``Name`` through the source's as-written rename (``Name`` -> ``b``) and refuses before any row exists."""
    source = _headerless_source(tmp_path, columns=["id", "Name"], field_mapping={"Name": "b"}, schema=_FIXED_ID_B)
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[transform]))

    if reason == _READ:
        assert "'Name' is a header spelling of 'b': the source's field_mapping renames its column 'Name' to 'b'. Declare 'b'" in output
        assert "headers are normalized" not in output
    else:
        assert "'Name' is a header spelling of the arriving field 'b'" in output


@pytest.mark.parametrize(("transform", "reason"), _mapped_surfaces(runtime=True))
def test_a_renamed_headerless_column_declaration_behind_an_observed_upstream_routes(
    tmp_path: Path, transform: dict[str, Any], reason: str
) -> None:
    """The run time resolves ``Name`` through the row's contract (recorded original of ``b``) and routes every row."""
    source = _headerless_source(tmp_path, columns=["id", "Name"], field_mapping={"Name": "b"})
    result = _run(_settings(tmp_path, source=source, transforms=[transform]))

    _assert_routed(tmp_path, result, reason=reason, literal="Name", canonical="b")


def test_a_case_variant_of_a_renamed_headerless_column_is_no_spelling_of_the_target(tmp_path: Path) -> None:
    """Control for the as-written keying: under ``columns: [id, name]`` + ``{name: b}`` the literal ``Name`` names nothing.

    No lookup resolves ``Name`` to ``b`` there (the contract records ``name``),
    so an optional ``Name`` declaration is not a spelling of ``b``: the build
    admits it and the row is delivered. Keying the rename by the literal's
    normalized form instead would refuse it as a spelling of ``b``.
    """
    vt = _transform(
        "value_transform",
        {"operations": [{"target": "doubled", "expression": "row['b'] + '!'"}], "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
    )
    source = _headerless_source(tmp_path, columns=["id", "name"], field_mapping={"name": "b"}, schema=_FIXED_ID_B)
    result = _run(_settings(tmp_path, source=source, transforms=[vt]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}


# The resolution follows the live reach TRANSITIVELY (review-C1-alias-bypass-r1 F2)
# ---------------------------------------------------------------------------
#
# The build resolves a declaration through the renames of every source whose
# rows reach the node over live edges, however many transforms lie between.
# A consumer behind an intermediate transform must be refused exactly as a
# direct consumer is; resolving through direct predecessors only would find
# no source there and let the Codex shape past ``elspeth validate``.


def _hop() -> dict[str, Any]:
    """A closed intermediate transform that carries ``b`` through (source -> hop -> consumer)."""
    return {
        "name": "hop",
        "plugin": "value_transform",
        "input": "rows",
        "on_success": "mid",
        "on_error": "quarantine",
        "options": {"operations": [{"target": "x", "expression": "row['b'] + '!'"}], "schema": _FIXED_ID_B},
    }


@pytest.mark.parametrize(
    ("consumer", "spelling"),
    [
        pytest.param(
            _transform(
                "field_mapper",
                {"mapping": {"Name": "given"}, "select_only": True, "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
            ),
            "'Name' is a header spelling of 'b': headers are normalized to lowercase identifiers ('Name' -> 'name') and "
            "the source's field_mapping renames 'name' to 'b'. Declare 'b'",
            id="read-field_mapper-codex-shape",
        ),
        pytest.param(
            _transform("value_transform", {"operations": [{"target": "Name", "expression": "row['b'] + '!'"}], "schema": _OBSERVED}),
            "'Name' is a header spelling of the arriving field 'b'",
            id="create-value_transform-target",
        ),
    ],
)
def test_a_renamed_header_declaration_behind_an_intermediate_transform_is_refused_at_build(
    tmp_path: Path, consumer: dict[str, Any], spelling: str
) -> None:
    source = _mapped_source(tmp_path, schema=_FIXED_ID_B)
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[_hop(), {**consumer, "input": "mid"}]))

    assert "its upstream 'transform_hop_" in output
    assert spelling in output


# ---------------------------------------------------------------------------
# A field a TRANSFORM renamed (review-C1-alias-bypass-r2)
# ---------------------------------------------------------------------------
#
# field_mapper ``{b: c}`` carries b's recorded original name ('Name') onto
# ``c``, so a lookup of 'Name' reads ``c`` and the run time refused every row of
# a consumer declaring 'Name'. The build resolved the declaration through the
# sources' renames and normalization only, never through the transform's, so
# ``elspeth validate`` admitted a config whose every row failed — with and
# without a source field_mapping. The build now follows each transform's
# ``renamed_input_fields`` between the sources and the consumer.


def _renaming_hop(*, mapped: bool) -> dict[str, Any]:
    """A closed field_mapper renaming the header field to ``c`` (source -> hop -> consumer)."""
    old = "b" if mapped else "name"
    return {
        "name": "hop",
        "plugin": "field_mapper",
        "input": "rows",
        "on_success": "mid",
        "on_error": "quarantine",
        "options": {"mapping": {old: "c"}, "schema": {"mode": "fixed", "fields": ["id: str", f"{old}: str"]}},
    }


def _source_before_the_hop(tmp_path: Path, *, mapped: bool) -> dict[str, Any]:
    if mapped:
        return _mapped_source(tmp_path, schema=_FIXED_ID_B)
    return _csv_source(tmp_path, schema=_FIXED_ID_NAME)


_CARRIED_READ = (
    "'Name' is a header spelling of 'c': a transform upstream renames the field it names to 'c', so rows carry it as 'c' "
    "(a lookup of 'Name' reads that field). Declare 'c'"
)


@pytest.mark.parametrize("mapped", [True, False], ids=["source-field_mapping", "no-source-field_mapping"])
@pytest.mark.parametrize(
    ("consumer", "spelling"),
    [
        pytest.param(
            _transform(
                "field_mapper",
                {"mapping": {"Name": "given"}, "select_only": True, "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
            ),
            _CARRIED_READ,
            id="read-field_mapper-codex-shape",
        ),
        pytest.param(
            _transform(
                "value_transform",
                {"operations": [{"target": "d", "expression": "row['c']"}], "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
            ),
            _CARRIED_READ,
            id="read-value_transform-schema-field",
        ),
        pytest.param(
            _transform("value_transform", {"operations": [{"target": "Name", "expression": "row['c'] + '!'"}], "schema": _OBSERVED}),
            "'Name' is a header spelling of the arriving field 'c'",
            id="create-value_transform-target",
        ),
    ],
)
def test_a_header_spelling_of_a_field_a_transform_renamed_is_refused_at_build(
    tmp_path: Path, mapped: bool, consumer: dict[str, Any], spelling: str
) -> None:
    source = _source_before_the_hop(tmp_path, mapped=mapped)
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[_renaming_hop(mapped=mapped), {**consumer, "input": "mid"}]))

    assert "its upstream 'transform_hop_" in output
    assert spelling in output


def test_a_sink_declaration_of_a_field_a_transform_renamed_is_refused_at_build(tmp_path: Path) -> None:
    hop = {**_renaming_hop(mapped=True), "on_success": "out"}
    sink = _json_sink(tmp_path / "out.jsonl", schema={"mode": "flexible", "fields": ["Name: str?"]})
    output = _refused_at_build(_settings(tmp_path, source=_mapped_source(tmp_path, schema=_FIXED_ID_B), transforms=[hop], out_sink=sink))

    assert _CARRIED_READ in output


def test_the_renamed_field_is_the_canonical_declaration_and_delivers(tmp_path: Path) -> None:
    consumer = _transform(
        "value_transform", {"operations": [{"target": "d", "expression": "row['c']"}], "schema": {"mode": "flexible", "fields": ["c: str"]}}
    )
    result = _run(
        _settings(
            tmp_path,
            source=_mapped_source(tmp_path, schema=_FIXED_ID_B),
            transforms=[_renaming_hop(mapped=True), {**consumer, "input": "mid"}],
        )
    )

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}
    assert [json.loads(line)["d"] for line in (tmp_path / "out.jsonl").read_text().splitlines()] == [SENTINEL, "Bob"]


def test_the_name_a_source_rename_gave_a_field_is_free_once_a_transform_renames_it(tmp_path: Path) -> None:
    """Behind source {name: b} -> field_mapper {b: c}, creating 'b' afresh shadows nothing: b's identity is 'Name'.

    A lookup of 'b' reads no field there (the contract records 'Name' as c's
    original), so both the build and the run time admit the target.
    """
    consumer = _transform("value_transform", {"operations": [{"target": "b", "expression": "row['c'] + '!'"}], "schema": _OBSERVED})
    result = _run(
        _settings(
            tmp_path,
            source=_mapped_source(tmp_path, schema=_FIXED_ID_B),
            transforms=[_renaming_hop(mapped=True), {**consumer, "input": "mid"}],
        )
    )

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}
    rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert [row["b"] for row in rows] == [f"{SENTINEL}!", "Bob!"]


def test_the_header_name_of_an_unmapped_field_follows_the_rename(tmp_path: Path) -> None:
    """Without a source rename the field's identity is its header: behind {name: c}, a created 'name' shadows c."""
    consumer = _transform("value_transform", {"operations": [{"target": "name", "expression": "row['c'] + '!'"}], "schema": _OBSERVED})
    output = _refused_at_build(
        _settings(
            tmp_path,
            source=_source_before_the_hop(tmp_path, mapped=False),
            transforms=[_renaming_hop(mapped=False), {**consumer, "input": "mid"}],
        )
    )

    assert "'name' is a header spelling of the arriving field 'c'" in output


# Only a field the lookup of a rename's source can read moves (review-C1-alias-bypass-r3
# F1): renaming a field away never makes a later declaration stricter than keeping it.
# A source that is itself a field (a field_mapping target) reads exactly that field, and
# behind a headerless source no lookup reads the field normalize() makes of a column.


def _rename_to_c_hop(old: str) -> dict[str, Any]:
    return {
        **_renaming_hop(mapped=True),
        "options": {"mapping": {old: "c"}, "schema": {"mode": "fixed", "fields": ["id: str", f"{old}: str"]}},
    }


_CREATE_NAME = _transform("value_transform", {"operations": [{"target": "name", "expression": "row['c'] + '!'"}], "schema": _OBSERVED})


def _mapped_to_name_source(tmp_path: Path) -> dict[str, Any]:
    source = _csv_source(tmp_path, f"ID,x\n1,{SENTINEL}\n2,Bob\n", schema={"mode": "fixed", "fields": ["id: str", "Name: str"]})
    source["options"]["field_mapping"] = {"x": "Name"}
    return source


@pytest.mark.parametrize("shape", ["fmval-create", "hl-create"])
def test_creating_the_normalized_name_of_a_renamed_field_that_never_carried_it_delivers(tmp_path: Path, shape: str) -> None:
    if shape == "fmval-create":
        source = _mapped_to_name_source(tmp_path)
    else:
        source = _headerless_source(
            tmp_path, columns=["id", "Name"], field_mapping={}, schema={"mode": "fixed", "fields": ["id: str", "Name: str"]}
        )
    result = _run(_settings(tmp_path, source=source, transforms=[_rename_to_c_hop("Name"), {**_CREATE_NAME, "input": "mid"}]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}
    rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert [(row["c"], row["name"]) for row in rows] == [(SENTINEL, f"{SENTINEL}!"), ("Bob", "Bob!")]


def test_behind_two_renames_of_a_headerless_source_creating_the_normalized_name_delivers(tmp_path: Path) -> None:
    """hl-create-2ren (review-C1C3-residuals-r1 F1): columns [id, Name], {id: i}, {Name: c}, create 'name'.

    The first rename must carry the headerless flag to the second. If it claimed
    the upstream normalizes names, the second rename would move the normalized
    'name' onto c and the build would falsely refuse creating 'name' — the r3 F1
    shape, one rename further from the source. One rename straight off the source
    (hl-create above) does not exercise the carry.
    """
    source = _headerless_source(tmp_path, columns=["id", "Name"], field_mapping={}, schema=_OBSERVED)
    first = {**_transform("field_mapper", {"mapping": {"id": "i"}, "schema": _OBSERVED}), "name": "ren_id", "on_success": "mid1"}
    second = {
        **_transform("field_mapper", {"mapping": {"Name": "c"}, "schema": _OBSERVED}),
        "name": "ren_name",
        "input": "mid1",
        "on_success": "mid",
    }
    result = _run(_settings(tmp_path, source=source, transforms=[first, second, {**_CREATE_NAME, "input": "mid"}]))

    assert result.exit_code == 0, result.output
    assert _terminal_outcomes(tmp_path) == {"success/default_flow": 2}
    rows = [json.loads(line) for line in (tmp_path / "out.jsonl").read_text().splitlines()]
    assert [(row["i"], row["c"], row["name"]) for row in rows] == [("1", SENTINEL, f"{SENTINEL}!"), ("2", "Bob", "Bob!")]


def test_a_renamed_headerless_column_is_still_read_by_its_own_spelling(tmp_path: Path) -> None:
    """columns [Name], {Name: c}: c records 'Name' as its original, so a declared 'Name' names c and is refused."""
    source = _headerless_source(
        tmp_path, columns=["id", "Name"], field_mapping={}, schema={"mode": "fixed", "fields": ["id: str", "Name: str"]}
    )
    consumer = _transform(
        "value_transform",
        {"operations": [{"target": "d", "expression": "row['c']"}], "schema": {"mode": "flexible", "fields": ["Name: int?"]}},
    )
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[_rename_to_c_hop("Name"), {**consumer, "input": "mid"}]))

    assert _CARRIED_READ in output


def test_a_renamed_headerless_fixed_point_column_is_not_recreated(tmp_path: Path) -> None:
    """columns [name], {name: c}: c records 'name' as its original, so creating 'name' would sit beside it."""
    source = _headerless_source(tmp_path, columns=["id", "name"], field_mapping={}, schema=_FIXED_ID_NAME)
    output = _refused_at_build(_settings(tmp_path, source=source, transforms=[_rename_to_c_hop("name"), {**_CREATE_NAME, "input": "mid"}]))

    assert "'name' is a header spelling of the arriving field 'c'" in output


def test_the_mapped_header_follows_a_rename_of_the_mapping_target(tmp_path: Path) -> None:
    """Source {x: Name}, then {Name: c}: 'x' is c's recorded original, so a declared 'x' names c (the literal leg, r3 F3)."""
    consumer = _transform(
        "value_transform",
        {"operations": [{"target": "d", "expression": "row['c']"}], "schema": {"mode": "flexible", "fields": ["x: int?"]}},
    )
    output = _refused_at_build(
        _settings(tmp_path, source=_mapped_to_name_source(tmp_path), transforms=[_rename_to_c_hop("Name"), {**consumer, "input": "mid"}])
    )

    assert "'x' is a header spelling of 'c'" in output
