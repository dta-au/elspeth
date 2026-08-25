# tests/integration/pipeline/test_pdf_rasterize_pipeline.py
"""End-to-end integration test for pdf_rasterize: expand group + bad-document quarantine.

Programmatic production path (model: test_deaggregation.py's
TestDeaggregationAuditTrail.run_pipeline) — builds plugins via
instantiate_plugins_from_config(), wires an ExecutionGraph, and drives the
real Orchestrator. Runs the real out-of-process rasterize worker at dpi 72
on hand-built tiny PDFs; nothing about the renderer is monkeypatched.

Pipeline: csv source (doc_name, blob_ref) -> pdf_rasterize (on_error: errors)
-> json sink `pages` for rendered page rows, json sink `errors` for the two
quarantined bad documents (malformed, encrypted).
"""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path

import pytest
import yaml
from sqlalchemy import select

from elspeth.cli_helpers import instantiate_plugins_from_config
from elspeth.contracts import RunStatus, TerminalOutcome, TerminalPath
from elspeth.contracts.identity import path_expand_group_id
from elspeth.contracts.run_result import RunResult
from elspeth.core.config import load_settings, resolve_config
from elspeth.core.dag import ExecutionGraph
from elspeth.core.landscape import LandscapeDB
from elspeth.core.landscape.schema import token_outcomes_table, transform_errors_table
from elspeth.core.payload_store import FilesystemPayloadStore
from elspeth.engine import Orchestrator, PipelineConfig
from tests.fixtures.landscape import make_factory
from tests.fixtures.pdf_documents import ENCRYPTED_PDF_PATH, MALFORMED_PDF, minimal_pdf

PipelineRunArtifacts = tuple[RunResult, LandscapeDB, Path, Path]

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


@pytest.fixture
def payload_store(tmp_path: Path) -> FilesystemPayloadStore:
    return FilesystemPayloadStore(tmp_path / "payloads")


@pytest.fixture
def pipeline_result(tmp_path: Path, payload_store: FilesystemPayloadStore) -> PipelineRunArtifacts:
    """Stage 4 documents (2 good, 2 bad), run the real pipeline, return (result, db, pages_path, errors_path)."""
    doc_two_pages = payload_store.store(minimal_pdf(2))
    doc_three_pages = payload_store.store(minimal_pdf(3))
    doc_malformed = payload_store.store(MALFORMED_PDF)
    doc_encrypted = payload_store.store(ENCRYPTED_PDF_PATH.read_bytes())

    input_csv = tmp_path / "documents.csv"
    with input_csv.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["doc_name", "blob_ref"])
        writer.writeheader()
        writer.writerow({"doc_name": "doc_two_pages", "blob_ref": doc_two_pages})
        writer.writerow({"doc_name": "doc_three_pages", "blob_ref": doc_three_pages})
        writer.writerow({"doc_name": "doc_bad_malformed", "blob_ref": doc_malformed})
        writer.writerow({"doc_name": "doc_bad_encrypted", "blob_ref": doc_encrypted})

    output_dir = tmp_path / "output"
    output_dir.mkdir()
    pages_path = output_dir / "pages.json"
    errors_path = output_dir / "errors.json"

    config_dict = {
        "sources": {
            "primary": {
                "plugin": "csv",
                "on_success": "raw",
                "options": {
                    "path": str(input_csv),
                    "schema": {
                        "mode": "fixed",
                        "fields": ["doc_name: str", "blob_ref: str"],
                    },
                    "on_validation_failure": "discard",
                },
            }
        },
        "transforms": [
            {
                "name": "rasterize",
                "plugin": "pdf_rasterize",
                "input": "raw",
                "on_success": "pages",
                "on_error": "errors",
                "options": {
                    "blob_ref_field": "blob_ref",
                    "dpi": 72,
                    "on_page_failure": "fail_document",
                    "schema": {"mode": "observed"},
                },
            },
        ],
        "sinks": {
            "pages": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(pages_path),
                    "schema": {"mode": "observed"},
                },
            },
            "errors": {
                "plugin": "json",
                "on_write_failure": "discard",
                "options": {
                    "path": str(errors_path),
                    "schema": {"mode": "observed"},
                },
            },
        },
        "landscape": {"url": f"sqlite:///{tmp_path / 'audit.db'}"},
    }

    config_file = tmp_path / "settings.yaml"
    config_file.write_text(yaml.dump(config_dict))
    settings = load_settings(config_file)

    db = LandscapeDB.from_url(settings.landscape.url)

    plugins = instantiate_plugins_from_config(settings)
    graph = ExecutionGraph.from_plugin_instances(
        sources=plugins.sources,
        source_settings_map=plugins.source_settings_map,
        transforms=plugins.transforms,
        sinks=plugins.sinks,
        aggregations=plugins.aggregations,
        gates=list(settings.gates),
    )
    pipeline_config = PipelineConfig(
        sources=plugins.sources,
        transforms=[wired.plugin for wired in plugins.transforms],
        sinks=plugins.sinks,
        config=resolve_config(settings),
    )

    orchestrator = Orchestrator(db)
    result = orchestrator.run(pipeline_config, graph=graph, settings=settings, payload_store=payload_store)

    return (result, db, pages_path, errors_path)


