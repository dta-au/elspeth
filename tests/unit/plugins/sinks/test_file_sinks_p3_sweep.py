"""Regression proofs for the P3 file-sink sweep findings (20261111-plugins-review)."""

from __future__ import annotations

import csv
from pathlib import Path

import pytest

from elspeth.contracts.sink_effects import SinkEffectInputKind, SinkEffectInspectionRequest
from elspeth.plugins.infrastructure.config_base import PluginConfigError
from elspeth.plugins.sinks import _local_file_effects as local_effects
from elspeth.plugins.sinks import database_sink as database_sink_module
from elspeth.plugins.sinks.csv_sink import CSVSink, CSVSinkConfig
from elspeth.plugins.sinks.database_sink import DatabaseSink
from tests.fixtures.base_classes import inject_write_failure
from tests.unit.plugins.sinks.test_local_file_sink_effects import _prepare, _stage_path


def _csv_config_error(**options: object) -> str | None:
    """Return the config-validation message, or None when CSVSinkConfig admits the options."""
    try:
        CSVSinkConfig.from_dict({"path": "out.csv", "schema": {"mode": "observed"}, **options}, plugin_name="csv")
    except PluginConfigError as exc:
        return str(exc)
    return None


# --- Finding #25: delimiters csv.writer rejects must fail config validation ---


@pytest.mark.parametrize("delimiter", ['"', "\n", "\r"])
def test_csv_sink_config_rejects_delimiters_csv_writer_cannot_use(delimiter: str) -> None:
    error = _csv_config_error(delimiter=delimiter)
    assert error is not None, f"CSVSinkConfig admitted delimiter {delimiter!r}, which csv.writer rejects at publication"
    assert "delimiter" in error


@pytest.mark.parametrize("delimiter", [",", ";", "\t", "|"])
def test_csv_sink_config_keeps_accepting_usable_delimiters(delimiter: str) -> None:
    assert _csv_config_error(delimiter=delimiter) is None


# --- Finding #26: non-text codecs must fail config validation ---


@pytest.mark.parametrize("encoding", ["base64", "hex", "rot13", "zlib"])
def test_csv_sink_config_rejects_non_text_codecs(encoding: str) -> None:
    error = _csv_config_error(encoding=encoding)
    assert error is not None, f"CSVSinkConfig admitted non-text codec {encoding!r}, which crashes at publication"
    assert "encoding" in error


@pytest.mark.parametrize("encoding", ["utf-8", "utf-8-sig", "latin-1", "cp1252", "utf-16"])
def test_csv_sink_config_keeps_accepting_text_encodings(encoding: str) -> None:
    assert _csv_config_error(encoding=encoding) is None


# --- Finding #27: opt-in spreadsheet formula neutralisation for pipeline rows ---

_FORMULA_ROWS = [
    {"id": 1, "cell": '=HYPERLINK("http://evil","x")'},
    {"id": 2, "cell": "+1+1"},
    {"id": 3, "cell": "-2"},
    {"id": 4, "cell": "@SUM(A1)"},
    {"id": 5, "cell": "\tTAB"},
    {"id": 6, "cell": "plain"},
    {"id": -7, "cell": "a=b"},
]


def _written_csv_rows(tmp_path: Path, name: str, effect_id: str, **options: object) -> list[dict[str, str]]:
    sink = inject_write_failure(CSVSink({"path": str(tmp_path / name), "schema": {"mode": "observed"}, **options}))
    plan = _prepare(sink, effect_id=effect_id, rows=_FORMULA_ROWS)
    with _stage_path(plan).open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def test_csv_sink_neutralize_formulas_prefixes_formula_cells(tmp_path: Path) -> None:
    assert "neutralize_formulas" in CSVSinkConfig.model_fields, "CSVSinkConfig has no opt-in formula-neutralisation option"
    assert CSVSinkConfig.model_fields["neutralize_formulas"].default is False

    rows = _written_csv_rows(tmp_path, "neutral.csv", "f1" * 32, neutralize_formulas=True)

    assert [row["cell"] for row in rows] == [
        '\'=HYPERLINK("http://evil","x")',
        "'+1+1",
        "'-2",
        "'@SUM(A1)",
        "'\tTAB",
        "plain",
        "a=b",
    ]
    # Non-string values are numbers, not formula text: the negative int is untouched.
    assert rows[-1]["id"] == "-7"


def test_csv_sink_default_output_is_verbatim(tmp_path: Path) -> None:
    rows = _written_csv_rows(tmp_path, "verbatim.csv", "f2" * 32)

    assert [row["cell"] for row in rows] == [row["cell"] for row in _FORMULA_ROWS]


# --- Finding #41: DatabaseSink composer guidance must match the runtime ---


