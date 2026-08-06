"""Integration coverage for source-validation discard accounting and disclosure.

elspeth-43f52d69a4: a run whose every source row is discarded at source
validation used to report ``accounting.routing.discarded=0`` (a terminal-token
count) next to ``discard_summary.total=4`` (a row count) with no surface
recording WHY the rows were discarded. These tests pin the labelled-units fix:

* ``accounting.source`` carries row-unit counts (``rows_read`` /
  ``rows_rejected``) that reconcile exactly with
  ``discard_summary.validation_errors``;
* ``/diagnostics`` projects the recorded, already-boundary-scrubbed rejection
  reasons from ``validation_errors`` (the one table that holds them — a
  discarded row emits no token, so the token-anchored projection cannot);
* the projection never exposes source row payload (``diagnostics.py`` rule).

The pipelines run the REAL csv source with a fixed schema whose ``amount``
field is declared int while every value is non-numeric (the g01 battery
shape): the file parses, the declaration is honest, validation rejects every
row.
"""

from __future__ import annotations

from pathlib import Path

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.core.config import load_settings_from_yaml_string
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape.database import LandscapeDB
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine.orchestrator import Orchestrator
from elspeth.engine.orchestrator.preflight import assemble_and_validate_pipeline_config
from elspeth.web.execution.accounting import load_run_accounting_from_db
from elspeth.web.execution.diagnostics import load_run_diagnostics_from_db
from elspeth.web.execution.discard_summary import load_discard_summaries_from_db

_CSV_TEXT = "id,amount\n1,alpha\n2,beta\n3,gamma\n4,delta\n"
_SOURCE_CELL_VALUES = ("alpha", "beta", "gamma", "delta")


def _run_source_validation_pipeline(workdir: Path, *, on_validation_failure: str):
    """Run the g01-shape pipeline with the given source validation policy.

    ``on_validation_failure="discard"`` drops failing rows with no token;
    any other value is a quarantine sink name, so a sink by that name is
    added and failing rows are admitted as quarantined tokens.
    """
    workdir.mkdir(parents=True, exist_ok=True)
    input_path = workdir / "tickets.csv"
    input_path.write_text(_CSV_TEXT)
    quarantine_sink = ""
    if on_validation_failure != "discard":
        quarantine_sink = f"""
  {on_validation_failure}:
    plugin: csv
    on_write_failure: discard
    options:
      path: {workdir / "rejects.csv"}
      schema:
        mode: observed
"""
    settings = load_settings_from_yaml_string(
        f"""
sources:
  tickets:
    plugin: csv
    on_success: output
    options:
      path: {input_path}
      on_validation_failure: {on_validation_failure}
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'amount: int'
sinks:
  output:
    plugin: csv
    on_write_failure: discard
    options:
      path: {workdir / "out.csv"}
      schema:
        mode: fixed
        fields:
        - 'id: int'
        - 'amount: int'
{quarantine_sink}
"""
    )
    bundle = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=bundle.sources,
        source_settings_map=bundle.source_settings_map,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        gates=list(settings.gates),
    )
    config = assemble_and_validate_pipeline_config(
        sources=bundle.sources,
        transforms=bundle.transforms,
        sinks=bundle.sinks,
        aggregations=bundle.aggregations,
        settings=settings,
        graph=graph,
    )
    db = LandscapeDB(f"sqlite:///{workdir / 'audit.db'}")
    store = FilesystemPayloadStore(workdir / "payloads")
    result = Orchestrator(db).run(config, graph=graph, settings=settings, payload_store=store)
    return result, db


