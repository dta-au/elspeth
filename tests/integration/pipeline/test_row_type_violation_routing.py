# tests/integration/pipeline/test_row_type_violation_routing.py
"""Operator-level proof that a wrong-typed row value ROUTES (elspeth-5887fb7928).

The per-plugin unit tests assert the RETURNED shape. This file asserts the
half that shape exists for: that the engine actually routes it, counts the row,
and leaves no token undecided — the operator-visible triple the defect broke.

Before the fix each plugin raised a bare ``TypeError``. ``TypeError`` matches no
clause in ``RowProcessor._execute_transform_with_retry`` and nothing in
``engine/`` converts it, so a ROW-level failure aborted the RUN: ``on_error``
never fired, the row was counted as neither succeeded nor failed, and the
operator got a raw traceback at exit 4. All three symptoms are asserted against
here — the row reaches its configured sink, the run reports it failed, and
``Orchestrator.run`` returns a result instead of raising.

Companion to ``test_retry.py::test_tier_2_contract_violation_routes_to_named_error_sink``,
which pins the same triple for the engine-raised Tier-2 violation. This one
pins it for the plugin-returned row-level error, across every per-row site the
ticket covers.
"""

from typing import Any

import pytest
from sqlalchemy import select

from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.core.config import ElspethSettings, SinkSettings, SourceSettings, TransformSettings
from elspeth.core.dag import ExecutionGraph
from elspeth.core.dag.wiring import WiredTransform
from elspeth.core.landscape.schema import token_outcomes_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator, PipelineConfig
from elspeth.plugins.infrastructure.base import BaseTransform
from elspeth.testing import make_pipeline_row
from tests.fixtures.base_classes import as_sink, as_source, as_transform
from tests.fixtures.factories import make_context
from tests.fixtures.landscape import make_landscape_db
from tests.fixtures.plugins import CollectSink, ListSource

DYNAMIC_SCHEMA = {"mode": "observed"}


def _build_transform(plugin_id: str) -> BaseTransform:
    """Construct the plugin under test with the minimum viable config."""
    if plugin_id == "json_explode":
        from elspeth.plugins.transforms.json_explode import JSONExplode

        return JSONExplode({"schema": DYNAMIC_SCHEMA, "array_field": "items"})
    if plugin_id == "line_explode":
        from elspeth.plugins.transforms.line_explode import LineExplode

        return LineExplode({"schema": DYNAMIC_SCHEMA, "source_field": "html", "output_field": "html_line"})
    if plugin_id == "blob_csv_expand":
        from elspeth.plugins.transforms.blob_csv_expand import BlobCSVExpand

        return BlobCSVExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"})
    if plugin_id == "blob_json_expand":
        from elspeth.plugins.transforms.blob_json_expand import BlobJSONExpand

        return BlobJSONExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref", "fields": ["value"]})
    if plugin_id == "blob_text_expand":
        from elspeth.plugins.transforms.blob_text_expand import BlobTextExpand

        return BlobTextExpand({"schema": DYNAMIC_SCHEMA, "blob_ref_field": "blob_ref"})
    raise AssertionError(f"unknown plugin id {plugin_id!r}")


# (plugin id, the row whose value has the WRONG TYPE, the error_type it reports).
#
# The rows differ on purpose: there is no uniform "bad value" for this
# population. json_explode wants a list and is handed a str; line_explode wants
# a str and is handed a list (deep-frozen to a tuple); blob_csv_expand wants a
# str payload hash and is handed an int. A single fixture shape would arm none
# of the others — the same trap recorded on the ticket for the batch half.
# ``reported_type`` is the name the OPERATOR sees, which is not always
# ``type(value).__name__``: PipelineRow deep-freezes the row, so a list arrives
# as a tuple and a dict as a mappingproxy. Carried explicitly so the freeze
# wrapper changing surfaces here rather than in a quarantine record.
_CASES = [
    pytest.param("json_explode", {"id": 1, "items": "abc"}, "wrong_type", "str", id="json_explode-str-for-list"),
    pytest.param("line_explode", {"id": 1, "html": ["not", "a", "string"]}, "wrong_type", "tuple", id="line_explode-list-for-str"),
    pytest.param("blob_csv_expand", {"id": 1, "blob_ref": 12}, "non_string_ref", "int", id="blob_csv_expand-int-for-ref"),
    # blob_json_expand declares `blob_content_type` alongside the ref, so the
    # row carries it: the engine's declared-required-fields gate fires BEFORE
    # the plugin and would abort on the missing column instead of exercising
    # the routing this file exists to prove.
    pytest.param(
        "blob_json_expand",
        {"id": 1, "blob_ref": 12, "blob_content_type": "application/json"},
        "non_string_ref",
        "int",
        id="blob_json_expand-int-for-ref",
    ),
    pytest.param("blob_text_expand", {"id": 1, "blob_ref": 12}, "non_string_ref", "int", id="blob_text_expand-int-for-ref"),
]