def _database_guidance_text() -> str:
    assistance = DatabaseSink.get_agent_assistance()
    assert assistance is not None
    return " ".join((assistance.summary, *assistance.composer_hints))


def test_database_sink_guidance_does_not_advertise_unsupported_behaviour() -> None:
    text = _database_guidance_text()
    lowered = text.lower()
    assert "sql server" not in lowered, "guidance advertises SQL Server, which _validate_effect_config refuses"
    assert "creating it if missing" not in lowered, "guidance advertises table creation; the sink performs no DDL"
    assert "drops and recreates" not in lowered, "guidance advertises if_exists='replace', which is refused"
    assert "map to column types" not in lowered, "guidance advertises schema-to-column type mapping; no DDL path exists"


def test_database_sink_guidance_states_the_runtime_requirements() -> None:
    lowered = _database_guidance_text().lower()
    assert "effect_ledger" in lowered
    assert "sqlite" in lowered and "postgresql" in lowered
    assert "provisioned" in lowered


def test_database_sink_replace_hint_says_replace_is_refused() -> None:
    hints = DatabaseSink.get_post_call_hints(tool_name="upsert_node", config_snapshot={"if_exists": "replace"})
    assert hints, "a 'replace' config must still get a corrective hint"
    joined = " ".join(hints).lower()
    assert "drops" not in joined, "hint claims 'replace' drops the table; the runtime refuses 'replace'"
    assert "not supported" in joined
    assert DatabaseSink.get_post_call_hints(tool_name="upsert_node", config_snapshot={"if_exists": "append"}) == ()


def test_database_sink_has_no_dead_ddl_type_map_or_stale_docstring() -> None:
    assert "SCHEMA_TYPE_TO_SQLALCHEMY" not in vars(database_sink_module), "unused DDL type map advertises a DDL path"
    assert DatabaseSink.__doc__ is not None
    assert "Creates the table" not in DatabaseSink.__doc__


# --- Finding #79: storage admission is per byte threshold, not per streamed chunk ---


def _prepare_under_recorded_admission(tmp_path: Path, chunks: tuple[bytes, ...], *, effect_id: str) -> tuple[list[tuple[int, int]], int]:
    """Prepare a local effect under a recording authority; return (admitted, on-disk-at-admission) pairs and total bytes."""
    root = tmp_path / "out"
    root.mkdir()
    target = root / "result.csv"
    calls: list[tuple[int, int]] = []

    def admit(admitted_target: Path, size_bytes: int) -> None:
        calls.append((size_bytes, local_effects.local_file_storage_bytes(admitted_target)))

    inspection = local_effects.inspect_local_effect(
        target_path=target,
        request=SinkEffectInspectionRequest(effect_id=effect_id, target="{}", predecessor_descriptor=None),
    )
    with local_effects.local_file_storage_admission([root], admit):
        plan = local_effects.prepare_local_effect(
            effect_id=effect_id,
            input_kind=SinkEffectInputKind.PIPELINE_MEMBERS,
            inspection=inspection,
            chunks=chunks,
            row_count=len(chunks),
            accepted_ordinals=tuple(range(len(chunks))),
            diverted_ordinals=(),
            encoding="utf-8",
            format_name="csv",
            stream_sequence=0,
        )
    assert _stage_path(plan).read_bytes() == b"".join(chunks)
    return calls, sum(len(chunk) for chunk in chunks)


def test_local_effect_admission_is_not_one_reservation_per_chunk(tmp_path: Path) -> None:
    # 500 row-sized chunks plus empty chunks: one admission each on the base.
    chunks = tuple(b"" if index % 50 == 0 else b"row,%05d,payload\n" % index for index in range(550))
    calls, total = _prepare_under_recorded_admission(tmp_path, chunks, effect_id="79" * 32)

    assert len(calls) <= 4, f"{len(calls)} storage admissions for {len(chunks)} chunks ({total} bytes)"
    # Every admission still precedes the bytes it covers, and the stream's full size is admitted before install.
    assert all(on_disk <= admitted for admitted, on_disk in calls)
    assert max(admitted for admitted, _ in calls) >= total


def test_local_effect_admission_scales_with_bytes_and_stays_pre_write(tmp_path: Path) -> None:
    chunk = b"x" * 1024
    chunks = (chunk,) * (4 * 64)  # 256 KiB in 1 KiB chunks
    calls, total = _prepare_under_recorded_admission(tmp_path, chunks, effect_id="7a" * 32)

    assert len(calls) <= total // (64 * 1024) + 3
    assert all(on_disk <= admitted for admitted, on_disk in calls)
    assert [admitted for admitted, _ in calls][-1] == total