def test_source_validation_discard_reconciles_accounting_with_discard_summary(tmp_path: Path) -> None:
    """source.rows_rejected must equal discard_summary.validation_errors — same population, one number."""
    result, db = _run_source_validation_pipeline(tmp_path, on_validation_failure="discard")
    try:
        summary = load_discard_summaries_from_db(db, [result.run_id])[result.run_id]
        assert summary.total == 4
        assert summary.validation_errors == 4

        accounting = load_run_accounting_from_db(db, landscape_run_id=result.run_id)
        assert accounting.source.rows_processed == 0
        assert accounting.source.rows_rejected == summary.validation_errors == 4
        assert accounting.source.rows_read == 4
        # The all-rows-rejected source still appears in the per-source map:
        # zero admitted rows must not erase the source from accounting.
        assert accounting.sources["tickets"].rows_processed == 0
        assert accounting.sources["tickets"].rows_rejected == 4
        assert accounting.sources["tickets"].rows_read == 4
        # routing.* keeps its documented terminal-token unit: no token was
        # emitted for a source-validation discard, so 0 remains honest there.
        assert accounting.routing.discarded == 0
        assert accounting.tokens.emitted == 0
    finally:
        db.close()


def test_source_validation_discard_reason_is_retrievable_from_diagnostics(tmp_path: Path) -> None:
    """The recorded (already-scrubbed) rejection reason must be projected on /diagnostics."""
    result, db = _run_source_validation_pipeline(tmp_path, on_validation_failure="discard")
    try:
        summary = load_discard_summaries_from_db(db, [result.run_id])[result.run_id]
        diagnostics = load_run_diagnostics_from_db(
            db,
            run_id="web-run-1",
            landscape_run_id=result.run_id,
            run_status="empty",
            limit=50,
        )
        assert diagnostics.summary.discard_count == 4
        assert len(diagnostics.discards) == 4
        stage_node_id = summary.stages[0].node_id
        for entry in diagnostics.discards:
            assert entry.stage == "source_validation"
            assert entry.node_id == stage_node_id
            assert entry.schema_mode == "fixed"
            assert "amount" in entry.error
            assert "int_parsing" in entry.error
    finally:
        db.close()


def test_source_validation_discard_diagnostics_never_exposes_row_payload(tmp_path: Path) -> None:
    """Guards the diagnostics rule: no row payload on the web surface.

    ``validation_errors.row_data_json`` holds the raw row for audit; the
    diagnostics projection must never carry it. Green before AND after the
    discards section exists — it pins the boundary, not the feature.
    """
    result, db = _run_source_validation_pipeline(tmp_path, on_validation_failure="discard")
    try:
        diagnostics = load_run_diagnostics_from_db(
            db,
            run_id="web-run-1",
            landscape_run_id=result.run_id,
            run_status="empty",
            limit=50,
        )
        rendered = diagnostics.model_dump_json()
        for cell_value in _SOURCE_CELL_VALUES:
            assert cell_value not in rendered
    finally:
        db.close()


def test_quarantine_and_discard_agree_on_rows_read(tmp_path: Path) -> None:
    """Same data, both policies: rows_read == 4 in BOTH arms.

    This is the test that catches an unscoped ``validation_errors`` count: a
    QUARANTINED row is admitted (it has a ``rows`` row and is already inside
    ``rows_processed``) yet still writes a ``validation_errors`` entry with the
    sink name as destination. Counting ``validation_errors`` without the
    ``destination='discard'`` scope double-counts quarantined rows and reports
    eight rows read from a four-row file.
    """
    discard_result, discard_db = _run_source_validation_pipeline(tmp_path / "discard", on_validation_failure="discard")
    quarantine_result, quarantine_db = _run_source_validation_pipeline(tmp_path / "quarantine", on_validation_failure="rejects")
    try:
        discard_accounting = load_run_accounting_from_db(discard_db, landscape_run_id=discard_result.run_id)
        quarantine_accounting = load_run_accounting_from_db(quarantine_db, landscape_run_id=quarantine_result.run_id)

        assert discard_accounting.source.rows_processed == 0
        assert discard_accounting.source.rows_rejected == 4
        assert discard_accounting.source.rows_read == 4

        assert quarantine_accounting.source.rows_processed == 4
        assert quarantine_accounting.source.rows_rejected == 0
        assert quarantine_accounting.source.rows_read == 4
        assert quarantine_accounting.routing.quarantined == 4
    finally:
        discard_db.close()
        quarantine_db.close()