def _build_pipeline(
    transform: BaseTransform,
    row: dict[str, Any],
    *,
    on_error: str,
) -> tuple[dict[str, CollectSink], ExecutionGraph, ElspethSettings, PipelineConfig]:
    source_name = "typed_source"
    connection = "typed_source_out"
    source = ListSource([row], name="typed_source_plugin", on_success=connection)
    transform.on_success = "output"
    transform.on_error = on_error

    transform_settings = TransformSettings(
        name="type_violation",
        plugin=transform.name,
        input=connection,
        on_success="output",
        on_error=on_error,
        options={},
    )
    source_settings = SourceSettings(plugin=source.name, on_success=connection, options={})
    sinks = {"output": CollectSink("output"), "quarantine": CollectSink("quarantine")}

    graph = ExecutionGraph.from_plugin_instances(
        sources={source_name: as_source(source)},
        source_settings_map={source_name: source_settings},
        transforms=[WiredTransform(plugin=as_transform(transform), settings=transform_settings)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
        aggregations={},
        gates=[],
    )
    settings = ElspethSettings(
        sources={source_name: source_settings},
        transforms=[transform_settings],
        sinks={name: SinkSettings(plugin=sink.name, options={}, on_write_failure="discard") for name, sink in sinks.items()},
    )
    config = PipelineConfig(
        sources={source_name: as_source(source)},
        transforms=[as_transform(transform)],
        sinks={name: as_sink(sink) for name, sink in sinks.items()},
    )
    return sinks, graph, settings, config


@pytest.mark.parametrize(("plugin_id", "row", "expected_error_type", "reported_type"), _CASES)
def test_wrong_typed_row_value_routes_to_the_named_error_sink(
    plugin_id: str,
    row: dict[str, Any],
    expected_error_type: str,
    reported_type: str,
    tmp_path: Any,
) -> None:
    """The row reaches on_error, is COUNTED failed, and leaves no pending token."""
    db = make_landscape_db()
    payload_store = FilesystemPayloadStore(tmp_path / "payloads")
    sinks, graph, settings, config = _build_pipeline(_build_transform(plugin_id), row, on_error="quarantine")

    # Symptom 3: this returns a result. Before the fix it raised past every
    # catch site and the operator saw a traceback instead of a run outcome.
    result = Orchestrator(db).run(
        config,
        graph=graph,
        settings=settings,
        payload_store=payload_store,
        openrouter_catalog_sha256="0" * 64,
        openrouter_catalog_source="bundled",
    )

    # Symptom 2: the row is COUNTED. The defect reported it as neither
    # succeeded nor failed — the ticket's "0 failed" on a run where the only
    # row failed.
    assert result.status is RunStatus.FAILED
    assert result.rows_processed == 1
    assert result.rows_failed == 1

    # Symptom 1: on_error FIRES. The defect never wrote the error sink at all.
    assert sinks["output"].results == []
    assert sinks["quarantine"].results == [row]

    # And the token is DECIDED. The defect left it pending forever, which is
    # what made the failed rows unattributable in the audit trail.
    with db.engine.connect() as conn:
        outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)).all()

    assert len(outcomes) == 1
    [outcome] = outcomes
    assert (outcome.outcome, outcome.path, outcome.sink_name, outcome.completed) == (
        TerminalOutcome.FAILURE.value,
        TerminalPath.ON_ERROR_ROUTED.value,
        "quarantine",
        1,
    )
    assert not [o for o in outcomes if o.completed == 0], "a finished run must leave zero pending outcomes"


@pytest.mark.parametrize(("plugin_id", "row", "expected_error_type", "reported_type"), _CASES)
def test_wrong_typed_row_value_is_reported_with_its_type_and_never_its_value(
    plugin_id: str,
    row: dict[str, Any],
    expected_error_type: str,
    reported_type: str,
    tmp_path: Any,
) -> None:
    """The routed reason names the field and the TYPE, and omits the value.

    The value is Tier-2/3 row content. Reporting it would leak row data into
    the audit trail, the bug family `batch_replicate` documents at its own
    quarantine site ("record the row INDEX for traceability, never the row
    body"). `llm/image_inputs.py` established this shape for payload refs and
    the two blob plugins follow it.
    """
    transform = _build_transform(plugin_id)
    if plugin_id == "blob_csv_expand":
        transform._payload_store = FilesystemPayloadStore(tmp_path / "payloads")

    result = transform.process(make_pipeline_row(row), make_context())

    assert result.status == "error"
    assert result.retryable is False
    assert result.reason is not None
    assert result.reason["reason"] == "invalid_input"
    assert result.reason["error_type"] == expected_error_type
    assert result.rows is None

    bad_field = next(k for k in row if k != "id")
    assert result.reason["field"] == bad_field
    # The TYPE is named; the VALUE never appears anywhere in the reason.
    rendered = repr(sorted(result.reason.items()))
    assert reported_type in rendered
    assert repr(row[bad_field]) not in rendered