class TestPDFRasterizePipeline:
    """5 rendered page rows across 2 documents, 2 documents quarantined."""

    def test_run_completes_with_failures(self, pipeline_result: PipelineRunArtifacts) -> None:
        result, *_ = pipeline_result
        assert result.status is RunStatus.COMPLETED_WITH_FAILURES

    def test_pages_sink_holds_five_rows_grouped_and_sequenced(
        self, pipeline_result: PipelineRunArtifacts, payload_store: FilesystemPayloadStore
    ) -> None:
        _, _, pages_path, _ = pipeline_result
        pages = json.loads(pages_path.read_text())
        assert len(pages) == 5, f"Expected 5 rendered page rows, got {len(pages)}"

        by_document: dict[str, list[int]] = {}
        for row in pages:
            by_document.setdefault(row["document_id"], []).append(row["page_number"])
        page_number_sequences = sorted(sorted(sequence) for sequence in by_document.values())
        assert page_number_sequences == [[1, 2], [1, 2, 3]]

        for row in pages:
            png_bytes = payload_store.retrieve(row["page_blob_ref"])
            assert png_bytes[:8] == PNG_MAGIC

    def test_errors_sink_holds_the_two_bad_documents(self, pipeline_result: PipelineRunArtifacts) -> None:
        _, _, _, errors_path = pipeline_result
        errors = json.loads(errors_path.read_text())
        assert len(errors) == 2, f"Expected 2 quarantined rows, got {len(errors)}"
        assert {row["doc_name"] for row in errors} == {"doc_bad_malformed", "doc_bad_encrypted"}

    def test_error_reasons_are_pdf_malformed_and_pdf_encrypted(self, pipeline_result: PipelineRunArtifacts) -> None:
        result, db, _, _ = pipeline_result
        with db.engine.connect() as conn:
            error_rows = conn.execute(select(transform_errors_table).where(transform_errors_table.c.run_id == result.run_id)).all()
        assert len(error_rows) == 2
        reasons = {json.loads(row.error_details_json)["reason"] for row in error_rows}
        assert reasons == {"pdf_malformed", "pdf_encrypted"}

    def test_landscape_records_five_expanded_tokens_grouped_two_and_three(self, pipeline_result: PipelineRunArtifacts) -> None:
        result, db, _, _ = pipeline_result
        factory = make_factory(db)
        rows = factory.query.get_rows(result.run_id)
        assert len(rows) == 4, f"Expected 4 source rows, got {len(rows)}"

        all_tokens = [token for row in rows for token in factory.query.get_tokens(row.row_id)]
        # 4 source tokens + 5 expanded page tokens = 9
        assert len(all_tokens) == 9, f"Expected 9 tokens, got {len(all_tokens)}"

        parent_counts = {token.token_id: len(factory.query.get_token_parents(token.token_id)) for token in all_tokens}
        tokens_with_one_parent = sum(1 for count in parent_counts.values() if count == 1)
        assert tokens_with_one_parent == 5, f"Expected 5 tokens each with exactly one token_parents row, got {tokens_with_one_parent}"
        assert all(count in (0, 1) for count in parent_counts.values()), f"No token should have more than one parent: {parent_counts}"

        token_ids = [token.token_id for token in all_tokens]
        lineage_paths = factory.data_flow.load_lineage_paths(result.run_id, token_ids)
        assert set(lineage_paths) == set(token_ids), "load_lineage_paths must return an entry for every requested token"
        expand_group_ids = [path_expand_group_id(lineage_paths[token_id]) for token_id in token_ids]
        non_none_group_ids = [group_id for group_id in expand_group_ids if group_id is not None]
        assert len(non_none_group_ids) == 5, f"Expected 5 tokens with expand_group_id set, got {len(non_none_group_ids)}"

        group_sizes = sorted(Counter(non_none_group_ids).values())
        assert group_sizes == [2, 3], f"Expected expand groups of size 2 and 3, got {group_sizes}"

    def test_bad_documents_carry_terminal_failure_routed_to_errors(self, pipeline_result: PipelineRunArtifacts) -> None:
        result, db, _, _ = pipeline_result
        with db.engine.connect() as conn:
            outcomes = conn.execute(select(token_outcomes_table).where(token_outcomes_table.c.run_id == result.run_id)).all()

        failure_outcomes = [outcome for outcome in outcomes if outcome.outcome == TerminalOutcome.FAILURE.value]
        assert len(failure_outcomes) == 2, f"Expected 2 FAILURE token outcomes, got {len(failure_outcomes)}"
        for outcome in failure_outcomes:
            assert outcome.path == TerminalPath.ON_ERROR_ROUTED.value
            assert outcome.sink_name == "errors"
